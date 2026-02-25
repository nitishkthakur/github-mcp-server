"""
GitHub MCP Server built with fastmcp.

Provides tools to:
- Read/write files and prompts in GitHub repositories
- Deploy content to GitHub Pages
- Manage branches, commits, issues, and repositories
"""

import base64
import os
from typing import Optional

from fastmcp import FastMCP
from github import Github, GithubException
from github import InputGitAuthor

# ---------------------------------------------------------------------------
# Initialise server and GitHub client
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="github-mcp-server",
    instructions=(
        "A GitHub MCP server that lets you read and write files, manage prompts, "
        "deploy to GitHub Pages, and perform common repository operations using "
        "a GitHub Personal Access Token."
    ),
)

_github_client: Optional[Github] = None


def _get_github(token: Optional[str] = None) -> Github:
    """Return a (cached) GitHub client, preferring the supplied token."""
    global _github_client
    if token:
        return Github(token)
    if _github_client is None:
        pat = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
        if not pat:
            raise ValueError(
                "No GitHub token provided. Set the GITHUB_TOKEN environment variable "
                "or pass a token argument to the tool."
            )
        _github_client = Github(pat)
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
