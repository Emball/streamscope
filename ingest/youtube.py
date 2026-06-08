"""
ingest/youtube.py — YouTube Data API v3 client for archive channel ingestion.

Fetches video metadata from:
  - @destinyggvods (UCJyTTRHqcKDMsctENez6oMQ) — 1080p, date in title
  - @OmniBased     (UClt_id2lCH-wIFjiMWSMCWA) — 360p, date + video_id in title

Auth: requires a YouTube Data API key passed at runtime (or YOUTUBE_API_KEY env var).
No OAuth needed for public playlist reads.

Results cached to data/cache/archive_cache.json.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from core.models import ArchiveVideo
from core.utils import parse_date, extract_video_id, parse_iso8601_duration, log_step

log = logging.getLogger(__name__)

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
PAGE_SIZE = 50

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
ARCHIVE_CACHE = CACHE_DIR / "archive_cache.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_channel_videos(
    channel_id: str,
    source_type: str,
    api_key: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    cache: bool = True,
    force_refresh: bool = False,
) -> list[ArchiveVideo]:
    """
    Fetch all videos from a YouTube channel's uploads playlist.

    Args:
        channel_id:  YouTube channel ID (UCxxx...)
        source_type: 'destinyggvods' | 'omnibased'
        api_key:     YouTube Data API key
        date_start:  optional YYYYMMDD filter (applied to parsed title date)
        date_end:    optional YYYYMMDD filter
        cache:       whether to use/write the JSON cache
        force_refresh: bypass cache

    Returns list of ArchiveVideo.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_key = f"{source_type}_{channel_id}"
    cached = _load_cache(cache_key) if (cache and not force_refresh) else None

    if cached is not None:
        log_step("youtube", f"Cache hit for {source_type}", count=len(cached))
        videos = [_dict_to_archive_video(d) for d in cached]
    else:
        log_step("youtube", f"Fetching {source_type} from API", channel_id=channel_id)
        uploads_id = _get_uploads_playlist_id(channel_id, api_key)
        if not uploads_id:
            log.error(f"[youtube] Could not find uploads playlist for channel {channel_id}")
            return []

        raw_items = _paginate_playlist(uploads_id, api_key)
        video_ids = [item['snippet']['resourceId']['videoId'] for item in raw_items
                     if item.get('snippet', {}).get('resourceId', {}).get('videoId')]

        # Fetch durations in batches of 50
        durations = _fetch_durations_batch(video_ids, api_key)

        videos = []
        for item in raw_items:
            av = _item_to_archive_video(item, source_type, durations)
            if av:
                videos.append(av)

        if cache:
            _save_cache(cache_key, [_archive_video_to_dict(v) for v in videos])
            log_step("youtube", f"Wrote cache for {source_type}", count=len(videos))

    # Filter to date range if provided
    if date_start or date_end:
        before = len(videos)
        videos = _filter_date_range(videos, date_start, date_end)
        log_step("youtube", f"Date filter {source_type}",
                 before=before, after=len(videos),
                 range=f"{date_start}–{date_end}")

    log_step("youtube", f"Returning {source_type}", count=len(videos))
    return videos


def fetch_all_channels(
    channel_configs: list,
    api_key: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    cache: bool = True,
    force_refresh: bool = False,
) -> list[ArchiveVideo]:
    """
    Fetch videos from all configured YouTube channels.
    channel_configs: list of YouTubeChannelConfig objects.
    Returns combined deduplicated list of ArchiveVideo.
    """
    all_videos = []
    for ch in channel_configs:
        vids = fetch_channel_videos(
            channel_id=ch.id,
            source_type=_channel_source_type(ch.handle),
            api_key=api_key,
            date_start=date_start,
            date_end=date_end,
            cache=cache,
            force_refresh=force_refresh,
        )
        all_videos.extend(vids)

    log_step("youtube", "All channels fetched", total=len(all_videos))
    return all_videos


# ---------------------------------------------------------------------------
# YouTube API helpers
# ---------------------------------------------------------------------------

def _get_uploads_playlist_id(channel_id: str, api_key: str) -> Optional[str]:
    """Resolve a channel's uploads playlist ID via channels.list."""
    url = f"{YT_API_BASE}/channels"
    params = {
        "part": "contentDetails",
        "id": channel_id,
        "key": api_key,
    }
    resp = _get(url, params)
    items = resp.json().get("items", [])
    if not items:
        return None
    return items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")


def _paginate_playlist(playlist_id: str, api_key: str) -> list[dict]:
    """Paginate playlistItems.list to get all video stubs."""
    items = []
    page_token = None

    while True:
        url = f"{YT_API_BASE}/playlistItems"
        params = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": PAGE_SIZE,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        resp = _get(url, params)
        data = resp.json()

        batch = data.get("items", [])
        items.extend(batch)
        log.debug(f"[youtube] playlist page: got={len(batch)} cumulative={len(items)}")

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    log_step("youtube", "Playlist paginated", playlist_id=playlist_id, total=len(items))
    return items


def _fetch_durations_batch(video_ids: list[str], api_key: str) -> dict[str, Optional[int]]:
    """
    Fetch video durations via videos.list (contentDetails).
    Processes in chunks of 50 (API limit).
    Returns dict of video_id → duration_sec.
    """
    durations = {}
    chunk_size = 50

    for i in range(0, len(video_ids), chunk_size):
        chunk = video_ids[i:i + chunk_size]
        url = f"{YT_API_BASE}/videos"
        params = {
            "part": "contentDetails",
            "id": ",".join(chunk),
            "key": api_key,
        }
        resp = _get(url, params)
        for item in resp.json().get("items", []):
            vid_id = item["id"]
            dur_str = item.get("contentDetails", {}).get("duration", "")
            durations[vid_id] = parse_iso8601_duration(dur_str)

        log.debug(f"[youtube] Fetched durations chunk {i//chunk_size + 1}, got={len(durations)}")

    return durations


def _item_to_archive_video(
    item: dict,
    source_type: str,
    durations: dict[str, Optional[int]],
) -> Optional[ArchiveVideo]:
    """Convert a playlistItems API item to ArchiveVideo."""
    snippet = item.get("snippet", {})
    video_id = snippet.get("resourceId", {}).get("videoId")
    if not video_id:
        return None

    title = snippet.get("title", "")
    duration_sec = durations.get(video_id)

    # Parse broadcast date from title (both channels embed date in title)
    date = parse_date(title)

    # For OmniBased: try to extract original YouTube video_id from title
    extracted_vid_id = None
    if source_type == "omnibased":
        extracted_vid_id = extract_video_id(title)
        # Don't let it return the archive video_id itself as the "original"
        if extracted_vid_id == video_id:
            extracted_vid_id = None

    url = f"https://www.youtube.com/watch?v={video_id}"

    return ArchiveVideo(
        source_type=source_type,
        source_id=video_id,
        url=url,
        title=title,
        date=date,
        duration_sec=duration_sec,
        extracted_video_id=extracted_vid_id,
    )


def _channel_source_type(handle: str) -> str:
    h = handle.lower().strip('@')
    if 'destinyggvods' in h or 'destinyvods' in h:
        return 'destinyggvods'
    if 'omnibased' in h:
        return 'omnibased'
    return h


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache(key: str) -> Optional[list]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"yt_{key}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_cache(key: str, data: list):
    path = CACHE_DIR / f"yt_{key}.json"
    with open(path, 'w') as f:
        json.dump(data, f)


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

def _get(url: str, params: dict, retries: int = 3) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 403, 404):
                log.error(f"[youtube] Fatal HTTP {e.response.status_code}: {url}")
                raise
            log.warning(f"[youtube] HTTP error attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except requests.RequestException as e:
            log.warning(f"[youtube] Request error attempt {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
