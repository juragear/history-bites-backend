from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint("source_name", "external_id", name="uq_facts_source_external"),
        Index("idx_facts_scheduled_date", "scheduled_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    fact_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_license: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    era: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_retracted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set when run_push (Step 9) successfully delivers this fact to FCM.
    # Nullable because past facts (pre-Step 9) never had a push and shouldn't
    # get a synthesized timestamp. Most-recent-wins on retry — see cron.run_push.
    pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PoolFact(Base):
    __tablename__ = "pool"
    __table_args__ = (
        UniqueConstraint("source_name", "external_id", name="uq_pool_source_external"),
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected')",
            name="ck_pool_status",
        ),
        Index("idx_pool_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fact_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_license: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    era: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending_review"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Step 13a: tagged review UI. Both nullable for backward compat with rows
    # reviewed before Step 13a. JSON (not ARRAY) so SQLite tests round-trip
    # without a @compiles hook — Postgres uses JSONB, SQLite serializes to TEXT,
    # SQLAlchemy hides the difference. Empty cleaned tag list normalizes to
    # NULL in the endpoint so "no tags" is one canonical state, not two.
    review_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
