import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ──────────────────────────────────────────
# Enums
# ──────────────────────────────────────────

class PRReviewStatus(str, enum.Enum):
    PENDING   = "PENDING"
    ANALYZING = "ANALYZING"
    POSTED    = "POSTED"
    FAILED    = "FAILED"


class SeverityEnum(str, enum.Enum):
    CRITICAL = "CRITICAL"
    MAJOR    = "MAJOR"
    MINOR    = "MINOR"


# ──────────────────────────────────────────
# Models
# ──────────────────────────────────────────

class Repo(Base):
    __tablename__ = "repos"

    id                   = Column(Integer, primary_key=True, index=True)
    github_repo_full_name = Column(String(512), nullable=False, unique=True, index=True)  # e.g. "owner/repo"
    created_at           = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    reviews = relationship("PRReview", back_populates="repo", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Repo id={self.id} full_name={self.github_repo_full_name!r}>"


class PRReview(Base):
    __tablename__ = "pr_reviews"

    id         = Column(Integer, primary_key=True, index=True)
    repo_id    = Column(Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True)
    pr_number  = Column(Integer, nullable=False, index=True)
    pr_title   = Column(String(1024), nullable=True)
    status     = Column(
        Enum(PRReviewStatus, name="pr_review_status_enum"),
        nullable=False,
        default=PRReviewStatus.PENDING,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    repo   = relationship("Repo", back_populates="reviews")
    issues = relationship("Issue", back_populates="review", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<PRReview id={self.id} pr=#{self.pr_number} status={self.status}>"


class Issue(Base):
    __tablename__ = "issues"

    id            = Column(Integer, primary_key=True, index=True)
    review_id     = Column(Integer, ForeignKey("pr_reviews.id", ondelete="CASCADE"), nullable=False, index=True)
    severity      = Column(Enum(SeverityEnum, name="severity_enum"), nullable=False)
    file_path     = Column(String(1024), nullable=False)
    line_number   = Column(Integer, nullable=True)
    description   = Column(Text, nullable=False)
    suggested_fix = Column(Text, nullable=True)

    review = relationship("PRReview", back_populates="issues")

    def __repr__(self) -> str:
        return f"<Issue id={self.id} severity={self.severity} file={self.file_path!r}>"


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id          = Column(Integer, primary_key=True, index=True)
    event_type  = Column(String(128), nullable=False, index=True)   # e.g. "pull_request"
    payload     = Column(JSON, nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<WebhookEvent id={self.id} type={self.event_type!r}>"
