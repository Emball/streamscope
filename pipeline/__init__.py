"""
pipeline — StreamScope processing pipeline.
"""

from pipeline.search import run_search, clear_checkpoint
from pipeline.unify import unify_sources
from pipeline.classify import classify_streams, compute_density_score, get_flagged_summary
from pipeline.transcripts import acquire_transcripts, load_transcript
from pipeline.corpus import build_corpus

__all__ = [
    "run_search", "clear_checkpoint",
    "unify_sources",
    "classify_streams", "compute_density_score", "get_flagged_summary",
    "acquire_transcripts", "load_transcript",
    "build_corpus",
]
