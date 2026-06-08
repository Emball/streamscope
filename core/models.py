"""
core/models.py — StreamScope dataclasses.
All data flowing through the pipeline is typed via these structures.
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Ingestion-layer models
# ---------------------------------------------------------------------------

@dataclass
class Stream:
    """A single unique stream in the master index, keyed by DGGVods vod_id."""
    dgg_vod_id: int
    video_id: Optional[str]        # original YouTube video_id (11 chars)
    title: str
    date: str                      # YYYYMMDD normalized
    duration_sec: Optional[int]
    dgg_has_transcript: bool = False

    def __repr__(self):
        return f"<Stream vod={self.dgg_vod_id} date={self.date} title={self.title[:40]!r}>"


@dataclass
class Source:
    """
    One archive mirror of a Stream.
    Multiple Sources can point to the same Stream (dgg_vod_id).
    """
    dgg_vod_id: int
    source_type: str               # 'dggvods' | 'destinyggvods' | 'omnibased' | 'odysee'
    source_id: str                 # video_id or odysee claim_id
    url: str
    duration_sec: Optional[int]
    match_score: float = 0.0
    match_note: str = ""
    is_confident: bool = False
    id: Optional[int] = None       # DB auto-assigned


@dataclass
class ArchiveVideo:
    """
    Raw record from a YouTube archive channel or Odysee before unification.
    Used internally by ingest modules; not persisted directly.
    """
    source_type: str               # 'destinyggvods' | 'omnibased' | 'odysee'
    source_id: str                 # video_id or claim_id
    url: str
    title: str
    date: Optional[str]            # YYYYMMDD or None if unparseable
    duration_sec: Optional[int]
    extracted_video_id: Optional[str] = None  # video_id pulled from title (OmniBased)


# ---------------------------------------------------------------------------
# Search / hit models
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    """A keyword hit from the DGGVods search API for a specific stream."""
    dgg_vod_id: int
    arc_name: str
    query: str
    start_time: int                # seconds from stream start
    end_time: int
    snippet: str
    id: Optional[int] = None       # DB auto-assigned


@dataclass
class SearchResult:
    """
    Raw result from a single DGGVods /search query page.
    Wraps multiple hits across multiple vods.
    """
    query: str
    total: int
    hits: list[Hit] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Arc classification models
# ---------------------------------------------------------------------------

@dataclass
class ArcResult:
    """Classification output for one stream against one arc."""
    dgg_vod_id: int
    arc_name: str
    total_hits: int
    density_score: float           # hits per 1000 seconds of stream
    is_flagged: bool = False
    id: Optional[int] = None       # DB auto-assigned


# ---------------------------------------------------------------------------
# Transcript / corpus models
# ---------------------------------------------------------------------------

@dataclass
class TranscriptSegment:
    """One timestamped segment from a stream transcript."""
    start_time: int                # seconds
    end_time: int
    text: str


@dataclass
class Transcript:
    """Full transcript for a stream."""
    dgg_vod_id: int
    source: str                    # 'dggvods' | 'ytdlp_destinyggvods' | 'ytdlp_omnibased'
    segments: list[TranscriptSegment] = field(default_factory=list)

    def full_text(self) -> str:
        return " ".join(s.text for s in self.segments)


@dataclass
class Window:
    """
    Context window extracted around a cluster of hits in a stream.
    Used for corpus packing.
    """
    dgg_vod_id: int
    arc_name: str
    start_time: int                # window start (seconds)
    end_time: int                  # window end (seconds)
    text: str                      # transcript text within window
    hit_count: int                 # number of arc hits in this window
    source_url: Optional[str] = None
    stream_title: Optional[str] = None
    stream_date: Optional[str] = None


@dataclass
class CorpusEntry:
    """
    One packed entry in the final corpus file.
    Represents a context window ready for LLM consumption.
    """
    dgg_vod_id: int
    stream_title: str
    stream_date: str               # YYYYMMDD
    source_url: str
    window_start: int              # seconds
    window_end: int
    hit_count: int
    text: str

    def format(self) -> str:
        """Render as a corpus block with metadata header."""
        ts_start = _fmt_timestamp(self.window_start)
        ts_end = _fmt_timestamp(self.window_end)
        return (
            f"=== [{self.stream_date}] {self.stream_title} ===\n"
            f"URL: {self.source_url}?t={self.window_start}\n"
            f"Segment: {ts_start} – {ts_end}  |  hits: {self.hit_count}\n"
            f"---\n"
            f"{self.text}\n"
        )


def _fmt_timestamp(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
