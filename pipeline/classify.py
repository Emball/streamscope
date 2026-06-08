"""
pipeline/classify.py — Arc classification engine.

For every stream that has hits in the DB, computes a density score
and flags it if density >= arc_flag_threshold.

Density score formula:
    density = (total_unique_hit_windows / duration_minutes)

Where "unique hit windows" are non-overlapping 60-second buckets containing at least one hit.
This prevents a single query with 50 hits in one sentence from inflating the score.

A stream is flagged if density >= cfg.thresholds.arc_flag_threshold.
"""

import logging
from typing import Optional

from core.config import ArcConfig
from core.db import DB
from core.models import ArcResult, Hit, Stream
from core.utils import log_step

log = logging.getLogger(__name__)

# Bucket size for deduplicating overlapping hits
HIT_BUCKET_SECONDS = 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_streams(
    cfg: ArcConfig,
    db: DB,
    streams: Optional[list[Stream]] = None,
) -> dict:
    """
    Compute arc classification for all streams with hits.

    Args:
        cfg:     Arc config (threshold, arc_name)
        db:      Open DB
        streams: If provided, only classify these streams.
                 If None, classifies all streams with hits in DB.

    Returns stats dict: total_classified, flagged, not_flagged
    """
    arc_name = cfg.arc_name
    threshold = cfg.thresholds.arc_flag_threshold

    log_step("classify", "Starting classification",
             arc=arc_name, threshold=threshold)

    # Get all hit data grouped by vod_id
    all_hits_by_vod = db.get_hits_for_arc(arc_name)

    if not all_hits_by_vod:
        log_step("classify", "No hits found for arc", arc=arc_name)
        return {"total_classified": 0, "flagged": 0, "not_flagged": 0}

    # Build stream lookup (for duration)
    if streams:
        stream_map = {s.dgg_vod_id: s for s in streams}
    else:
        all_streams = db.get_all_streams()
        stream_map = {s.dgg_vod_id: s for s in all_streams}

    results: list[ArcResult] = []
    stats = {"total_classified": 0, "flagged": 0, "not_flagged": 0}

    for vod_id, hits in all_hits_by_vod.items():
        stream = stream_map.get(vod_id)
        duration_sec = stream.duration_sec if stream else None

        score = compute_density_score(hits, duration_sec)
        flagged = score >= threshold
        total_hits = len(hits)

        result = ArcResult(
            dgg_vod_id=vod_id,
            arc_name=arc_name,
            total_hits=total_hits,
            density_score=score,
            is_flagged=flagged,
        )
        results.append(result)
        stats["total_classified"] += 1

        if flagged:
            stats["flagged"] += 1
            log.debug(
                f"[classify] FLAGGED vod_id={vod_id} "
                f"density={score:.2f} hits={total_hits} "
                f"title={stream.title[:40] if stream else '?'!r}"
            )
        else:
            stats["not_flagged"] += 1

    db.upsert_arc_results(results)

    log_step("classify", "Classification complete",
             arc=arc_name, **stats)
    return stats


def compute_density_score(hits: list[Hit], duration_sec: Optional[int]) -> float:
    """
    Compute the density score for a stream's hits.

    Uses 60-second buckets to count unique hit windows (prevents hit inflation
    from a single repeated keyword at the same timestamp).

    density = unique_hit_buckets / duration_minutes

    If duration is unknown, falls back to using total hit count directly
    (which is still useful for ranking, just not strictly comparable).
    """
    if not hits:
        return 0.0

    # Deduplicate: count how many distinct 60-second buckets have at least one hit
    buckets = set()
    for h in hits:
        bucket = h.start_time // HIT_BUCKET_SECONDS
        buckets.add(bucket)

    unique_windows = len(buckets)

    if duration_sec and duration_sec > 0:
        duration_min = max(1.0, duration_sec / 60.0)
        return unique_windows / duration_min
    else:
        # No duration: use raw unique windows as score
        # This still ranks by hit density but isn't normalized
        log.debug(f"[classify] No duration for stream — using raw bucket count")
        return float(unique_windows)


def get_flagged_summary(cfg: ArcConfig, db: DB) -> list[dict]:
    """
    Return a sorted list of flagged stream summaries (for reporting/debugging).
    Each dict has: vod_id, title, date, density_score, total_hits, best_url
    """
    flagged = db.get_flagged_streams(cfg.arc_name)
    summary = []
    for stream, arc_result in flagged:
        url = db.get_best_source_url(stream.dgg_vod_id)
        summary.append({
            "vod_id": stream.dgg_vod_id,
            "title": stream.title,
            "date": stream.date,
            "density_score": arc_result.density_score,
            "total_hits": arc_result.total_hits,
            "best_url": url or f"https://dggvods.dev/vods/{stream.dgg_vod_id}",
        })
    return sorted(summary, key=lambda x: x["density_score"], reverse=True)


def print_classification_report(cfg: ArcConfig, db: DB):
    """Print a human-readable classification report to stdout."""
    counts = db.arc_result_count(cfg.arc_name)
    flagged_list = get_flagged_summary(cfg, db)

    print(f"\n=== Classification Report: {cfg.display_name} ===")
    print(f"Total streams scored:  {counts['total']}")
    print(f"Flagged as arc streams: {counts['flagged']}")
    print(f"Threshold: {cfg.thresholds.arc_flag_threshold} hits/min\n")

    if flagged_list:
        print(f"{'Date':<10} {'Density':>8} {'Hits':>6}  Title")
        print("-" * 70)
        for entry in flagged_list:
            print(
                f"{entry['date']:<10} "
                f"{entry['density_score']:>8.2f} "
                f"{entry['total_hits']:>6}  "
                f"{entry['title'][:45]}"
            )
