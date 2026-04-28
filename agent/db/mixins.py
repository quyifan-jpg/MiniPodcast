"""
SQLAlchemy ORM mixins for common entity patterns.

Equivalent to CAgent's MyBatis-Plus base entity with auto-fill fields
and soft delete support.

Usage:
    from db.mixins import TimestampMixin, SoftDeleteMixin

    class Article(Base, TimestampMixin, SoftDeleteMixin):
        __tablename__ = "crawled_articles"
        id = mapped_column(Integer, primary_key=True)
        ...

    # Query only non-deleted records:
    stmt = select(Article).where(Article.active())
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    """
    Adds created_at and updated_at fields with automatic population.
    Equivalent to CAgent's BaseEntity createTime / updateTime with @TableField auto-fill.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """
    Adds deleted_at field for logical (soft) deletion.
    Equivalent to CAgent's MyBatis-Plus logical delete on the `deleted` column.

    Records are "deleted" by setting deleted_at to a timestamp,
    not by actually removing the row.

    Usage:
        # Soft delete a record
        article.deleted_at = datetime.now(timezone.utc)
        session.commit()

        # Filter to active (non-deleted) records
        stmt = select(Article).where(Article.active())
    """

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        index=True,
    )

    @classmethod
    def active(cls):
        """
        SQLAlchemy WHERE clause for non-deleted records.
        Usage: select(Article).where(Article.active())
        """
        return cls.deleted_at.is_(None)

    def soft_delete(self) -> None:
        """Mark this record as deleted (does not commit)."""
        self.deleted_at = _utcnow()

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
