import strawberry
from sqlalchemy import select
from strawberry.types import Info

from app.models import Repo, PRReview, PRReviewStatus
from app.schema import ReviewResult, PRReviewStatus as GQLStatus
from app.services.pipeline import run_analysis_pipeline
from app.services.cache import compute_diff_hash, cache_review


@strawberry.type
class Mutation:

    @strawberry.mutation(
        description=(
            "Manually trigger a PR review for any repo/PR number. "
            "Creates a PRReview with PENDING status, immediately returns the review ID, "
            "then runs the full analysis pipeline (fetch diff → LLM → post comments) "
            "as a background task."
        )
    )
    async def trigger_review(
        self,
        info:           Info,
        repo_full_name: str,
        pr_number:      int,
    ) -> ReviewResult:
        session          = info.context["db"]
        background_tasks = info.context["background_tasks"]

        # Upsert Repo
        result = await session.execute(
            select(Repo).where(Repo.github_repo_full_name == repo_full_name)
        )
        repo = result.scalar_one_or_none()
        if repo is None:
            repo = Repo(github_repo_full_name=repo_full_name)
            session.add(repo)
            await session.flush()

        # Create PRReview in PENDING state
        review = PRReview(
            repo_id=repo.id,
            pr_number=pr_number,
            status=PRReviewStatus.PENDING,
        )
        session.add(review)
        await session.flush()

        # Enqueue the full analysis pipeline — runs after this request completes
        background_tasks.add_task(
            run_analysis_pipeline,
            repo_full_name,
            pr_number,
            None,          # pr_title unknown when triggered manually
            review.id,
        )

        return ReviewResult(
            id=review.id,
            repo_id=review.repo_id,
            pr_number=review.pr_number,
            pr_title=review.pr_title,
            status=GQLStatus(review.status.value),
            created_at=review.created_at,
            issues=[],
        )

    @strawberry.mutation(
        description=(
            "Re-run the analysis pipeline for an existing review, bypassing the Redis "
            "cache so the LLM is always called fresh. Useful when the LLM was offline "
            "during the original run (status=FAILED) or when you want a second opinion. "
            "Resets the review status to PENDING and deletes all previous issues."
        )
    )
    async def force_re_review(
        self,
        info:      Info,
        review_id: strawberry.ID,
    ) -> ReviewResult:
        session          = info.context["db"]
        background_tasks = info.context["background_tasks"]

        rid = int(review_id)
        result = await session.execute(
            select(PRReview).where(PRReview.id == rid)
        )
        review = result.scalar_one_or_none()
        if review is None:
            raise ValueError(f"Review {review_id} not found.")

        # Fetch the repo so we have the full name for the pipeline
        from app.models import Repo, Issue
        repo_result = await session.execute(
            select(Repo).where(Repo.id == review.repo_id)
        )
        repo = repo_result.scalar_one()

        # Delete previous issues so we start clean
        from sqlalchemy import delete as sql_delete
        await session.execute(
            sql_delete(Issue).where(Issue.review_id == rid)
        )

        # Reset to PENDING
        review.status = PRReviewStatus.PENDING
        await session.flush()

        background_tasks.add_task(
            run_analysis_pipeline,
            repo.github_repo_full_name,
            review.pr_number,
            review.pr_title,
            rid,
            True,   # force=True — skip cache
        )

        return ReviewResult(
            id=review.id,
            repo_id=review.repo_id,
            pr_number=review.pr_number,
            pr_title=review.pr_title,
            status=GQLStatus(PRReviewStatus.PENDING.value),
            created_at=review.created_at,
            issues=[],
        )

    @strawberry.mutation(description="Delete a review and all its issues from the database.")
    async def delete_review(
        self,
        info:      Info,
        review_id: strawberry.ID,
    ) -> bool:
        """Returns True if deleted, False if not found."""
        session = info.context["db"]
        rid     = int(review_id)

        result = await session.execute(
            select(PRReview).where(PRReview.id == rid)
        )
        review = result.scalar_one_or_none()
        if review is None:
            return False

        await session.delete(review)   # cascade deletes issues via FK
        return True
