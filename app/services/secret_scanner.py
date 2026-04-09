"""
Static secret / credential scanner.

Runs before the LLM to deterministically catch secrets and sensitive files
committed to source control.  Results use the same dict schema as the LLM
service so they can be merged transparently.

Detections:
  - .env and other sensitive file additions
  - API keys, secret keys, tokens, passwords (generic patterns)
  - AWS access / secret keys
  - GitHub PATs (classic + fine-grained)
  - PEM / private key headers
  - Database connection strings with embedded credentials
"""

import os
import re
from typing import Any


# ──────────────────────────────────────────
# Pattern registry
# ──────────────────────────────────────────

# Each tuple: (compiled regex, human label, severity)
# Patterns are tested against individual *added* lines (the '+' lines) of the diff.
_LINE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # ── Generic key/secret/token assignments ─────────────────────────────
    (
        re.compile(r'(?i)(api[_\-]?key|apikey)\s*[=:]\s*["\']?([A-Za-z0-9_\-]{16,})["\']?'),
        "API Key", "CRITICAL",
    ),
    (
        re.compile(r'(?i)(secret[_\-]?key|secret)\s*[=:]\s*["\']?([A-Za-z0-9_\-]{16,})["\']?'),
        "Secret Key", "CRITICAL",
    ),
    (
        re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']?([^\s"\']{8,})["\']?'),
        "Password", "CRITICAL",
    ),
    (
        re.compile(
            r'(?i)(token|auth[_\-]?token|access[_\-]?token|bearer[_\-]?token)'
            r'\s*[=:]\s*["\']?([A-Za-z0-9_\-\.]{20,})["\']?'
        ),
        "Auth Token", "CRITICAL",
    ),
    # ── AWS ───────────────────────────────────────────────────────────────
    (
        re.compile(r'AKIA[0-9A-Z]{16}'),
        "AWS Access Key ID", "CRITICAL",
    ),
    (
        re.compile(
            r'(?i)aws[_\-]?secret[_\-]?access[_\-]?key\s*[=:]\s*["\']?([A-Za-z0-9/+=]{40})["\']?'
        ),
        "AWS Secret Access Key", "CRITICAL",
    ),
    # ── GitHub tokens ────────────────────────────────────────────────────
    (
        re.compile(r'ghp_[A-Za-z0-9]{36}'),
        "GitHub Classic PAT", "CRITICAL",
    ),
    (
        re.compile(r'github_pat_[A-Za-z0-9_]{82}'),
        "GitHub Fine-grained PAT", "CRITICAL",
    ),
    (
        re.compile(r'ghs_[A-Za-z0-9]{36}'),
        "GitHub App Token", "CRITICAL",
    ),
    # ── Private keys ─────────────────────────────────────────────────────
    (
        re.compile(r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'),
        "Private Key", "CRITICAL",
    ),
    # ── Connection strings with embedded credentials ──────────────────────
    (
        re.compile(
            r'(?i)(mongodb(\+srv)?|postgres(ql)?|mysql|redis|amqp)://'
            r'[^\s"\']+:[^\s"\'@]+@'
        ),
        "Database connection string with embedded credentials", "CRITICAL",
    ),
    # ── Generic high-entropy strings assigned to obvious secret vars ──────
    (
        re.compile(
            r'(?i)(private[_\-]?key|encryption[_\-]?key|signing[_\-]?key|jwt[_\-]?secret)'
            r'\s*[=:]\s*["\']?([A-Za-z0-9+/=_\-]{20,})["\']?'
        ),
        "Cryptographic Key/Secret", "CRITICAL",
    ),
]

# File extensions that should almost never be committed
_SENSITIVE_EXTENSIONS: frozenset[str] = frozenset({
    ".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".cer", ".der",
})

# Filename patterns that should almost never be committed
_SENSITIVE_FILENAMES: frozenset[str] = frozenset({
    ".env", ".env.local", ".env.production", ".env.staging", ".env.development",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json",
})


# ──────────────────────────────────────────
# Public API
# ──────────────────────────────────────────

def scan_diff(diff: str) -> list[dict[str, Any]]:
    """
    Scan a unified diff string for secrets and sensitive file additions.

    Args:
        diff: Raw unified diff (as returned by the GitHub API).

    Returns:
        List of issue dicts with keys:
            severity, file_path, line_number, description, suggested_fix
        Results are deduplicated by (file_path, line_number).
    """
    issues: list[dict[str, Any]] = []
    current_file = "unknown"
    current_new_line = 0

    for raw_line in diff.splitlines():
        # ── Track current file ────────────────────────────────────────────
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:].strip()
            current_new_line = 0
            _check_sensitive_file(current_file, issues)
            continue

        # ── Update line counter from hunk header ──────────────────────────
        if raw_line.startswith("@@"):
            m = re.search(r'\+(\d+)', raw_line)
            if m:
                current_new_line = int(m.group(1)) - 1
            continue

        # ── Process added lines ───────────────────────────────────────────
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_new_line += 1
            line_content = raw_line[1:]  # strip leading '+'
            _check_line(current_file, current_new_line, line_content, issues)
        elif not raw_line.startswith("-"):
            current_new_line += 1

    return _deduplicate(issues)


# ──────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────

def _check_sensitive_file(file_path: str, issues: list[dict[str, Any]]) -> None:
    """Flag additions of inherently sensitive files."""
    basename  = os.path.basename(file_path)
    _, ext    = os.path.splitext(file_path)

    if basename in _SENSITIVE_FILENAMES or ext in _SENSITIVE_EXTENSIONS:
        issues.append({
            "severity":      "CRITICAL",
            "file_path":     file_path,
            "line_number":   None,
            "description":   (
                f"Sensitive file `{file_path}` is being added to the repository. "
                "This file likely contains secrets or credentials that must never be "
                "committed to version control."
            ),
            "suggested_fix": (
                f"Remove `{file_path}` from this commit, add it to `.gitignore`, "
                "and immediately rotate any exposed credentials."
            ),
        })


def _check_line(
    file_path: str,
    line_number: int,
    line_content: str,
    issues: list[dict[str, Any]],
) -> None:
    """Test a single added line against all secret patterns."""
    for pattern, label, severity in _LINE_PATTERNS:
        if pattern.search(line_content):
            issues.append({
                "severity":      severity,
                "file_path":     file_path,
                "line_number":   line_number,
                "description":   (
                    f"Possible {label} exposed in source code: "
                    f"`{line_content.strip()[:120]}`"
                ),
                "suggested_fix": (
                    f"Remove the hardcoded {label.lower()}, store it in an "
                    "environment variable or secrets manager, and rotate the "
                    "exposed credential immediately."
                ),
            })
            return  # one finding per line is sufficient


def _deduplicate(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    result: list[dict[str, Any]] = []
    for issue in issues:
        key = (issue["file_path"], issue.get("line_number"))
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
