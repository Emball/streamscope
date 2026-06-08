"""
pipeline/corpus.py — Context window extraction and corpus packer.

For each flagged stream with a transcript:
  1. Identify all hit timestamps
  2. Extract context windows (±half_window seconds) around each cluster of hits
  3. Deduplicate overlapping windows
  4. Cap at max_windows_per_stream highest-density windows per stream
  5. Sort all windows globally by density (hits per window)
  6. Pack into corpus file until target_chars reached

Output files:
  output/{arc}_corpus.txt   — LLM-ready corpus (~800K chars)
  output/{arc}_index.json   — Full stream index with hit moments and window metadata
"""

import json
import logging
from pathlib import Path
from typing import Optional

from core.config import ArcConfig
from core.db import DB
from core.models import (
    ArcResult, CorpusEntry, Hit, Stream, Transcript, Window
)
from core.utils import fmt_timestamp, log_step
from pipeline.transcripts import load_transcript

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_corpus(cfg: ArcConfig, db: DB) -> dict:
    """
    Build the LLM corpus for an arc.

    Reads flagged streams from DB, loads their transcripts,
    extracts context windows, and packs the corpus file.

    Returns stats dict.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    arc_name = cfg.arc_name
    corpus_cfg = cfg.corpus
    half_window = corpus_cfg.window_seconds // 2

    log_step("corpus", "Starting corpus build",
             arc=arc_name,
             target_chars=corpus_cfg.target_chars,
             window_seconds=corpus_cfg.window_seconds,
             max_windows_per_stream=corpus_cfg.max_windows_per_stream)

    # Load all flagged streams
    flagged = db.get_flagged_streams(arc_name)
    if not flagged:
        log_step("corpus", "No flagged streams — nothing to build")
        return {"flagged_streams": 0, "windows_extracted": 0, "corpus_chars": 0}

    log_step("corpus", f"Processing {len(flagged)} flagged streams")

    all_windows: list[Window] = []
    stats = {
        "flagged_streams": len(flagged),
        "streams_with_transcript": 0,
        "streams_without_transcript": 0,
        "windows_extracted": 0,
        "corpus_chars": 0,
    }

    for stream, arc_result in flagged:
        transcript = load_transcript(stream.dgg_vod_id)
        if not transcript or not transcript.segments:
            log.debug(
                f"[corpus] No transcript: vod_id={stream.dgg_vod_id} "
                f"title={stream.title[:40]!r}"
            )
            stats["streams_without_transcript"] += 1
            continue

        stats["streams_with_transcript"] += 1
        hits = db.get_hits(stream.dgg_vod_id, arc_name)

        windows = extract_windows(
            stream=stream,
            arc_result=arc_result,
            transcript=transcript,
            hits=hits,
            half_window=half_window,
            max_windows=corpus_cfg.max_windows_per_stream,
            db=db,
        )

        all_windows.extend(windows)
        log.debug(
            f"[corpus] vod_id={stream.dgg_vod_id} "
            f"hits={len(hits)} windows={len(windows)}"
        )

    stats["windows_extracted"] = len(all_windows)

    # Sort globally by hit_count DESC (density first), then date
    all_windows.sort(key=lambda w: (-w.hit_count, w.stream_date or ""))

    # Pack corpus
    entries = pack_corpus(
        windows=all_windows,
        target_chars=corpus_cfg.target_chars,
    )
    stats["corpus_chars"] = sum(len(e.format()) for e in entries)

    # Write outputs
    _write_corpus_txt(arc_name, entries)
    _write_index_json(arc_name, flagged, all_windows, db)

    log_step("corpus", "Corpus build complete", **stats)
    return stats


# ---------------------------------------------------------------------------
# Window extraction
# ---------------------------------------------------------------------------

def extract_windows(
    stream: Stream,
    arc_result: ArcResult,
    transcript: Transcript,
    hits: list[Hit],
    half_window: int,
    max_windows: int,
    db: DB,
) -> list[Window]:
    """
    Extract context windows around hit clusters in a transcript.

    Steps:
    1. For each hit timestamp, define a window [hit.start - half_window, hit.start + half_window]
    2. Merge overlapping windows
    3. For each merged window, extract transcript text and count hits inside it
    4. Cap to max_windows highest-hit-count windows
    """
    if not hits or not transcript.segments:
        return []

    best_url = db.get_best_source_url(stream.dgg_vod_id)
    source_url = best_url or f"https://dggvods.dev/vods/{stream.dgg_vod_id}"

    # Step 1: Build raw windows from hit timestamps
    raw_windows = []
    for hit in hits:
        start = max(0, hit.start_time - half_window)
        end = hit.start_time + half_window
        raw_windows.append((start, end))

    # Step 2: Merge overlapping windows
    merged = _merge_intervals(raw_windows)

    # Step 3: Extract text and count hits for each merged window
    windows: list[Window] = []
    for (w_start, w_end) in merged:
        text = _extract_text(transcript, w_start, w_end)
        if not text:
            continue

        hit_count = sum(1 for h in hits if w_start <= h.start_time <= w_end)

        windows.append(Window(
            dgg_vod_id=stream.dgg_vod_id,
            arc_name=arc_result.arc_name,
            start_time=w_start,
            end_time=w_end,
            text=text,
            hit_count=hit_count,
            source_url=source_url,
            stream_title=stream.title,
            stream_date=stream.date,
        ))

    # Step 4: Cap at max_windows (highest hit_count first)
    windows.sort(key=lambda w: -w.hit_count)
    return windows[:max_windows]


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent time intervals."""
    if not intervals:
        return []
    sorted_ivs = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_ivs[0]]
    for start, end in sorted_ivs[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _extract_text(transcript: Transcript, start: int, end: int) -> str:
    """Extract and join transcript segments that fall within [start, end]."""
    parts = []
    for seg in transcript.segments:
        # Include segment if it overlaps with the window
        if seg.end_time >= start and seg.start_time <= end:
            parts.append(seg.text)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Corpus packing
# ---------------------------------------------------------------------------

def pack_corpus(
    windows: list[Window],
    target_chars: int,
) -> list[CorpusEntry]:
    """
    Pack windows into CorpusEntry objects until target_chars is reached.
    Windows are already sorted by density (caller's responsibility).
    """
    entries: list[CorpusEntry] = []
    total_chars = 0

    for window in windows:
        entry = CorpusEntry(
            dgg_vod_id=window.dgg_vod_id,
            stream_title=window.stream_title or "",
            stream_date=window.stream_date or "",
            source_url=window.source_url or "",
            window_start=window.start_time,
            window_end=window.end_time,
            hit_count=window.hit_count,
            text=window.text,
        )
        entry_text = entry.format()
        if total_chars + len(entry_text) > target_chars:
            # Try to include a truncated version if we have significant space left
            remaining = target_chars - total_chars
            if remaining > 500:
                entry.text = entry.text[:remaining - 200] + "...[truncated]"
                entries.append(entry)
                total_chars += len(entry.format())
            break

        entries.append(entry)
        total_chars += len(entry_text)

    log_step("corpus", "Packed corpus",
             entries=len(entries), chars=total_chars, target=target_chars)
    return entries


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------

def _write_corpus_txt(arc_name: str, entries: list[CorpusEntry]):
    path = OUTPUT_DIR / f"{arc_name}_corpus.txt"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"=== StreamScope Corpus: {arc_name} ===\n")
        f.write(f"Total entries: {len(entries)}\n")
        f.write(f"Total chars: {sum(len(e.format()) for e in entries):,}\n")
        f.write("\n\n")
        for entry in entries:
            f.write(entry.format())
            f.write("\n\n")
    log_step("corpus", "Wrote corpus", path=str(path),
             entries=len(entries), chars=path.stat().st_size)


def _write_index_json(
    arc_name: str,
    flagged: list[tuple[Stream, ArcResult]],
    windows: list[Window],
    db: DB,
):
    """Write the full stream index with hit moments and window metadata."""
    path = OUTPUT_DIR / f"{arc_name}_index.json"

    # Group windows by vod_id
    windows_by_vod: dict[int, list[Window]] = {}
    for w in windows:
        windows_by_vod.setdefault(w.dgg_vod_id, []).append(w)

    index = []
    for stream, arc_result in flagged:
        sources = db.get_sources(stream.dgg_vod_id)
        hits = db.get_hits(stream.dgg_vod_id, arc_name)
        stream_windows = windows_by_vod.get(stream.dgg_vod_id, [])

        index.append({
            "vod_id": stream.dgg_vod_id,
            "video_id": stream.video_id,
            "title": stream.title,
            "date": stream.date,
            "duration_sec": stream.duration_sec,
            "density_score": arc_result.density_score,
            "total_hits": arc_result.total_hits,
            "sources": [
                {
                    "type": s.source_type,
                    "url": s.url,
                    "score": s.match_score,
                    "confident": s.is_confident,
                }
                for s in sources
            ],
            "hit_moments": [
                {
                    "query": h.query,
                    "start": h.start_time,
                    "end": h.end_time,
                    "ts": fmt_timestamp(h.start_time),
                    "snippet": h.snippet[:150],
                }
                for h in hits
            ],
            "windows": [
                {
                    "start": w.start_time,
                    "end": w.end_time,
                    "ts": fmt_timestamp(w.start_time),
                    "hit_count": w.hit_count,
                    "url": f"{w.source_url}?t={w.start_time}" if w.source_url else None,
                }
                for w in stream_windows
            ],
        })

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)

    log_step("corpus", "Wrote index", path=str(path), streams=len(index))
