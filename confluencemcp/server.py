"""
Confluence MCP Server built with fastmcp.

Optimized for LLM context retrieval – searching for, locating, and pushing
documentation in Atlassian Confluence.

Provides tools to:
- Search pages using CQL (Confluence Query Language) and full-text search
- Retrieve page content as clean Markdown for LLM consumption
- Browse space and page hierarchies
- Push documentation and prompts to Confluence
- Get page metadata, labels, and comments

Authentication (via environment variables):
  CONFLUENCE_URL       – Confluence base URL (e.g. https://your-domain.atlassian.net)
  CONFLUENCE_USERNAME  – Confluence username (email for Atlassian Cloud)
  CONFLUENCE_API_TOKEN – Confluence API token
                         (generate at https://id.atlassian.com/manage-profile/security/api-tokens)

  Alternatively for Confluence Server/Data Center:
  CONFLUENCE_PAT       – Personal Access Token (used instead of username+token)

Connection options:
  CONFLUENCE_VERIFY_SSL – Set to "false" to disable SSL verification.
  CONFLUENCE_TIMEOUT    – Request timeout in seconds (default: 15).
"""

import os
import re
import logging
from typing import Optional

from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_md
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialise server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="confluence-mcp-server",
    instructions=(
        "A Confluence MCP server optimized for LLM context retrieval. "
        "Search for documentation, retrieve page content as clean Markdown, "
        "browse spaces and hierarchies, and push documentation updates. "
        "Ideal for locating relevant context for LLM tasks and maintaining "
        "documentation in Confluence."
    ),
)

# ---------------------------------------------------------------------------
# Auth / connection helpers
# ---------------------------------------------------------------------------

_confluence_client = None


def _get_confluence():
    """Return a cached Confluence client, building it on first use."""
    global _confluence_client
    if _confluence_client is not None:
        return _confluence_client

    try:
        from atlassian import Confluence
    except ImportError:
        raise ImportError(
            "atlassian-python-api is required. Install with: "
            "pip install atlassian-python-api"
        )

    url = os.getenv("CONFLUENCE_URL")
    if not url:
        raise ValueError(
            "CONFLUENCE_URL environment variable is required. "
            "Set it to your Confluence instance URL "
            "(e.g. https://your-domain.atlassian.net)."
        )

    verify_env = os.getenv("CONFLUENCE_VERIFY_SSL", "true").strip().lower()
    verify = verify_env not in ("false", "0", "no")

    timeout = 15
    timeout_env = os.getenv("CONFLUENCE_TIMEOUT")
    if timeout_env:
        try:
            timeout = int(timeout_env)
        except ValueError:
            pass

    # Try PAT first (Confluence Server/Data Center)
    pat = os.getenv("CONFLUENCE_PAT")
    if pat:
        _confluence_client = Confluence(
            url=url,
            token=pat,
            verify_ssl=verify,
            timeout=timeout,
        )
        return _confluence_client

    # Atlassian Cloud: username + API token
    username = os.getenv("CONFLUENCE_USERNAME")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")
    if username and api_token:
        _confluence_client = Confluence(
            url=url,
            username=username,
            password=api_token,
            cloud=True,
            verify_ssl=verify,
            timeout=timeout,
        )
        return _confluence_client

    raise ValueError(
        "No Confluence credentials found. Provide one of:\n"
        "  • CONFLUENCE_USERNAME + CONFLUENCE_API_TOKEN (Atlassian Cloud)\n"
        "  • CONFLUENCE_PAT (Confluence Server/Data Center)\n"
        "Along with CONFLUENCE_URL."
    )


# ---------------------------------------------------------------------------
# Content conversion helpers
# ---------------------------------------------------------------------------


def _confluence_html_to_markdown(html: str) -> str:
    """Convert Confluence storage format HTML to clean Markdown."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Remove Confluence macros that don't render well
    for tag in soup.find_all("ac:structured-macro"):
        macro_name = tag.get("ac:name", "")
        if macro_name in ("toc", "anchor", "jira"):
            tag.decompose()
    # Remove style and script tags
    for tag in soup(["style", "script"]):
        tag.decompose()
    md = html_to_md(str(soup), heading_style="ATX", strip=["img"])
    # Clean up excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _page_summary(page: dict) -> str:
    """Format a Confluence page as a summary line."""
    page_id = page.get("id", "?")
    title = page.get("title", "(untitled)")
    space_key = ""
    if "space" in page:
        space_key = page["space"].get("key", "")
    status = page.get("status", "")
    url = ""
    if "_links" in page and "webui" in page["_links"]:
        base = page.get("_links", {}).get("base", "")
        url = base + page["_links"]["webui"]
    parts = [f"[{page_id}] {title}"]
    if space_key:
        parts.append(f"(space: {space_key})")
    if status and status != "current":
        parts.append(f"[{status}]")
    if url:
        parts.append(f"– {url}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Space operations
# ---------------------------------------------------------------------------


@mcp.tool()
def list_spaces(
    space_type: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """
    List available Confluence spaces.

    Args:
        space_type: Optional filter – "global" or "personal".
        max_results: Maximum number of spaces to return. Defaults to 50.
    """
    confluence = _get_confluence()
    kwargs = {"limit": min(max_results, 100)}
    if space_type:
        kwargs["space_type"] = space_type
    spaces = confluence.get_all_spaces(**kwargs)
    results = spaces.get("results", [])
    if not results:
        return "No spaces found."
    lines = []
    for s in results:
        key = s.get("key", "?")
        name = s.get("name", "(unnamed)")
        stype = s.get("type", "")
        lines.append(f"[{key}] {name} ({stype})")
    return "\n".join(lines)


@mcp.tool()
def get_space_info(space_key: str) -> str:
    """
    Get detailed information about a Confluence space.

    Args:
        space_key: The space key (e.g. "DEV", "DOCS").
    """
    confluence = _get_confluence()
    space = confluence.get_space(space_key, expand="description.plain,homepage")
    if not space:
        return f"Space '{space_key}' not found."
    name = space.get("name", "(unnamed)")
    stype = space.get("type", "")
    desc = ""
    if "description" in space and "plain" in space["description"]:
        desc = space["description"]["plain"].get("value", "")
    homepage_title = ""
    if "homepage" in space:
        homepage_title = space["homepage"].get("title", "")
    return (
        f"Space:       {name} [{space_key}]\n"
        f"Type:        {stype}\n"
        f"Description: {desc or '(none)'}\n"
        f"Homepage:    {homepage_title or '(none)'}"
    )


# ---------------------------------------------------------------------------
# Page retrieval
# ---------------------------------------------------------------------------


@mcp.tool()
def get_page_content(
    page_id: Optional[str] = None,
    space_key: Optional[str] = None,
    title: Optional[str] = None,
    output_format: str = "markdown",
) -> str:
    """
    Retrieve a Confluence page and return it as clean Markdown (or raw storage).

    Optimized for LLM consumption – strips boilerplate and converts to
    readable Markdown by default.

    You must provide either page_id, or both space_key and title.

    Args:
        page_id: Confluence page ID (numeric string).
        space_key: Space key to look up the page by title.
        title: Page title (used with space_key).
        output_format: "markdown" (default) or "storage" (raw Confluence HTML).
    """
    confluence = _get_confluence()

    if page_id:
        page = confluence.get_page_by_id(
            page_id, expand="body.storage,version,space"
        )
    elif space_key and title:
        page = confluence.get_page_by_title(
            space_key, title, expand="body.storage,version,space"
        )
    else:
        return "Provide either page_id, or both space_key and title."

    if not page:
        return "Page not found."

    page_title = page.get("title", "(untitled)")
    version = page.get("version", {}).get("number", "?")
    space = page.get("space", {}).get("key", "?")
    html_content = page.get("body", {}).get("storage", {}).get("value", "")

    if output_format == "storage":
        header = (
            f"# {page_title}\n"
            f"Space: {space} | Version: {version}\n\n"
        )
        return header + html_content

    md_content = _confluence_html_to_markdown(html_content)
    header = (
        f"# {page_title}\n\n"
        f"*Space: {space} | Version: {version}*\n\n---\n\n"
    )
    return header + md_content


@mcp.tool()
def get_page_children(
    page_id: str,
    max_results: int = 50,
) -> str:
    """
    List child pages of a given Confluence page. Useful for navigating
    documentation hierarchies.

    Args:
        page_id: Parent page ID.
        max_results: Maximum number of children to return.
    """
    confluence = _get_confluence()
    children = confluence.get_page_child_by_type(
        page_id, type="page", limit=min(max_results, 100)
    )
    if not children:
        return f"No child pages found for page {page_id}."
    lines = [_page_summary(c) for c in children]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Search – optimized for LLM context retrieval
# ---------------------------------------------------------------------------


@mcp.tool()
def search_confluence(
    query: str,
    space_key: Optional[str] = None,
    max_results: int = 10,
    return_content: bool = True,
) -> str:
    """
    Search Confluence using CQL (Confluence Query Language) and return results
    as clean Markdown – optimized for LLM context.

    This is the primary tool for finding relevant documentation. It performs
    a full-text search across all page content and titles.

    Args:
        query: Search text. The tool builds a CQL query that searches both
               title and body. For advanced use, you can pass raw CQL
               (prefix with "cql:").
        space_key: Optional space key to limit the search scope.
        max_results: Maximum number of results. Defaults to 10.
        return_content: If True (default), includes page content as Markdown.
                        If False, returns only titles and links.
    """
    confluence = _get_confluence()

    # Build CQL
    if query.startswith("cql:"):
        cql = query[4:].strip()
    else:
        # Escape special CQL characters
        escaped = query.replace('"', '\\"')
        cql = f'(text ~ "{escaped}" OR title ~ "{escaped}") AND type = "page"'
        if space_key:
            cql += f' AND space = "{space_key}"'

    results = confluence.cql(cql, limit=min(max_results, 50), expand="content.body.storage,content.space,content.version")
    search_results = results.get("results", [])

    if not search_results:
        return f"No results found for: {query}"

    parts = [
        f"# Confluence Search Results\n\n"
        f"**Query:** {query}\n"
        f"**Results:** {len(search_results)}\n\n---\n"
    ]

    for i, result in enumerate(search_results, 1):
        content = result.get("content", result)
        title = content.get("title", "(untitled)")
        page_id = content.get("id", "?")
        space = content.get("space", {}).get("key", "")
        url = ""
        if "_links" in content and "webui" in content["_links"]:
            base = result.get("_links", {}).get("base", "")
            if not base:
                base = content.get("_links", {}).get("base", "")
            url = base + content["_links"]["webui"]

        parts.append(f"\n## {i}. {title}\n")
        parts.append(f"**Page ID:** {page_id} | **Space:** {space}\n")
        if url:
            parts.append(f"**URL:** {url}\n")

        if return_content:
            html = content.get("body", {}).get("storage", {}).get("value", "")
            if html:
                md = _confluence_html_to_markdown(html)
                # Truncate very long pages to keep output manageable
                if len(md) > 5000:
                    md = md[:5000] + "\n\n*[Content truncated – use get_page_content for full text]*"
                parts.append(f"\n{md}\n")

        parts.append("\n---\n")

    return "\n".join(parts)


@mcp.tool()
def search_confluence_by_label(
    label: str,
    space_key: Optional[str] = None,
    max_results: int = 20,
) -> str:
    """
    Find Confluence pages by label. Useful for finding categorized documentation.

    Args:
        label: Label to search for (e.g. "architecture", "api-docs", "runbook").
        space_key: Optional space key to limit the search.
        max_results: Maximum number of results.
    """
    confluence = _get_confluence()
    cql = f'label = "{label}" AND type = "page"'
    if space_key:
        cql += f' AND space = "{space_key}"'

    results = confluence.cql(cql, limit=min(max_results, 50))
    search_results = results.get("results", [])

    if not search_results:
        return f"No pages found with label '{label}'."

    lines = [f"Pages with label '{label}' ({len(search_results)} found):\n"]
    for result in search_results:
        content = result.get("content", result)
        lines.append(_page_summary(content))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page comments
# ---------------------------------------------------------------------------


@mcp.tool()
def get_page_comments(
    page_id: str,
    max_results: int = 20,
) -> str:
    """
    Retrieve comments on a Confluence page. Useful for understanding
    discussion context around documentation.

    Args:
        page_id: The Confluence page ID.
        max_results: Maximum number of comments to return.
    """
    confluence = _get_confluence()
    comments = confluence.get_page_comments(
        page_id, expand="body.storage", depth="all"
    )
    results = comments.get("results", [])
    if not results:
        return f"No comments found on page {page_id}."

    results = results[:max_results]
    lines = [f"Comments on page {page_id} ({len(results)}):\n"]
    for comment in results:
        author = comment.get("version", {}).get("by", {}).get("displayName", "Unknown")
        created = comment.get("version", {}).get("when", "?")
        html = comment.get("body", {}).get("storage", {}).get("value", "")
        text = _confluence_html_to_markdown(html) if html else "(empty)"
        # Truncate long comments
        if len(text) > 500:
            text = text[:500] + "..."
        lines.append(f"**{author}** ({created}):\n{text}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Content creation / update – push context or prompts for documentation
# ---------------------------------------------------------------------------


@mcp.tool()
def create_page(
    space_key: str,
    title: str,
    content: str,
    parent_id: Optional[str] = None,
    content_format: str = "markdown",
) -> str:
    """
    Create a new Confluence page. Ideal for pushing generated documentation,
    prompts, or LLM-produced context to Confluence.

    Args:
        space_key: Space key where the page will be created.
        title: Page title.
        content: Page content. By default interpreted as Markdown and
                 converted to Confluence storage format.
        parent_id: Optional parent page ID to create as a child page.
        content_format: "markdown" (default – auto-converted) or "storage"
                        (raw Confluence HTML).
    """
    confluence = _get_confluence()

    if content_format == "markdown":
        # Convert markdown to basic HTML for Confluence storage
        html_content = _markdown_to_confluence_html(content)
    else:
        html_content = content

    result = confluence.create_page(
        space=space_key,
        title=title,
        body=html_content,
        parent_id=parent_id,
        type="page",
        representation="storage",
    )
    page_id = result.get("id", "?")
    url = ""
    if "_links" in result:
        base = result["_links"].get("base", "")
        webui = result["_links"].get("webui", "")
        url = base + webui
    return f"Created page '{title}' (ID: {page_id})\n{url}"


@mcp.tool()
def update_page(
    page_id: str,
    title: str,
    content: str,
    content_format: str = "markdown",
) -> str:
    """
    Update an existing Confluence page. Useful for pushing updated
    documentation or LLM-generated content.

    Args:
        page_id: The Confluence page ID to update.
        title: Updated page title.
        content: Updated page content.
        content_format: "markdown" (default – auto-converted) or "storage"
                        (raw Confluence HTML).
    """
    confluence = _get_confluence()

    if content_format == "markdown":
        html_content = _markdown_to_confluence_html(content)
    else:
        html_content = content

    result = confluence.update_page(
        page_id=page_id,
        title=title,
        body=html_content,
        representation="storage",
    )
    version = result.get("version", {}).get("number", "?")
    return f"Updated page '{title}' (ID: {page_id}, version: {version})"


def _markdown_to_confluence_html(md: str) -> str:
    """
    Convert Markdown to basic Confluence storage format HTML.
    Handles headings, bold, italic, code blocks, links, and lists.
    """
    lines = md.split("\n")
    html_lines: list[str] = []
    in_code_block = False
    in_list = False

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                html_lines.append("</ac:plain-text-body></ac:structured-macro>")
                in_code_block = False
            else:
                lang = line.strip()[3:].strip()
                macro = '<ac:structured-macro ac:name="code">'
                if lang:
                    macro += f'<ac:parameter ac:name="language">{lang}</ac:parameter>'
                macro += "<ac:plain-text-body><![CDATA["
                html_lines.append(macro)
                in_code_block = True
            continue

        if in_code_block:
            html_lines.append(line)
            continue

        # Close list if not a list item
        if in_list and not line.strip().startswith(("- ", "* ", "1.")):
            html_lines.append("</ul>")
            in_list = False

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            html_lines.append(f"<h{level}>{text}</h{level}>")
            continue

        # Unordered list items
        list_match = re.match(r"^[\-\*]\s+(.*)", line.strip())
        if list_match:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{list_match.group(1)}</li>")
            continue

        # Empty lines
        if not line.strip():
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
            continue

        # Inline formatting
        formatted = line
        formatted = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", formatted)
        formatted = re.sub(r"\*(.+?)\*", r"<em>\1</em>", formatted)
        formatted = re.sub(r"`(.+?)`", r"<code>\1</code>", formatted)
        formatted = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', formatted)

        html_lines.append(f"<p>{formatted}</p>")

    if in_list:
        html_lines.append("</ul>")
    if in_code_block:
        html_lines.append("]]></ac:plain-text-body></ac:structured-macro>")

    return "\n".join(html_lines)


@mcp.tool()
def add_label(
    page_id: str,
    label: str,
) -> str:
    """
    Add a label to a Confluence page.

    Args:
        page_id: The Confluence page ID.
        label: Label to add.
    """
    confluence = _get_confluence()
    confluence.set_page_label(page_id, label)
    return f"Added label '{label}' to page {page_id}."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
