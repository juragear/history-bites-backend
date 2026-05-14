"""Wikipedia client for HistoryBites.

Two operations, both against English Wikipedia:
  1. list_candidates(category) — list articles in a category via the action API's
     `categorymembers` endpoint (D10). `cmtype=page` filters out sub-categories
     and file entries; we only want article pages. Step 13e: title regex
     pre-filter rejects List/Timeline/Society-of/Election articles before they
     reach the model — these dominated the v1/v2 boring cohort.
  2. fetch_extract(title) — Step 13e: switched from REST `/page/summary/`
     (~800 char lead only) to the action API `prop=extracts` endpoint (full
     plaintext article). Result is section-aware truncated to ≤15k chars,
     dropping References/See also entirely and prioritizing
     History/Background/Notable/Significance sections. The fuller extract
     gives the model enough material to find the consequential angle rather
     than the most prominent one.

Both share a single httpx.AsyncClient (10s timeout, User-Agent from settings)
and are wrapped in tenacity retry that backs off on transient failures (network
errors, 5xx) but fails fast on 4xx (e.g. 404 — the article just doesn't exist).
"""
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings


logger = logging.getLogger(__name__)


ACTION_API_URL = "https://en.wikipedia.org/w/api.php"


@dataclass(frozen=True)
class Candidate:
    page_id: int
    title: str


@dataclass(frozen=True)
class ArticleExtract:
    page_id: int
    title: str
    extract: str
    source_url: str


class WikipediaError(Exception):
    """Generic Wikipedia client error (non-HTTP).

    Code Review Chunk 3 P3.5 / Chunk 5 P3.1: this parent is currently
    raised once (at fetch_extract's empty-pages guard, see :282) and never
    caught at its own type — `generate_one_pool_fact` catches
    `WikipediaNotFound` (subclass) explicitly and the rest falls through
    to a broad `except Exception`. Kept as a typed parent because:
      1. `WikipediaNotFound(WikipediaError)` is a proper hierarchy; the
         parent serves the documentation role of "non-HTTP Wikipedia
         client error" even without a direct catch site.
      2. Future code that wants to distinguish Wikipedia-specific failures
         from arbitrary `Exception` (e.g., a hypothetical structured
         retry-budget at the wikipedia layer) gets a typed handle for free.
      3. Removing it would force `WikipediaNotFound` to subclass
         `Exception` directly, weakening the typed boundary for a few
         lines saved.
    """


class WikipediaNotFound(WikipediaError):
    """Article missing from Wikipedia (action API returned `missing` flag).

    Distinct from httpx 4xx — the action API returns 200 with a `missing`
    marker for non-existent titles instead of 404. Callers treat this as a
    skip (same semantics as the old 4xx path).
    """

    def __init__(self, title: str) -> None:
        super().__init__(f"Wikipedia article not found: {title!r}")
        self.title = title


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": settings.WIKIPEDIA_USER_AGENT},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _should_retry(exc: BaseException) -> bool:
    # Retry transient failures (network errors, timeouts, 5xx), NOT 4xx.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.HTTPError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1),
    retry=retry_if_exception(_should_retry),
    reraise=True,
    # Code Review Fix 3 (P3.2): without this hook, tenacity retries are
    # silent — operator sees the eventual outcome but no signal that the
    # call hit transient flakiness on the way. INFO level keeps the noise
    # floor low (one line per retry-attempted) while making sustained 5xx
    # visible in Railway logs.
    before_sleep=before_sleep_log(logger, logging.INFO),
)
async def _get_json(url: str, params: dict | None = None) -> dict:
    client = _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


# --- Title pre-filter (Step 13e) -------------------------------------------
#
# Rejects category members whose title shape correlates with low-quality fact
# generation. v1/v2 calibration showed three repeating failure clusters:
#
#   1. List/Timeline articles — "List of strikes in Australia",
#      "Timeline of Maori battles". Reference content, not narrative.
#   2. Meta-organizational articles — "Canadian Society for History and
#      Philosophy of Mathematics", "Journal of X". The article is *about* the
#      society/journal, not about history.
#   3. Election articles — "1924 Victorian state election". Three near-
#      identical Victoria election facts shipped from v1/v2 because the
#      Wikipedia infobox shape produces the same fact every time.
#
# Apply at list_candidates time, BEFORE the model is called — saves the API
# spend and keeps the candidate pool focused on narrative content.
_REJECTED_TITLE_PATTERNS = re.compile(
    # Reference/list articles
    r"^(List of |Lists of |Timeline of |Outline of |Index of |Glossary of )"
    r"|"
    # Meta-history articles about terms/concepts/definitions
    r"^(History of (the term|the concept|the word|the phrase)|Definition of |Etymology of )"
    r"|"
    # Organizational meta-history (Society for the Study of X, Institute for
    # the Promotion of Y, etc.). The shape captures most academic-society
    # patterns without false-positive on real subject articles.
    r"\b(Society|Association|Institute|Foundation|Academy|Council|Committee|Federation|Union) "
    r"(for|of) (the )?(Study|History|Philosophy|Research|Promotion|Advancement|Development|Preservation) "
    r"(of |for )?\b"
    r"|"
    # Academic journal/publication wrappers
    r"^(Journal of |Bulletin of |Proceedings of )"
    r"|"
    # Election articles (extremely repetitive — produced 3 near-identical
    # Victoria election facts and 3 near-identical Roman consul facts)
    r"\b\d{4} .* (election|by-election|referendum)\b",
    re.IGNORECASE,
)


def _is_rejected_title(title: str) -> bool:
    return bool(_REJECTED_TITLE_PATTERNS.search(title))


# --- Section-aware truncation (Step 13e) -----------------------------------
#
# Action API extracts return plain text with section headers like
#   "== History ==\n..."
# Split on those, drop References/See also, prioritize narrative sections.
_SECTION_BOUNDARY = re.compile(r"\n(?=={2,}\s)")
_PRIORITY_SECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"^={2,}\s*(History|Background|Origins?|Founding|Etymology)\s*={2,}",
        re.I | re.M,
    ),
    re.compile(
        r"^={2,}\s*(Notable|Significance|Legacy|Aftermath|Impact)\s*={2,}",
        re.I | re.M,
    ),
    re.compile(
        r"^={2,}\s*(Description|Overview|Account|Narrative)\s*={2,}",
        re.I | re.M,
    ),
]
_REJECTED_SECTION_PATTERNS = re.compile(
    r"^={2,}\s*(See also|References|External links|Bibliography|Notes|Citations|Further reading|Sources)\s*={2,}",
    re.I | re.M,
)
_MAX_EXTRACT_CHARS = 15_000
_MAX_SECTIONS = 8


def _section_score(s: str) -> int:
    for i, pattern in enumerate(_PRIORITY_SECTION_PATTERNS):
        if pattern.match(s):
            return i
    # Everything else (no priority match) sorts to lowest priority but still
    # makes it into the candidate set if there's room.
    return len(_PRIORITY_SECTION_PATTERNS)


def _select_sections(raw_extract: str) -> str:
    """Pure helper: split action-API plaintext into sections, drop reference
    sections, sort by narrative priority, return ≤_MAX_EXTRACT_CHARS string.

    Lead (sections[0], no header) is always included first. Body sections are
    filtered, sorted by priority, capped at _MAX_SECTIONS - 1, then joined.
    """
    sections = _SECTION_BOUNDARY.split(raw_extract)
    if not sections:
        return ""
    lead, body = sections[0], sections[1:]
    body = [s for s in body if not _REJECTED_SECTION_PATTERNS.match(s)]
    body.sort(key=_section_score)
    selected = [lead] + body[: _MAX_SECTIONS - 1]
    truncated = "\n\n".join(selected)
    if len(truncated) > _MAX_EXTRACT_CHARS:
        truncated = truncated[:_MAX_EXTRACT_CHARS]
    return truncated


async def list_candidates(category: str) -> list[Candidate]:
    """Return article-page members of a Wikipedia category.

    `category` must include the `Category:` prefix and use underscores for
    spaces, e.g. `"Category:History_of_Japan"`. Step 13e: titles matching
    `_REJECTED_TITLE_PATTERNS` (List/Timeline/Society-of/Election/...) are
    filtered out here so they never reach the model.
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category,
        "cmlimit": 50,
        "cmtype": "page",
        "format": "json",
    }
    data = await _get_json(ACTION_API_URL, params=params)
    members = data.get("query", {}).get("categorymembers", [])
    return [
        Candidate(page_id=m["pageid"], title=m["title"])
        for m in members
        if not _is_rejected_title(m["title"])
    ]


async def fetch_extract(title: str) -> ArticleExtract:
    """Fetch full article extract via action API, section-aware truncated.

    Step 13e: switched from REST `/page/summary/` (lead paragraphs only,
    ~800 chars) to action API `prop=extracts&explaintext=1` (full article
    plain text). The fuller body lets the model find the consequential angle
    rather than the most prominent fact in the lead.

    Truncation strategy (`_select_sections`):
      1. Always include the lead (sections[0]).
      2. Drop See also / References / External links / Bibliography / Notes
         / Citations / Further reading / Sources entirely.
      3. Sort remaining sections by narrative priority:
         History/Background/Origins/Founding/Etymology > Notable/Significance/
         Legacy/Aftermath/Impact > Description/Overview/Account/Narrative >
         everything else.
      4. Cap at 8 sections OR 15k chars, whichever hits first.

    Raises:
      WikipediaNotFound — action API returned the `missing` marker.
      httpx.HTTPStatusError — non-2xx HTTP response (5xx retried by tenacity,
        4xx propagated for the caller's existing skip path).
    """
    params = {
        "action": "query",
        "prop": "extracts",
        "exintro": 0,
        "explaintext": 1,
        "redirects": 1,
        "format": "json",
        "titles": title,
    }
    data = await _get_json(ACTION_API_URL, params=params)
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise WikipediaError(f"action API returned no pages for title={title!r}")
    page = next(iter(pages.values()))
    if "missing" in page:
        raise WikipediaNotFound(title)
    raw_extract = page.get("extract", "") or ""
    truncated = _select_sections(raw_extract)
    canonical_title = page.get("title", title)
    return ArticleExtract(
        page_id=page["pageid"],
        title=canonical_title,
        extract=truncated,
        source_url=f"https://en.wikipedia.org/wiki/{quote(canonical_title.replace(' ', '_'), safe='_/()')}",
    )


# Curated category set for generation (Step 13b expansion of Step 3 starter).
#
# Format: (wikipedia_category_with_prefix, region, era).
#   - region/era values are tags, used downstream by the variety scorer
#     (D4 cultural diversity, D21b region/era avoidance in scheduling).
#   - Each tuple was verified at curation time to have 20+ article-type
#     members via wikipedia.list_candidates(). Categories that 404 or fall
#     under the threshold get rejected — guessing is unreliable, the API
#     check is cheap.
#   - The grouping comments (# East Asia, etc.) are scan aids only; only
#     the tuple values are load-bearing.
#
# Adding a new entry: probe with list_candidates() first. Aim for under-
# represented (region, era) cells before adding more entries to a cell that
# already has coverage. Don't recurse into subcategories — cmtype=page in
# list_candidates already filters those out.
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    # East Asia
    ("Category:Han_dynasty",                          "East Asia",          "ancient"),
    ("Category:Tang_dynasty",                         "East Asia",          "medieval"),
    ("Category:Heian_period",                         "East Asia",          "medieval"),
    # 2026-05-14 add: named-individual-rich (Minamoto no Yoshinaka, etc.)
    ("Category:Shōguns",                              "East Asia",          "medieval"),
    ("Category:Ming_dynasty",                         "East Asia",          "early-modern"),
    ("Category:Sengoku_period",                       "East Asia",          "early-modern"),
    ("Category:Meiji_era",                            "East Asia",          "modern"),
    # South Asia
    ("Category:Maurya_Empire",                        "South Asia",         "ancient"),
    ("Category:Chola_dynasty",                        "South Asia",         "medieval"),
    ("Category:Mughal_Empire",                        "South Asia",         "early-modern"),
    ("Category:Indian_independence_movement",         "South Asia",         "modern"),
    # Middle East
    ("Category:Achaemenid_Empire",                    "Middle East",        "ancient"),
    ("Category:Sasanian_Empire",                      "Middle East",        "classical"),
    ("Category:Islamic_Golden_Age",                   "Middle East",        "medieval"),
    ("Category:Ottoman_Empire",                       "Middle East",        "early-modern"),
    # North Africa
    ("Category:Ancient_Egypt",                        "North Africa",       "ancient"),
    # 2026-05-14 add: pharaohs are named individuals (Tutankhamun, Cleopatra, etc.)
    ("Category:Pharaohs",                             "North Africa",       "ancient"),
    ("Category:Carthage",                             "North Africa",       "ancient"),
    ("Category:Ptolemaic_Kingdom",                    "North Africa",       "classical"),
    # Sub-Saharan Africa
    ("Category:Kingdom_of_Kush",                      "Sub-Saharan Africa", "ancient"),
    ("Category:Mali_Empire",                          "Sub-Saharan Africa", "medieval"),
    ("Category:Kingdom_of_Kongo",                     "Sub-Saharan Africa", "early-modern"),
    # Mediterranean
    ("Category:Ancient_Greece",                       "Mediterranean",      "ancient"),
    ("Category:Roman_Republic",                       "Mediterranean",      "ancient"),
    ("Category:Roman_Empire",                         "Mediterranean",      "classical"),
    # 2026-05-14 add: named-individual-rich (Augustus, Trajan, Marcus Aurelius, etc.)
    ("Category:Roman_emperors",                       "Mediterranean",      "classical"),
    ("Category:Crusades",                             "Mediterranean",      "medieval"),
    # 2026-05-14 add: 200+ named individual popes across the medieval era
    ("Category:Popes",                                "Mediterranean",      "medieval"),
    ("Category:Renaissance",                          "Mediterranean",      "early-modern"),
    # 2026-05-14 add: named-individual-rich (Erasmus, More, Petrarch, Pico della Mirandola, ...)
    ("Category:Renaissance_humanists",                "Mediterranean",      "early-modern"),
    # 2026-05-14 add: 200+ named individual painters (Botticelli, da Vinci, Michelangelo, ...)
    ("Category:Italian_Renaissance_painters",         "Mediterranean",      "early-modern"),
    # Northern Europe
    # 2026-05-14 add: named-individual-rich (Charlemagne, Pepin the Short, Louis the Pious, ...)
    ("Category:Carolingian_dynasty",                  "Northern Europe",    "medieval"),
    ("Category:Hanseatic_League",                     "Northern Europe",    "medieval"),
    # 2026-05-14 add: Tudor monarchs + named courtiers (Henry VIII, Elizabeth I, Cromwell, ...)
    ("Category:Tudor_England",                        "Northern Europe",    "early-modern"),
    ("Category:Tsardom_of_Russia",                    "Northern Europe",    "early-modern"),
    # Mesoamerica / South America (pre-Columbian)
    ("Category:Mesoamerican_cultures",                "Mesoamerica",        "pre-Columbian"),
    ("Category:Mississippian_culture",                "North America",      "pre-Columbian"),
    ("Category:Inca_Empire",                          "South America",      "pre-Columbian"),
    # Americas (colonial / modern)
    ("Category:Spanish_colonization_of_the_Americas", "Mesoamerica",        "early-modern"),
    # 2026-05-14 add: named-individual-rich (Cortés, Pizarro, de Soto, Coronado, ...)
    ("Category:Conquistadors",                        "Mesoamerica",        "early-modern"),
    ("Category:Colonial_United_States_(British)",     "North America",      "early-modern"),
    # 2026-05-14 add: 144 named individuals (Washington, Jefferson, Franklin, Hamilton, ...)
    ("Category:Founding_Fathers_of_the_United_States", "North America",     "modern"),
    ("Category:American_Civil_War",                   "North America",      "modern"),
    # Oceania
    ("Category:Polynesian_navigation",                "Oceania",            "medieval"),
    ("Category:Māori_history",                   "Oceania",            "medieval"),
    ("Category:History_of_Australia",                 "Oceania",            "modern"),
    # Central Asia
    ("Category:Xiongnu",                              "Central Asia",       "ancient"),
    ("Category:Mongol_Empire",                        "Central Asia",       "medieval"),
    ("Category:Silk_Road",                            "Central Asia",       "medieval"),
    # Cross-regional themes (per Step 13b: lean toward "surprising-angle"
    # material that's not just battles and dynasties)
    ("Category:Ancient_astronomy",                    "Cross-regional",     "ancient"),
    ("Category:History_of_cartography",               "Cross-regional",     "early-modern"),
    ("Category:History_of_navigation",                "Cross-regional",     "early-modern"),
    ("Category:History_of_mathematics",               "Cross-regional",     "modern"),
    ("Category:History_of_medicine",                  "Cross-regional",     "modern"),
    ("Category:History_of_science",                   "Cross-regional",     "modern"),
)
