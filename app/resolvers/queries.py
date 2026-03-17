from typing import Optional

import strawberry
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from strawberry.types import Info

from app.models import Issue, Review, ReviewStatusEnum, SeverityEnum
from app.schema import ReviewType, IssueType


@strawberry.type
class Query:

    @strawberry.field(description="List reviews with optional filtering by repo URL or issue severity, with pagination.")
    async def reviews(
        self,
        info: Info,
        repo_url: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ReviewType]:
        session = info.context["db"]

        stmt = (
            select(Review)
            .options(selectinload(Review.issues), selectinload(Review.repo))
            .order_by(Review.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        if repo_url:
            from app.models import Repo
            stmt = stmt.join(Review.repo).where(Repo.url == repo_url)

        if severity:
            try:
                sev_enum = SeverityEnum(severity.upper())
            except ValueError:
                raise ValueError(f"Invalid severity '{severity}'. Must be one of: CRITICAL, MAJOR, MINOR.")
            stmt = stmt.join(Review.issues).where(Issue.severity == sev_enum)

        result = await session.execute(stmt)
        reviews = result.scalars().unique().all()

        return [_review_to_type(r) for r in reviews]

    @strawberry.field(description="Fetch a single review by ID, including all nested issues.")
    async def review(self, info: Info, id: strawberry.ID) -> Optional[ReviewType]:
        session = info.context["db"]

        stmt = (
            select(Review)
            .options(selectinload(Review.issues))
            .where(Review.id == int(id))
        )
        result = await session.execute(stmt)
        review = result.scalar_one_or_none()

        if review is None:
            return None

        return _review_to_type(review)


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _review_to_type(r: Review) -> ReviewType:
    from app.schema import Severity, ReviewStatus

    return ReviewType(
        id=r.id,
        repo_id=r.repo_id,
        status=ReviewStatus(r.status.value),
        created_at=r.created_at,
        issues=[
            IssueType(
                id=issue.id,
                review_id=issue.review_id,
                severity=Severity(issue.severity.value),
                file_path=issue.file_path,
                line_number=issue.line_number,
                description=issue.description,
                suggested_fix=issue.suggested_fix,
            )
            for issue in r.issues
        ],
    )
