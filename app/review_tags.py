"""Tag palette for the review UI. Implementation, not architecture.

Step 13a: structured commentary on each approve/reject. The palette below is
hand-curated for v1 review patterns; if a tag turns out to be load-bearing
later (e.g. the judge in D23 keys off it), revisit then. For now: descriptive
labels Will picks at review time so calibration data (Step 13/14) has more
than just a binary verdict to learn from.

Tags are kebab-case for storage and URL safety. Display labels (with spaces)
live in templates/review.html, not here — this module is data-only.

Approve/reject categorization is a UI affordance, not a constraint: the
endpoint accepts any combination (e.g. approve + `stylistic-tic`) because the
`action` field is the gate, and tags are commentary on top.
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
