"""
Analysis pipeline — shared by the webhook handler and the triggerReview /
forceReReview mutations.

Flow:
  PENDING → ANALYZING
    → fetch diff
    → load prior issues for this PR (for LLM context deduplication)
    → secret scanner (sync, deterministic)
    → cache check (skipped when force=True)
    → LLM analysis per-file (with prior-issue context)
    → merge & deduplicate all findings
    → persist issues
    → post inline GitHub comments
  → POSTED | FAILED

Pass force=True to skip the Redis cache and always call the LLM.
"""

import os

from app.database import AsyncSessionLocal
from app.models import Issue, PRReview, PRReviewStatus, SeverityEnum
from app.services.cache import cache_review, compute_diff_hash, get_cached_review
from app.services.github import get_pr_diff, post_review_comments
from app.services.llm import analyze_pr_diff
from app.services.pubsub import publish_status_update
from app.services.secret_scanner import scan_diff


async def run_analysis_pipeline(
    repo_full_name: str,
    pr_number:      int,
    pr_title:       str | None,
    review_id:      int,
    force:          bool = False,
) -> None:
    """
    Fetch the PR diff, run analysis, persist issues, post inline GitHub comments.

    Opens its own DB session so it is fully decoupled from the request that
    enqueued it (that session is already committed and closed by the time
    this runs).

    Args:
        force: If True, skip the Redis cache and always call the LLM.
               The fresh result is still written back to the cache afterwards.
    """
    token = os.getenv("GITHUB_APP_TOKEN", "")

    async with AsyncSessionLocal() as session:
        review: PRReview | None = None
        try:
            from sqlalchemy import select

            # ── 1. Mark ANALYZING ──────────────────────────────────────────
            result = await session.execute(
                select(PRReview).where(PRReview.id == review_id)
            )
            review = result.scalar_one()
            review.status = PRReviewStatus.ANALYZING
            if pr_title:
                review.pr_title = pr_title
            await session.commit()
            await publish_status_update(review_id, PRReviewStatus.ANALYZING.value)

            # ── 2. Fetch diff from GitHub ──────────────────────────────────
            diff = await get_pr_diff(repo_full_name, pr_number, token)

            # ── 3. Load prior issues for this PR (for LLM dedup context) ──
            prior_issues = await _load_prior_issues(
                session, repo_full_name, pr_number, exclude_review_id=review_id
            )

            # ── 4. Secret scanner — always runs, never cached ──────────────
            secret_issues = scan_diff(diff)

            # ── 5. Cache check for LLM results (skipped when force=True) ───
            diff_hash  = compute_diff_hash(diff)
            llm_issues = None if force else await get_cached_review(diff_hash)

            if llm_issues is None:
                llm_issues = await analyze_pr_diff(diff, prior_issues=prior_issues)
                await cache_review(diff_hash, llm_issues)

            # ── 6. Merge secret + LLM findings, deduplicate ────────────────
            all_issues = _merge_issues(secret_issues, llm_issues)

            # ── 7. Persist issues ──────────────────────────────────────────
            for raw in all_issues:
                try:
                    sev = SeverityEnum(raw.get("severity", "MINOR").upper())
                except ValueError:
                    sev = SeverityEnum.MINOR
                session.add(Issue(
                    review_id=review_id,
                    severity=sev,
                    file_path=raw.get("file_path", "unknown"),
                    line_number=raw.get("line_number"),
                    description=raw.get("description", ""),
                    suggested_fix=raw.get("suggested_fix"),
                ))

            # ── 8. Post inline comments to GitHub ─────────────────────────
            await post_review_comments(repo_full_name, pr_number, all_issues, token)

            review.status = PRReviewStatus.POSTED
            await session.commit()
            await publish_status_update(review_id, PRReviewStatus.POSTED.value)

        except Exception as exc:
            print(f"[pipeline] ERROR review_id={review_id}: {exc}")
            if review is not None:
                try:
                    review.status = PRReviewStatus.FAILED
                    await session.commit()
                    await publish_status_update(review_id, PRReviewStatus.FAILED.value)
                except Exception:
                    await session.rollback()


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

async def _load_prior_issues(
    session,
    repo_full_name:    str,
    pr_number:         int,
    exclude_review_id: int,
) -> list[dict]:
    """
    Return all issues found in previous POSTED reviews for this repo+PR,
    excluding the current review_id.  Used as context for the LLM so it
    doesn't re-report already-known problems.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import Repo

    # Find the repo row
    repo_result = await session.execute(
        select(Repo).where(Repo.github_repo_full_name == repo_full_name)
    )
    repo = repo_result.scalar_one_or_none()
    if repo is None:
        return []

    # Fetch previous POSTED reviews for this PR
    reviews_result = await session.execute(
        select(PRReview)
        .options(selectinload(PRReview.issues))
        .where(
            PRReview.repo_id   == repo.id,
            PRReview.pr_number == pr_number,
            PRReview.id        != exclude_review_id,
            PRReview.status    == PRReviewStatus.POSTED,
        )
    )
    prior_reviews = reviews_result.scalars().unique().all()

    issues: list[dict] = []
    for review in prior_reviews:
        for issue in review.issues:
            issues.append({
                "severity":    issue.severity.value,
                "file_path":   issue.file_path,
                "line_number": issue.line_number,
                "description": issue.description,
            })
    return issues


def _merge_issues(
    secret_issues: list[dict],
    llm_issues:    list[dict],
) -> list[dict]:
    """
    Combine secret scanner and LLM findings, deduplicating by
    (file_path, line_number).  Secret scanner results always win on conflicts
    since they are deterministic.
    """
    seen: set[tuple] = set()
    merged: list[dict] = []

    def _add(issue: dict) -> None:
        key = (issue.get("file_path", ""), issue.get("line_number"))
        if key not in seen:
            seen.add(key)
            merged.append(issue)

    # Secret scanner issues first — highest priority
    for issue in secret_issues:
        _add(issue)

    for issue in llm_issues:
        _add(issue)

    return merged
