"""
core/db.py — SQLite master index for StreamScope.

Manages:
- streams       — one row per unique stream (keyed by dgg_vod_id)
- sources       — one row per archive mirror of a stream
- arc_results   — arc classification output per stream
- hits          — keyword hit moments per stream per arc

All writes are wrapped in transactions. The DB file lives at
data/streamscope.db by default (configurable via DB_PATH env var or argument).
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from core.models import (
    ArcResult, Hit, Source, Stream, TranscriptSegment, Transcript, Window
)

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "streamscope.db"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS streams (
    dgg_vod_id          INTEGER PRIMARY KEY,
    video_id            TEXT,
    title               TEXT    NOT NULL,
    date                TEXT    NOT NULL,
    duration_sec        INTEGER,
    dgg_has_transcript  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dgg_vod_id      INTEGER NOT NULL REFERENCES streams(dgg_vod_id),
    source_type     TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    duration_sec    INTEGER,
    match_score     REAL    NOT NULL DEFAULT 0.0,
    match_note      TEXT    NOT NULL DEFAULT '',
    is_confident    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(dgg_vod_id, source_type, source_id)
);

CREATE TABLE IF NOT EXISTS arc_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dgg_vod_id      INTEGER NOT NULL REFERENCES streams(dgg_vod_id),
    arc_name        TEXT    NOT NULL,
    total_hits      INTEGER NOT NULL DEFAULT 0,
    density_score   REAL    NOT NULL DEFAULT 0.0,
    is_flagged      INTEGER NOT NULL DEFAULT 0,
    flagged_at      TEXT,
    UNIQUE(dgg_vod_id, arc_name)
);

CREATE TABLE IF NOT EXISTS hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dgg_vod_id  INTEGER NOT NULL REFERENCES streams(dgg_vod_id),
    arc_name    TEXT    NOT NULL,
    query       TEXT    NOT NULL,
    start_time  INTEGER NOT NULL,
    end_time    INTEGER NOT NULL,
    snippet     TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_streams_date       ON streams(date);
CREATE INDEX IF NOT EXISTS idx_streams_video_id   ON streams(video_id);
CREATE INDEX IF NOT EXISTS idx_sources_vod        ON sources(dgg_vod_id);
CREATE INDEX IF NOT EXISTS idx_sources_type       ON sources(source_type);
CREATE INDEX IF NOT EXISTS idx_arc_results_arc    ON arc_results(arc_name, is_flagged);
CREATE INDEX IF NOT EXISTS idx_hits_vod_arc       ON hits(dgg_vod_id, arc_name);
"""


# ---------------------------------------------------------------------------
# DB class
# ---------------------------------------------------------------------------

class DB:
    """
    Thin wrapper around a SQLite connection.
    Use as a context manager or call .close() when done.

    All public methods log their actions at DEBUG level.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or os.environ.get("STREAMSCOPE_DB", DEFAULT_DB_PATH))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"[db] Opening database: {self.path}")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._conn:
            self._conn.executescript(_SCHEMA)
        log.debug("[db] Schema initialized")

    def close(self):
        self._conn.close()
        log.debug("[db] Connection closed")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # -----------------------------------------------------------------------
    # streams
    # -----------------------------------------------------------------------

    def upsert_stream(self, s: Stream) -> bool:
        """
        Insert or update a stream row.
        Returns True if a new row was inserted, False if updated.
        """
        now = _now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT dgg_vod_id FROM streams WHERE dgg_vod_id = ?",
                (s.dgg_vod_id,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE streams
                    SET video_id=?, title=?, date=?, duration_sec=?,
                        dgg_has_transcript=?
                    WHERE dgg_vod_id=?
                """, (
                    s.video_id, s.title, s.date, s.duration_sec,
                    int(s.dgg_has_transcript), s.dgg_vod_id
                ))
                log.debug(f"[db] Updated stream vod_id={s.dgg_vod_id}")
                return False
            else:
                conn.execute("""
                    INSERT INTO streams
                        (dgg_vod_id, video_id, title, date, duration_sec,
                         dgg_has_transcript, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    s.dgg_vod_id, s.video_id, s.title, s.date, s.duration_sec,
                    int(s.dgg_has_transcript), now
                ))
                log.debug(f"[db] Inserted stream vod_id={s.dgg_vod_id} date={s.date}")
                return True

    def upsert_streams(self, streams: list[Stream]) -> tuple[int, int]:
        """Bulk upsert. Returns (inserted, updated)."""
        ins = upd = 0
        for s in streams:
            if self.upsert_stream(s):
                ins += 1
            else:
                upd += 1
        log.info(f"[db] upsert_streams: inserted={ins} updated={upd} total={ins+upd}")
        return ins, upd

    def get_stream(self, dgg_vod_id: int) -> Optional[Stream]:
        row = self._conn.execute(
            "SELECT * FROM streams WHERE dgg_vod_id = ?", (dgg_vod_id,)
        ).fetchone()
        return _row_to_stream(row) if row else None

    def get_streams_in_range(self, start: str, end: str) -> list[Stream]:
        """Return all streams with date BETWEEN start and end (YYYYMMDD inclusive)."""
        rows = self._conn.execute(
            "SELECT * FROM streams WHERE date >= ? AND date <= ? ORDER BY date",
            (start, end)
        ).fetchall()
        return [_row_to_stream(r) for r in rows]

    def get_all_streams(self) -> list[Stream]:
        rows = self._conn.execute(
            "SELECT * FROM streams ORDER BY date"
        ).fetchall()
        return [_row_to_stream(r) for r in rows]

    def stream_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0]

    def mark_transcript_available(self, dgg_vod_id: int):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE streams SET dgg_has_transcript=1 WHERE dgg_vod_id=?",
                (dgg_vod_id,)
            )

    # -----------------------------------------------------------------------
    # sources
    # -----------------------------------------------------------------------

    def upsert_source(self, s: Source) -> bool:
        """Insert or replace a source row. Returns True if new."""
        with self.transaction() as conn:
            existing = conn.execute("""
                SELECT id FROM sources
                WHERE dgg_vod_id=? AND source_type=? AND source_id=?
            """, (s.dgg_vod_id, s.source_type, s.source_id)).fetchone()

            if existing:
                conn.execute("""
                    UPDATE sources
                    SET url=?, duration_sec=?, match_score=?, match_note=?, is_confident=?
                    WHERE dgg_vod_id=? AND source_type=? AND source_id=?
                """, (
                    s.url, s.duration_sec, s.match_score, s.match_note,
                    int(s.is_confident), s.dgg_vod_id, s.source_type, s.source_id
                ))
                return False
            else:
                conn.execute("""
                    INSERT INTO sources
                        (dgg_vod_id, source_type, source_id, url, duration_sec,
                         match_score, match_note, is_confident)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    s.dgg_vod_id, s.source_type, s.source_id, s.url,
                    s.duration_sec, s.match_score, s.match_note, int(s.is_confident)
                ))
                return True

    def upsert_sources(self, sources: list[Source]) -> tuple[int, int]:
        ins = upd = 0
        for s in sources:
            if self.upsert_source(s):
                ins += 1
            else:
                upd += 1
        log.info(f"[db] upsert_sources: inserted={ins} updated={upd}")
        return ins, upd

    def get_sources(self, dgg_vod_id: int) -> list[Source]:
        rows = self._conn.execute(
            "SELECT * FROM sources WHERE dgg_vod_id=? ORDER BY match_score DESC",
            (dgg_vod_id,)
        ).fetchall()
        return [_row_to_source(r) for r in rows]

    def get_best_source_url(self, dgg_vod_id: int, prefer_type: str = 'destinyggvods') -> Optional[str]:
        """Return the URL of the best available archive for this stream."""
        # Try preferred type first
        row = self._conn.execute("""
            SELECT url FROM sources
            WHERE dgg_vod_id=? AND source_type=? AND is_confident=1
            LIMIT 1
        """, (dgg_vod_id, prefer_type)).fetchone()
        if row:
            return row['url']
        # Fall back to any confident source
        row = self._conn.execute("""
            SELECT url FROM sources
            WHERE dgg_vod_id=? AND is_confident=1
            ORDER BY match_score DESC LIMIT 1
        """, (dgg_vod_id,)).fetchone()
        if row:
            return row['url']
        # Last resort: any source
        row = self._conn.execute("""
            SELECT url FROM sources WHERE dgg_vod_id=?
            ORDER BY match_score DESC LIMIT 1
        """, (dgg_vod_id,)).fetchone()
        return row['url'] if row else None

    # -----------------------------------------------------------------------
    # hits
    # -----------------------------------------------------------------------

    def insert_hits(self, hits: list[Hit]):
        """Bulk insert keyword hits. Duplicates (same vod+arc+query+start_time) are ignored."""
        with self.transaction() as conn:
            conn.executemany("""
                INSERT OR IGNORE INTO hits
                    (dgg_vod_id, arc_name, query, start_time, end_time, snippet)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (h.dgg_vod_id, h.arc_name, h.query, h.start_time, h.end_time, h.snippet)
                for h in hits
            ])
        log.debug(f"[db] Inserted {len(hits)} hits")

    def get_hits(self, dgg_vod_id: int, arc_name: str) -> list[Hit]:
        rows = self._conn.execute("""
            SELECT * FROM hits WHERE dgg_vod_id=? AND arc_name=?
            ORDER BY start_time
        """, (dgg_vod_id, arc_name)).fetchall()
        return [_row_to_hit(r) for r in rows]

    def get_hits_for_arc(self, arc_name: str) -> dict[int, list[Hit]]:
        """Return all hits for an arc, grouped by dgg_vod_id."""
        rows = self._conn.execute("""
            SELECT * FROM hits WHERE arc_name=? ORDER BY dgg_vod_id, start_time
        """, (arc_name,)).fetchall()
        result: dict[int, list[Hit]] = {}
        for r in rows:
            h = _row_to_hit(r)
            result.setdefault(h.dgg_vod_id, []).append(h)
        return result

    def hit_count(self, arc_name: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM hits WHERE arc_name=?", (arc_name,)
        ).fetchone()[0]

    def vods_with_hits(self, arc_name: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT DISTINCT dgg_vod_id FROM hits WHERE arc_name=?", (arc_name,)
        ).fetchall()
        return [r[0] for r in rows]

    # -----------------------------------------------------------------------
    # arc_results
    # -----------------------------------------------------------------------

    def upsert_arc_result(self, r: ArcResult):
        now = _now() if r.is_flagged else None
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO arc_results
                    (dgg_vod_id, arc_name, total_hits, density_score, is_flagged, flagged_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dgg_vod_id, arc_name) DO UPDATE SET
                    total_hits=excluded.total_hits,
                    density_score=excluded.density_score,
                    is_flagged=excluded.is_flagged,
                    flagged_at=excluded.flagged_at
            """, (
                r.dgg_vod_id, r.arc_name, r.total_hits,
                r.density_score, int(r.is_flagged), now
            ))

    def upsert_arc_results(self, results: list[ArcResult]):
        for r in results:
            self.upsert_arc_result(r)
        flagged = sum(1 for r in results if r.is_flagged)
        log.info(f"[db] upsert_arc_results: total={len(results)} flagged={flagged}")

    def get_flagged_streams(self, arc_name: str) -> list[tuple[Stream, ArcResult]]:
        """Return (Stream, ArcResult) pairs for all flagged streams in this arc."""
        rows = self._conn.execute("""
            SELECT s.*, ar.id as ar_id, ar.total_hits, ar.density_score,
                   ar.is_flagged, ar.flagged_at
            FROM arc_results ar
            JOIN streams s ON s.dgg_vod_id = ar.dgg_vod_id
            WHERE ar.arc_name=? AND ar.is_flagged=1
            ORDER BY ar.density_score DESC
        """, (arc_name,)).fetchall()

        result = []
        for r in rows:
            stream = _row_to_stream(r)
            arc_result = ArcResult(
                dgg_vod_id=r['dgg_vod_id'],
                arc_name=arc_name,
                total_hits=r['total_hits'],
                density_score=r['density_score'],
                is_flagged=bool(r['is_flagged']),
                id=r['ar_id'],
            )
            result.append((stream, arc_result))
        return result

    def arc_result_count(self, arc_name: str) -> dict:
        row = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(is_flagged) as flagged
            FROM arc_results WHERE arc_name=?
        """, (arc_name,)).fetchone()
        return {'total': row['total'], 'flagged': row['flagged'] or 0}

    # -----------------------------------------------------------------------
    # Introspection / reporting
    # -----------------------------------------------------------------------

    def summary(self) -> dict:
        return {
            'streams': self.stream_count(),
            'sources': self._conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            'hits': self._conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0],
            'arc_results': self._conn.execute("SELECT COUNT(*) FROM arc_results").fetchone()[0],
        }


# ---------------------------------------------------------------------------
# Row converters
# ---------------------------------------------------------------------------

def _row_to_stream(r) -> Stream:
    return Stream(
        dgg_vod_id=r['dgg_vod_id'],
        video_id=r['video_id'],
        title=r['title'],
        date=r['date'],
        duration_sec=r['duration_sec'],
        dgg_has_transcript=bool(r['dgg_has_transcript']),
    )


def _row_to_source(r) -> Source:
    return Source(
        id=r['id'],
        dgg_vod_id=r['dgg_vod_id'],
        source_type=r['source_type'],
        source_id=r['source_id'],
        url=r['url'],
        duration_sec=r['duration_sec'],
        match_score=r['match_score'],
        match_note=r['match_note'],
        is_confident=bool(r['is_confident']),
    )


def _row_to_hit(r) -> Hit:
    return Hit(
        id=r['id'],
        dgg_vod_id=r['dgg_vod_id'],
        arc_name=r['arc_name'],
        query=r['query'],
        start_time=r['start_time'],
        end_time=r['end_time'],
        snippet=r['snippet'],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
