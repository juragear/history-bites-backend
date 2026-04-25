"""Wikipedia client for HistoryBites.

Two operations, both against English Wikipedia:
  1. list_candidates(category) — list articles in a category via the action API's
     `categorymembers` endpoint (D10). `cmtype=page` filters out sub-categories
     and file entries; we only want article pages.
  2. fetch_extract(title) — fetch the REST API summary for one article. The
     returned `extract` becomes the source material we hand to Gemini later.

Both share a single httpx.AsyncClient (10s timeout, User-Agent from settings)
and are wrapped in tenacity retry that backs off on transient failures (network
errors, 5xx) but fails fast on 4xx (e.g. 404 — the article just doesn't exist).
"""
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings


ACTION_API_URL = "https://en.wikipedia.org/w/api.php"
REST_API_BASE = "https://en.wikipedia.org/api/rest_v1"


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


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0,
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
)
async def _get_json(url: str, params: dict | None = None) -> dict:
    client = _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


async def list_candidates(category: str) -> list[Candidate]:
    """Return article-page members of a Wikipedia category.

    `category` must include the `Category:` prefix and use underscores for
    spaces, e.g. `"Category:History_of_Japan"`.
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
    return [Candidate(page_id=m["pageid"], title=m["title"]) for m in members]


async def fetch_extract(title: str) -> ArticleExtract:
    """Fetch the REST API summary for a single article title.

    Returns the `extract` field (lede paragraphs as plain text) plus the
    canonical desktop URL for attribution.
    """
    url = f"{REST_API_BASE}/page/summary/{quote(title, safe='')}"
    data = await _get_json(url)
    source_url = (
        data.get("content_urls", {}).get("desktop", {}).get("page")
        or f"https://en.wikipedia.org/wiki/{quote(title, safe='')}"
    )
    return ArticleExtract(
        page_id=data["pageid"],
        title=data["title"],
        extract=data.get("extract", ""),
        source_url=source_url,
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
    ("Category:Crusades",                             "Mediterranean",      "medieval"),
    ("Category:Renaissance",                          "Mediterranean",      "early-modern"),
    # Northern Europe
    ("Category:Hanseatic_League",                     "Northern Europe",    "medieval"),
    ("Category:Tsardom_of_Russia",                    "Northern Europe",    "early-modern"),
    # Mesoamerica / South America (pre-Columbian)
    ("Category:Mesoamerican_cultures",                "Mesoamerica",        "pre-Columbian"),
    ("Category:Mississippian_culture",                "North America",      "pre-Columbian"),
    ("Category:Inca_Empire",                          "South America",      "pre-Columbian"),
    # Americas (colonial / modern)
    ("Category:Spanish_colonization_of_the_Americas", "Mesoamerica",        "early-modern"),
    ("Category:Colonial_United_States_(British)",     "North America",      "early-modern"),
    ("Category:American_Civil_War",                   "North America",      "modern"),
    # Oceania
    ("Category:Polynesian_navigation",                "Oceania",            "medieval"),
    ("Category:M\u0101ori_history",                   "Oceania",            "medieval"),
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
