from typing import Optional

import strawberry
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from strawberry.types import Info

from app.models import Issue, PRReview, PRReviewStatus, SeverityEnum, Repo
from app.schema import ReviewResult, IssueType, Severity, PRReviewStatus as GQLStatus


@strawberry.type
class Query:

    @strawberry.field(description="List PR reviews with optional filtering by repo name or status, with pagination.")
    async def pr_reviews(
        self,
        info: Info,
        repo_name:  Optional[str] = None,
        status:     Optional[str] = None,
        limit:      int = 20,
        offset:     int = 0,
    ) -> list[ReviewResult]:
        session = info.context["db"]

        # Note: selectinload(PRReview.repo) intentionally omitted — `repo` is not
        # part of ReviewResult and would conflict with the explicit join below.
        stmt = (
            select(PRReview)
            .options(selectinload(PRReview.issues))
            .order_by(PRReview.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        if repo_name:
            # Plain join for WHERE filter; no eager-load needed since repo_id is on PRReview
            stmt = stmt.join(PRReview.repo).where(Repo.github_repo_full_name == repo_name)

        if status:
            try:
                status_enum = PRReviewStatus(status.upper())
            except ValueError:
                raise ValueError(f"Invalid status '{status}'. Must be one of: PENDING, ANALYZING, POSTED, FAILED.")
            stmt = stmt.where(PRReview.status == status_enum)

        result  = await session.execute(stmt)
        reviews = result.scalars().unique().all()
        return [_review_to_type(r) for r in reviews]

    @strawberry.field(description="Fetch a single PR review by ID with all nested issues.")
    async def pr_review(self, info: Info, id: strawberry.ID) -> Optional[ReviewResult]:
        session = info.context["db"]

        stmt = (
            select(PRReview)
            .options(selectinload(PRReview.issues))
            .where(PRReview.id == int(id))
        )
        result = await session.execute(stmt)
        review = result.scalar_one_or_none()
        return _review_to_type(review) if review else None

    @strawberry.field(description="Get all issues filtered by severity, optionally scoped to a repo.")
    async def issues_by_severity(
        self,
        info:      Info,
        severity:  str,
        repo_name: Optional[str] = None,
    ) -> list[IssueType]:
        session = info.context["db"]

        try:
            sev_enum = SeverityEnum(severity.upper())
        except ValueError:
            raise ValueError(f"Invalid severity '{severity}'. Must be one of: CRITICAL, MAJOR, MINOR.")

        stmt = (
            select(Issue)
            .where(Issue.severity == sev_enum)
            .order_by(Issue.id.desc())
        )

        if repo_name:
            stmt = (
                stmt
                .join(Issue.review)
                .join(PRReview.repo)
                .where(Repo.github_repo_full_name == repo_name)
            )

        result = await session.execute(stmt)
        issues = result.scalars().all()
        return [_issue_to_type(i) for i in issues]


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _issue_to_type(i: Issue) -> IssueType:
    return IssueType(
        id=i.id,
        review_id=i.review_id,
        severity=Severity(i.severity.value),
        file_path=i.file_path,
        line_number=i.line_number,
        description=i.description,
        suggested_fix=i.suggested_fix,
    )


def _review_to_type(r: PRReview) -> ReviewResult:
    return ReviewResult(
        id=r.id,
        repo_id=r.repo_id,
        pr_number=r.pr_number,
        pr_title=r.pr_title,
        status=GQLStatus(r.status.value),
        created_at=r.created_at,
        issues=[_issue_to_type(i) for i in r.issues],
    )
