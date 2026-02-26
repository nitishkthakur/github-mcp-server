# MCP Servers – GitHub & Confluence

A monorepo containing two [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers built with [fastmcp](https://github.com/jlowin/fastmcp):

| Server | Directory | Description |
|---|---|---|
| **GitHub MCP Server** | [`githubmcp/`](githubmcp/) | Read/write files, manage prompts, deploy GitHub Pages, search repositories, and **search & retrieve GitHub Pages content as Markdown for LLMs** |
| **Confluence MCP Server** | [`confluencemcp/`](confluencemcp/) | Search, navigate, read Confluence pages as clean Markdown, and push documentation / prompts back to Confluence — optimised for LLM context retrieval |

---

## 📁 Project structure

```
├── githubmcp/                  # GitHub MCP Server
│   ├── server.py               # FastMCP server with all tools
│   ├── requirements.txt        # Python dependencies
│   ├── mcp.json                # MCP configuration
│   └── tests/
│       └── test_pages_retriever.py
├── confluencemcp/              # Confluence MCP Server
│   ├── server.py               # FastMCP server with all tools
│   ├── requirements.txt        # Python dependencies
│   ├── mcp.json                # MCP configuration
│   └── tests/
│       └── test_confluence.py
├── README.md                   # This file
└── LICENSE
```

---

## 🚀 Quick start

### GitHub MCP Server

```bash
pip install -r githubmcp/requirements.txt
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
python githubmcp/server.py
```

### Confluence MCP Server

```bash
pip install -r confluencemcp/requirements.txt
export CONFLUENCE_URL="https://mycompany.atlassian.net/wiki"
export CONFLUENCE_USERNAME="your-email@company.com"
export CONFLUENCE_API_TOKEN="your-api-token"
python confluencemcp/server.py
```

---

## 🔌 Loading into GitHub Copilot / MCP Clients

Copy the relevant `mcp.json` from either server directory and adjust the paths and credentials. See each server's directory for full configuration examples.

---

## GitHub MCP Server – Tool reference

### Original tools

| Category | Tools |
|---|---|
| **File I/O** | `get_file_contents`, `create_or_update_file`, `delete_file` |
| **Prompt management** | `list_prompts`, `get_prompt`, `push_prompt` |
| **GitHub Pages deploy** | `deploy_to_github_pages` |
| **Repository info** | `list_repos`, `get_repo_info` |
| **Branches** | `list_branches`, `create_branch` |
| **Commits** | `list_commits` |
| **Code search** | `search_code` |
| **Issues** | `list_issues`, `create_issue` |

### GitHub Pages retriever tools (NEW)

| Tool | Description |
|---|---|
| `search_github_pages` | **Regex-based retriever for LLMs.** Crawls a GitHub Pages site (sitemap.xml → BFS fallback), searches all pages with a regex, and returns the top-matching pages as clean Markdown with context snippets. |
| `search_github_pages_multi` | Multi-keyword convenience wrapper — supply a list of keywords, pages mentioning more keywords rank higher. |
| `get_github_pages_sitemap` | Discover all pages on a GitHub Pages site (sitemap.xml or link crawling). |
| `fetch_github_page_as_markdown` | Fetch any web page and convert to clean Markdown (strips nav, sidebar, footer, scripts). |

#### How `search_github_pages` works

1. **Discovery** — Tries `sitemap.xml` first, then falls back to breadth-first crawl of internal links.
2. **Fetch & convert** — Each discovered page is fetched and converted to clean Markdown (nav, sidebar, footer, scripts stripped).
3. **Regex match & rank** — The regex pattern is matched against the Markdown text. Pages are ranked by match count.
4. **Return top-k** — The top-k pages are returned with full Markdown content and context snippets around each match.

---

## Confluence MCP Server – Tool reference

| Tool | Description |
|---|---|
| `search_confluence` | CQL-based full-text search returning results as clean Markdown |
| `search_by_label` | Find pages with a specific label |
| `get_page_content` | Retrieve a page by ID as Markdown |
| `get_page_by_title` | Retrieve a page by space key + title |
| `get_page_tree` | Get page hierarchy (child pages) for understanding structure |
| `list_spaces` | List available Confluence spaces |
| `push_documentation` | Create or update a documentation page (Markdown → Confluence storage) |
| `push_prompt_to_confluence` | Push a prompt as a labelled Confluence page |

### Confluence authentication

| Strategy | Environment Variables |
|---|---|
| API Token (Cloud) | `CONFLUENCE_URL` + `CONFLUENCE_USERNAME` + `CONFLUENCE_API_TOKEN` |
| Personal Access Token (Data Center) | `CONFLUENCE_URL` + `CONFLUENCE_PERSONAL_TOKEN` |

Additional options: `CONFLUENCE_VERIFY_SSL`, `CONFLUENCE_TIMEOUT`, `CONFLUENCE_PROXY`.

---

## 🔑 GitHub authentication

The GitHub MCP server supports **eight authentication strategies** — see [`githubmcp/server.py`](githubmcp/server.py) header for full details.

Most common:

```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
```

---

## 🧪 Running tests

```bash
pip install pytest
python -m pytest -v
```

---

## 📄 License

[MIT](LICENSE)
