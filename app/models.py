import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ──────────────────────────────────────────
# Enums
# ──────────────────────────────────────────

class ReviewStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    COMPLETE = "COMPLETE"


class SeverityEnum(str, enum.Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


# ──────────────────────────────────────────
# Models
# ──────────────────────────────────────────

class Repo(Base):
    __tablename__ = "repos"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(2048), nullable=False, unique=True, index=True)
    name = Column(String(512), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    reviews = relationship("Review", back_populates="repo", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Repo id={self.id} name={self.name!r}>"


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    repo_id = Column(Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(
        Enum(ReviewStatusEnum, name="review_status_enum"),
        nullable=False,
        default=ReviewStatusEnum.PENDING,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    repo = relationship("Repo", back_populates="reviews")
    issues = relationship("Issue", back_populates="review", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Review id={self.id} status={self.status}>"


class Issue(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True, index=True)
    review_id = Column(Integer, ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False, index=True)
    severity = Column(
        Enum(SeverityEnum, name="severity_enum"),
        nullable=False,
    )
    file_path = Column(String(1024), nullable=False)
    line_number = Column(Integer, nullable=True)
    description = Column(Text, nullable=False)
    suggested_fix = Column(Text, nullable=True)

    review = relationship("Review", back_populates="issues")

    def __repr__(self) -> str:
        return f"<Issue id={self.id} severity={self.severity} file={self.file_path!r}>"
