"""
pipeline/transcripts.py — Transcript acquisition with fallback chain.

For each flagged stream, tries to get a transcript in priority order:
  1. DGGVods /transcript/{vod_id}       (highest quality, no auth)
  2. yt-dlp on DestinyGGVods URL        (auto-captions, good quality)
  3. yt-dlp on OmniBased URL            (auto-captions, lower res)
  4. Odysee: no transcripts — skip

All transcripts cached to data/cache/transcript_cache/{vod_id}.json
(shared with dggvods ingest module — same format).
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from core.db import DB
from core.models import Stream, Source, Transcript, TranscriptSegment
from core.utils import log_step
from ingest.dggvods import fetch_transcript as dgg_fetch_transcript

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
TRANSCRIPT_CACHE_DIR = CACHE_DIR / "transcript_cache"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def acquire_transcripts(
    db: DB,
    streams: list[Stream],
    force_refresh: bool = False,
) -> dict:
    """
    Acquire transcripts for a list of streams using the fallback chain.
    Updates dgg_has_transcript in the DB for successful DGGVods fetches.

    Returns stats: attempted, from_dggvods, from_ytdlp, failed
    """
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    stats = {
        "attempted": len(streams),
        "from_dggvods": 0,
        "from_ytdlp": 0,
        "failed": 0,
        "already_cached": 0,
    }

    for i, stream in enumerate(streams):
        vod_id = stream.dgg_vod_id
        cache_path = TRANSCRIPT_CACHE_DIR / f"{vod_id}.json"

        log_step("transcripts", f"Processing {i+1}/{len(streams)}",
                 vod_id=vod_id, date=stream.date, title=stream.title[:40])

        if cache_path.exists() and not force_refresh:
            log.debug(f"[transcripts] Already cached: vod_id={vod_id}")
            stats["already_cached"] += 1
            continue

        transcript = _acquire_with_fallback(db, stream)

        if transcript is None:
            log.warning(f"[transcripts] All sources failed: vod_id={vod_id}")
            stats["failed"] += 1
            # Write empty sentinel so we don't retry repeatedly
            _write_cache(vod_id, {"segments": [], "_source": "none"})
            continue

        _write_cache(vod_id, _transcript_to_dict(transcript))

        if transcript.source == "dggvods":
            db.mark_transcript_available(vod_id)
            stats["from_dggvods"] += 1
        else:
            stats["from_ytdlp"] += 1

        log_step("transcripts", "Transcript acquired",
                 vod_id=vod_id, source=transcript.source,
                 segments=len(transcript.segments))

    log_step("transcripts", "Acquisition complete", **stats)
    return stats


def load_transcript(vod_id: int) -> Optional[Transcript]:
    """
    Load a transcript from cache. Returns None if not cached or empty.
    """
    cache_path = TRANSCRIPT_CACHE_DIR / f"{vod_id}.json"
    if not cache_path.exists():
        return None

    with open(cache_path) as f:
        data = json.load(f)

    segments_raw = data.get("segments", [])
    if not segments_raw:
        return None  # empty sentinel

    source = data.get("_source", "unknown")
    segments = []
    for seg in segments_raw:
        text = seg.get("text", "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start_time=int(seg.get("start_time", seg.get("start", 0))),
            end_time=int(seg.get("end_time", seg.get("end", 0))),
            text=text,
        ))

    if not segments:
        return None

    return Transcript(dgg_vod_id=vod_id, source=source, segments=segments)


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

def _acquire_with_fallback(db: DB, stream: Stream) -> Optional[Transcript]:
    """
    Try each transcript source in priority order. Return first success.
    """

    # 1. DGGVods
    transcript = _try_dggvods(stream.dgg_vod_id)
    if transcript:
        log.debug(f"[transcripts] Got DGGVods transcript: vod_id={stream.dgg_vod_id}")
        return transcript

    # 2. yt-dlp from available archive sources
    sources = db.get_sources(stream.dgg_vod_id)
    yt_sources = _prioritize_yt_sources(sources)

    for source in yt_sources:
        transcript = _try_ytdlp(stream.dgg_vod_id, source.url, source.source_type)
        if transcript:
            log.debug(
                f"[transcripts] Got yt-dlp transcript: vod_id={stream.dgg_vod_id} "
                f"source={source.source_type}"
            )
            return transcript

    return None


def _try_dggvods(vod_id: int) -> Optional[Transcript]:
    """Attempt DGGVods transcript fetch (uses ingest module, which handles caching)."""
    try:
        return dgg_fetch_transcript(vod_id, cache=True)
    except Exception as e:
        log.debug(f"[transcripts] DGGVods fetch failed vod_id={vod_id}: {e}")
        return None


def _try_ytdlp(vod_id: int, url: str, source_type: str) -> Optional[Transcript]:
    """
    Use yt-dlp to download auto-captions from a YouTube URL.
    Returns Transcript or None on failure.
    """
    log.debug(f"[transcripts] yt-dlp attempt: vod_id={vod_id} url={url}")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = str(Path(tmpdir) / "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-subs",
            "--sub-lang", "en",
            "--sub-format", "json3",
            "--output", output_template,
            "--quiet",
            "--no-warnings",
            url,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            log.warning(f"[transcripts] yt-dlp timed out: vod_id={vod_id}")
            return None
        except FileNotFoundError:
            log.error("[transcripts] yt-dlp not found — install it with: pip install yt-dlp")
            return None

        if result.returncode != 0:
            log.debug(
                f"[transcripts] yt-dlp failed: vod_id={vod_id} "
                f"stderr={result.stderr[:200]}"
            )
            return None

        # Find the downloaded subtitle file
        sub_files = list(Path(tmpdir).glob("*.json3"))
        if not sub_files:
            # Also check for .en.json3
            sub_files = list(Path(tmpdir).glob("*.json3"))
            if not sub_files:
                log.debug(f"[transcripts] No subtitle file found: vod_id={vod_id}")
                return None

        return _parse_json3_subs(vod_id, sub_files[0], source=f"ytdlp_{source_type}")


def _parse_json3_subs(vod_id: int, path: Path, source: str) -> Optional[Transcript]:
    """
    Parse yt-dlp json3 subtitle format into a Transcript.
    json3 format: { "events": [ { "tStartMs": N, "dDurationMs": N, "segs": [ {"utf8": "..."} ] } ] }
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[transcripts] Failed to parse json3: {e}")
        return None

    events = data.get("events", [])
    segments = []

    for event in events:
        start_ms = event.get("tStartMs", 0)
        dur_ms = event.get("dDurationMs", 0)
        segs = event.get("segs", [])

        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = text.replace("\n", " ").strip()
        if not text or text == "\n":
            continue

        start_sec = start_ms // 1000
        end_sec = (start_ms + dur_ms) // 1000 if dur_ms else start_sec + 3

        segments.append(TranscriptSegment(
            start_time=start_sec,
            end_time=end_sec,
            text=text,
        ))

    if not segments:
        return None

    return Transcript(dgg_vod_id=vod_id, source=source, segments=segments)


def _prioritize_yt_sources(sources: list[Source]) -> list[Source]:
    """
    Return YouTube sources sorted by transcript priority:
    destinyggvods (1080p, better captions) before omnibased.
    Skip Odysee sources (no captions).
    """
    priority = {"destinyggvods": 1, "omnibased": 2}
    yt_sources = [
        s for s in sources
        if s.source_type in priority and s.is_confident
    ]
    return sorted(yt_sources, key=lambda s: priority.get(s.source_type, 99))


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _write_cache(vod_id: int, data: dict):
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = TRANSCRIPT_CACHE_DIR / f"{vod_id}.json"
    with open(cache_path, 'w') as f:
        json.dump(data, f)


def _transcript_to_dict(t: Transcript) -> dict:
    return {
        "_source": t.source,
        "segments": [
            {
                "start_time": s.start_time,
                "end_time": s.end_time,
                "text": s.text,
            }
            for s in t.segments
        ],
    }
