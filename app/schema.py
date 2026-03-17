import enum
from datetime import datetime
from typing import Optional

import strawberry


# ──────────────────────────────────────────
# Enums
# ──────────────────────────────────────────

@strawberry.enum
class ReviewStatus(enum.Enum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    COMPLETE = "COMPLETE"


@strawberry.enum
class Severity(enum.Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


# ──────────────────────────────────────────
# Types
# ──────────────────────────────────────────

@strawberry.type
class RepoType:
    id: int
    url: str
    name: str
    created_at: datetime


@strawberry.type
class IssueType:
    id: int
    review_id: int
    severity: Severity
    file_path: str
    line_number: Optional[int]
    description: str
    suggested_fix: Optional[str]


@strawberry.type
class ReviewType:
    id: int
    repo_id: int
    status: ReviewStatus
    created_at: datetime
    issues: list[IssueType] = strawberry.field(default_factory=list)


# ──────────────────────────────────────────
# Input types
# ──────────────────────────────────────────

@strawberry.input
class SubmitReviewInput:
    repo_url: str
    code: str
