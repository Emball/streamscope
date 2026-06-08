# AGENTS.md — StreamScope

## Project Overview

**StreamScope** is a multi-source stream corpus builder and arc classifier. Given a configurable arc definition (keyword groups, thresholds, date range), it:

1. Ingests VOD metadata from DGGVods, YouTube archive channels, and Odysee
2. Cross-references all sources into a unified, deduplicated master stream index
3. Searches transcripts (DGGVods API primary, yt-dlp fallback) for arc-relevant keyword hits
4. Scores each stream against the arc's keyword density rules
5. Extracts context windows around keyword hits from flagged streams
6. Packs a ~800K-char LLM-ready corpus file with timestamps, source URLs, and snippets

The system is **arc-agnostic**: the January 6th research arc is the pilot, but every parameter is config-driven. New arcs are added by dropping a new config file.

---

## Version

**Current:** `0.1.0.0`

Versioning format: `MAJOR.MINOR.PATCH.MICRO`
- 300+ lines changed → MAJOR
- 100+ lines changed → MINOR
- 20+ lines changed → PATCH
- 1+ lines changed → MICRO

---

## Repository Structure

```
streamscope/
├── AGENTS.md                  # This file
├── README.md
├── requirements.txt
├── streamscope.py             # CLI entry point
│
├── core/
│   ├── __init__.py
│   ├── config.py              # Config loader/validator, arc schema
│   ├── db.py                  # SQLite master index (streams, sources, transcripts)
│   ├── models.py              # Dataclasses: Stream, Source, Hit, Window, ArcResult
│   └── utils.py               # Date parsing, title normalization, fuzzy helpers
│
├── ingest/
│   ├── __init__.py
│   ├── dggvods.py             # DGGVods API: /vods, /search, /transcript
│   ├── youtube.py             # YouTube Data API v3: playlist fetch, duration inject
│   └── odysee.py              # Odysee JSONRPC: claim_search pagination
│
├── pipeline/
│   ├── __init__.py
│   ├── unify.py               # Cross-reference engine: ID/date/duration/title matching
│   ├── search.py              # Keyword search runner
│   ├── classify.py            # Arc classification: density scoring, threshold flagging
│   ├── transcripts.py         # Transcript fetch/cache: DGGVods → yt-dlp fallback
│   └── corpus.py              # Context window extraction, corpus packer (~800K chars)
│
├── arcs/
│   ├── j6.yaml                # January 6th arc definition (keywords, thresholds, dates)
│   └── example.yaml           # Blank arc template
│
├── data/
│   ├── streamscope.db         # SQLite master index (gitignored)
│   └── cache/                 # Per-run caches (gitignored)
│       ├── vods_cache.json
│       ├── search_checkpoint.json
│       ├── archive_cache.json
│       ├── odysee_cache.json
│       └── transcript_cache/
│           └── {vod_id}.json
│
└── output/
    ├── {arc}_index.json       # Full stream index with hit moments
    ├── {arc}_matches.txt      # Human-readable match report (confident + review)
    └── {arc}_corpus.txt       # LLM-ready corpus (~800K chars)
```

---

## Data Sources

### 1. DGGVods (`https://dggvods.dev/api`)
Primary source for transcripts and VOD metadata. No rate limiting. Unauthenticated.

| Endpoint | Purpose |
|---|---|
| `GET /vods?page=N&limit=50&filter=all` | Paginate all VODs. Fields: `vod_id`, `video_id`, `title`, `date`, `duration` |
| `GET /search?q=TERM&page=N&limit=50` | Search by keyword. Returns VOD hits with `segments[]` (each has `start_time`, `snippet`) |
| `GET /transcript/{vod_id}` | Full transcript. Returns `segments[]` with `start_time`, `end_time`, `text` |

Response shape (search):
```json
{
  "total": 120,
  "vods": [
    {
      "vod_id": 123,
      "video_id": "abc11chars_",
      "title": "...",
      "date": "20240115",
      "segments": [
        { "start_time": 3720, "snippet": "...marked text...", "end_time": 3740 }
      ]
    }
  ]
}
```

**Session headers required:**
```
User-Agent: Mozilla/5.0 ...
Referer: https://dggvods.dev/
Accept: application/json
```

**Search query logic:** AND logic — all words in query must co-occur in the same sentence. Shorter query = broader net. Single-word queries catch all inflections via substring match (whole-word tokenized, so "schizo" does NOT catch "schizoposting").

### 2. YouTube Archive Channels
- `@destinyggvods` — `UCJyTTRHqcKDMsctENez6oMQ` — 1080p, original broadcast date in title
- `@OmniBased` — `UClt_id2lCH-wIFjiMWSMCWA` — 360p, original YouTube video ID often in title

Uses **YouTube Data API v3** (OAuth2 or API key). Requires `client_secrets.json`.

Fields extracted: `video_id`, `title`, `published_at`, `duration_seconds` (via `contentDetails`).

Title date formats differ between channels — normalize all to `YYYYMMDD`.

### 3. Odysee (`https://api.na-backend.odysee.com/api/v1/proxy`)
JSONRPC API. Method: `claim_search`. No auth required. No rate limiting observed at normal pagination speeds.

Target channels:
- `@odysteve:7` — channel_id `777097516b312ee377e1cc63e2d3aa4097d0e63d`
- `@stefanfs:5` — channel_id `5886d12f05f78c70aebb336b8f33cbe3f0a6cda4`

Payload:
```json
{
  "jsonrpc": "2.0",
  "method": "claim_search",
  "params": {
    "channel_ids": ["<channel_id>"],
    "claim_type": ["stream"],
    "has_source": true,
    "no_totals": true,
    "order_by": ["release_time"],
    "page_size": 50,
    "page": 1
  }
}
```

Fields used: `claim_id`, `name` (extract YouTube video_id via regex), `value.release_time` (unix timestamp).

---

## Master Stream Index (SQLite)

Schema (managed by `core/db.py`):

```sql
-- One row per unique stream
CREATE TABLE streams (
    dgg_vod_id      INTEGER PRIMARY KEY,
    video_id        TEXT,           -- original YouTube video_id
    title           TEXT,
    date            TEXT,           -- YYYYMMDD normalized
    duration_sec    INTEGER,
    dgg_has_transcript INTEGER DEFAULT 0,
    created_at      TEXT
);

-- One row per archive source for a stream (multiple mirrors allowed)
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dgg_vod_id      INTEGER REFERENCES streams(dgg_vod_id),
    source_type     TEXT,           -- 'dggvods' | 'destinyggvods' | 'omnibased' | 'odysee'
    source_id       TEXT,           -- video_id or claim_id
    url             TEXT,
    duration_sec    INTEGER,
    match_score     REAL,
    match_note      TEXT,
    is_confident    INTEGER DEFAULT 0
);

-- Arc classification results
CREATE TABLE arc_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dgg_vod_id      INTEGER REFERENCES streams(dgg_vod_id),
    arc_name        TEXT,
    total_hits      INTEGER,
    density_score   REAL,
    is_flagged      INTEGER DEFAULT 0,
    flagged_at      TEXT
);

-- Keyword hit moments
CREATE TABLE hits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dgg_vod_id      INTEGER REFERENCES streams(dgg_vod_id),
    arc_name        TEXT,
    query           TEXT,
    start_time      INTEGER,
    end_time        INTEGER,
    snippet         TEXT
);
```

---

## Arc Config Schema (`arcs/*.yaml`)

```yaml
arc_name: "january_6"
display_name: "Destiny January 6th Research Arc"
date_range:
  start: "20231201"
  end: "20240901"

thresholds:
  confident: 85          # match score for archive URL mapping
  review: 60
  date_tolerance_days: 1
  duration_tolerance_sec: 90
  duration_match_bonus: 30

  # Arc flagging: a stream is flagged if keyword density exceeds this threshold
  arc_flag_threshold: 3.0

corpus:
  target_chars: 800000
  window_seconds: 300     # context window around each hit (±150s)
  max_windows_per_stream: 10

query_groups:
  # Defined in the arc's query module or inline here
  # Each group is a list of query strings
  organizations: [...]
  key_people: [...]
  ...
```

---

## Pipeline Phases

### Phase 1 — VOD Discovery
- `ingest/dggvods.py`: paginate `/vods`, filter to arc date range, cache to `vods_cache.json`
- Output: list of `{vod_id, video_id, title, date, duration_sec}`

### Phase 2 — Keyword Search
- `pipeline/search.py`: run all queries against DGGVods `/search`, paginating results
- Checkpoint after every completed query (`search_checkpoint.json`)
- Output: `{query → [hits]}` where each hit has `vod_id`, `start_time`, `snippet`

### Phase 3 — Archive Ingestion
- `ingest/youtube.py`: fetch both YT channels via Data API v3, extract video_id + date + duration
- `ingest/odysee.py`: paginate both Odysee channels via claim_search, extract video_id from slug
- Output: `archive_cache.json`, `odysee_cache.json`

### Phase 4 — Unification
- `pipeline/unify.py`: cross-reference DGGVods index against YT + Odysee
- Matching priority: direct video_id match (score 200) → date+title fuzzy (date 25%, title 75%, dur bonus) → date-weighted fallback if title_score < 40 (date 60%, title 40%)
- Dedup same-day same-title streams by duration delta (>= 2% → different stream)
- Confident threshold: score >= 85; Review: 60–84; Unmatched: < 60
- Output: master index in SQLite, match report

### Phase 5 — Arc Classification
- `pipeline/classify.py`: for each stream in index, compute density score from hit data
- Flag stream if density_score >= arc_flag_threshold
- Output: `arc_results` table populated

### Phase 6 — Transcript Acquisition
- `pipeline/transcripts.py`: for each flagged stream:
  1. Try DGGVods `/transcript/{vod_id}` (highest quality)
  2. Try yt-dlp on DestinyVODs URL (auto-captions)
  3. Try yt-dlp on OmniBased URL
  4. Odysee: no transcripts — audio only, skip unless manual transcription added
- Cache to `transcript_cache/{vod_id}.json`

### Phase 7 — Corpus Generation
- `pipeline/corpus.py`: for each flagged stream, extract context windows (±150s) around each hit
- Dedup overlapping windows
- Pack corpus to ~800K chars: highest-density windows first
- Output: `{arc}_corpus.txt`, `{arc}_index.json`

---

## Key Design Rules

1. **Everything is logged** — every step emits structured logs with step name, counts, and timing
2. **Everything is cached** — no API is hit twice for data already on disk
3. **Everything is resumable** — checkpoints after each query; phases are independently re-runnable
4. **Arc-agnostic** — no January 6th hardcoding anywhere outside `arcs/j6.yaml` and `arcs/j6_queries.py`
5. **Speaker tags ignored** — DGGVods transcript speaker tags are stripped; searching across all text
6. **No special characters in queries** — hyphens break search; plain text only
7. **Whole-word substring matching** — "schizo" does NOT catch "schizoposting"; add separately if needed
8. **Same-day duplicate handling** — same title + same date + duration within 2% → same stream; otherwise → distinct stream, flagged for review
9. **Date tolerance** — ±1 day allowed in archive matching (configurable)
10. **SQLite for index, JSON for exports** — index is queryable; outputs are portable

---

## Dependencies (`requirements.txt`)

```
requests>=2.31
rapidfuzz>=3.6
yt-dlp>=2024.1
pyyaml>=6.0
google-api-python-client>=2.120
google-auth-oauthlib>=1.2
```

---

## Commit & Version Rules

- Commit after every file creation or meaningful edit
- If multiple small files created in one session, one commit for the batch
- Bump version in this file and `streamscope.py` per threshold rules above
- Keep `requirements.txt` in sync with any new imports

---

## Notes for Claude

- DGGVods API: no auth, no rate limit — hammer freely
- Odysee API: JSONRPC POST to `https://api.na-backend.odysee.com/api/v1/proxy` — no rate limit observed
- YouTube Data API: OAuth2 required; `client_secrets.json` provided by user at runtime
- OmniBased video titles often contain original YouTube video_id — extract with regex `[A-Za-z0-9_-]{11}`
- DestinyVODs titles contain broadcast date but NOT video_id
- Date in titles: normalize all formats to YYYYMMDD before any comparison
- Corpus target: ~800K characters. Prioritize density (many hits in tight time window) over recency
- Do NOT use speaker tags from DGGVods transcripts for filtering — they are unreliable
- The arc flag logic is a density score, not a raw hit count — prevents single-word noise inflation
