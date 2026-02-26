"""
Tests for GitHub Pages regex-based search tools in githubmcp/server.py.

These tests use mocked HTTP responses to validate the page discovery,
content extraction, scoring, and markdown conversion logic without
requiring a live GitHub Pages site.
"""

import importlib.util
import re
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

def _load_server():
    spec = importlib.util.spec_from_file_location(
        "githubmcp_server", "githubmcp/server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

srv = _load_server()


# ---------------------------------------------------------------------------
# Fixtures: sample HTML pages
# ---------------------------------------------------------------------------

SAMPLE_HTML_PAGE = """
<!DOCTYPE html>
<html>
<head><title>Authentication Guide</title></head>
<body>
  <nav><a href="/">Home</a> <a href="/about">About</a></nav>
  <main>
    <h1>Authentication Guide</h1>
    <p>This guide covers OAuth2 authentication and API key management.</p>
    <h2>OAuth2 Flow</h2>
    <p>The OAuth2 flow involves redirecting users to the authorization endpoint.</p>
    <h2>API Keys</h2>
    <p>API keys provide simpler authentication for server-to-server calls.</p>
    <pre><code>Authorization: Bearer YOUR_TOKEN</code></pre>
  </main>
  <footer>Copyright 2024</footer>
</body>
</html>
"""

SAMPLE_HTML_PAGE_2 = """
<!DOCTYPE html>
<html>
<head><title>Deployment Guide</title></head>
<body>
  <main>
    <h1>Deployment Guide</h1>
    <p>Deploy your application using Docker containers.</p>
    <h2>Docker Setup</h2>
    <p>Run the following command to build the image.</p>
  </main>
</body>
</html>
"""

SAMPLE_HTML_NO_CONTENT = """
<!DOCTYPE html>
<html>
<head><title>Empty Page</title></head>
<body>
  <script>console.log('hello');</script>
  <style>body { margin: 0; }</style>
</body>
</html>
"""

SAMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://testuser.github.io/testrepo/</loc></url>
  <url><loc>https://testuser.github.io/testrepo/auth</loc></url>
  <url><loc>https://testuser.github.io/testrepo/deploy</loc></url>
</urlset>
"""


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------

class TestPagesBaseUrl:
    def test_default_url(self):
        url = srv._pages_base_url("octocat", "my-docs")
        assert url == "https://octocat.github.io/my-docs"

    def test_custom_domain(self):
        url = srv._pages_base_url("octocat", "my-docs", "https://docs.example.com")
        assert url == "https://docs.example.com"

    def test_custom_domain_trailing_slash(self):
        url = srv._pages_base_url("octocat", "my-docs", "https://docs.example.com/")
        assert url == "https://docs.example.com"


class TestExtractTextFromHtml:
    def test_extracts_visible_text(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_PAGE)
        assert "Authentication Guide" in text
        assert "OAuth2" in text

    def test_removes_scripts_and_styles(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_NO_CONTENT)
        assert "console.log" not in text
        assert "margin" not in text

    def test_removes_nav_and_footer(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_PAGE)
        assert "Copyright 2024" not in text


class TestHtmlToCleanMarkdown:
    def test_converts_headings(self):
        md = srv._html_to_clean_markdown(SAMPLE_HTML_PAGE)
        assert "Authentication Guide" in md

    def test_preserves_content(self):
        md = srv._html_to_clean_markdown(SAMPLE_HTML_PAGE)
        assert "OAuth2" in md
        assert "API" in md

    def test_strips_nav_and_footer(self):
        md = srv._html_to_clean_markdown(SAMPLE_HTML_PAGE)
        assert "Copyright 2024" not in md

    def test_empty_page(self):
        md = srv._html_to_clean_markdown(SAMPLE_HTML_NO_CONTENT)
        assert "console.log" not in md


class TestScorePage:
    def test_scores_matching_page(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_PAGE)
        pattern = re.compile("authentication", re.IGNORECASE)
        score = srv._score_page(text, pattern)
        assert score > 0

    def test_zero_score_for_no_match(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_PAGE)
        pattern = re.compile("xyznonexistent", re.IGNORECASE)
        score = srv._score_page(text, pattern)
        assert score == 0.0

    def test_higher_score_for_more_matches(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_PAGE)
        pattern_few = re.compile("Docker", re.IGNORECASE)
        pattern_many = re.compile("authentication|OAuth2|API", re.IGNORECASE)
        score_few = srv._score_page(text, pattern_few)
        score_many = srv._score_page(text, pattern_many)
        assert score_many > score_few

    def test_query_terms_bonus(self):
        text = srv._extract_text_from_html(SAMPLE_HTML_PAGE)
        pattern = re.compile("OAuth", re.IGNORECASE)
        score_no_terms = srv._score_page(text, pattern, query_terms=None)
        score_with_terms = srv._score_page(text, pattern, query_terms=["OAuth", "authentication"])
        assert score_with_terms >= score_no_terms


# ---------------------------------------------------------------------------
# Tests: fetch_page
# ---------------------------------------------------------------------------

class TestFetchPage:
    @patch.object(srv.requests, "get")
    def test_returns_html(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = SAMPLE_HTML_PAGE
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = srv._fetch_page("https://example.com/page")
        assert result is not None
        assert "Authentication Guide" in result

    @patch.object(srv.requests, "get")
    def test_returns_none_for_non_html(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "binary data"
        mock_resp.headers = {"Content-Type": "application/octet-stream"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = srv._fetch_page("https://example.com/file.bin")
        assert result is None

    @patch.object(srv.requests, "get")
    def test_returns_none_on_error(self, mock_get):
        mock_get.side_effect = srv.requests.RequestException("Connection refused")
        result = srv._fetch_page("https://example.com/bad")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: search_github_pages (integration-style with mocked HTTP)
# ---------------------------------------------------------------------------

class TestSearchGithubPages:
    def _mock_get(self, url_responses):
        """Create a mock requests.get that returns different responses per URL."""
        def side_effect(url, **kwargs):
            if url in url_responses:
                resp = MagicMock()
                resp.status_code = 200
                resp.text = url_responses[url]
                resp.headers = {"Content-Type": "text/html"}
                resp.raise_for_status = MagicMock()
                return resp
            raise srv.requests.RequestException(f"Not found: {url}")
        return side_effect

    @patch.object(srv.requests, "get")
    def test_finds_matching_pages(self, mock_get):
        base = "https://testuser.github.io/testrepo"
        mock_get.side_effect = self._mock_get({
            f"{base}/sitemap.xml": SAMPLE_SITEMAP,
            f"{base}/": SAMPLE_HTML_PAGE,
            f"{base}/auth": SAMPLE_HTML_PAGE,
            f"{base}/deploy": SAMPLE_HTML_PAGE_2,
        })

        result = srv.search_github_pages(
            owner="testuser",
            repo="testrepo",
            regex_pattern="authentication",
            top_k=3,
        )
        assert "Search Results" in result
        assert "authentication" in result.lower() or "Authentication" in result

    @patch.object(srv.requests, "get")
    def test_invalid_regex(self, mock_get):
        result = srv.search_github_pages(
            owner="testuser",
            repo="testrepo",
            regex_pattern="[invalid",
        )
        assert "Invalid regex" in result

    @patch.object(srv.requests, "get")
    def test_no_pages_found(self, mock_get):
        mock_get.side_effect = srv.requests.RequestException("Not found")
        result = srv.search_github_pages(
            owner="testuser",
            repo="testrepo",
            regex_pattern="test",
        )
        assert "No pages found" in result

    @patch.object(srv.requests, "get")
    def test_no_matches_found(self, mock_get):
        base = "https://testuser.github.io/testrepo"
        mock_get.side_effect = self._mock_get({
            f"{base}/sitemap.xml": SAMPLE_SITEMAP,
            f"{base}/": SAMPLE_HTML_PAGE,
            f"{base}/auth": SAMPLE_HTML_PAGE,
            f"{base}/deploy": SAMPLE_HTML_PAGE_2,
        })

        result = srv.search_github_pages(
            owner="testuser",
            repo="testrepo",
            regex_pattern="xyznonexistent123",
        )
        assert "No matches found" in result

    @patch.object(srv.requests, "get")
    def test_snippets_only_mode(self, mock_get):
        base = "https://testuser.github.io/testrepo"
        mock_get.side_effect = self._mock_get({
            f"{base}/sitemap.xml": SAMPLE_SITEMAP,
            f"{base}/": SAMPLE_HTML_PAGE,
            f"{base}/auth": SAMPLE_HTML_PAGE,
            f"{base}/deploy": SAMPLE_HTML_PAGE_2,
        })

        result = srv.search_github_pages(
            owner="testuser",
            repo="testrepo",
            regex_pattern="OAuth2",
            return_full_markdown=False,
        )
        assert "snippet" in result.lower() or "Match" in result


# ---------------------------------------------------------------------------
# Tests: list_github_pages
# ---------------------------------------------------------------------------

class TestListGithubPages:
    @patch.object(srv.requests, "get")
    def test_discovers_via_sitemap(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = SAMPLE_SITEMAP
        mock_resp.headers = {"Content-Type": "text/xml"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = srv.list_github_pages(owner="testuser", repo="testrepo")
        assert "sitemap" in result.lower()
        assert "testuser.github.io" in result


# ---------------------------------------------------------------------------
# Tests: get_github_page_as_markdown
# ---------------------------------------------------------------------------

class TestGetGithubPageAsMarkdown:
    @patch.object(srv.requests, "get")
    def test_converts_page_to_markdown(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = SAMPLE_HTML_PAGE
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = srv.get_github_page_as_markdown(url="https://example.com/auth")
        assert "Authentication Guide" in result
        assert "OAuth2" in result
        assert "Source:" in result

    @patch.object(srv.requests, "get")
    def test_handles_failed_fetch(self, mock_get):
        mock_get.side_effect = srv.requests.RequestException("Timeout")
        result = srv.get_github_page_as_markdown(url="https://example.com/bad")
        assert "Failed to fetch" in result
