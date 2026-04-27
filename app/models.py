from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    SmallInteger,
    String,
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
        # D27 (Step 13d): uniqueness widened to include prompt_version so the
        # same Wikipedia article can have one row per prompt version. Required
        # for v1/v2 A/B regeneration on the same external_ids — without
        # prompt_version in the key, the v2 inserts hit the v1 row's unique
        # constraint and fail. Generation pipeline still uses topic-level
        # dedup (D3) via get_used_external_ids; the constraint is the safety
        # net for races, not the dedup mechanism.
        UniqueConstraint(
            "source_name",
            "external_id",
            "prompt_version",
            name="uq_pool_source_external_prompt",
        ),
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected')",
            name="ck_pool_status",
        ),
        # Step 13c (D26): rating is the primary review label, status derives.
        # Nullable so pre-D26 rows (and freshly-generated rows) can exist without
        # a rating; the migration resets pre-D26 rated rows to pending so they
        # come back through the review UI.
        CheckConstraint(
            "review_rating IS NULL OR (review_rating BETWEEN 1 AND 5)",
            name="ck_pool_review_rating_range",
        ),
        # Step 14 (D23): LLM-as-judge fields. judge_score is the predicted
        # rating in [1.0, 5.0]; judge_verdict is the threshold-mapped bucket;
        # judge_reason is a short audit string from the judge call. All
        # nullable: pre-Step-14 rows have NULL across the three; rows
        # generated post-Step-14 always have all three populated (judge
        # failures populate verdict='borderline' with reason explaining why).
        CheckConstraint(
            "judge_score IS NULL OR (judge_score >= 1.0 AND judge_score <= 5.0)",
            name="ck_pool_judge_score_range",
        ),
        CheckConstraint(
            "judge_verdict IS NULL OR judge_verdict IN ('auto_approve', 'auto_reject', 'borderline')",
            name="ck_pool_judge_verdict_values",
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
    # Step 13c (D26): primary review label, 1-5 Likert. status derives from it
    # (>=4 -> approved, <=3 -> rejected). NULL on freshly-generated rows and on
    # pre-D26 rows that have been reset to pending_review for re-rating.
    review_rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Step 14 (D23): judge fields. See CheckConstraints above for ranges.
    judge_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
