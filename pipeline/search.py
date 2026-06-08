"""
pipeline/search.py — Keyword search runner against DGGVods /search API.

For each query in the arc's query_groups (+ reaction_terms), hits the
DGGVods search endpoint, paginates results, and stores Hits in the DB.

Checkpoints after every completed query so a crashed run can resume.
Checkpoint file: data/cache/search_checkpoint.json

Search API:
  GET https://dggvods.dev/api/search?q=TERM&page=N&limit=50
  Returns: { "total": N, "vods": [ { vod_id, video_id, title, date, segments: [{start_time, end_time, snippet}] } ] }

AND logic: all words in a multi-word query must co-occur in same sentence.
Whole-word tokenized: "schizo" does NOT catch "schizoposting".
No special chars (hyphens break search) — already handled by query lists.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from core.config import ArcConfig, all_queries
from core.db import DB
from core.models import Hit, Stream
from core.utils import log_step

log = logging.getLogger(__name__)

BASE_URL = "https://dggvods.dev/api"
PAGE_LIMIT = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://dggvods.dev/",
    "Accept": "application/json",
}

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
CHECKPOINT_FILE = CACHE_DIR / "search_checkpoint.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_search(
    cfg: ArcConfig,
    db: DB,
    stream_ids: Optional[set[int]] = None,
    resume: bool = True,
    dry_run: bool = False,
    cookie: Optional[str] = None,
) -> dict:
    """
    Run all queries for an arc against the DGGVods search API.

    Args:
        cfg:        Arc config (provides query list + date range)
        db:         Open DB to write hits into
        stream_ids: If provided, only store hits for VODs in this set.
                    Pass None to store all hits regardless of date range filtering.
        resume:     If True, load checkpoint and skip already-completed queries.
        dry_run:    If True, log what would be done but don't write to DB.

    Returns:
        dict with stats: queries_total, queries_done, hits_found, hits_stored
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    queries = all_queries(cfg)
    log_step("search", "Starting search run",
             arc=cfg.arc_name, total_queries=len(queries))

    # Load checkpoint
    completed = _load_checkpoint() if resume else set()
    if completed:
        log_step("search", "Resuming from checkpoint", already_done=len(completed))

    session = _session(cookie)
    stats = {
        "queries_total": len(queries),
        "queries_done": len(completed),
        "queries_skipped": 0,
        "hits_found": 0,
        "hits_stored": 0,
    }

    for i, (group, query) in enumerate(queries):
        checkpoint_key = f"{cfg.arc_name}::{query}"

        if checkpoint_key in completed:
            log.debug(f"[search] Skip (already done): {query!r}")
            stats["queries_skipped"] += 1
            continue

        log_step("search", f"Query {i+1}/{len(queries)}",
                 group=group, query=repr(query))

        hits = _search_query(
            session=session,
            query=query,
            arc_name=cfg.arc_name,
            date_start=cfg.date_range.start,
            date_end=cfg.date_range.end,
            stream_ids=stream_ids,
        )

        stats["hits_found"] += len(hits)

        if hits and not dry_run:
            db.insert_hits(hits)
            stats["hits_stored"] += len(hits)
            log_step("search", "Stored hits",
                     query=repr(query), hits=len(hits))
        elif hits:
            log_step("search", "DRY RUN — would store",
                     query=repr(query), hits=len(hits))

        # Checkpoint after each completed query
        completed.add(checkpoint_key)
        _save_checkpoint(completed)
        stats["queries_done"] += 1

    log_step("search", "Search run complete",
             **{k: v for k, v in stats.items()})
    return stats


def clear_checkpoint():
    """Delete the search checkpoint (forces full re-run next time)."""
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        log.info("[search] Checkpoint cleared")


# ---------------------------------------------------------------------------
# Search execution
# ---------------------------------------------------------------------------

def _search_query(
    session: requests.Session,
    query: str,
    arc_name: str,
    date_start: str,
    date_end: str,
    stream_ids: Optional[set[int]],
) -> list[Hit]:
    """
    Run one query against /search, paginate all results,
    filter to arc date range and known stream_ids, return Hit list.
    """
    hits = []
    page = 1

    while True:
        url = f"{BASE_URL}/search"
        params = {"q": query, "page": page, "limit": PAGE_LIMIT}
        log.debug(f"[search] GET /search q={query!r} page={page}")

        try:
            resp = _get(session, url, params)
        except requests.HTTPError as e:
            log.warning(f"[search] HTTP error for query {query!r}: {e}")
            break
        except requests.RequestException as e:
            log.warning(f"[search] Request error for query {query!r}: {e}")
            break

        data = resp.json()
        vods = data.get("vods") or []
        total = data.get("total", 0)

        if not vods:
            break

        for vod in vods:
            vod_id = vod.get("vod_id") or vod.get("id")
            if vod_id is None:
                continue
            vod_id = int(vod_id)

            # Date filter: DGGVods search doesn't filter by date natively
            vod_date = str(vod.get("date", "")).replace("-", "")
            if vod_date and (vod_date < date_start or vod_date > date_end):
                log.debug(f"[search] Skip vod_id={vod_id} date={vod_date} out of range")
                continue

            # Stream ID filter (if we only care about known streams)
            if stream_ids is not None and vod_id not in stream_ids:
                log.debug(f"[search] Skip vod_id={vod_id} not in stream_ids")
                continue

            segments = vod.get("segments", [])
            for seg in segments:
                start = int(seg.get("start_time", 0))
                end = int(seg.get("end_time", start + 5))
                snippet = seg.get("snippet", "").strip()
                hits.append(Hit(
                    dgg_vod_id=vod_id,
                    arc_name=arc_name,
                    query=query,
                    start_time=start,
                    end_time=end,
                    snippet=snippet,
                ))

        log.debug(
            f"[search] q={query!r} page={page} "
            f"vods={len(vods)} cumulative_hits={len(hits)} total_reported={total}"
        )

        # DGGVods may not always have reliable 'total' — stop when last page
        if len(vods) < PAGE_LIMIT:
            break
        page += 1

    return hits


# ---------------------------------------------------------------------------
# Per-stream search (used by classify to enrich specific streams)
# ---------------------------------------------------------------------------

def search_vod(
    vod_id: int,
    query: str,
    arc_name: str,
) -> list[Hit]:
    """Search a single vod_id for a query. Used for targeted lookups."""
    session = _session()
    # DGGVods /search doesn't support vod_id filter directly,
    # so we search globally and filter by vod_id in results.
    url = f"{BASE_URL}/search"
    hits = []
    page = 1

    while True:
        params = {"q": query, "page": page, "limit": PAGE_LIMIT}
        try:
            resp = _get(session, url, params)
        except Exception as e:
            log.warning(f"[search] search_vod error: {e}")
            break

        data = resp.json()
        vods = data.get("vods") or []
        found_target = False

        for vod in vods:
            v_id = vod.get("vod_id") or vod.get("id")
            if v_id and int(v_id) == vod_id:
                found_target = True
                for seg in vod.get("segments", []):
                    hits.append(Hit(
                        dgg_vod_id=vod_id,
                        arc_name=arc_name,
                        query=query,
                        start_time=int(seg.get("start_time", 0)),
                        end_time=int(seg.get("end_time", 0)),
                        snippet=seg.get("snippet", ""),
                    ))

        if not vods or len(vods) < PAGE_LIMIT:
            break
        # Stop early if we found the target and we're past it (results sorted by relevance)
        page += 1

    return hits


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> set:
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            data = json.load(f)
        return set(data.get("completed", []))
    return set()


def _save_checkpoint(completed: set):
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump({"completed": sorted(completed)}, f)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _session(cookie: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def _get(session, url, params, retries=3) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 404):
                raise
            if e.response is not None and e.response.status_code == 401:
                raise
            log.warning(f"[search] HTTP error attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except requests.RequestException as e:
            log.warning(f"[search] Request error attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
