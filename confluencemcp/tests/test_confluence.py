"""
Tests for the Confluence MCP server.

These tests validate the conversion logic and helpers without making real
network requests.
"""

import re
from unittest.mock import MagicMock, patch

import pytest

from confluencemcp.server import (
    _escape_html,
    _format_page_metadata,
    _markdown_to_storage,
    _storage_to_markdown,
)


# ---------------------------------------------------------------------------
# _storage_to_markdown
# ---------------------------------------------------------------------------


class TestStorageToMarkdown:
    def test_simple_paragraph(self):
        html = "<p>Hello world</p>"
        md = _storage_to_markdown(html)
        assert "Hello world" in md

    def test_heading_conversion(self):
        html = "<h1>Title</h1><p>Content</p>"
        md = _storage_to_markdown(html)
        assert "# Title" in md
        assert "Content" in md

    def test_code_macro_converted(self):
        html = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body>print('hi')</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        md = _storage_to_markdown(html)
        assert "print('hi')" in md

    def test_toc_macro_removed(self):
        html = (
            '<ac:structured-macro ac:name="toc"></ac:structured-macro>'
            "<p>Real content</p>"
        )
        md = _storage_to_markdown(html)
        assert "Real content" in md
        # TOC macro should be gone
        assert "toc" not in md.lower() or "toc" in "Real content".lower()

    def test_panel_macro_unwrapped(self):
        html = (
            '<ac:structured-macro ac:name="panel">'
            "<ac:rich-text-body><p>Panel content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        md = _storage_to_markdown(html)
        assert "Panel content" in md

    def test_layout_elements_unwrapped(self):
        html = (
            "<ac:layout><ac:layout-section><ac:layout-cell>"
            "<p>Cell content</p>"
            "</ac:layout-cell></ac:layout-section></ac:layout>"
        )
        md = _storage_to_markdown(html)
        assert "Cell content" in md


# ---------------------------------------------------------------------------
# _markdown_to_storage
# ---------------------------------------------------------------------------


class TestMarkdownToStorage:
    def test_heading(self):
        storage = _markdown_to_storage("# Hello")
        assert "<h1>Hello</h1>" in storage

    def test_paragraph(self):
        storage = _markdown_to_storage("Some text")
        assert "<p>Some text</p>" in storage

    def test_code_block(self):
        md = "```python\nprint('hi')\n```"
        storage = _markdown_to_storage(md)
        assert "code" in storage
        assert "python" in storage
        assert "print" in storage

    def test_unordered_list(self):
        storage = _markdown_to_storage("- Item one")
        assert "<ul><li>Item one</li></ul>" in storage

    def test_ordered_list(self):
        storage = _markdown_to_storage("1. First item")
        assert "<ol><li>First item</li></ol>" in storage

    def test_horizontal_rule(self):
        storage = _markdown_to_storage("---")
        assert "<hr />" in storage

    def test_escapes_html_entities(self):
        storage = _markdown_to_storage("a < b & c > d")
        assert "&lt;" in storage
        assert "&amp;" in storage
        assert "&gt;" in storage


# ---------------------------------------------------------------------------
# _format_page_metadata
# ---------------------------------------------------------------------------


class TestFormatPageMetadata:
    def test_formats_full_metadata(self):
        page = {
            "title": "Test Page",
            "id": "12345",
            "space": {"key": "ENG"},
            "version": {"number": 3},
            "_links": {"webui": "/display/ENG/Test+Page", "base": "https://wiki.example.com"},
        }
        output = _format_page_metadata(page)
        assert "# Test Page" in output
        assert "12345" in output
        assert "ENG" in output
        assert "3" in output
        assert "https://wiki.example.com" in output

    def test_handles_minimal_metadata(self):
        page = {"title": "Minimal"}
        output = _format_page_metadata(page)
        assert "# Minimal" in output


# ---------------------------------------------------------------------------
# _escape_html
# ---------------------------------------------------------------------------


class TestEscapeHtml:
    def test_escapes_all_special_chars(self):
        assert _escape_html('a < b > c & d "e"') == 'a &lt; b &gt; c &amp; d &quot;e&quot;'

    def test_no_change_for_safe_text(self):
        assert _escape_html("hello world") == "hello world"
