"""Tag palette + rating-to-status derivation for the review UI.
Implementation, not architecture.

Step 13a: structured commentary on each approve/reject. The palette below is
hand-curated for v1 review patterns; if a tag turns out to be load-bearing
later (e.g. the judge in D23 keys off it), revisit then. For now: descriptive
labels Will picks at review time so calibration data (Step 13/14) has more
than just a binary verdict to learn from.

Step 13c (D26): rating became the primary review label. `derive_status_from_rating`
is the single source of truth for the rating -> status mapping. Endpoint imports
from here; tests import from here. Don't duplicate the threshold elsewhere.

Tags are kebab-case for storage and URL safety. Display labels (with spaces)
live in templates/review.html, not here — this module is data-only.

Approve/reject categorization on tags is a UI affordance, not a constraint:
the endpoint accepts any combination (e.g. rating=5 + `stylistic-tic`) because
rating is the gate, and tags are commentary on top.
"""
from __future__ import annotations


APPROVE_TAGS: frozenset[str] = frozenset(
    {
        "surprising-angle",  # tells you something new about a familiar topic
        "human-scale",  # has a person, a stake, a moment
        "concrete-detail",  # specific numbers, names, places
        "well-paraphrased",  # clearly Gemini's voice, not extracted from infobox
        "clear-stakes",  # implies why-this-matters without spelling it out
    }
)


REJECT_TAGS: frozenset[str] = frozenset(
    {
        "textbooky",  # reads like an encyclopedia summary
        "dates-and-names-only",  # no insight, just a fact card
        "obvious",  # anyone who's heard of the topic knows this
        "stylistic-tic",  # "incredibly/astonishingly" filler
        "category-mismatch",  # fact is fine but doesn't represent the category
        "infobox-flavored",  # feels copy-pasted from a sidebar
        "boring-even-if-true",  # accurate but not memorable
    }
)


ALL_TAGS: frozenset[str] = APPROVE_TAGS | REJECT_TAGS


class InvalidTagError(ValueError):
    """Raised when validate_tags receives a tag not in ALL_TAGS."""


class InvalidRatingError(ValueError):
    """Raised when a rating is outside 1-5 or not an int."""


def derive_status_from_rating(rating: int) -> str:
    """Map a 1-5 ordinal rating to a pool status string (D26).

    >=4 -> 'approved', <=3 -> 'rejected'. Threshold is 4 deliberately:
    rating=3 means 'borderline / I'm not sure', and treating it as rejected
    is the safer default for a daily-fact app where a published miss costs
    more than an unpublished hit. See D26 for the full rationale.

    Raises InvalidRatingError if rating is not an int in [1, 5]. Caller is
    responsible for catching it and returning a 400; this helper just throws.
    """
    if not isinstance(rating, int) or isinstance(rating, bool):
        raise InvalidRatingError(f"rating must be an int 1-5, got {rating!r}")
    if not 1 <= rating <= 5:
        raise InvalidRatingError(f"rating must be 1-5, got {rating}")
    return "approved" if rating >= 4 else "rejected"


def validate_tags(tags: list[str] | None) -> list[str]:
    """Normalize, dedupe, and validate a list of tags.

    Lowercases and strips each entry. Skips empties. Deduplicates while
    preserving first-seen order. Raises InvalidTagError on the first unknown
    tag (intentional — we want the bad payload to fail loudly, not silently
    drop entries).

    Empty list or None returns []. The endpoint then maps [] to NULL on the
    way to the DB so "no tags" is one canonical state.
    """
    if not tags:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in tags:
        normalized = tag.strip().lower()
        if not normalized:
            continue
        if normalized not in ALL_TAGS:
            raise InvalidTagError(
                f"Unknown tag: {tag!r}. Allowed: {sorted(ALL_TAGS)}"
            )
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return cleaned
