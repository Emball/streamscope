"""
ingest/dggvods.py — DGGVods API client.

Endpoints used:
  GET /vods?page=N&limit=50&filter=all   → paginate all VODs
  GET /transcript/{vod_id}               → full transcript for one VOD

The /search endpoint is handled by pipeline/search.py (it's arc-specific).

No rate limiting. No auth. Hammerable.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from core.models import Stream, TranscriptSegment, Transcript
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
VODS_CACHE = CACHE_DIR / "vods_cache.json"


# ---------------------------------------------------------------------------
# VOD pagination
# ---------------------------------------------------------------------------

def fetch_all_vods(
    date_start: str,
    date_end: str,
    cache: bool = True,
    force_refresh: bool = False,
) -> list[Stream]:
    """
    Paginate the DGGVods /vods endpoint and return Stream objects
    whose date falls within [date_start, date_end] (YYYYMMDD inclusive).

    Results are cached to data/cache/vods_cache.json.
    Set force_refresh=True to bypass the cache.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache and VODS_CACHE.exists() and not force_refresh:
        log_step("dggvods", "Loading VODs from cache", path=str(VODS_CACHE))
        with open(VODS_CACHE) as f:
            raw = json.load(f)
        streams = [_raw_to_stream(r) for r in raw]
        in_range = _filter_range(streams, date_start, date_end)
        log_step("dggvods", "Cache hit", total=len(streams), in_range=len(in_range))
        return in_range

    log_step("dggvods", "Fetching all VODs from API", date_start=date_start, date_end=date_end)
    all_raw = _paginate_vods()

    if cache:
        with open(VODS_CACHE, 'w') as f:
            json.dump(all_raw, f)
        log_step("dggvods", "Wrote VODs cache", count=len(all_raw), path=str(VODS_CACHE))

    streams = [_raw_to_stream(r) for r in all_raw]
    in_range = _filter_range(streams, date_start, date_end)
    log_step("dggvods", "Fetch complete", total=len(streams), in_range=len(in_range))
    return in_range


def _paginate_vods() -> list[dict]:
    """Hit /vods until exhausted. Returns raw dicts."""
    all_vods = []
    page = 1
    session = _session()

    while True:
        url = f"{BASE_URL}/vods"
        params = {"page": page, "limit": PAGE_LIMIT, "filter": "all"}
        log.debug(f"[dggvods] GET /vods page={page}")

        resp = _get(session, url, params=params)
        data = resp.json()

        vods = data.get("vods") or data.get("data") or []
        if not vods:
            log_step("dggvods", "Pagination exhausted", pages=page - 1, total=len(all_vods))
            break

        all_vods.extend(vods)
        log.debug(f"[dggvods] page={page} got={len(vods)} cumulative={len(all_vods)}")

        # Check if we've received fewer than the page limit (last page)
        if len(vods) < PAGE_LIMIT:
            log_step("dggvods", "Last page reached", page=page, total=len(all_vods))
            break

        page += 1

    return all_vods


def _raw_to_stream(r: dict) -> Stream:
    """Convert a raw /vods API dict to a Stream dataclass."""
    vod_id = r.get("vod_id") or r.get("id")
    video_id = r.get("video_id") or r.get("videoId")
    title = r.get("title", "")
    date_raw = r.get("date", "")
    duration = r.get("duration") or r.get("duration_sec")

    # Date: API returns YYYYMMDD compact or similar — normalize
    date = _normalize_dgg_date(date_raw)

    # Duration: may be seconds (int) or "HH:MM:SS" string
    if isinstance(duration, str) and ":" in duration:
        duration = _parse_hhmmss(duration)
    elif duration is not None:
        duration = int(duration)

    has_transcript = bool(r.get("has_transcript") or r.get("hasTranscript"))

    return Stream(
        dgg_vod_id=int(vod_id),
        video_id=str(video_id) if video_id else None,
        title=title,
        date=date,
        duration_sec=duration,
        dgg_has_transcript=has_transcript,
    )


def _normalize_dgg_date(date_raw) -> str:
    """DGGVods dates come back as YYYYMMDD integers or strings."""
    if date_raw is None:
        return ""
    s = str(date_raw).strip()
    # Already compact YYYYMMDD
    if len(s) == 8 and s.isdigit():
        return s
    # ISO format YYYY-MM-DD
    if len(s) >= 10 and s[4] == '-':
        return s[:10].replace('-', '')
    return s


def _parse_hhmmss(s: str) -> Optional[int]:
    """Parse HH:MM:SS or MM:SS to seconds."""
    try:
        parts = list(map(int, s.split(":")))
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    except ValueError:
        pass
    return None


def _filter_range(streams: list[Stream], start: str, end: str) -> list[Stream]:
    return [s for s in streams if s.date and start <= s.date <= end]


# ---------------------------------------------------------------------------
# Transcript fetch
# ---------------------------------------------------------------------------

TRANSCRIPT_CACHE_DIR = CACHE_DIR / "transcript_cache"


def fetch_transcript(
    vod_id: int,
    cache: bool = True,
    force_refresh: bool = False,
) -> Optional[Transcript]:
    """
    Fetch the full transcript for a VOD from DGGVods API.
    Caches to data/cache/transcript_cache/{vod_id}.json.

    Returns None if the transcript is unavailable (404 or empty).
    """
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = TRANSCRIPT_CACHE_DIR / f"{vod_id}.json"

    if cache and cache_path.exists() and not force_refresh:
        log.debug(f"[dggvods] Transcript cache hit: vod_id={vod_id}")
        with open(cache_path) as f:
            raw = json.load(f)
        return _raw_to_transcript(vod_id, raw, source='dggvods')

    url = f"{BASE_URL}/transcript/{vod_id}"
    log.debug(f"[dggvods] GET /transcript/{vod_id}")

    session = _session()
    try:
        resp = _get(session, url)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            log.debug(f"[dggvods] Transcript not found: vod_id={vod_id}")
            return None
        raise

    data = resp.json()
    segments = data.get("segments") or data.get("transcript") or []

    if not segments:
        log.debug(f"[dggvods] Empty transcript: vod_id={vod_id}")
        return None

    if cache:
        with open(cache_path, 'w') as f:
            json.dump(data, f)

    return _raw_to_transcript(vod_id, data, source='dggvods')


def fetch_transcripts_batch(
    vod_ids: list[int],
    cache: bool = True,
) -> dict[int, Optional[Transcript]]:
    """
    Fetch transcripts for a list of vod_ids.
    Returns a dict mapping vod_id → Transcript (or None if unavailable).
    """
    results = {}
    for i, vod_id in enumerate(vod_ids):
        log.debug(f"[dggvods] Fetching transcript {i+1}/{len(vod_ids)} vod_id={vod_id}")
        results[vod_id] = fetch_transcript(vod_id, cache=cache)
    fetched = sum(1 for v in results.values() if v is not None)
    log_step("dggvods", "Batch transcript fetch complete",
             requested=len(vod_ids), fetched=fetched, missing=len(vod_ids)-fetched)
    return results


def _raw_to_transcript(vod_id: int, data: dict, source: str) -> Optional[Transcript]:
    """Convert raw API transcript data to a Transcript dataclass."""
    segments_raw = data.get("segments") or data.get("transcript") or []
    segments = []
    for seg in segments_raw:
        text = seg.get("text", "").strip()
        # Strip speaker tags (e.g. "[Destiny]: ..." or "DESTINY: ...")
        text = _strip_speaker_tag(text)
        if not text:
            continue
        start = int(seg.get("start_time", seg.get("start", 0)))
        end = int(seg.get("end_time", seg.get("end", start + 5)))
        segments.append(TranscriptSegment(start_time=start, end_time=end, text=text))

    if not segments:
        return None

    return Transcript(dgg_vod_id=vod_id, source=source, segments=segments)


_SPEAKER_RE = __import__('re').compile(
    r'^\s*\[?[A-Z][A-Z0-9 _]{1,20}\]?\s*:\s*', __import__('re').IGNORECASE
)


def _strip_speaker_tag(text: str) -> str:
    """Remove leading speaker tags like '[Destiny]: ' or 'DESTINY: '."""
    return _SPEAKER_RE.sub('', text).strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(session: requests.Session, url: str, params: dict = None, retries: int = 3) -> requests.Response:
    """GET with retry on transient errors."""
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 400):
                raise
            log.warning(f"[dggvods] HTTP error (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except requests.RequestException as e:
            log.warning(f"[dggvods] Request error (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
