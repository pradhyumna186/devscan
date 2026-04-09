import enum
from datetime import datetime
from typing import Optional

import strawberry


# ──────────────────────────────────────────
# Enums
# ──────────────────────────────────────────

@strawberry.enum
class PRReviewStatus(enum.Enum):
    PENDING   = "PENDING"
    ANALYZING = "ANALYZING"
    POSTED    = "POSTED"
    FAILED    = "FAILED"


@strawberry.enum
class Severity(enum.Enum):
    CRITICAL = "CRITICAL"
    MAJOR    = "MAJOR"
    MINOR    = "MINOR"


# ──────────────────────────────────────────
# Types
# ──────────────────────────────────────────

@strawberry.type
class IssueType:
    id:            int
    review_id:     int
    severity:      Severity
    file_path:     str
    line_number:   Optional[int]
    description:   str
    suggested_fix: Optional[str]


@strawberry.type
class ReviewResult:
    """A PR review with its nested list of issues."""
    id:         int
    repo_id:    int
    pr_number:  int
    pr_title:   Optional[str]
    status:     PRReviewStatus
    created_at: datetime
    issues:     list[IssueType] = strawberry.field(default_factory=list)


@strawberry.type
class RepoType:
    id:                    int
    github_repo_full_name: str
    created_at:            datetime


# ──────────────────────────────────────────
# Input types
# ──────────────────────────────────────────

@strawberry.input
class TriggerReviewInput:
    repo_full_name: str
    pr_number:      int
