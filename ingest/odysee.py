"""
ingest/odysee.py — Odysee JSONRPC API client.

Paginates claim_search for configured Odysee channels and extracts:
  - claim_id
  - title
  - release_time (unix timestamp → YYYYMMDD)
  - duration (if available in metadata)
  - original YouTube video_id (extracted from slug/name via regex)

No auth required. No rate limiting observed.
Results cached to data/cache/odysee_cache.json.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from core.models import ArchiveVideo
from core.utils import extract_video_id, log_step

log = logging.getLogger(__name__)

ODYSEE_API = "https://api.na-backend.odysee.com/api/v1/proxy"
PAGE_SIZE = 50

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"

# Regex to find original YT video_id in Odysee slug/name field
# Odysee slugs like "stream-title-abc12345678" where abc12345678 is the YT id
_YT_ID_IN_SLUG_RE = re.compile(r'[A-Za-z0-9_-]{11}$')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_channel_claims(
    channel_id: str,
    slug: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    cache: bool = True,
    force_refresh: bool = False,
) -> list[ArchiveVideo]:
    """
    Paginate all claims for an Odysee channel.

    Args:
        channel_id: Odysee channel_id hex string
        slug:       Human-readable slug e.g. '@odysteve:7' (used for logging)
        date_start: optional YYYYMMDD date filter
        date_end:   optional YYYYMMDD date filter
        cache:      use/write cache
        force_refresh: bypass cache

    Returns list of ArchiveVideo with source_type='odysee'.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    safe_slug = slug.replace(':', '_').replace('@', '')
    cache_path = CACHE_DIR / f"odysee_{safe_slug}.json"

    if cache and cache_path.exists() and not force_refresh:
        log_step("odysee", f"Cache hit for {slug}")
        with open(cache_path) as f:
            raw = json.load(f)
        videos = [_dict_to_archive_video(d) for d in raw]
    else:
        log_step("odysee", f"Fetching {slug} from API", channel_id=channel_id)
        raw_claims = _paginate_claims(channel_id)
        videos = [_claim_to_archive_video(c) for c in raw_claims]
        videos = [v for v in videos if v is not None]

        if cache:
            with open(cache_path, 'w') as f:
                json.dump([_archive_video_to_dict(v) for v in videos], f)
            log_step("odysee", f"Wrote cache for {slug}",
                     count=len(videos), path=str(cache_path))

    # Filter to date range
    if date_start or date_end:
        before = len(videos)
        videos = _filter_date_range(videos, date_start, date_end)
        log_step("odysee", f"Date filter {slug}",
                 before=before, after=len(videos), range=f"{date_start}–{date_end}")

    log_step("odysee", f"Returning {slug}", count=len(videos))
    return videos


def fetch_all_channels(
    channel_configs: list,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    cache: bool = True,
    force_refresh: bool = False,
) -> list[ArchiveVideo]:
    """
    Fetch all claims from all configured Odysee channels.
    Returns combined list of ArchiveVideo.
    """
    all_videos = []
    for ch in channel_configs:
        vids = fetch_channel_claims(
            channel_id=ch.channel_id,
            slug=ch.slug,
            date_start=date_start,
            date_end=date_end,
            cache=cache,
            force_refresh=force_refresh,
        )
        all_videos.extend(vids)

    log_step("odysee", "All channels fetched", total=len(all_videos))
    return all_videos


# ---------------------------------------------------------------------------
# JSONRPC helpers
# ---------------------------------------------------------------------------

def _paginate_claims(channel_id: str) -> list[dict]:
    """Paginate claim_search until no more results."""
    all_claims = []
    page = 1
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })

    while True:
        payload = {
            "jsonrpc": "2.0",
            "method": "claim_search",
            "params": {
                "channel_ids": [channel_id],
                "claim_type": ["stream"],
                "has_source": True,
                "no_totals": True,
                "order_by": ["release_time"],
                "page_size": PAGE_SIZE,
                "page": page,
            },
        }
        log.debug(f"[odysee] claim_search channel={channel_id[:8]}... page={page}")

        resp = _post(session, payload)
        data = resp.json()

        error = data.get("error")
        if error:
            log.error(f"[odysee] JSONRPC error: {error}")
            break

        items = data.get("result", {}).get("items", [])
        if not items:
            log_step("odysee", "Pagination exhausted",
                     channel=channel_id[:8], pages=page - 1, total=len(all_claims))
            break

        all_claims.extend(items)
        log.debug(f"[odysee] page={page} got={len(items)} cumulative={len(all_claims)}")

        if len(items) < PAGE_SIZE:
            log_step("odysee", "Last page reached", page=page, total=len(all_claims))
            break

        page += 1

    return all_claims


def _claim_to_archive_video(claim: dict) -> Optional[ArchiveVideo]:
    """Convert a raw Odysee claim dict to an ArchiveVideo."""
    claim_id = claim.get("claim_id", "")
    name = claim.get("name", "")  # URL slug — may contain YT video_id at end
    value = claim.get("value", {})

    title = value.get("title") or name or ""

    # Release time: unix timestamp
    release_time = value.get("release_time") or claim.get("timestamp")
    date = None
    if release_time:
        try:
            dt = datetime.fromtimestamp(int(release_time), tz=timezone.utc)
            date = dt.strftime("%Y%m%d")
        except (ValueError, OSError):
            pass

    # Duration from video stream metadata
    duration_sec = None
    video = value.get("video") or {}
    if video.get("duration"):
        try:
            duration_sec = int(video["duration"])
        except (ValueError, TypeError):
            pass

    # Try to extract original YT video_id from the slug name
    # Odysee slugs often end with the YT video_id: "stream-title-abc12345DEF"
    extracted_video_id = _extract_yt_id_from_slug(name) or extract_video_id(title)

    # Build Odysee URL from channel_name + claim name
    channel_name = claim.get("signing_channel", {}).get("name", "")
    url = f"https://odysee.com/{channel_name}/{name}"

    return ArchiveVideo(
        source_type="odysee",
        source_id=claim_id,
        url=url,
        title=title,
        date=date,
        duration_sec=duration_sec,
        extracted_video_id=extracted_video_id,
    )


def _extract_yt_id_from_slug(slug: str) -> Optional[str]:
    """
    Odysee slugs often end with the original YT video_id after a hyphen.
    E.g. "stream-title-2024-01-06-abc12345DEF" → "abc12345DEF"
    """
    if not slug:
        return None
    # Split on hyphens, check last token
    parts = slug.split('-')
    for candidate in reversed(parts):
        if len(candidate) == 11:
            has_digit = any(c.isdigit() for c in candidate)
            has_alpha = any(c.isalpha() for c in candidate)
            if has_digit and has_alpha:
                return candidate
    return None


# ---------------------------------------------------------------------------
# Cache / serialization helpers
# ---------------------------------------------------------------------------

def _archive_video_to_dict(v: ArchiveVideo) -> dict:
    return {
        "source_type": v.source_type,
        "source_id": v.source_id,
        "url": v.url,
        "title": v.title,
        "date": v.date,
        "duration_sec": v.duration_sec,
        "extracted_video_id": v.extracted_video_id,
    }


def _dict_to_archive_video(d: dict) -> ArchiveVideo:
    return ArchiveVideo(
        source_type=d["source_type"],
        source_id=d["source_id"],
        url=d["url"],
        title=d["title"],
        date=d.get("date"),
        duration_sec=d.get("duration_sec"),
        extracted_video_id=d.get("extracted_video_id"),
    )


def _filter_date_range(
    videos: list[ArchiveVideo],
    date_start: Optional[str],
    date_end: Optional[str],
) -> list[ArchiveVideo]:
    result = []
    for v in videos:
        if v.date is None:
            continue
        if date_start and v.date < date_start:
            continue
        if date_end and v.date > date_end:
            continue
        result.append(v)
    return result


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(session: requests.Session, payload: dict, retries: int = 3) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = session.post(ODYSEE_API, json=payload, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            log.warning(f"[odysee] HTTP error attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except requests.RequestException as e:
            log.warning(f"[odysee] Request error attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
