import strawberry
from sqlalchemy import select
from strawberry.types import Info

from app.models import Repo, Review, ReviewStatusEnum
from app.schema import ReviewType, ReviewStatus


@strawberry.type
class Mutation:

    @strawberry.mutation(description="Submit a repository for code review. Creates a Review with PENDING status and returns its ID.")
    async def submit_review(
        self,
        info: Info,
        repo_url: str,
        code: str,
    ) -> ReviewType:
        session = info.context["db"]

        # Upsert the Repo record
        result = await session.execute(select(Repo).where(Repo.url == repo_url))
        repo = result.scalar_one_or_none()

        if repo is None:
            # Derive a display name from the URL (last path segment)
            name = repo_url.rstrip("/").split("/")[-1] or repo_url
            repo = Repo(url=repo_url, name=name)
            session.add(repo)
            await session.flush()  # populate repo.id before FK use

        # Create the review in PENDING state
        review = Review(repo_id=repo.id, status=ReviewStatusEnum.PENDING)
        session.add(review)
        await session.flush()

        # TODO (Day 3): dispatch background task to call analyze_code(code)
        #               and update the review status + persist issues.

        return ReviewType(
            id=review.id,
            repo_id=review.repo_id,
            status=ReviewStatus(review.status.value),
            created_at=review.created_at,
            issues=[],
        )
