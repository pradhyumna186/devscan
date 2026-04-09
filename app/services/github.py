"""
GitHub API service.

Handles two operations:
  1. Fetching the raw unified diff for a PR.
  2. Posting each analyzed issue as an inline review comment on the PR.
"""

from typing import Any

import httpx

GITHUB_API_BASE = "https://api.github.com"


async def get_pr_diff(repo_full_name: str, pr_number: int, token: str) -> str:
    """
    Fetch the raw unified diff for a pull request.

    Args:
        repo_full_name: e.g. "octocat/hello-world"
        pr_number:      The PR number.
        token:          GitHub personal access token or App installation token.

    Returns:
        The raw diff string (unified diff format).

    Raises:
        httpx.HTTPStatusError: if GitHub returns a non-2xx response.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


async def post_review_comments(
    repo_full_name: str,
    pr_number:      int,
    issues:         list[dict[str, Any]],
    token:          str,
) -> None:
    """
    Post each issue as an inline review comment on the PR.

    Each issue dict is expected to have:
        {
            "severity":      "CRITICAL" | "MAJOR" | "MINOR",
            "file_path":     str,
            "line_number":   int | None,
            "description":   str,
            "suggested_fix": str | None,
        }

    Uses the Pull Request Review Comments API so comments appear inline on the diff.
    GitHub requires ``commit_id`` (the PR head commit SHA) for that endpoint; without it,
    inline requests fail and only a subset of issues could appear on the PR before.

    If an inline comment still fails (e.g. line not part of the diff), posts the same
    text as a PR conversation comment so GitHub matches what DevScan stored.

    If ``line_number`` is missing, posts a regular issue comment.

    Args:
        repo_full_name: e.g. "octocat/hello-world"
        pr_number:      The PR number.
        issues:         List of issue dicts from the LLM service.
        token:          GitHub personal access token or App installation token.
    """
    if not issues:
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    pull_url      = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}"
    inline_url    = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/comments"
    issue_comment = f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"

    async with httpx.AsyncClient(timeout=30.0) as client:
        pr_resp = await client.get(pull_url, headers=headers)
        head_sha: str | None = None
        if pr_resp.status_code == 200:
            head_sha = pr_resp.json().get("head", {}).get("sha")
        else:
            print(f"[github] Failed to fetch PR for head SHA: {pr_resp.status_code} {pr_resp.text[:200]}")

        for issue in issues:
            severity      = issue.get("severity", "MINOR").upper()
            file_path     = issue.get("file_path", "unknown")
            line_number   = issue.get("line_number")
            description   = issue.get("description", "")
            suggested_fix = issue.get("suggested_fix")

            # Format the comment body with severity badge
            body = f"**[{severity}]** {description}"
            if suggested_fix:
                body += f"\n\n**Suggested fix:**\n```\n{suggested_fix}\n```"

            if line_number and head_sha:
                payload = {
                    "body":       body,
                    "commit_id":  head_sha,
                    "path":       file_path,
                    "line":       line_number,
                    "side":       "RIGHT",
                }
                response = await client.post(inline_url, headers=headers, json=payload)
                if response.status_code in (200, 201):
                    continue
                print(f"[github] Inline comment failed: {response.status_code} {response.text[:300]}")
                fb_body = (
                    f"`{file_path}` (suggested line {line_number}) — {body}\n\n"
                    "_Inline placement failed; posted as a PR comment instead._"
                )
                response = await client.post(issue_comment, headers=headers, json={"body": fb_body})
                if response.status_code not in (200, 201):
                    print(f"[github] Fallback comment failed: {response.status_code} {response.text[:200]}")
            elif line_number and not head_sha:
                response = await client.post(
                    issue_comment,
                    headers=headers,
                    json={"body": f"`{file_path}` (line {line_number}) — {body}"},
                )
                if response.status_code not in (200, 201):
                    print(f"[github] Failed to post comment: {response.status_code} {response.text[:200]}")
            else:
                response = await client.post(
                    issue_comment,
                    headers=headers,
                    json={"body": f"`{file_path}` — {body}"},
                )
                if response.status_code not in (200, 201):
                    print(f"[github] Failed to post comment: {response.status_code} {response.text[:200]}")
