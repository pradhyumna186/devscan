"""
LLM service — sends PR diff chunks to a remote LLM API and parses issues.

The remote endpoint must be OpenAI-compatible:
  POST {REMOTE_LLM_BASE_URL}/v1/chat/completions

Environment variables:
    REMOTE_LLM_BASE_URL   (required) e.g. http://192.168.1.20:11434
    REMOTE_LLM_MODEL      (optional) default: qwen2.5-coder:7b
    REMOTE_LLM_API_KEY    (optional) bearer token if your server requires auth
    REMOTE_LLM_TIMEOUT    (optional) seconds per request, default: 120
    LLM_MAX_CHUNK_LINES   (optional) max diff lines per LLM call, default: 300

Strategy:
  1. The diff is split into per-file chunks. Each chunk is sent to the LLM in
     parallel so the model focuses on one file at a time (better recall on
     small models).
  2. Known issues from prior reviews of the same PR are passed as context so
     the model does not re-report what was already found.
  3. Results from all chunks are merged and deduplicated before returning.
"""

import asyncio
import json
import os
from typing import Any

import httpx


# ──────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert code reviewer performing a thorough security and correctness \
audit of a GitHub Pull Request diff.

## Your task
Find ALL issues in the diff provided. Be exhaustive — prefer false positives \
over false negatives.

## Analysis checklist (apply to EVERY change in the diff)

### 1. SECURITY — highest priority
- Secrets, API keys, tokens, passwords, or private keys hardcoded or added in \
ANY file (source, config, tests, .env, docs).
- SQL / command / LDAP / XSS injection vulnerabilities.
- Insecure deserialization, path traversal, SSRF, open redirects.
- Authentication or authorization bypasses.
- Weak or broken cryptography (MD5/SHA1 for security, ECB mode, etc.).

### 2. LOGIC CORRECTNESS — check every modified function
For each changed function, trace through the logic manually:
  - Does the return value match the function name, docstring, and expected \
behavior?
  - Are arithmetic operators correct? (+ vs -, * vs /, etc.)
  - Are comparison operators correct? (< vs >, == vs !=, etc.)
  - Are variable names correct? (no copy-paste name confusion)
  - Are edge cases handled? (division by zero, empty collections, None/null, \
negative numbers)
  - Are loop bounds correct? (off-by-one errors)

### 3. BUGS & ERRORS
- Incorrect algorithm implementation.
- Missing error handling for realistic failure modes.
- Resource leaks (unclosed file handles, DB connections, sockets).
- Race conditions or concurrency issues.

### 4. PERFORMANCE
- O(n²) where O(n) is straightforward.
- N+1 database query patterns.
- Unnecessary recomputation inside loops.

### 5. STYLE — MINOR severity only
- Misleading names (variable, function, class).
- Dead code added by the PR.

## Severity
- CRITICAL: security vulnerabilities (secrets, injections, bypasses), crashes, \
data corruption or loss.
- MAJOR: incorrect logic / wrong output, broken behavior, serious performance \
regressions.
- MINOR: style, naming, minor inefficiencies.

## Output format
Respond ONLY with a valid JSON array. Each element MUST follow this schema:
{
  "severity":      "CRITICAL" | "MAJOR" | "MINOR",
  "file_path":     "<file path from the diff header>",
  "line_number":   <1-based line number in the NEW file, or null>,
  "description":   "<specific, precise description of the issue>",
  "suggested_fix": "<concrete, actionable fix or null>"
}

Return [] if no issues are found.
Output ONLY the JSON array — no markdown fences, no prose.
"""


# ──────────────────────────────────────────
# Public API
# ──────────────────────────────────────────

async def analyze_pr_diff(
    diff: str,
    prior_issues: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Analyze a PR diff and return a list of issue dicts.

    The diff is split into per-file chunks; each is sent to the LLM in
    parallel.  Results from all chunks are merged and deduplicated.

    Args:
        diff:         Raw unified diff string (from GitHub API).
        prior_issues: Issues already found in earlier reviews of this PR.
                      Passed as context so the LLM doesn't re-report them.

    Returns:
        Merged, deduplicated list of issue dicts.

    Raises:
        RuntimeError: if REMOTE_LLM_BASE_URL is not set.
        httpx.HTTPStatusError: on non-2xx responses.
        ValueError: if the model returns malformed JSON that cannot be recovered.
    """
    chunks = _split_diff_by_file(diff)
    if not chunks:
        return []

    prior_context = _format_prior_issues(prior_issues or [])

    tasks = [
        _analyze_chunk(file_path, chunk_diff, prior_context)
        for file_path, chunk_diff in chunks
    ]
    results: list[list[dict[str, Any]]] = await asyncio.gather(*tasks)

    merged = [issue for file_issues in results for issue in file_issues]
    return _deduplicate(merged)


# ──────────────────────────────────────────
# Prior-issue context
# ──────────────────────────────────────────

def _format_prior_issues(prior_issues: list[dict[str, Any]]) -> str:
    """
    Render prior issues as a compact numbered list for inclusion in the prompt.
    Returns an empty string if there are no prior issues.
    """
    if not prior_issues:
        return ""

    lines = [
        "## Already-reported issues from earlier reviews of this PR",
        "Do NOT re-report these — only flag genuinely NEW or different problems:\n",
    ]
    for i, issue in enumerate(prior_issues, 1):
        fp  = issue.get("file_path", "unknown")
        ln  = issue.get("line_number")
        sev = issue.get("severity", "")
        desc = issue.get("description", "")
        loc  = f"{fp}:{ln}" if ln else fp
        lines.append(f"{i}. [{sev}] {loc} — {desc}")

    return "\n".join(lines)


# ──────────────────────────────────────────
# Diff splitting
# ──────────────────────────────────────────

def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """
    Split a unified diff into (file_path, file_diff) tuples.

    Large per-file diffs are further split at hunk boundaries so no chunk
    exceeds LLM_MAX_CHUNK_LINES lines (env-configurable, default 300).
    """
    max_lines = int(os.getenv("LLM_MAX_CHUNK_LINES", "300"))

    chunks: list[tuple[str, str]]  = []
    current_file: str | None       = None
    current_lines: list[str]       = []
    header_lines: list[str]        = []  # diff/---/+++ lines for the current file

    def _flush() -> None:
        if current_file and current_lines:
            _add_chunks(current_file, header_lines + current_lines, max_lines, chunks)

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            _flush()
            current_file  = None
            current_lines = []
            header_lines  = [line]
        elif line.startswith("--- "):
            header_lines.append(line)
        elif line.startswith("+++ b/"):
            current_file = line[6:].rstrip()
            header_lines.append(line)
        else:
            current_lines.append(line)

    _flush()
    return chunks


def _add_chunks(
    file_path: str,
    lines:     list[str],
    max_lines: int,
    out:       list[tuple[str, str]],
) -> None:
    """Append one or more (file_path, diff_text) tuples, splitting at hunk boundaries."""
    if len(lines) <= max_lines:
        out.append((file_path, "".join(lines)))
        return

    hunk_starts = [i for i, l in enumerate(lines) if l.startswith("@@")]
    if not hunk_starts:
        out.append((file_path, "".join(lines)))
        return

    file_header   = [l for l in lines if l.startswith(("diff ", "--- ", "+++ "))]
    chunk_lines   = list(file_header)

    for idx, hunk_start in enumerate(hunk_starts):
        hunk_end = hunk_starts[idx + 1] if idx + 1 < len(hunk_starts) else len(lines)
        hunk     = lines[hunk_start:hunk_end]

        if len(chunk_lines) + len(hunk) > max_lines and len(chunk_lines) > len(file_header):
            out.append((file_path, "".join(chunk_lines)))
            chunk_lines = list(file_header)

        chunk_lines.extend(hunk)

    if len(chunk_lines) > len(file_header):
        out.append((file_path, "".join(chunk_lines)))


# ──────────────────────────────────────────
# LLM call
# ──────────────────────────────────────────

async def _analyze_chunk(
    file_path:     str,
    chunk_diff:    str,
    prior_context: str,
) -> list[dict[str, Any]]:
    """Send one diff chunk to the LLM and return parsed issues."""
    base_url = os.getenv("REMOTE_LLM_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("REMOTE_LLM_BASE_URL environment variable is not set.")

    model   = os.getenv("REMOTE_LLM_MODEL", "qwen2.5-coder:7b")
    timeout = float(os.getenv("REMOTE_LLM_TIMEOUT", "120"))
    api_key = os.getenv("REMOTE_LLM_API_KEY", "")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Build the user message: optionally prepend prior issues as context
    user_content = f"Review this diff for `{file_path}` and return all issues as a JSON array:\n\n{chunk_diff}"
    if prior_context:
        user_content = f"{prior_context}\n\n---\n\n{user_content}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0.1,
    }

    url = f"{base_url}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    return _parse_response(data, file_path)


# ──────────────────────────────────────────
# Response parsing
# ──────────────────────────────────────────

def _parse_response(data: dict, file_path: str) -> list[dict[str, Any]]:
    """Extract and parse the JSON array from an LLM chat completion response."""
    try:
        raw_text: str = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected LLM response format: {str(data)[:300]}") from exc

    raw_text = _strip_markdown_fences(raw_text)
    raw_text = _extract_json_array(raw_text)

    try:
        issues = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Model returned non-JSON for `{file_path}`: {raw_text[:300]}"
        ) from exc

    if not isinstance(issues, list):
        raise ValueError(f"Expected JSON array, got {type(issues).__name__}")

    # Ensure file_path is populated (model may omit it)
    for issue in issues:
        if not issue.get("file_path") or issue["file_path"] in ("unknown", ""):
            issue["file_path"] = file_path

    return issues


def _strip_markdown_fences(text: str) -> str:
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    return text.strip()


def _extract_json_array(text: str) -> str:
    """Extract the first [...] block even if the model added surrounding prose."""
    start = text.find("[")
    end   = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start: end + 1]
    return text


# ──────────────────────────────────────────
# Deduplication
# ──────────────────────────────────────────

def _deduplicate(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    result: list[dict[str, Any]] = []
    for issue in issues:
        key = (
            issue.get("file_path", ""),
            issue.get("line_number"),
            issue.get("severity", ""),
            issue.get("description", "")[:60],
        )
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
