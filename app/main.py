"""
DevScan — FastAPI entry point.

Routes:
  GET  /                    → redirect to dashboard
  GET  /ui/                 → trigger dashboard (static)
  GET  /health              → liveness check
  POST /webhook/github      → receives GitHub webhook events
  *    /graphql             → Strawberry GraphQL playground + API
"""

import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import strawberry
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.fastapi import GraphQLRouter

from app.database import get_session, init_db
from app.models import PRReview, Repo, WebhookEvent
from app.resolvers.mutations import Mutation
from app.resolvers.queries import Query
from app.resolvers.subscriptions import Subscription
from app.services.cache import close as close_redis
from app.services.pipeline import run_analysis_pipeline


# ──────────────────────────────────────────
# GraphQL schema
# ──────────────────────────────────────────

schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)


async def get_context(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Strawberry context injected into every GraphQL request.
      - db:               async SQLAlchemy session (lifecycle owned by FastAPI Depends)
      - background_tasks: FastAPI BackgroundTasks so mutations can enqueue async work
    """
    return {"db": session, "background_tasks": background_tasks}


# ──────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()
    yield
    await close_redis()


# ──────────────────────────────────────────
# App
# ──────────────────────────────────────────

app = FastAPI(
    title="DevScan",
    description="AI GitHub PR Review Bot — GraphQL API + GitHub webhook receiver",
    version="0.1.0",
    lifespan=lifespan,
)

graphql_app = GraphQLRouter(schema, context_getter=get_context)
app.include_router(graphql_app, prefix="/graphql")

_STATIC = Path(__file__).resolve().parent / "static"
app.mount("/ui", StaticFiles(directory=str(_STATIC), html=True), name="ui")


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify HMAC-SHA256 signature sent by GitHub."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ──────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/", status_code=307)


@app.get("/health", tags=["meta"])
async def health_check():
    return {"status": "ok"}


@app.post("/webhook/github", tags=["webhook"], status_code=status.HTTP_200_OK)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    Receive GitHub webhook events.

    Verifies HMAC-SHA256 signature, persists the raw event, and for
    `pull_request` events with action `opened` or `synchronize` kicks off
    the full analysis pipeline as a FastAPI background task.
    """
    body       = await request.body()
    sig        = request.headers.get("X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event", "unknown")

    # ── Signature verification ──────────────
    webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if webhook_secret and not _verify_github_signature(webhook_secret, body, sig):
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")

    # ── Parse payload ───────────────────────
    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    # ── Persist raw event ───────────────────
    session.add(WebhookEvent(event_type=event_type, payload=payload))

    # ── Trigger review on PR open / update ─
    if event_type == "pull_request" and payload.get("action") in ("opened", "synchronize"):
        pr        = payload.get("pull_request", {})
        repo_info = payload.get("repository", {})
        repo_name = repo_info.get("full_name", "")
        pr_number = pr.get("number")
        pr_title  = pr.get("title")

        if not repo_name or not pr_number:
            return JSONResponse({"received": True, "triggered": False})

        from sqlalchemy import select

        # Upsert Repo
        result = await session.execute(
            select(Repo).where(Repo.github_repo_full_name == repo_name)
        )
        repo = result.scalar_one_or_none()
        if repo is None:
            repo = Repo(github_repo_full_name=repo_name)
            session.add(repo)
            await session.flush()

        # Create PRReview record
        review = PRReview(repo_id=repo.id, pr_number=pr_number, pr_title=pr_title)
        session.add(review)
        await session.flush()
        review_id = review.id

        background_tasks.add_task(
            run_analysis_pipeline, repo_name, pr_number, pr_title, review_id
        )

        return JSONResponse({"received": True, "triggered": True, "review_id": review_id})

    return JSONResponse({"received": True, "triggered": False})
