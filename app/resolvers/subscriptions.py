"""
GraphQL Subscription resolvers.

Clients connect via WebSocket to /graphql and subscribe to real-time
review status updates. The subscription first emits the current review
state (so the client knows where it stands), then streams each
subsequent status change until POSTED or FAILED is reached.

Example GraphQL subscription:
    subscription {
        reviewStatusUpdated(reviewId: "1") {
            id
            status
            prNumber
            prTitle
            issues {
                severity
                filePath
                description
                suggestedFix
            }
        }
    }
"""

from typing import AsyncGenerator, Optional

import strawberry
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from strawberry.types import Info

from app.database import AsyncSessionLocal
from app.models import PRReview
from app.resolvers.queries import _review_to_type
from app.schema import ReviewResult
from app.services.pubsub import TERMINAL_STATUSES, subscribe_review_status


@strawberry.type
class Subscription:

    @strawberry.subscription(
        description=(
            "Stream real-time status updates for a PR review. "
            "Emits the current state immediately on connect, then one update "
            "per status transition (PENDING → ANALYZING → POSTED | FAILED). "
            "The stream closes automatically once a terminal status is reached."
        )
    )
    async def review_status_updated(
        self,
        info: Info,
        review_id: strawberry.ID,
    ) -> AsyncGenerator[ReviewResult, None]:
        rid = int(review_id)

        # ── Emit current state immediately on connect ──────────────────────
        current = await _fetch_review(rid)
        if current is None:
            return   # review doesn't exist — close stream

        yield current

        if current.status.value in TERMINAL_STATUSES:
            return   # already done — nothing more to stream

        # ── Stream each subsequent status change ───────────────────────────
        async for _ in subscribe_review_status(rid):
            snapshot = await _fetch_review(rid)
            if snapshot is None:
                return
            yield snapshot
            if snapshot.status.value in TERMINAL_STATUSES:
                return


# ── Helper ─────────────────────────────────────────────────────────────────

async def _fetch_review(review_id: int) -> Optional[ReviewResult]:
    """Fetch a PRReview with its issues from the DB and convert to GraphQL type."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PRReview)
            .options(selectinload(PRReview.issues))
            .where(PRReview.id == review_id)
        )
        review = result.scalar_one_or_none()
        return _review_to_type(review) if review else None
