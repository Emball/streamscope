"""
core — StreamScope core layer.
Provides models, config, database, and utilities.
"""

from core.config import ArcConfig, load_arc, list_arcs, all_queries
from core.db import DB
from core.models import (
    Stream, Source, ArchiveVideo,
    Hit, SearchResult,
    ArcResult,
    TranscriptSegment, Transcript,
    Window, CorpusEntry,
)
from core.utils import (
    parse_date, dates_within,
    normalize_title, title_similarity,
    extract_video_id,
    durations_match, parse_iso8601_duration,
    fmt_timestamp, yt_url_with_timestamp,
    log_step,
)

__all__ = [
    # config
    "ArcConfig", "load_arc", "list_arcs", "all_queries",
    # db
    "DB",
    # models
    "Stream", "Source", "ArchiveVideo",
    "Hit", "SearchResult",
    "ArcResult",
    "TranscriptSegment", "Transcript",
    "Window", "CorpusEntry",
    # utils
    "parse_date", "dates_within",
    "normalize_title", "title_similarity",
    "extract_video_id",
    "durations_match", "parse_iso8601_duration",
    "fmt_timestamp", "yt_url_with_timestamp",
    "log_step",
]
