"""
Tests for the GitHub Pages retriever tools in the GitHub MCP server.

These tests mock HTTP responses to validate the crawling, searching, and
conversion logic without making real network requests.
"""

import re
from unittest.mock import MagicMock, patch

import pytest

from githubmcp.server import (
    _discover_pages,
    _extract_links,
    _fetch_page,
    _html_to_clean_markdown,
    _resolve_pages_base_url,
    _score_page,
)


# ---------------------------------------------------------------------------
# _resolve_pages_base_url
# ---------------------------------------------------------------------------


class TestResolvePagesBaseUrl:
    def test_standard_project_site(self):
        url = _resolve_pages_base_url("octocat", "my-project")
        assert url == "https://octocat.github.io/my-project/"

    def test_user_site(self):
        url = _resolve_pages_base_url("octocat", "octocat.github.io")
        assert url == "https://octocat.github.io/"

    def test_user_site_case_insensitive(self):
        url = _resolve_pages_base_url("Octocat", "Octocat.github.io")
        assert url == "https://Octocat.github.io/"

    @patch.dict("os.environ", {"GITHUB_BASE_URL": "https://github.corp.com/api/v3"})
    def test_ghes_project_site(self):
        url = _resolve_pages_base_url("octocat", "my-project")
        assert url == "https://pages.github.corp.com/octocat/my-project/"


# ---------------------------------------------------------------------------
# _extract_links
# ---------------------------------------------------------------------------


class TestExtractLinks:
    def test_internal_links_only(self):
        html = """
        <html><body>
            <a href="/repo/page1.html">Page 1</a>
            <a href="/repo/page2.html">Page 2</a>
            <a href="https://external.com/other">External</a>
        </body></html>
        """
        links = _extract_links(html, "https://user.github.io/repo/")
        assert "https://user.github.io/repo/page1.html" in links
        assert "https://user.github.io/repo/page2.html" in links
        assert "https://external.com/other" not in links

    def test_skips_fragments_and_mailto(self):
        html = """
        <html><body>
            <a href="#section">Section</a>
            <a href="mailto:test@example.com">Email</a>
            <a href="javascript:void(0)">JS</a>
            <a href="/repo/real.html">Real</a>
        </body></html>
        """
        links = _extract_links(html, "https://user.github.io/repo/")
        assert len(links) == 1
        assert "real.html" in links[0]

    def test_deduplicates(self):
        html = """
        <html><body>
            <a href="/repo/page.html">Link 1</a>
            <a href="/repo/page.html">Link 2</a>
        </body></html>
        """
        links = _extract_links(html, "https://user.github.io/repo/")
        assert len(links) == 1


# ---------------------------------------------------------------------------
# _html_to_clean_markdown
# ---------------------------------------------------------------------------


class TestHtmlToCleanMarkdown:
    def test_extracts_main_content(self):
        html = """
        <html><body>
            <nav><a href="/">Home</a></nav>
            <main><h1>Title</h1><p>Content here.</p></main>
            <footer>Footer stuff</footer>
        </body></html>
        """
        md = _html_to_clean_markdown(html)
        assert "Title" in md
        assert "Content here." in md
        assert "Home" not in md  # nav stripped
        assert "Footer stuff" not in md  # footer stripped

    def test_strips_scripts_and_styles(self):
        html = """
        <html><body>
            <script>alert('x');</script>
            <style>.x {color: red;}</style>
            <p>Real content</p>
        </body></html>
        """
        md = _html_to_clean_markdown(html)
        assert "alert" not in md
        assert "color" not in md
        assert "Real content" in md

    def test_strips_sidebar_by_class(self):
        html = """
        <html><body>
            <div class="sidebar">Sidebar content</div>
            <article><p>Main article text.</p></article>
        </body></html>
        """
        md = _html_to_clean_markdown(html)
        assert "Sidebar content" not in md
        assert "Main article text." in md

    def test_handles_empty_html(self):
        md = _html_to_clean_markdown("<html><body></body></html>")
        assert md == ""


# ---------------------------------------------------------------------------
# _score_page
# ---------------------------------------------------------------------------


class TestScorePage:
    def test_no_matches(self):
        pattern = re.compile(r"foo")
        score, snippets = _score_page("http://x.com", "bar baz", pattern)
        assert score == 0
        assert snippets == []

    def test_counts_matches(self):
        pattern = re.compile(r"hello", re.IGNORECASE)
        text = "Hello world! Say hello again. HELLO!"
        score, snippets = _score_page("http://x.com", text, pattern)
        assert score == 3
        assert len(snippets) == 3

    def test_snippets_limited_to_five(self):
        pattern = re.compile(r"x")
        text = "x " * 100
        score, snippets = _score_page("http://x.com", text, pattern)
        assert score == 100
        assert len(snippets) == 5  # capped

    def test_snippets_contain_context(self):
        pattern = re.compile(r"target")
        text = "aaaa target bbbb"
        score, snippets = _score_page("http://x.com", text, pattern)
        assert score == 1
        assert "target" in snippets[0]


# ---------------------------------------------------------------------------
# _fetch_page (with mocked requests)
# ---------------------------------------------------------------------------


class TestFetchPage:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.text = "<html><body>Hello</body></html>"
        mock_resp.content = b"<html><body>Hello</body></html>"

        session = MagicMock()
        session.get.return_value = mock_resp

        result = _fetch_page("https://example.com", session)
        assert result == "<html><body>Hello</body></html>"

    def test_404_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        session = MagicMock()
        session.get.return_value = mock_resp

        result = _fetch_page("https://example.com/missing", session)
        assert result is None

    def test_non_html_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/pdf"}

        session = MagicMock()
        session.get.return_value = mock_resp

        result = _fetch_page("https://example.com/file.pdf", session)
        assert result is None

    def test_request_exception_returns_none(self):
        import requests as _req

        session = MagicMock()
        session.get.side_effect = _req.RequestException("timeout")

        result = _fetch_page("https://example.com", session)
        assert result is None


# ---------------------------------------------------------------------------
# _discover_pages (with mocked HTTP)
# ---------------------------------------------------------------------------


class TestDiscoverPages:
    def test_discovers_from_sitemap(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://user.github.io/repo/page1.html</loc></url>
            <url><loc>https://user.github.io/repo/page2.html</loc></url>
        </urlset>"""

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"Content-Type": "text/html"}
            resp.content = sitemap_xml.encode()
            resp.text = sitemap_xml
            return resp

        session = MagicMock()
        session.get.side_effect = mock_get

        pages = _discover_pages("https://user.github.io/repo/", session)
        assert "https://user.github.io/repo/page1.html" in pages
        assert "https://user.github.io/repo/page2.html" in pages

    def test_falls_back_to_crawling(self):
        call_count = [0]

        def mock_get(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"Content-Type": "text/html"}

            if "sitemap.xml" in url:
                # Empty sitemap - no <loc> tags
                resp.text = "<html><body>Not found</body></html>"
                resp.content = resp.text.encode()
            elif url.endswith("/repo/"):
                # Root page with links
                html = """<html><body>
                    <a href="/repo/about.html">About</a>
                    <a href="/repo/docs.html">Docs</a>
                </body></html>"""
                resp.text = html
                resp.content = html.encode()
            else:
                resp.text = "<html><body><p>Leaf page</p></body></html>"
                resp.content = resp.text.encode()
            return resp

        session = MagicMock()
        session.get.side_effect = mock_get

        pages = _discover_pages("https://user.github.io/repo/", session)
        # Should include root + linked pages
        assert "https://user.github.io/repo/" in pages
        assert any("about.html" in p for p in pages)
        assert any("docs.html" in p for p in pages)

    def test_respects_max_pages(self):
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"Content-Type": "text/html"}
            resp.text = "<html><body>" + "".join(
                f'<a href="/repo/page{i}.html">P{i}</a>' for i in range(100)
            ) + "</body></html>"
            resp.content = resp.text.encode()
            return resp

        session = MagicMock()
        session.get.side_effect = mock_get

        pages = _discover_pages("https://user.github.io/repo/", session, max_pages=5)
        assert len(pages) <= 5
