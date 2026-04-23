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


# Starter curation. Full 30-50 list lives in Step 13.
# Format: (wikipedia_category_with_prefix, region, era)
CATEGORIES: list[tuple[str, str, str]] = [
    ("Category:History_of_Japan", "East Asia", "pre-modern"),
    ("Category:History_of_the_Ottoman_Empire", "Middle East", "early modern"),
    ("Category:Pre-Columbian_cultures", "South America", "pre-Columbian"),
    ("Category:History_of_science_in_the_Islamic_world", "Middle East", "medieval"),
    ("Category:History_of_the_Mali_Empire", "West Africa", "medieval"),
    ("Category:History_of_the_Byzantine_Empire", "Mediterranean", "medieval"),
    ("Category:History_of_the_Mongol_Empire", "Central Asia", "medieval"),
    ("Category:Han_dynasty", "East Asia", "ancient"),
]
