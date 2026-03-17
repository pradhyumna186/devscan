"""
LLM service — wraps the Anthropic API to perform static code analysis.

Returns a list of issue dicts with the schema:
    {
        "severity":      "CRITICAL" | "MAJOR" | "MINOR",
        "file_path":     str,
        "line_number":   int | None,
        "description":   str,
        "suggested_fix": str | None,
    }
"""

import json
import os
from typing import Any

import anthropic

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """\
You are an expert code reviewer. When given source code, you identify bugs, security vulnerabilities, \
performance problems, and style issues.

Respond ONLY with a valid JSON array. Each element must follow this exact schema:
{
  "severity":      "CRITICAL" | "MAJOR" | "MINOR",
  "file_path":     "<filename or 'unknown' if not determinable>",
  "line_number":   <integer or null>,
  "description":   "<clear, concise description of the issue>",
  "suggested_fix": "<actionable fix suggestion or null>"
}

Return an empty array [] if no issues are found. Do not include markdown, explanations, or any text outside the JSON array.
"""


async def analyze_code(code: str) -> list[dict[str, Any]]:
    """
    Send `code` to Claude for analysis and return a list of issue dicts.

    Raises:
        anthropic.APIError: on API-level failures.
        ValueError: if the model returns malformed JSON.
    """
    client = _get_client()

    message = await client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Please review the following code:\n\n```\n{code}\n```",
            }
        ],
    )

    raw_text = message.content[0].text.strip()

    # Strip accidental markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
        raw_text = raw_text.rsplit("```", 1)[0].strip()

    try:
        issues: list[dict] = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned non-JSON response: {raw_text[:200]}") from exc

    if not isinstance(issues, list):
        raise ValueError(f"Expected a JSON array, got: {type(issues).__name__}")

    return issues
