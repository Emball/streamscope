"""
pipeline/unify.py — Cross-reference engine.

Takes the DGGVods stream index and archive videos from YouTube/Odysee,
matches them to each other using a cascade of signals:

  Signal priority (highest to lowest):
  1. Direct video_id match (DGGVods video_id == archive source_id or extracted_video_id)
  2. Date match (within tolerance) + title fuzzy match (≥ confident threshold) + duration bonus
  3. Date match + title fuzzy match (≥ review threshold)
  4. Unmatched

Same-day/same-title handling:
  If two archive videos have the same normalized title on the same date
  but durations differ beyond tolerance → treated as distinct streams.
  If durations are within tolerance → same stream.

Outputs Sources (confident + needs_review) that are written to the DB.
Also writes a human-readable match report to output/{arc}_matches.txt.
"""

import logging
from pathlib import Path
from typing import Optional

from core.config import ArcConfig
from core.db import DB
from core.models import ArchiveVideo, Source, Stream
from core.utils import (
    normalize_title, title_similarity,
    dates_within, durations_match,
    log_step,
)

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def unify_sources(
    cfg: ArcConfig,
    db: DB,
    streams: list[Stream],
    archive_videos: list[ArchiveVideo],
    write_report: bool = True,
) -> dict:
    """
    Cross-reference DGGVods streams against all archive videos.
    Writes Source rows to DB for every match found.

    Returns stats dict.
    """
    log_step("unify", "Starting unification",
             streams=len(streams), archive_videos=len(archive_videos))

    th = cfg.thresholds
    stats = {
        "direct_id_matches": 0,
        "confident_fuzzy_matches": 0,
        "review_fuzzy_matches": 0,
        "unmatched_archive": 0,
        "total_sources_written": 0,
    }

    # First: insert a 'dggvods' source record for every DGGVods stream
    dgg_sources = []
    for s in streams:
        video_url = (
            f"https://www.youtube.com/watch?v={s.video_id}"
            if s.video_id else f"https://dggvods.dev/vods/{s.dgg_vod_id}"
        )
        dgg_sources.append(Source(
            dgg_vod_id=s.dgg_vod_id,
            source_type="dggvods",
            source_id=str(s.dgg_vod_id),
            url=video_url,
            duration_sec=s.duration_sec,
            match_score=100.0,
            match_note="primary source",
            is_confident=True,
        ))
    ins, upd = db.upsert_sources(dgg_sources)
    log_step("unify", "DGGVods sources recorded", inserted=ins, updated=upd)

    # Build lookup structures
    stream_by_video_id: dict[str, Stream] = {}
    for s in streams:
        if s.video_id:
            stream_by_video_id[s.video_id] = s

    # Normalize titles for all streams (once)
    stream_norm_titles: dict[int, str] = {
        s.dgg_vod_id: normalize_title(s.title) for s in streams
    }

    # Match each archive video
    sources_to_write: list[Source] = []
    match_log: list[str] = []

    for av in archive_videos:
        match = _match_archive_video(
            av=av,
            streams=streams,
            stream_by_video_id=stream_by_video_id,
            stream_norm_titles=stream_norm_titles,
            th=th,
        )

        if match is None:
            stats["unmatched_archive"] += 1
            match_log.append(
                f"UNMATCHED  [{av.source_type}] {av.date} {av.title[:60]}\n"
            )
            continue

        source, tier = match

        if tier == "direct":
            stats["direct_id_matches"] += 1
        elif tier == "confident":
            stats["confident_fuzzy_matches"] += 1
        else:
            stats["review_fuzzy_matches"] += 1

        sources_to_write.append(source)
        match_log.append(
            f"{tier.upper():10s}  [{av.source_type}] {av.date} {av.title[:50]}"
            f"  → vod_id={source.dgg_vod_id} score={source.match_score:.1f}"
            f"  note={source.match_note}\n"
        )

    ins, upd = db.upsert_sources(sources_to_write)
    stats["total_sources_written"] = ins + upd

    log_step("unify", "Unification complete", **stats)

    if write_report:
        _write_report(cfg.arc_name, match_log, stats)

    return stats


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _match_archive_video(
    av: ArchiveVideo,
    streams: list[Stream],
    stream_by_video_id: dict[str, Stream],
    stream_norm_titles: dict[int, str],
    th,
) -> Optional[tuple[Source, str]]:
    """
    Try to match one ArchiveVideo to a Stream.
    Returns (Source, tier) or None if no match.
    Tier is 'direct', 'confident', or 'review'.
    """

    # --- Signal 1: Direct video_id match ---
    # Check av.source_id (the archive's own video_id) and extracted_video_id
    for candidate_id in [av.source_id, av.extracted_video_id]:
        if candidate_id and candidate_id in stream_by_video_id:
            stream = stream_by_video_id[candidate_id]
            source = Source(
                dgg_vod_id=stream.dgg_vod_id,
                source_type=av.source_type,
                source_id=av.source_id,
                url=av.url,
                duration_sec=av.duration_sec,
                match_score=100.0,
                match_note=f"direct video_id match ({candidate_id})",
                is_confident=True,
            )
            return source, "direct"

    # --- Signal 2 & 3: Date + fuzzy title match ---
    if not av.date:
        return None

    av_norm = normalize_title(av.title)
    best_score = 0.0
    best_stream: Optional[Stream] = None

    # Filter candidates to streams within date tolerance
    date_candidates = [
        s for s in streams
        if s.date and dates_within(av.date, s.date, th.date_tolerance_days)
    ]

    for stream in date_candidates:
        score = title_similarity(av_norm, stream_norm_titles[stream.dgg_vod_id])

        # Duration bonus: if durations match within tolerance, bump score
        if durations_match(av.duration_sec, stream.duration_sec, th.duration_tolerance_sec):
            score = min(100.0, score + th.duration_match_bonus)

        if score > best_score:
            best_score = score
            best_stream = stream

    if best_stream is None:
        return None

    if best_score >= th.confident:
        source = Source(
            dgg_vod_id=best_stream.dgg_vod_id,
            source_type=av.source_type,
            source_id=av.source_id,
            url=av.url,
            duration_sec=av.duration_sec,
            match_score=best_score,
            match_note=f"fuzzy title+date match score={best_score:.1f}",
            is_confident=True,
        )
        return source, "confident"

    if best_score >= th.review:
        source = Source(
            dgg_vod_id=best_stream.dgg_vod_id,
            source_type=av.source_type,
            source_id=av.source_id,
            url=av.url,
            duration_sec=av.duration_sec,
            match_score=best_score,
            match_note=f"fuzzy review score={best_score:.1f} — NEEDS REVIEW",
            is_confident=False,
        )
        return source, "review"

    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _write_report(arc_name: str, match_log: list[str], stats: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{arc_name}_matches.txt"

    with open(path, 'w') as f:
        f.write(f"=== StreamScope Match Report: {arc_name} ===\n\n")
        f.write("STATS\n")
        for k, v in stats.items():
            f.write(f"  {k}: {v}\n")
        f.write("\n--- MATCHES ---\n\n")
        f.writelines(match_log)

    log_step("unify", f"Match report written", path=str(path))
