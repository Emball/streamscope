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

**Current:** `0.1.1.0`

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

**Search query logic:** AND logic — all words in a multi-word query must co-occur in the same sentence. Shorter query = broader net. Single-word queries match via substring, but whole-word tokenized — so "schizo" does NOT catch "schizoposting".

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
- Matching signals: direct video_id match (highest confidence) → date + title fuzzy match + duration bonus
- Same-day same-title streams: if duration differs beyond tolerance → distinct stream; otherwise → same stream
- Outputs confidence tier per match: confident / needs review / unmatched
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
7. **Same-day duplicate handling** — same title + same date + duration within tolerance → same stream; otherwise → distinct stream, flagged for review
8. **Date tolerance** — ±1 day allowed in archive matching (configurable)
9. **SQLite for index, JSON for exports** — index is queryable; outputs are portable

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

User's Original Instructions:

So I want to work. Uh, this is my second time attempting this. I've learned a lot of lessons in the first time I attempted it, and I'm ready to tackle it again, um, with a new Outlook and starting from scratch to help make everything cleaner. I'm gonna lay out the exact... I'm gonna try to be as detailed as possible so that we don't lose any information and we don't make any errors in the process. First thing we're gonna do after I tell you, you're gonna tell me what you think. You're gonna give me your thoughts. Um, flag anything you wanna flag, and then we're gonna write an Aegis on MD. We're gonna make the corresponding repo. And we're gonna get everything set up on GitHub so it's clean. Make sure we use clean versioning and set up, you know, my code rules and everything. But, anyway, um, so, essentially, through... towards the end of twenty twenty three and going into probably, like, uh, like, August, September twenty twenty four, there was a period of time where Destiny was doing j... January six insurrection research on his stream. Essentially, over the course of many months, you get... you got to see him slowly dive deeper and deeper into the rabbit hole and go from a... of thinking Trump, you know, is just innocent to thinking January sixth was an insane fucking thing that happened to America. It was a seditious conspiracy to overthrow the fucking government. And you got to see destiny react to these things in real time. You got to see him discover this information. You got you get to see the arc unfold. You get to absorb all the research with him. Essentially, this is the goal that I set out to accomplish. I want to create a tool that, via some config files, directly allows you to search specific key terms across his entire corpus of streams. Now you might be thinking, how the fuck are we going to do that? You know, that's already in hard... a hard task as it is, and it's been made even harder by the fact that a lot of those VODs from back in that time are now archived due to monetization problems on his channel, so they were deleted. But, um, luckily, there is two archive channels that have a very large amount of the VODs. Um, there's Omni based, which has two... twenty five hundred videos. I can link that channel when we get deeper. But most of them are only three sixty p, so that would be a lower priority, um, like, stream or or, like, a, um, or, like, a mirror. There's another one called Destiny VODs, which I'll I'll just send the link to that soon. That one has less streams, but they're all ten eighty p. And then finally, and then this is, I guess, the biggest piece of the puzzle, that we have Odyssey. This Odyssey channel called Audi Steve. and I can link that too. That also has a bunch of thoughts. No transcripts, but you could paginate through the through the site and find a bunch of audio URLs for anything that those two channels don't cover. And then the last source, is actually what inspired this whole concept because it made me realize that it's totally possible to do is this site called d... DDG VODs. And this site is, um, has, like, over eight hundred transcripts generated. It only has the audio, but it has over eight hundred transcripts generated of Destiny's old VODs. And you could search any key terms via the API and essentially use that to find whatever. The API is exposed and is not rape re limited at all, I can tell also in the links, uh, to that, it serves... you can... you send the the queue thing in the header, which is the query, and the way it works is, uh, essentially, whatever key terms are in the search query, it will look for those terms within a sentence. And if they coexist together, that's what it shows in the results. So, for example, if you were searching for someone saying, some variation of, uh, holy, um, holy shit. If you... or if you put holy shit, you would also get results for holy fucking shit. But if you put holy fucking shit, you would not get results for holy shit, if that makes sense. So that's the way the search works on on the API side. The API also serves other data. It serves the full transcript of any stream. You can paginate through the search results via the API. The... each stream has a the number associated... the DGG VODs ID and then the original video ID as well is also attached to the the headers somewhere in there. So, essentially, my idea is we build a comprehensive system to scrape all of this data and cross reference it to build a ground truth list of videos and VODs with their corresponding It is all unified and deduplicated and has mirrors for every stream and everything. And then once we have that list, we use the d d g vods API for every stream that they have, and they'll have a lot of them. We use that to search a list of key terms associated with whatever arc, you know, whatever destiny arc I'm specifically trying to find, essentially. for any stream that it... that is only on... like, let's say there's there's only a stream on the DestinyVos channel, we should be able to grab those transcripts with YTDLP and search the transcripts directly using the same logic that the DDGVods API uses. So, essentially, no stone is left unturned. We'll be able to search the the the... for, like, context around these terms. Essentially, it's the idea. And once we do that, we find every stream that mentions these key terms that are associated with the arc. I already have it premade. I'll send you the list of... in a bit. But, um, essentially, we use this information to define a threshold of whether a stream is January six arc content or not or just whatever arc value. It can be configurable to be defined for any arc. Essentially, I want it to be a living system so that it's not hard coded to be just for January sixth. But it will be defined... every... I want the... everything to be everything to be logged, everything to be tunable, everything highly verbose. So, like... and just to expand a bit more on how this system would work. Using Odyssey API, it paginates through the page, gets the, uh, duration and the ID. Okay. So... sorry. I'm very scared to bring this. A lot I have to talk about. So on the OmniBase channel, the original upload data associated with the videos, obviously, the reupload date, not the original upload date. That's very important to understand. However, the date of the original stream is in the title and is also in the title of the DestinyVos channel. The Omnibase channel also has... and at least, I believe, in most of the videos, maybe not all of them, but most of the video, it also has the original video... YouTube video ID of that stream in the title that needs to be extracted. And that could be used as a cross reference point to build this, like, corpus of of accurate stream data. And, like, we can use title matching, duration matching, ID matching, date matching. We can use all this data to essentially build a confidence threshold of matches. And after cross referencing all these different sources, it should be pretty foolproof. I can't imagine there wouldn't be any reason that there'd be a mismatch because, like, for example, I wanted to be able to handle a case where, let's say... well, here... here's a, you know, kind of a baroque scenario Let's say on Odyssey... let's say on dgGVods, there's a blind spot where there's three streams that are missing on the site for some reason. These three streams exist on Odyssey. However, only the... so we have that title information. and the ID in for... in Odyssey videos. So if that ID is not available within the same time frame, again, we're we're narrowing the search a shit ton by going in the specific time frame. That's how... why why this is even possible at all, actually. Um, but we use that duration data in that ID once we have those two data points on Odyssey. Those two data points and the title, by the way, all that data, those three data points are enough to find that same stream on either YouTube channel And most likely, one of those channels will have the transcript associated with it. So there's basically no path where we won't be able to get a transcript. There probably will be a few, maybe potentially, because Odyssey doesn't have transcripts. But what I'm saying is that if you... if everything is cross referenced, everything is linked, everything is unified, so there will never ever possibly be a path where something cannot be discovered. You understand what I mean? also, by the way, I forgot to mention Destiny VODs does not have YouTube IDs in the title, but does have date, but is in a different format. I think it's in... the the date... this... the the date of the date of the original broadcast is in the title, but it's a different format than Omnibase. Um, also, title matching. Title matching is very important. I think normalizing the title, removing special characters, all that. But the problem is, and this is very important, Destiny streams multiple times in a day. Sometimes he doesn't really change this, um, the the title, so that might get registered as a duplicate It is very important we need to have handling for that so that doesn't happen. If the duration differs between two different, you know, streams that have the same title on the same date, it's obviously a different stream. That's a pretty good signal. Um, also, sometimes there's a discrepancy in the dates where it might be one or two days, but two days... at most, one day behind the original broadcast date is that, you know, the video. So there should be some allowance there. Um, if the title and the duration are the same, but the day is one day off, then, obviously, it's probably the same stream. So, yeah, I think I've touched on everything that I need to touch on at this point. I'm trying to think if there's anything else. Um, Oh, yeah. So the point of essentially gathering all this data is once we once we identify every arc stream and we flag it as sufficiently arc related, as in there is enough density of relevant keywords, not like... let's say, for for the January six example, If he just said capital twenty times in the video... in the stream, that's obviously not a January sixth stream. But if he says capital, um, riot, proud boys, uh, lecture scheme, fake electors, all in one stream, you know, twenty times, then that's obviously in our extreme. So that is the type of logic that we need to do to flag it. And, essentially, once we flag a stream, be... at some at some certain threshold as an arc stream, we use that to refine the search. We pull the full transcript from, uh, whatever ideal stream is available, guess, the highest priority would be to do GVODs. Second would be desk... DestinyVODs, and the third would be Omnibase. Fourth would be Odyssey. In the in the order of, like, the likelyhood to have transcripts. We essentially use this data to get it to fetch each transcript of every arc stream within, like, a five hundred or six hundred degree character window around... of of more context around each search query. Again, the API of g voz exposes that information and UTBLP, we can grab the full transcript for searching and... or for refining. So in this refining step, we can get more context. We can... and then, essentially, what this will be is the corpus. This is a corpus of information that essentially contains the context of the maximum amount of context that we can possibly infuse to essentially index his mind state and how the arc unfolded and what streams contain what. And we can use this state... this corpus. It generates a corpus file. this corpus file should be calculated to be around, uh, eight hundred thousand characters or so to pack as much density and as much variety as we can in that character window to be processed by LLMs. So in the end, the goal would be, I'm gonna send this corpus, and I'm gonna say, hey. Create me an index with time stamps and... of of every moment where Destiny had a reaction or or give me a full index of the whole arc and an LLM can just do it.

Okay. I'm gonna go through each of these. Um, odyssey, in my experience, because the channels I am talking about don't actually have that many videos, or, like, it has, like, fifty pages of videos, but I never experienced any rate limiting with when when when when crawling it through the API. Or if you use the API to page in it, because the reason is because even in a browser, when you scroll... when... as you're scrolling through the page, it's hammering the API to load the videos in the same way that a scraper would. So it's not a problem. transcript availability. You say old streams, but, again, uh, this isn't a very specific date range of... so some... it it is true that some of the streams most likely won't have transcripts, but we can account for that at some point. Maybe... I highly doubt it'd be more than, like, ten or twenty streams. We could do manual transcription if if that really becomes such an issue. But... Since you're in... considering there's three sources of potential transcripts, I think that's pretty fucking good. And I highly doubt that we won't be able to find one there. But you're... you are right that the YouTube transcripts will be lower quality than the ones on d g g thoughts. The ones on d g g thoughts are actually very high quality. They even have tag... oh, yeah. I forgot to mention. Um, and, actually, You know, before, I was doing queries to search specifically. It has tag speakers. Like, I don't know what transcription service they used or how they did it, but it it tries to tag the speakers as as different people. The problem is I've, uh, I worry that there might be destiny talking at some point that was incorrectly flagged, and that would narrow our search unintentionally. So I would say maybe we actually don't do that this time around. Is the final corpus will be properly condensed regardless. Let's Yep. That sounds good. Um, Oh, yeah. Did you do VOD's API? It's completely unrate limited. You can hammer it as hard as you want. They didn't put any limitation on it because it's it's... some user runs it. It's it's not a problem. So, yeah, um, I think as you like, it's definitely a better choice. I was using JSON in the previous iteration of this concept, and that was pretty fragile. Um, so I'm thinking, like, the config can contain all the options, thresholds, and stuff, and it can also... like, you can define your arcs, like, in each arc you want to search for. You can define the thresholds for each arc and the key terms to search for for each arc and all that shit. That would be really sick. Um, let me go ahead and send you the, uh, how the, like, the ape... you're gonna have to help me reinvestigate the API calls for DGG VODs. because I forgot how to do that. But, uh, let me share you. Be sure you have all the URLs for the channels. I'll send you, like, snippets of the, um, what the, like, what the YouTube channel title structure for both channels is and the Odyssey channel title structure. Um, you're gonna have to also help me get the API calls for Odyssey. But, um, Oh, yeah. Let me let me send you the, um, the key terms as well that I have for January six, GARC.
- The arc flag logic is a density score, not a raw hit count — prevents single-word noise inflation
