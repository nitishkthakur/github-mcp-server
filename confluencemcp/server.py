"""
Confluence MCP Server built with fastmcp.

Provides tools optimised for LLMs to:
- Search and retrieve Confluence content as clean Markdown
- Navigate page hierarchies for contextual understanding
- Push documentation and prompts back to Confluence

Authentication (all via environment variables):
  CONFLUENCE_URL            – Base URL of the Confluence instance
                              e.g. https://mycompany.atlassian.net/wiki
  CONFLUENCE_USERNAME       – Email address for Confluence Cloud (or username for Server)
  CONFLUENCE_API_TOKEN      – API token (Cloud) or password (Server)
  CONFLUENCE_PERSONAL_TOKEN – Personal Access Token (Confluence Data Center 7.9+)

Connection options:
  CONFLUENCE_VERIFY_SSL – Set to "false" to disable SSL verification.
  CONFLUENCE_TIMEOUT    – Request timeout in seconds (default: 15).
  CONFLUENCE_PROXY      – HTTP/HTTPS proxy URL.
"""

import json
import os
import re
import logging
from typing import Optional
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_md
from dotenv import load_dotenv
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

load_dotenv()

# ---------------------------------------------------------------------------
# Initialise server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="confluence-mcp-server",
    instructions=(
        "A Confluence MCP server optimised for LLM context retrieval. "
        "Search, navigate, and read Confluence pages as clean Markdown. "
        "Push documentation and prompts back to Confluence spaces."
    ),
)

_DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Auth / connection helpers
# ---------------------------------------------------------------------------


def _build_session(token: Optional[str] = None) -> tuple[requests.Session, str]:
    """
    Build a ``requests.Session`` authenticated for the Confluence REST API.

    Returns (session, base_url) tuple.

    Raises ``ValueError`` if required credentials are missing.
    """
    base_url = os.getenv("CONFLUENCE_URL", "").rstrip("/")
    if not base_url:
        raise ValueError(
            "CONFLUENCE_URL is not set. Set it to your Confluence instance URL "
            "(e.g. https://mycompany.atlassian.net/wiki)."
        )

    session = requests.Session()
    session.headers["Accept"] = "application/json"
    session.headers["Content-Type"] = "application/json"

    # SSL verification
    verify_env = os.getenv("CONFLUENCE_VERIFY_SSL", "true").strip().lower()
    if verify_env == "false":
        session.verify = False
    elif verify_env not in ("true", "1", "yes", ""):
        session.verify = verify_env

    # Proxy
    proxy = os.getenv("CONFLUENCE_PROXY")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    # Auth priority:
    # 1. Explicit token parameter
    # 2. Personal Access Token (Data Center)
    # 3. API token (Cloud) with username
    pat = token or os.getenv("CONFLUENCE_PERSONAL_TOKEN")
    if pat:
        session.headers["Authorization"] = f"Bearer {pat}"
        return session, base_url

    username = os.getenv("CONFLUENCE_USERNAME")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")
    if username and api_token:
        session.auth = (username, api_token)
        return session, base_url

    raise ValueError(
        "No Confluence credentials found. Provide one of:\n"
        "  • CONFLUENCE_PERSONAL_TOKEN\n"
        "  • CONFLUENCE_USERNAME + CONFLUENCE_API_TOKEN\n"
        "See README.md for details."
    )


def _get_timeout() -> int:
    try:
        return int(os.getenv("CONFLUENCE_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _api_get(
    session: requests.Session,
    base_url: str,
    path: str,
    params: Optional[dict] = None,
) -> dict:
    """Make an authenticated GET request to the Confluence REST API."""
    url = f"{base_url}/rest/api{path}"
    resp = session.get(url, params=params, timeout=_get_timeout())
    resp.raise_for_status()
    return resp.json()


def _api_post(
    session: requests.Session,
    base_url: str,
    path: str,
    data: dict,
) -> dict:
    """Make an authenticated POST request to the Confluence REST API."""
    url = f"{base_url}/rest/api{path}"
    resp = session.post(url, json=data, timeout=_get_timeout())
    resp.raise_for_status()
    return resp.json()


def _api_put(
    session: requests.Session,
    base_url: str,
    path: str,
    data: dict,
) -> dict:
    """Make an authenticated PUT request to the Confluence REST API."""
    url = f"{base_url}/rest/api{path}"
    resp = session.put(url, json=data, timeout=_get_timeout())
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _storage_to_markdown(storage_html: str) -> str:
    """
    Convert Confluence storage format (XHTML) to clean Markdown suitable for
    LLM consumption.

    Strips Confluence-specific macros and layout elements to produce readable
    Markdown.
    """
    soup = BeautifulSoup(storage_html, "html.parser")

    # Remove Confluence macros that add noise (e.g. TOC, status, panel chrome)
    for macro in soup.find_all("ac:structured-macro"):
        macro_name = macro.get("ac:name", "")
        # Keep code blocks, convert them to fenced Markdown later
        if macro_name in ("code", "noformat"):
            body = macro.find("ac:plain-text-body")
            if body:
                pre = soup.new_tag("pre")
                code = soup.new_tag("code")
                code.string = body.get_text()
                pre.append(code)
                macro.replace_with(pre)
                continue
        # Remove layout macros but keep their content
        if macro_name in ("section", "column", "panel", "expand", "excerpt"):
            macro.unwrap()
            continue
        # Remove pure-noise macros
        if macro_name in ("toc", "status", "recently-updated", "page-tree"):
            macro.decompose()
            continue

    # Remove layout containers but keep content
    for tag in soup.find_all("ac:layout"):
        tag.unwrap()
    for tag in soup.find_all("ac:layout-section"):
        tag.unwrap()
    for tag in soup.find_all("ac:layout-cell"):
        tag.unwrap()

    # Convert to Markdown
    md = html_to_md(str(soup), heading_style="ATX", strip=["img"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md


def _format_page_metadata(page: dict) -> str:
    """Format page metadata as a human-readable header."""
    title = page.get("title", "Untitled")
    page_id = page.get("id", "")
    space = page.get("space", {}).get("key", "")
    version = page.get("version", {}).get("number", "")
    url = page.get("_links", {}).get("webui", "")
    base = page.get("_links", {}).get("base", "")
    full_url = f"{base}{url}" if base and url else ""

    lines = [f"# {title}"]
    if page_id:
        lines.append(f"**Page ID:** {page_id}")
    if space:
        lines.append(f"**Space:** {space}")
    if version:
        lines.append(f"**Version:** {version}")
    if full_url:
        lines.append(f"**URL:** {full_url}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Search tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search_confluence(
    query: str,
    space_key: Optional[str] = None,
    max_results: int = 10,
    include_content: bool = True,
    token: Optional[str] = None,
) -> str:
    """
    Search Confluence using CQL (Confluence Query Language) and return results
    as clean Markdown optimised for LLM context.

    The query is automatically enhanced for full-text search. You can also
    pass raw CQL for advanced queries.

    Args:
        query: Search query (plain text or CQL expression).
        space_key: Optional space key to restrict search to a single space.
        max_results: Maximum results to return (1-25). Defaults to 10.
        include_content: Whether to include page body as Markdown. Defaults to True.
        token: Optional Confluence Personal Access Token (overrides env var).
    """
    if max_results < 1:
        max_results = 1
    if max_results > 25:
        max_results = 25

    session, base_url = _build_session(token)

    # Build CQL
    cql_parts: list[str] = [f'type = "page"']
    if space_key:
        cql_parts.append(f'space = "{space_key}"')

    # If the query looks like raw CQL (contains operators), use as-is.
    # Otherwise, wrap in a text search.
    cql_operators = ("=", "~", "!=", ">=", "<=", ">", "<", "IN", "NOT")
    if any(op in query for op in cql_operators):
        cql_parts.append(f"({query})")
    else:
        cql_parts.append(f'text ~ "{query}"')

    cql = " AND ".join(cql_parts)

    expand = "version,space"
    if include_content:
        expand += ",body.storage"

    params = {
        "cql": cql,
        "limit": max_results,
        "expand": expand,
    }

    data = _api_get(session, base_url, "/content/search", params)
    results = data.get("results", [])

    if not results:
        return f"No results found for query: {query}"

    parts: list[str] = [f"Found {len(results)} result(s) for: {query}\n"]

    for i, page in enumerate(results, 1):
        parts.append(f"{'=' * 60}")
        parts.append(f"## Result {i}")
        parts.append(_format_page_metadata(page))

        if include_content:
            body_html = page.get("body", {}).get("storage", {}).get("value", "")
            if body_html:
                md = _storage_to_markdown(body_html)
                parts.append(md)
            else:
                parts.append("(No content)")
        parts.append("")

    return "\n".join(parts)


@mcp.tool()
def search_by_label(
    label: str,
    space_key: Optional[str] = None,
    max_results: int = 10,
    include_content: bool = False,
    token: Optional[str] = None,
) -> str:
    """
    Find Confluence pages with a specific label.

    Args:
        label: The label to search for.
        space_key: Optional space key to restrict search.
        max_results: Maximum results (1-25). Defaults to 10.
        include_content: Whether to include full page content. Defaults to False.
        token: Optional Confluence Personal Access Token.
    """
    if max_results < 1:
        max_results = 1
    if max_results > 25:
        max_results = 25

    session, base_url = _build_session(token)

    cql = f'type = "page" AND label = "{label}"'
    if space_key:
        cql += f' AND space = "{space_key}"'

    expand = "version,space"
    if include_content:
        expand += ",body.storage"

    data = _api_get(
        session,
        base_url,
        "/content/search",
        params={"cql": cql, "limit": max_results, "expand": expand},
    )
    results = data.get("results", [])

    if not results:
        return f"No pages found with label '{label}'."

    parts: list[str] = [f"Found {len(results)} page(s) with label '{label}':\n"]
    for page in results:
        parts.append(_format_page_metadata(page))
        if include_content:
            body_html = page.get("body", {}).get("storage", {}).get("value", "")
            if body_html:
                parts.append(_storage_to_markdown(body_html))
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Content retrieval tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_page_content(
    page_id: str,
    token: Optional[str] = None,
) -> str:
    """
    Retrieve a Confluence page by ID and return its content as clean Markdown.

    Args:
        page_id: The numeric Confluence page ID.
        token: Optional Confluence Personal Access Token.
    """
    session, base_url = _build_session(token)
    data = _api_get(
        session,
        base_url,
        f"/content/{page_id}",
        params={"expand": "body.storage,version,space"},
    )

    output = _format_page_metadata(data)
    body_html = data.get("body", {}).get("storage", {}).get("value", "")
    if body_html:
        output += "\n" + _storage_to_markdown(body_html)
    else:
        output += "\n(Page has no content)"
    return output


@mcp.tool()
def get_page_by_title(
    space_key: str,
    title: str,
    token: Optional[str] = None,
) -> str:
    """
    Retrieve a Confluence page by its space key and title.

    Args:
        space_key: The space key (e.g. "ENG", "DOC").
        title: The exact page title.
        token: Optional Confluence Personal Access Token.
    """
    session, base_url = _build_session(token)
    data = _api_get(
        session,
        base_url,
        "/content",
        params={
            "spaceKey": space_key,
            "title": title,
            "expand": "body.storage,version,space",
            "limit": 1,
        },
    )
    results = data.get("results", [])
    if not results:
        return f"Page '{title}' not found in space '{space_key}'."

    page = results[0]
    output = _format_page_metadata(page)
    body_html = page.get("body", {}).get("storage", {}).get("value", "")
    if body_html:
        output += "\n" + _storage_to_markdown(body_html)
    else:
        output += "\n(Page has no content)"
    return output


@mcp.tool()
def get_page_tree(
    page_id: str,
    max_depth: int = 3,
    token: Optional[str] = None,
) -> str:
    """
    Get the page tree (hierarchy of child pages) starting from a given page.

    Useful for understanding the structure of a documentation space and
    deciding which pages to read for context.

    Args:
        page_id: The root page ID to start from.
        max_depth: Maximum depth of child pages to include (1-5). Defaults to 3.
        token: Optional Confluence Personal Access Token.
    """
    if max_depth < 1:
        max_depth = 1
    if max_depth > 5:
        max_depth = 5

    session, base_url = _build_session(token)

    def _get_children(pid: str, depth: int, indent: int) -> list[str]:
        if depth <= 0:
            return []
        data = _api_get(
            session,
            base_url,
            f"/content/{pid}/child/page",
            params={"expand": "version", "limit": 50},
        )
        lines: list[str] = []
        for child in data.get("results", []):
            prefix = "  " * indent + "- "
            title = child.get("title", "Untitled")
            child_id = child.get("id", "")
            lines.append(f"{prefix}{title} (id: {child_id})")
            lines.extend(_get_children(child_id, depth - 1, indent + 1))
        return lines

    # Get the root page info
    root = _api_get(
        session, base_url, f"/content/{page_id}", params={"expand": "space"}
    )
    root_title = root.get("title", "Untitled")
    space = root.get("space", {}).get("key", "")

    tree_lines = [f"Page tree for: {root_title} (space: {space}, id: {page_id})\n"]
    tree_lines.append(f"- {root_title} (id: {page_id})")
    tree_lines.extend(_get_children(page_id, max_depth, 1))

    return "\n".join(tree_lines)


@mcp.tool()
def list_spaces(
    max_results: int = 25,
    token: Optional[str] = None,
) -> str:
    """
    List available Confluence spaces.

    Args:
        max_results: Maximum spaces to return (1-100). Defaults to 25.
        token: Optional Confluence Personal Access Token.
    """
    if max_results < 1:
        max_results = 1
    if max_results > 100:
        max_results = 100

    session, base_url = _build_session(token)
    data = _api_get(
        session,
        base_url,
        "/space",
        params={"limit": max_results, "expand": "description.plain"},
    )
    spaces = data.get("results", [])

    if not spaces:
        return "No spaces found."

    lines: list[str] = [f"Found {len(spaces)} space(s):\n"]
    for sp in spaces:
        key = sp.get("key", "")
        name = sp.get("name", "")
        desc = (
            sp.get("description", {}).get("plain", {}).get("value", "").strip()
        )
        desc_line = f" – {desc}" if desc else ""
        lines.append(f"  [{key}] {name}{desc_line}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Content push tools
# ---------------------------------------------------------------------------


@mcp.tool()
def push_documentation(
    space_key: str,
    title: str,
    content: str,
    parent_id: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Create or update a Confluence page with documentation content.

    If a page with the given title already exists in the space it will be
    updated; otherwise a new page is created.

    Content should be provided as Markdown – it will be converted to
    Confluence storage format automatically.

    Args:
        space_key: The space key to create the page in.
        title: Page title.
        content: Documentation content (Markdown).
        parent_id: Optional parent page ID to nest under.
        token: Optional Confluence Personal Access Token.
    """
    session, base_url = _build_session(token)

    # Convert Markdown to basic Confluence storage format (XHTML)
    storage_body = _markdown_to_storage(content)

    # Check if page already exists
    existing = _api_get(
        session,
        base_url,
        "/content",
        params={
            "spaceKey": space_key,
            "title": title,
            "expand": "version",
            "limit": 1,
        },
    )
    existing_pages = existing.get("results", [])

    if existing_pages:
        # Update existing page
        page = existing_pages[0]
        page_id = page["id"]
        current_version = page.get("version", {}).get("number", 1)
        update_data = {
            "id": page_id,
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {"storage": {"value": storage_body, "representation": "storage"}},
            "version": {"number": current_version + 1},
        }
        result = _api_put(session, base_url, f"/content/{page_id}", update_data)
        r_id = result.get("id", "")
        return f"Updated page '{title}' (id: {r_id}) in space '{space_key}' – version {current_version + 1}"
    else:
        # Create new page
        create_data: dict = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {"storage": {"value": storage_body, "representation": "storage"}},
        }
        if parent_id:
            create_data["ancestors"] = [{"id": parent_id}]
        result = _api_post(session, base_url, "/content", create_data)
        r_id = result.get("id", "")
        return f"Created page '{title}' (id: {r_id}) in space '{space_key}'"


@mcp.tool()
def push_prompt_to_confluence(
    space_key: str,
    prompt_name: str,
    content: str,
    parent_id: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Push a prompt to Confluence as a documentation page.

    Creates or updates a page titled "Prompt: <prompt_name>" in the given
    space. The page is labelled with "prompt" and "llm" for easy retrieval.

    Args:
        space_key: The space key.
        prompt_name: Name of the prompt.
        content: The prompt content (Markdown).
        parent_id: Optional parent page ID.
        token: Optional Confluence Personal Access Token.
    """
    title = f"Prompt: {prompt_name}"
    result = push_documentation(
        space_key=space_key,
        title=title,
        content=content,
        parent_id=parent_id,
        token=token,
    )

    # Try to add labels (non-fatal if it fails)
    try:
        session, base_url = _build_session(token)
        # Extract page ID from result
        existing = _api_get(
            session,
            base_url,
            "/content",
            params={"spaceKey": space_key, "title": title, "limit": 1},
        )
        pages = existing.get("results", [])
        if pages:
            page_id = pages[0]["id"]
            label_url = f"{base_url}/rest/api/content/{page_id}/label"
            session.post(
                label_url,
                json=[{"name": "prompt"}, {"name": "llm"}],
                timeout=_get_timeout(),
            )
    except (requests.RequestException, KeyError, ValueError):
        pass  # Labels are nice-to-have, not critical

    return result


# ---------------------------------------------------------------------------
# Markdown ↔ Storage format conversion
# ---------------------------------------------------------------------------


def _markdown_to_storage(md: str) -> str:
    """
    Convert Markdown content to Confluence storage format (XHTML).

    This is a pragmatic conversion that handles common Markdown constructs.
    For complex documents, the Confluence REST API's convert endpoint can
    be used instead.
    """
    lines = md.split("\n")
    html_parts: list[str] = []
    in_code_block = False
    code_lang = ""
    code_lines: list[str] = []

    for line in lines:
        # Fenced code blocks
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lang = line.strip().removeprefix("```").strip()
                code_lines = []
            else:
                in_code_block = False
                code_content = "\n".join(code_lines)
                if code_lang:
                    html_parts.append(
                        f'<ac:structured-macro ac:name="code">'
                        f'<ac:parameter ac:name="language">{_escape_html(code_lang)}</ac:parameter>'
                        f"<ac:plain-text-body><![CDATA[{code_content}]]>"
                        f"</ac:plain-text-body></ac:structured-macro>"
                    )
                else:
                    html_parts.append(f"<pre><code>{_escape_html(code_content)}</code></pre>")
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = _escape_html(heading_match.group(2))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            continue

        # Unordered list items
        ul_match = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if ul_match:
            text = _escape_html(ul_match.group(2))
            html_parts.append(f"<ul><li>{text}</li></ul>")
            continue

        # Ordered list items
        ol_match = re.match(r"^(\s*)\d+\.\s+(.+)$", line)
        if ol_match:
            text = _escape_html(ol_match.group(2))
            html_parts.append(f"<ol><li>{text}</li></ol>")
            continue

        # Horizontal rule
        if re.match(r"^---+$", line.strip()):
            html_parts.append("<hr />")
            continue

        # Empty line → paragraph break
        if not line.strip():
            html_parts.append("")
            continue

        # Bold, italic, inline code
        text = _escape_html(line)
        text = re.sub(
            r"`([^`]+)`",
            lambda m: f"<code>{m.group(1)}</code>",
            text,
        )
        html_parts.append(f"<p>{text}</p>")

    return "\n".join(html_parts)


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
