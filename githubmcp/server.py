"""
GitHub MCP Server built with fastmcp.

Provides tools to:
- Read/write files and prompts in GitHub repositories
- Deploy content to GitHub Pages
- Search GitHub Pages content using regex for LLM context retrieval
- Manage branches, commits, issues, and repositories

Authentication styles supported (checked in priority order):
1. Explicit ``token`` argument passed directly to any tool
2. GitHub App Installation  (GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY[_FILE] + GITHUB_APP_INSTALLATION_ID)
3. GitHub App JWT only      (GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY[_FILE], no installation id)
4. OAuth App user token     (GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET + GITHUB_OAUTH_TOKEN)
5. Token from file          (GITHUB_TOKEN_FILE)
6. Personal / fine-grained access token from env  (GITHUB_TOKEN | GH_TOKEN | GITHUB_PERSONAL_ACCESS_TOKEN)
7. Login + password         (GITHUB_LOGIN + GITHUB_PASSWORD)  [deprecated by GitHub but still functional]
8. Netrc credentials        (GITHUB_USE_NETRC=true, reads ~/.netrc)

Connection / firewall options (all via environment variables):
  GITHUB_BASE_URL      – Custom API base URL for GitHub Enterprise Server (GHES)
                         e.g. https://github.mycompany.com/api/v3
  GITHUB_PROXY         – HTTP/HTTPS proxy URL forwarded to the underlying
                         requests session, e.g. http://proxy.corp.example.com:8080
                         Standard HTTPS_PROXY / HTTP_PROXY env vars also work.
  GITHUB_VERIFY_SSL    – Set to "false" to disable SSL verification (useful when
                         a corporate firewall performs TLS inspection).
                         Set to a file path to use a custom CA bundle.
  GITHUB_TIMEOUT       – Request timeout in seconds (default: 15).
"""

import os
import re
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_md
from dotenv import load_dotenv
from fastmcp import FastMCP
from github import Auth, Github, GithubException
from github import InputGitAuthor

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialise server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="github-mcp-server",
    instructions=(
        "A GitHub MCP server that lets you read and write files, manage prompts, "
        "deploy to GitHub Pages, and perform common repository operations. "
        "Supports multiple authentication styles including PAT, GitHub Apps, "
        "OAuth, .netrc, and GitHub Enterprise Server."
    ),
)

# Module-level cached client (used when no per-call token is supplied)
_github_client: Optional[Github] = None


# ---------------------------------------------------------------------------
# Auth / connection helpers
# ---------------------------------------------------------------------------


def _connection_kwargs() -> dict:
    """
    Build keyword arguments common to all Github() instantiations:
    base_url, verify, timeout, and proxy injection.
    """
    kwargs: dict = {}

    # GitHub Enterprise Server base URL
    base_url = os.getenv("GITHUB_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url.rstrip("/")

    # SSL verification (supports "false" to disable, or a CA bundle path)
    verify_env = os.getenv("GITHUB_VERIFY_SSL", "true").strip().lower()
    if verify_env == "false":
        kwargs["verify"] = False
    elif verify_env not in ("true", "1", "yes", ""):
        # Non-boolean → treat as path to a CA bundle file
        kwargs["verify"] = verify_env

    # Request timeout
    timeout_env = os.getenv("GITHUB_TIMEOUT")
    if timeout_env:
        try:
            kwargs["timeout"] = int(timeout_env)
        except ValueError:
            pass

    # Proxy – inject into environment so that requests picks it up automatically.
    # We support GITHUB_PROXY as an alias for HTTPS_PROXY / HTTP_PROXY.
    proxy = os.getenv("GITHUB_PROXY")
    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)

    return kwargs


def _build_github_client() -> Github:
    """
    Build a Github client by trying every supported authentication strategy
    in priority order.  Connection settings (proxy, SSL, base URL, timeout)
    are applied to every strategy.
    """
    ckw = _connection_kwargs()

    # ------------------------------------------------------------------
    # 1. GitHub App Installation auth
    #    Requires: GITHUB_APP_ID + (GITHUB_APP_PRIVATE_KEY or
    #              GITHUB_APP_PRIVATE_KEY_FILE) + GITHUB_APP_INSTALLATION_ID
    # ------------------------------------------------------------------
    app_id = os.getenv("GITHUB_APP_ID")
    installation_id_env = os.getenv("GITHUB_APP_INSTALLATION_ID")
    private_key = _load_app_private_key()

    if app_id and private_key and installation_id_env:
        app_auth = Auth.AppAuth(int(app_id), private_key)
        inst_auth = Auth.AppInstallationAuth(app_auth, int(installation_id_env))
        return Github(auth=inst_auth, **ckw)

    # ------------------------------------------------------------------
    # 2. GitHub App JWT auth (no installation – server-level API access)
    #    Requires: GITHUB_APP_ID + (GITHUB_APP_PRIVATE_KEY or
    #              GITHUB_APP_PRIVATE_KEY_FILE)
    # ------------------------------------------------------------------
    if app_id and private_key:
        app_auth = Auth.AppAuth(int(app_id), private_key)
        return Github(auth=app_auth, **ckw)

    # ------------------------------------------------------------------
    # 3. OAuth App user token
    #    Requires: GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET + GITHUB_OAUTH_TOKEN
    # ------------------------------------------------------------------
    client_id = os.getenv("GITHUB_CLIENT_ID")
    client_secret = os.getenv("GITHUB_CLIENT_SECRET")
    oauth_token = os.getenv("GITHUB_OAUTH_TOKEN")
    if client_id and client_secret and oauth_token:
        user_auth = Auth.AppUserAuth(
            client_id=client_id,
            client_secret=client_secret,
            token=oauth_token,
        )
        return Github(auth=user_auth, **ckw)

    # ------------------------------------------------------------------
    # 4. Token from file
    #    Requires: GITHUB_TOKEN_FILE pointing to a file whose first line
    #              is the token.
    # ------------------------------------------------------------------
    token_file = os.getenv("GITHUB_TOKEN_FILE")
    if token_file:
        try:
            with open(token_file) as fh:
                file_token = fh.readline().strip()
            if file_token:
                return Github(auth=Auth.Token(file_token), **ckw)
        except OSError as exc:
            raise ValueError(
                f"GITHUB_TOKEN_FILE is set but the file could not be read: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 5. Personal / fine-grained access token from environment variable
    #    Checks GITHUB_TOKEN, GH_TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN
    # ------------------------------------------------------------------
    pat = (
        os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    )
    if pat:
        return Github(auth=Auth.Token(pat), **ckw)

    # ------------------------------------------------------------------
    # 6. Login + password (Basic auth – deprecated but functional)
    #    Requires: GITHUB_LOGIN + GITHUB_PASSWORD
    # ------------------------------------------------------------------
    login = os.getenv("GITHUB_LOGIN")
    password = os.getenv("GITHUB_PASSWORD")
    if login and password:
        return Github(auth=Auth.Login(login, password), **ckw)

    # ------------------------------------------------------------------
    # 7. Netrc credentials
    #    Activated by: GITHUB_USE_NETRC=true
    #    Reads credentials from ~/.netrc for api.github.com (or the
    #    GITHUB_BASE_URL host for GHES).
    # ------------------------------------------------------------------
    if os.getenv("GITHUB_USE_NETRC", "").strip().lower() in ("true", "1", "yes"):
        return Github(auth=Auth.NetrcAuth(), **ckw)

    raise ValueError(
        "No GitHub credentials found. Provide one of the following:\n"
        "  • GITHUB_TOKEN (or GH_TOKEN / GITHUB_PERSONAL_ACCESS_TOKEN)\n"
        "  • GITHUB_TOKEN_FILE\n"
        "  • GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY[_FILE] + GITHUB_APP_INSTALLATION_ID\n"
        "  • GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY[_FILE]  (JWT / app-level only)\n"
        "  • GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET + GITHUB_OAUTH_TOKEN\n"
        "  • GITHUB_LOGIN + GITHUB_PASSWORD\n"
        "  • GITHUB_USE_NETRC=true  (reads ~/.netrc)\n"
        "See README.md for full details."
    )


def _load_app_private_key() -> Optional[str]:
    """
    Return the GitHub App private key as a string, or None if not configured.
    Checks GITHUB_APP_PRIVATE_KEY (raw PEM string) first, then
    GITHUB_APP_PRIVATE_KEY_FILE (path to a PEM file).
    """
    raw = os.getenv("GITHUB_APP_PRIVATE_KEY")
    if raw:
        # Allow the PEM to be stored with literal \n sequences in the env var
        return raw.replace("\\n", "\n")
    key_file = os.getenv("GITHUB_APP_PRIVATE_KEY_FILE")
    if key_file:
        with open(key_file) as fh:
            return fh.read()
    return None


def _get_github(token: Optional[str] = None) -> Github:
    """
    Return a GitHub client.

    If *token* is supplied it is used directly (no caching).
    Otherwise a module-level cached client is returned, building it on first
    use via :func:`_build_github_client`.
    """
    global _github_client
    if token:
        ckw = _connection_kwargs()
        return Github(auth=Auth.Token(token), **ckw)
    if _github_client is None:
        _github_client = _build_github_client()
    return _github_client


def _get_repo(owner: str, repo: str, token: Optional[str] = None):
    """Convenience helper to fetch a Repository object."""
    gh = _get_github(token)
    return gh.get_repo(f"{owner}/{repo}")


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


@mcp.tool()
def get_file_contents(
    owner: str,
    repo: str,
    path: str,
    ref: str = "main",
    token: Optional[str] = None,
) -> str:
    """
    Return the decoded text content of a file from a GitHub repository.

    Args:
        owner: Repository owner (user or organisation).
        repo: Repository name.
        path: File path inside the repository (e.g. "docs/index.md").
        ref: Branch, tag, or commit SHA to read from. Defaults to "main".
        token: Optional GitHub Personal Access Token (overrides env var).
    """
    repository = _get_repo(owner, repo, token)
    contents = repository.get_contents(path, ref=ref)
    if isinstance(contents, list):
        # It's a directory – return a listing instead
        return "\n".join(c.path for c in contents)
    return contents.decoded_content.decode("utf-8")


@mcp.tool()
def create_or_update_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    commit_message: str,
    branch: str = "main",
    author_name: Optional[str] = None,
    author_email: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Create a new file or update an existing file in a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path inside the repository.
        content: Text content to write to the file.
        commit_message: Git commit message.
        branch: Branch to commit to. Defaults to "main".
        author_name: Optional commit author name.
        author_email: Optional commit author e-mail.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    kwargs: dict = {"message": commit_message, "content": content, "branch": branch}
    if author_name and author_email:
        kwargs["committer"] = InputGitAuthor(author_name, author_email)

    try:
        existing = repository.get_contents(path, ref=branch)
        if isinstance(existing, list):
            return "Path points to a directory, not a file."
        result = repository.update_file(path, sha=existing.sha, **kwargs)
        return f"Updated '{path}' – commit {result['commit'].sha}"
    except GithubException as exc:
        if exc.status == 404:
            result = repository.create_file(path, **kwargs)
            return f"Created '{path}' – commit {result['commit'].sha}"
        raise


@mcp.tool()
def delete_file(
    owner: str,
    repo: str,
    path: str,
    commit_message: str,
    branch: str = "main",
    token: Optional[str] = None,
) -> str:
    """
    Delete a file from a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path inside the repository.
        commit_message: Git commit message for the deletion.
        branch: Branch to commit to. Defaults to "main".
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    contents = repository.get_contents(path, ref=branch)
    if isinstance(contents, list):
        return "Path points to a directory – only individual files can be deleted."
    result = repository.delete_file(path, commit_message, contents.sha, branch=branch)
    return f"Deleted '{path}' – commit {result['commit'].sha}"


# ---------------------------------------------------------------------------
# Prompt management
# ---------------------------------------------------------------------------

_DEFAULT_PROMPTS_DIR = "prompts"


@mcp.tool()
def list_prompts(
    owner: str,
    repo: str,
    prompts_dir: str = _DEFAULT_PROMPTS_DIR,
    ref: str = "main",
    token: Optional[str] = None,
) -> str:
    """
    List all prompt files stored in a repository directory.

    Args:
        owner: Repository owner.
        repo: Repository name.
        prompts_dir: Directory inside the repo that stores prompts.
        ref: Branch, tag, or commit SHA to read from.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    try:
        contents = repository.get_contents(prompts_dir, ref=ref)
    except GithubException as exc:
        if exc.status == 404:
            return f"Directory '{prompts_dir}' not found in {owner}/{repo}@{ref}."
        raise
    if not isinstance(contents, list):
        return f"'{prompts_dir}' is a file, not a directory."
    files = [c.name for c in contents if c.type == "file"]
    if not files:
        return f"No prompt files found in '{prompts_dir}'."
    return "\n".join(files)


@mcp.tool()
def get_prompt(
    owner: str,
    repo: str,
    prompt_name: str,
    prompts_dir: str = _DEFAULT_PROMPTS_DIR,
    ref: str = "main",
    token: Optional[str] = None,
) -> str:
    """
    Retrieve a single prompt by name from a repository's prompts directory.

    The prompt file may be stored with or without a file extension. If the
    exact name is not found the tool appends '.txt', '.md', and '.prompt'
    suffixes and tries again before giving up.

    Args:
        owner: Repository owner.
        repo: Repository name.
        prompt_name: Prompt file name (with or without extension).
        prompts_dir: Directory inside the repo that stores prompts.
        ref: Branch, tag, or commit SHA to read from.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    candidates = [
        prompt_name,
        f"{prompt_name}.txt",
        f"{prompt_name}.md",
        f"{prompt_name}.prompt",
    ]
    for name in candidates:
        full_path = f"{prompts_dir}/{name}"
        try:
            contents = repository.get_contents(full_path, ref=ref)
            if isinstance(contents, list):
                continue
            return contents.decoded_content.decode("utf-8")
        except GithubException as exc:
            if exc.status != 404:
                raise
    return (
        f"Prompt '{prompt_name}' not found in '{prompts_dir}' "
        f"of {owner}/{repo}@{ref}."
    )


@mcp.tool()
def push_prompt(
    owner: str,
    repo: str,
    prompt_name: str,
    content: str,
    prompts_dir: str = _DEFAULT_PROMPTS_DIR,
    branch: str = "main",
    commit_message: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Create or update a prompt file in a repository's prompts directory.

    Args:
        owner: Repository owner.
        repo: Repository name.
        prompt_name: Prompt file name (include extension, e.g. "my_prompt.md").
        content: Text content of the prompt.
        prompts_dir: Directory inside the repo that stores prompts.
        branch: Branch to commit to. Defaults to "main".
        commit_message: Optional custom commit message.
        token: Optional GitHub Personal Access Token.
    """
    path = f"{prompts_dir}/{prompt_name}"
    message = commit_message or f"chore: upsert prompt '{prompt_name}'"
    return create_or_update_file(
        owner=owner,
        repo=repo,
        path=path,
        content=content,
        commit_message=message,
        branch=branch,
        token=token,
    )


# ---------------------------------------------------------------------------
# GitHub Pages deployment
# ---------------------------------------------------------------------------


@mcp.tool()
def deploy_to_github_pages(
    owner: str,
    repo: str,
    file_path: str,
    content: str,
    commit_message: Optional[str] = None,
    pages_branch: str = "gh-pages",
    token: Optional[str] = None,
) -> str:
    """
    Deploy (create or update) a file on the GitHub Pages branch of a repository.

    This tool writes *content* to *file_path* on the *pages_branch* branch
    (default: ``gh-pages``), effectively triggering a GitHub Pages rebuild.

    To deploy an entire site, call this tool once per file you wish to publish.

    Args:
        owner: Repository owner.
        repo: Repository name.
        file_path: Destination path on the pages branch (e.g. "index.html").
        content: Text content to write.
        commit_message: Optional custom commit message.
        pages_branch: The branch used by GitHub Pages. Defaults to "gh-pages".
        token: Optional GitHub Personal Access Token.
    """
    message = commit_message or f"deploy: update '{file_path}' via MCP"
    return create_or_update_file(
        owner=owner,
        repo=repo,
        path=file_path,
        content=content,
        commit_message=message,
        branch=pages_branch,
        token=token,
    )


# ---------------------------------------------------------------------------
# Repository information
# ---------------------------------------------------------------------------


@mcp.tool()
def list_repos(
    visibility: str = "all",
    sort: str = "updated",
    token: Optional[str] = None,
) -> str:
    """
    List repositories belonging to the authenticated user.

    Args:
        visibility: Filter by visibility – "all", "public", or "private".
        sort: Sort order – "created", "updated", "pushed", or "full_name".
        token: Optional GitHub Personal Access Token.
    """
    gh = _get_github(token)
    user = gh.get_user()
    repos = user.get_repos(visibility=visibility, sort=sort)
    lines = [f"{r.full_name} ({r.private and 'private' or 'public'})" for r in repos]
    return "\n".join(lines) if lines else "No repositories found."


@mcp.tool()
def get_repo_info(
    owner: str,
    repo: str,
    token: Optional[str] = None,
) -> str:
    """
    Return key metadata about a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        token: Optional GitHub Personal Access Token.
    """
    r = _get_repo(owner, repo, token)
    return (
        f"Name:         {r.full_name}\n"
        f"Description:  {r.description or '(none)'}\n"
        f"Stars:        {r.stargazers_count}\n"
        f"Forks:        {r.forks_count}\n"
        f"Open issues:  {r.open_issues_count}\n"
        f"Default branch: {r.default_branch}\n"
        f"URL:          {r.html_url}\n"
        f"Topics:       {', '.join(r.get_topics()) or '(none)'}"
    )


# ---------------------------------------------------------------------------
# Branch management
# ---------------------------------------------------------------------------


@mcp.tool()
def list_branches(
    owner: str,
    repo: str,
    token: Optional[str] = None,
) -> str:
    """
    List all branches in a repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    branches = [b.name for b in repository.get_branches()]
    return "\n".join(branches) if branches else "No branches found."


@mcp.tool()
def create_branch(
    owner: str,
    repo: str,
    branch_name: str,
    from_branch: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Create a new branch in a repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch_name: Name of the new branch to create.
        from_branch: Source branch/SHA to branch from. Defaults to the repo's
                     default branch.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    source = from_branch or repository.default_branch
    source_sha = repository.get_branch(source).commit.sha
    ref = repository.create_git_ref(f"refs/heads/{branch_name}", source_sha)
    return f"Created branch '{branch_name}' from '{source}' ({source_sha[:7]})"


# ---------------------------------------------------------------------------
# Commit history
# ---------------------------------------------------------------------------


@mcp.tool()
def list_commits(
    owner: str,
    repo: str,
    branch: Optional[str] = None,
    max_results: int = 20,
    token: Optional[str] = None,
) -> str:
    """
    List recent commits in a repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch: Branch to inspect. Defaults to the repo's default branch.
        max_results: Maximum number of commits to return. Defaults to 20.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    kwargs: dict = {}
    if branch:
        kwargs["sha"] = branch
    commits = list(repository.get_commits(**kwargs)[:max_results])
    lines = [
        f"{c.sha[:7]}  {c.commit.author.date.date()}  {c.commit.message.splitlines()[0]}"
        for c in commits
    ]
    return "\n".join(lines) if lines else "No commits found."


# ---------------------------------------------------------------------------
# Code search
# ---------------------------------------------------------------------------


@mcp.tool()
def search_code(
    query: str,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    max_results: int = 20,
    token: Optional[str] = None,
) -> str:
    """
    Search for code across GitHub repositories.

    Args:
        query: Code search query (GitHub code search syntax is supported).
        owner: Optional repository owner to scope the search.
        repo: Optional repository name to scope the search (requires owner).
        max_results: Maximum number of results to return. Defaults to 20.
        token: Optional GitHub Personal Access Token.
    """
    gh = _get_github(token)
    scoped_query = query
    if owner and repo:
        scoped_query = f"{query} repo:{owner}/{repo}"
    elif owner:
        scoped_query = f"{query} user:{owner}"
    results = gh.search_code(scoped_query)
    lines = []
    for i, item in enumerate(results):
        if i >= max_results:
            break
        lines.append(f"{item.repository.full_name}  {item.path}")
    return "\n".join(lines) if lines else "No results found."


# ---------------------------------------------------------------------------
# Issue management
# ---------------------------------------------------------------------------


@mcp.tool()
def list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    max_results: int = 20,
    token: Optional[str] = None,
) -> str:
    """
    List issues in a repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        state: Issue state to filter by – "open", "closed", or "all".
        max_results: Maximum number of issues to return. Defaults to 20.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    issues = list(repository.get_issues(state=state)[:max_results])
    lines = [f"#{i.number}  {i.title}  [{i.state}]" for i in issues]
    return "\n".join(lines) if lines else "No issues found."


@mcp.tool()
def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: Optional[str] = None,
    labels: Optional[list[str]] = None,
    token: Optional[str] = None,
) -> str:
    """
    Create a new issue in a repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        title: Issue title.
        body: Optional issue body / description.
        labels: Optional list of label names to attach to the issue.
        token: Optional GitHub Personal Access Token.
    """
    repository = _get_repo(owner, repo, token)
    kwargs: dict = {"title": title}
    if body:
        kwargs["body"] = body
    if labels:
        kwargs["labels"] = labels
    issue = repository.create_issue(**kwargs)
    return f"Created issue #{issue.number}: {issue.title}\n{issue.html_url}"


# ---------------------------------------------------------------------------
# GitHub Pages – regex-based retrieval for LLMs
# ---------------------------------------------------------------------------

_DEFAULT_REQUEST_TIMEOUT = 15
_MAX_CRAWL_PAGES = 200
_MAX_PAGE_SIZE = 500_000  # 500KB per page


def _pages_base_url(owner: str, repo: str, custom_domain: Optional[str] = None) -> str:
    """Determine the GitHub Pages base URL for a repository."""
    if custom_domain:
        return custom_domain.rstrip("/")
    # Standard GitHub Pages URL pattern
    return f"https://{owner}.github.io/{repo}"


def _fetch_page(url: str, timeout: int = _DEFAULT_REQUEST_TIMEOUT) -> Optional[str]:
    """Fetch a page and return its HTML content, or None on failure."""
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "GitHub-MCP-Server-Pages-Crawler/1.0",
        })
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return None
        return resp.text[:_MAX_PAGE_SIZE]
    except requests.RequestException as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
        return None


def _discover_pages_from_sitemap(base_url: str, timeout: int = _DEFAULT_REQUEST_TIMEOUT) -> list[str]:
    """Try to discover pages from sitemap.xml."""
    urls: list[str] = []
    sitemap_url = f"{base_url}/sitemap.xml"
    try:
        resp = requests.get(sitemap_url, timeout=timeout, headers={
            "User-Agent": "GitHub-MCP-Server-Pages-Crawler/1.0",
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for loc in soup.find_all("loc"):
            if loc.string:
                urls.append(loc.string.strip())
    except requests.RequestException:
        pass
    return urls


def _discover_pages_by_crawling(
    base_url: str,
    max_pages: int = _MAX_CRAWL_PAGES,
    timeout: int = _DEFAULT_REQUEST_TIMEOUT,
) -> list[str]:
    """
    Crawl the site starting from base_url to discover internal pages.
    Performs breadth-first crawl limited by max_pages.
    """
    visited: set[str] = set()
    queue: list[str] = [base_url, f"{base_url}/"]
    discovered: list[str] = []
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        # Normalise the URL
        normalized = url.split("#")[0].rstrip("/")
        if normalized in visited:
            continue
        visited.add(normalized)

        html = _fetch_page(url, timeout)
        if html is None:
            continue
        discovered.append(url)

        # Extract internal links
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            # Resolve relative URLs
            full_url = urljoin(url, href).split("#")[0].rstrip("/")
            parsed = urlparse(full_url)
            # Only follow links on the same domain
            if parsed.netloc != base_domain:
                continue
            # Skip non-HTTP links
            if parsed.scheme not in ("http", "https"):
                continue
            # Skip common non-page extensions
            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in (
                ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
                ".zip", ".tar", ".gz", ".css", ".js", ".woff", ".woff2",
                ".ttf", ".eot", ".mp4", ".mp3", ".webm",
            )):
                continue
            if full_url not in visited:
                queue.append(full_url)

    return discovered


def _extract_text_from_html(html: str) -> str:
    """Extract visible text from HTML, removing scripts and styles."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove non-visible elements
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _html_to_clean_markdown(html: str) -> str:
    """Convert HTML to clean markdown, removing navigation/boilerplate."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove non-content elements
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    # Prefer <main>, <article>, or <div role="main"> for content
    content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", attrs={"role": "main"})
        or soup.find("div", class_="content")
        or soup.find("div", class_="markdown-body")
        or soup.body
        or soup
    )
    md = html_to_md(str(content), heading_style="ATX", strip=["img"])
    # Clean up excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _score_page(text: str, pattern: re.Pattern, query_terms: Optional[list[str]] = None) -> float:
    """
    Score a page for relevance. Higher is better.

    Combines:
    - Number of regex matches
    - Match density (matches per 1000 chars)
    - Bonus for query term matches in title/headings (first 500 chars)
    """
    matches = pattern.findall(text)
    match_count = len(matches)
    if match_count == 0:
        return 0.0

    text_len = max(len(text), 1)
    density = (match_count / text_len) * 1000
    score = match_count + density * 10

    # Bonus for matches in the first 500 chars (likely title/headings)
    head = text[:500]
    head_matches = len(pattern.findall(head))
    score += head_matches * 5

    # Bonus for query term matches if provided
    if query_terms:
        text_lower = text.lower()
        for term in query_terms:
            if term.lower() in text_lower:
                score += 3

    return round(score, 2)


@mcp.tool()
def list_github_pages(
    owner: str,
    repo: str,
    custom_domain: Optional[str] = None,
    max_pages: int = 50,
    token: Optional[str] = None,
) -> str:
    """
    Discover and list all available pages on a GitHub Pages site.

    First attempts sitemap.xml discovery, then falls back to breadth-first
    crawling from the site root. Returns a list of discovered page URLs.

    Args:
        owner: Repository owner (e.g. "octocat").
        repo: Repository name (e.g. "octocat.github.io").
        custom_domain: Optional custom domain if the site uses one
                       (e.g. "https://docs.example.com").
        max_pages: Maximum number of pages to discover. Defaults to 50.
        token: Optional GitHub Personal Access Token.
    """
    base_url = _pages_base_url(owner, repo, custom_domain)
    max_pages = min(max_pages, _MAX_CRAWL_PAGES)

    # Try sitemap first
    urls = _discover_pages_from_sitemap(base_url)
    if urls:
        urls = urls[:max_pages]
        header = f"Found {len(urls)} pages via sitemap.xml for {base_url}:\n"
        return header + "\n".join(urls)

    # Fall back to crawling
    urls = _discover_pages_by_crawling(base_url, max_pages=max_pages)
    if urls:
        header = f"Discovered {len(urls)} pages by crawling {base_url}:\n"
        return header + "\n".join(urls)

    return f"No pages found for {base_url}. Verify the GitHub Pages site is published."


@mcp.tool()
def get_github_page_as_markdown(
    url: str,
    include_title: bool = True,
) -> str:
    """
    Fetch a single web page and convert it to clean Markdown for LLM consumption.

    Strips navigation, scripts, styles, and boilerplate. Extracts the main
    content area and converts it to readable Markdown.

    Args:
        url: Full URL of the page to fetch.
        include_title: Whether to include the page <title> as an H1 heading.
    """
    html = _fetch_page(url)
    if html is None:
        return f"Failed to fetch page: {url}"

    title = ""
    if include_title:
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = f"# {title_tag.string.strip()}\n\n"

    md = _html_to_clean_markdown(html)
    if not md:
        return f"Page at {url} returned no extractable content."

    source_line = f"\n\n---\n*Source: {url}*"
    return f"{title}{md}{source_line}"


@mcp.tool()
def search_github_pages(
    owner: str,
    repo: str,
    regex_pattern: str,
    custom_domain: Optional[str] = None,
    max_pages_to_crawl: int = 50,
    top_k: int = 5,
    context_chars: int = 200,
    return_full_markdown: bool = True,
    token: Optional[str] = None,
) -> str:
    """
    Search GitHub Pages using regex and return the most relevant pages as Markdown.

    This tool acts as a regex-based retriever for LLMs:
    1. Discovers all pages on the GitHub Pages site (via sitemap or crawling).
    2. Fetches each page and searches its text content with the given regex.
    3. Scores and ranks pages by relevance (match count, density, position).
    4. Returns the top-K pages converted to clean Markdown with match highlights.

    Args:
        owner: Repository owner (e.g. "octocat").
        repo: Repository name.
        regex_pattern: Python regex pattern to search for. Use simple strings for
                       literal search, or full regex syntax for complex patterns.
                       Examples: "authentication", "def \\w+_handler",
                       "(?i)error|exception|fail".
        custom_domain: Optional custom domain for the GitHub Pages site.
        max_pages_to_crawl: Maximum pages to crawl and search. Defaults to 50.
        top_k: Number of top matching pages to return. Defaults to 5.
        context_chars: Characters of context around each match in the summary.
        return_full_markdown: If True, returns full page content as Markdown.
                              If False, returns only match snippets.
        token: Optional GitHub Personal Access Token.
    """
    # Validate regex
    try:
        pattern = re.compile(regex_pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return f"Invalid regex pattern '{regex_pattern}': {exc}"

    base_url = _pages_base_url(owner, repo, custom_domain)
    max_pages_to_crawl = min(max_pages_to_crawl, _MAX_CRAWL_PAGES)

    # Discover pages
    urls = _discover_pages_from_sitemap(base_url)
    if not urls:
        urls = _discover_pages_by_crawling(base_url, max_pages=max_pages_to_crawl)

    if not urls:
        return (
            f"No pages found for {base_url}. "
            "Verify the GitHub Pages site is published and accessible."
        )

    urls = urls[:max_pages_to_crawl]

    # Extract query terms from the pattern for bonus scoring
    query_terms = re.findall(r"[a-zA-Z]{3,}", regex_pattern)

    # Search each page and score results
    scored_pages: list[tuple[float, str, str, str, list[str]]] = []
    for url in urls:
        html = _fetch_page(url)
        if html is None:
            continue

        text = _extract_text_from_html(html)
        if not text:
            continue

        score = _score_page(text, pattern, query_terms)
        if score <= 0:
            continue

        # Extract match snippets with context
        snippets: list[str] = []
        for match in pattern.finditer(text):
            start = max(0, match.start() - context_chars)
            end = min(len(text), match.end() + context_chars)
            snippet = text[start:end].replace("\n", " ")
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            snippets.append(snippet)
            if len(snippets) >= 5:  # Limit snippets per page
                break

        scored_pages.append((score, url, html, text, snippets))

    if not scored_pages:
        return (
            f"No matches found for pattern '{regex_pattern}' "
            f"across {len(urls)} pages on {base_url}."
        )

    # Sort by score descending and take top-k
    scored_pages.sort(key=lambda x: x[0], reverse=True)
    top_pages = scored_pages[:top_k]

    # Build output
    parts: list[str] = []
    parts.append(
        f"# GitHub Pages Search Results\n\n"
        f"**Pattern:** `{regex_pattern}`\n"
        f"**Site:** {base_url}\n"
        f"**Pages searched:** {len(urls)}\n"
        f"**Matches found in:** {len(scored_pages)} pages\n"
        f"**Showing top:** {len(top_pages)}\n\n---\n"
    )

    for rank, (score, url, html, text, snippets) in enumerate(top_pages, 1):
        match_count = len(pattern.findall(text))
        parts.append(f"\n## Result {rank} (score: {score}, matches: {match_count})\n")
        parts.append(f"**URL:** {url}\n")

        if return_full_markdown:
            md = _html_to_clean_markdown(html)
            parts.append(f"\n{md}\n")
        else:
            parts.append("\n**Match snippets:**\n")
            for i, snippet in enumerate(snippets, 1):
                parts.append(f"  {i}. {snippet}\n")

        parts.append("\n---\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
