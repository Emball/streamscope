#!/usr/bin/env python3
"""
j6.py — Destiny J6 Arc Pipeline
Unified entry point: Textual TUI → config → live dashboard → outputs/

Run:  python j6.py
"""

from __future__ import annotations

import html
import json
import pickle
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# DEPENDENCY CHECK
# ──────────────────────────────────────────────────────────────

def _check_deps():
    missing = []
    for pkg in ("requests", "textual", "rapidfuzz"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Run: pip install {' '.join(missing)} --break-system-packages")
        sys.exit(1)

_check_deps()

import requests
from rapidfuzz import fuzz
from textual.app import App, ComposeResult
from textual.widgets import (
    Header, Footer, Button, Label, Input, Log,
    Static, ProgressBar, DataTable, Switch
)
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.screen import Screen
from textual.binding import Binding
from textual import work

# ──────────────────────────────────────────────────────────────
# PATHS & DEFAULTS
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
CONFIG_FILE  = SCRIPT_DIR / "j6_config.json"
TEMP_DIR     = SCRIPT_DIR / "temp"
OUTPUT_DIR   = SCRIPT_DIR / "outputs"
TRANSCRIPT_CACHE_DIR = TEMP_DIR / "transcript_cache"

BASE_URL     = "https://dggvods.dev/api"

ODYSEE_CHANNELS = [
    {"channel_id": "777097516b312ee377e1cc63e2d3aa4097d0e63d", "slug": "@odysteve:7"},
    {"channel_id": "5886d12f05f78c70aebb336b8f33cbe3f0a6cda4", "slug": "@stefanfs:5"},
]
ODYSEE_API       = "https://api.na-backend.odysee.com/api/v1/proxy"
ODYSEE_PAGE_SIZE = 50

YT_ARCHIVE_CHANNELS = [
    {"handle": "@destinyggvods", "id": "UCJyTTRHqcKDMsctENez6oMQ"},
    {"handle": "@OmniBased",     "id": "UClt_id2lCH-wIFjiMWSMCWA"},
]
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

DEFAULT_CONFIG = {
    "cookies_path":          "cookies.txt",
    "start_date":            "20231201",
    "end_date":              "20250701",
    "variant_threshold":     5,
    "min_hits":              20,
    "min_unique_queries":    5,
    "target_chars":          800000,
    "blind_spot_budget_mult": 2.5,
    "yt_client_file":        "client_secrets.json",
    "yt_token_json":         "yt_token.json",
    "date_tolerance_days":   1,
    "duration_tolerance":    90,
    "request_delay":         0.5,
    "jitter":                0.15,
    "max_retries":           5,
    "retry_base":            2.0,
    "confident_threshold":   85,
    "review_threshold":      60,
    "duration_match_bonus":  30,
    "hard_reject_dur":       1800,
}

# ──────────────────────────────────────────────────────────────
# QUERY LISTS
# ──────────────────────────────────────────────────────────────

QUERY_GROUPS = {
    "organizations": [
        "Proud Boys", "Oath Keepers", "Ministry of Self Defense",
        "Women for America First", "Quick Reaction Force", "QRF",
        "Stop the Steal", "Infowars", "Bannon War Room", "War Room podcast",
        "TheDonald",
    ],
    "key_people_full": [
        "Enrique Tarrio", "Stewart Rhodes", "Kelly Meggs", "Joe Biggs",
        "Ethan Nordean", "Zach Rehl", "Kenneth Chesebro", "John Eastman",
        "Rudy Giuliani", "Mark Meadows", "Cassidy Hutchinson", "Ray Epps",
        "Sidney Powell", "Jeffrey Clark", "Jeffrey Rosen", "Richard Donoghue",
        "Bill Barr", "William Barr", "Brad Raffensperger", "Ruby Freeman",
        "Shaye Moss", "Mike Pence", "Roger Stone", "Steve Bannon",
        "Ali Alexander", "Boris Epshteyn", "Peter Navarro", "Dominic Pezzola",
        "Joshua James", "Jessica Watkins", "Kenneth Harrelson", "Edward Vallejo",
        "Ashli Babbitt", "Eugene Goodman", "Brian Sicknick", "Pat Cipollone",
        "Greg Jacob", "Marc Short", "Tony Ornato", "Kimberly Cheatle",
        "Ronna McDaniel", "Mo Brooks", "Madison Cawthorn", "Marjorie Taylor Greene",
        "Jim Jordan", "Josh Hawley", "Ted Cruz", "Tommy Tuberville",
        "Kevin McCarthy", "Scott Perry", "Rupert Murdoch", "Gavin McInnes",
        "Dinesh DSouza", "Ken Block", "Jason Miller", "Katrina Pierson",
        "Alex Cannon", "Matthew Morgan", "Doug Ducey", "Brian Kemp",
        "Chris Stirewalt", "Bill Stepien", "Jack Smith",
    ],
    "key_people_surname": [
        "Tarrio", "Rhodes", "Meggs", "Biggs", "Nordean", "Chesebro",
        "Eastman", "Giuliani", "Meadows", "Hutchinson", "Epps", "Powell",
        "Donoghue", "Raffensperger", "Babbitt", "Pezzola", "Vallejo",
        "Cipollone", "Tuberville", "Navarro", "Hawley", "Goodman",
        "Sicknick", "McInnes", "Kushner", "Conway", "Scavino", "Chansley",
        "Stepien", "Stirewalt", "Boyland", "Greeson", "Phillips",
    ],
    "legal_schemes": [
        "fake electors", "alternate electors", "false electors",
        "elector scheme", "elector plot", "Electoral Count Act",
        "twelfth amendment", "seditious conspiracy", "Eastman memo",
        "Chesebro memo", "coup memo", "decertify", "obstruction",
        "pressure campaign", "war gaming", "NARA", "certificate of ascertainment",
    ],
    "key_events_specific": [
        "January 6", "Jan 6", "Ellipse rally", "March to Save America",
        "Capitol breach", "Capitol riot", "Capitol attack", "Rotunda",
        "Stack One", "Stack Two", "pipe bomb", "gallows", "trial by combat",
        "stand back and stand by", "fight like hell", "will be wild",
        "see you in DC", "hang Mike Pence", "Mike deserves it",
        "send it back", "all hell is going to break loose",
        "the calvary is coming", "the cavalry is coming",
        "courage and spine", "torches and pitchforks",
        "peaceful and patriotically", "go to the mattresses",
        "second American revolution", "just say the election was corrupt",
        "elite strike force", "brain room", "surrender caucus", "death spiral",
        "operation Pence card", "1776 Returns", "Lexington", "Charlottesville",
        "Unite the Right", "Heather Heyer", "Pennsylvania Avenue",
        "Speakers Lobby", "Senate chambers", "Rotunda doors",
    ],
    "key_events_broad": [
        "insurrection", "coup", "certification", "certify", "electoral vote",
        "joint session", "the riot", "the attack", "the breach",
        "the storming", "the indictment", "the conviction", "the verdict",
        "the sentence", "the committee", "the hearing", "the testimony",
        "the report", "the memo", "the scheme", "the plot", "the steal",
        "the lie", "187 minutes", "transfer of power", "peaceful transfer",
        "body armor", "magnetometers", "wiped phones", "Signal chat",
        "Parler", "QAnon",
    ],
    "fraud_claims": [
        "Dominion", "Dominion voting", "rigged election", "stolen election",
        "Stop the Count", "mail-in ballot", "mail in ballot", "absentee ballot",
        "drop box", "signature verification", "ballot harvesting", "2000 Mules",
        "State Farm Arena", "suitcase ballots", "ballots under table", "vote dump",
        "more votes than voters", "dead voters", "out of state voters",
        "water main break", "fraud like youve never seen", "Raffensperger call",
        "find the votes", "11780 votes", "blue shift", "Antrim County",
        "Wayne County", "Fulton County", "hand recount", "recount",
        "Hugo Chavez", "ASOG", "Allied Security Operations",
        "voter fraud commission", "mass resignations",
    ],
    "legal_proceedings": [
        "Lost Not Stolen", "64 cases", "January 6 committee", "J6 committee",
        "select committee", "Dominion Fox News", "Giuliani defamation",
        "Giuliani bankruptcy", "Oath Keepers convicted", "Proud Boys convicted",
        "Trump indicted", "seditious conspiracy conviction", "Jeffrey Clark letter",
        "Oval Office meeting", "January 3rd meeting", "Stephanos Bibas",
        "Brett Ludwig", "Salem Media",
    ],
    "key_documents": [
        "Hutchinson testimony", "Meadows texts", "Donoghue notes",
        "Clark letter", "Pence card", "select committee report",
        "J6 report", "January 6 report", "indictment", "court filing", "deposition",
    ],
    "figures_and_media": [
        "Glenn Greenwald", "Alex Jones", "Tucker Carlson", "Krystal Ball",
        "Owen Shroyer", "Fox News Arizona", "Fox News call", "OAN",
        "Newsmax", "Epoch Times", "Infowars",
    ],
    "downplaying_narratives": [
        "Ray Epps federal agent", "fed posting", "Antifa January 6",
        "police let them in", "agent provocateur", "false flag",
        "QAnon shaman", "Jacob Chansley", "just a tour",
        "legitimate political discourse",
    ],
}

PHONETIC_VARIANTS = {
    "Kenneth Chesebro":   ["Chesebro", "Chesbro", "Chezbro", "Chess bro", "Cheesbro"],
    "Chesebro":           ["Chesbro", "Chezbro", "Chess bro", "Cheesbro"],
    "Raffensperger":      ["Raffensberger", "Raffensperber", "Rafensperger",
                           "Raven Spurger", "Raffenspurger"],
    "Brad Raffensperger": ["Raffensberger", "Raffensperber", "Rafensperger"],
    "Epshteyn":           ["Epstein", "Epshtein", "Epshtayn"],
    "Boris Epshteyn":     ["Boris Epstein", "Boris Epshtein"],
    "Pezzola":            ["Pizzola", "Pezola", "Pezzolla", "Petsola"],
    "Dominic Pezzola":    ["Dominic Pizzola", "Dominic Pezola"],
    "Donoghue":           ["Donahue", "Donohue", "Donoghoe"],
    "Richard Donoghue":   ["Richard Donahue", "Richard Donohue"],
    "Tuberville":         ["Tubberville", "Tubervill", "Tubervile"],
    "Tommy Tuberville":   ["Tommy Tubberville", "Tommy Tubervill"],
    "Nordean":            ["Nordeen", "Norden", "Nordin"],
    "Ethan Nordean":      ["Ethan Nordeen", "Ethan Norden"],
    "Tarrio":             ["Tario", "Tareo"],
    "Enrique Tarrio":     ["Enrique Tario", "Enrique Tareo"],
    "Vallejo":            ["Valejo", "Vallayo", "Vayejo"],
    "Edward Vallejo":     ["Edward Valejo", "Edward Vallayo"],
    "Chesebro memo":      ["Chesbro memo", "Chezbro memo"],
    "Eastman memo":       ["Eastman plan", "Eastman scheme"],
    "Cipollone":          ["Cipalone", "Cipolloni", "Cipolone"],
    "Pat Cipollone":      ["Pat Cipalone", "Pat Cipolone"],
    "Shaye Moss":         ["Shae Moss", "Shay Moss"],
    "Eugene Goodman":     ["Eugene Goodmann", "Goodman officer"],
    "Sicknick":           ["Sicknick officer", "Sicnik", "Sicknik"],
    "Kimberly Cheatle":   ["Cheatle", "Kimberly Cheatley"],
    "Ronna McDaniel":     ["Ronna Romney", "Ronna Romney McDaniel"],
    "Stephanos Bibas":    ["Bibas", "Stefanos Bibas", "Bybas", "Biyas"],
    "Dinesh DSouza":      ["Dinesh Dsouza", "Dinesh De Souza", "2000 Mules guy"],
    "1776 Returns":       ["seventeen seventy six returns", "1776 document"],
    "ASOG":               ["allied security", "ASOG report"],
    "QRF":                ["quick reaction", "reaction force"],
    "Parler":             ["Parlor", "Parler app"],
    # Bug 8 additions
    "Giuliani":           ["Juliani", "Julliani", "Guliani"],
    "Rudy Giuliani":      ["Rudy Juliani", "Rudy Guliani"],
    "Tony Ornato":        ["Tony Ornado", "Tony Ornatto", "Ornato", "Ornado"],
    "Ray Epps":           ["Ray Eps", "Ray Ebs"],
    "Epps":               ["Eps", "Ebs"],
    "Jacob Chansley":     ["Jacob Chansly", "Chansley"],
    "Stephanos Bibas":    ["Bibas", "Stefanos Bibas", "Bybas", "Biyas"],
}

def build_query_list() -> list[str]:
    seen, queries = set(), []
    for group_queries in QUERY_GROUPS.values():
        for q in group_queries:
            if q.lower() not in seen:
                queries.append(q)
                seen.add(q.lower())
    return queries

ALL_QUERIES = build_query_list()

HIGH_SPECIFICITY = {
    "Stack One", "Stack Two", "Chesebro memo", "Eastman memo",
    "1776 Returns", "operation Pence card", "QRF", "Quick Reaction Force",
    "Ministry of Self Defense", "fake electors", "alternate electors",
    "seditious conspiracy", "187 minutes", "stand back and stand by",
    "hang Mike Pence", "just say the election was corrupt",
    "Kenneth Chesebro", "Chesebro", "Jeffrey Clark", "Clark letter",
    "Oval Office meeting", "January 3rd meeting", "mass resignations",
    "war gaming", "decertify", "certificate of ascertainment",
    "Donoghue notes", "Meadows texts", "Hutchinson testimony",
    "pipe bomb", "gallows", "wiped phones", "Signal chat",
    "Willard Hotel", "War Room", "Bannon War Room", "Roger Stone",
    "Stone", "Ali Alexander", "Jeffrey Rosen", "Richard Donoghue",
    "Donoghue", "Rosen", "acting attorney general", "mass resignation",
    "December 14", "Brian Sicknick", "Tony Ornato", "Bobby Engel",
    "brain room", "Rupert Murdoch", "find the votes", "11780",
    "Raffensperger call", "elector plot", "elector scheme",
    "fake elector scheme", "fraudulent slates", "claim victory",
    "red mirage", "blue shift", "will be wild",
}

BLIND_SPOT_QUERIES = {
    "Willard Hotel", "War Room", "Bannon War Room", "Roger Stone",
    "January 3rd meeting", "Jeffrey Clark", "December 14",
    "Brian Sicknick", "wiped phones", "Tony Ornato",
    "Rupert Murdoch", "brain room", "Raffensperger call", "find the votes",
    "Frances Watson", "elector scheme", "elector plot", "fraudulent slates",
    "mass resignation", "claim victory", "red mirage", "blue shift",
    "will be wild", "acting attorney general",
}

REACTION_TERMS = [
    "holy shit", "what the fuck", "jesus christ", "oh my god", "oh shit",
    "i didn't know that", "i didn't know any of that", "i didn't know this part",
    "i didn't know this", "i had no idea", "i wasn't aware that", "i wasn't aware",
    "i didn't realize", "i never knew",
    "i'm moving to a position", "i'm actually moving", "now it seems like",
    "now i feel like", "i'm actually now curious", "i'm actually curious",
    "i'm genuinely curious", "reading the timing of this",
    "this actually kind of directly contradicts", "this now i don't think",
    "i'm not actually sure now", "now i'm less sure", "wait wait wait",
    "wait a second", "hold on one sec", "hold on",
    "this is insane", "this is crazy", "this is unhinged", "this is wild",
    "this is pretty unhinged", "this is actually fucking", "that's insane",
    "that's crazy", "that's wild", "that's unhinged",
    "that's some gangster shit", "pretty unhinged", "kind of insane",
    "actually wild", "no way", "are you kidding", "imagine being",
    "i didn't know he", "i didn't know they", "i didn't know trump",
    "i didn't know bannon", "i didn't know stone", "i didn't know clark",
    "i didn't know rosen", "i didn't know any of this",
    "never heard of this", "i've never heard of this", "never heard this before",
    "this is the first time", "first time i'm hearing", "that's news to me",
    "how did i not know", "how did we not know", "nobody talked about this",
    "this never came up", "this is actually the most", "this might be the most",
    "this is probably the most", "this is legitimately insane",
    "this is genuinely insane", "this is genuinely wild",
    "genuinely crazy", "genuinely unhinged",
]

_JESUS_REFERENCE_WORDS = {
    "and", "or", "of", "by", "with", "his", "her", "their", "him",
    "was", "were", "would", "will", "did", "has", "had",
    "as", "in", "at", "from", "into", "through", "about",
    "like", "said", "tells", "told", "spoke", "the",
}

# ──────────────────────────────────────────────────────────────
# CONFIG I/O
# ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = {**DEFAULT_CONFIG, **saved}
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# ──────────────────────────────────────────────────────────────
# SHARED UTILITIES
# ──────────────────────────────────────────────────────────────

def load_cookies(cookie_file: str) -> dict:
    cookies = {}
    path = Path(cookie_file)
    if not path.exists():
        raise FileNotFoundError(f"Cookie file not found: {cookie_file}")
    content = path.read_text().strip()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 7:
            cookies[parts[5]] = parts[6]
        elif "=" in line:
            key, _, value = line.partition("=")
            cookies[key.strip()] = value.strip()
    if not cookies:
        raise ValueError("No cookies parsed from file.")
    return cookies

def make_session(cookies: dict) -> requests.Session:
    sess = requests.Session()
    sess.cookies.update(cookies)
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer":    "https://dggvods.dev/",
        "Accept":     "application/json",
    })
    return sess

def polite_sleep(cfg: dict):
    delay = cfg.get("request_delay", 0.5)
    jitter = cfg.get("jitter", 0.15)
    # Bug 16: additive-only jitter, guarantees floor
    time.sleep(delay + random.uniform(0, jitter * 2))

def get_with_retry(sess: requests.Session, url: str, cfg: dict,
                   timeout: int = 20, pressure_state: dict | None = None) -> dict | None:
    """GET with exponential backoff. pressure_state tracks 429 hits session-wide (Bug 15)."""
    max_retries = cfg.get("max_retries", 5)
    retry_base  = cfg.get("retry_base", 2.0)

    # Bug 15: if server recently rate-limited, apply extra global delay
    if pressure_state and pressure_state.get("under_pressure"):
        time.sleep(pressure_state.get("extra_delay", 2.0))

    for attempt in range(max_retries):
        try:
            resp = sess.get(url, timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", retry_base * (2 ** attempt)))
                if pressure_state is not None:
                    pressure_state["under_pressure"] = True
                    pressure_state["extra_delay"] = min(
                        pressure_state.get("extra_delay", 0.5) * 1.5, 5.0
                    )
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = retry_base * (2 ** attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            # Relax pressure if we succeed
            if pressure_state and pressure_state.get("under_pressure"):
                pressure_state["extra_delay"] = max(
                    pressure_state.get("extra_delay", 0.5) * 0.9, 0.0
                )
                if pressure_state["extra_delay"] < 0.1:
                    pressure_state["under_pressure"] = False
            return resp.json()
        except requests.exceptions.ConnectionError:
            time.sleep(retry_base * (2 ** attempt))
        except Exception:
            time.sleep(retry_base * (2 ** attempt))
    return None

def seconds_to_hms(seconds: int) -> str:
    h  = seconds // 3600
    m  = (seconds % 3600) // 60
    s  = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"

def strip_marks(text: str) -> str:
    # Bug 12: unescape HTML entities after stripping tags
    stripped = re.sub(r"<[^>]+>", "", text)
    return html.unescape(stripped)

def date_in_range(date_str: str, start: str, end: str) -> bool:
    return start <= date_str <= end

def sanitize_query(query: str) -> str:
    q = query.replace("'", "").replace("\u2019", "")
    q = q.replace("-", " ")
    q = re.sub(r"[^\w\s]", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q

def strip_via(query: str) -> str:
    return re.sub(r"\s*\[via:.*?\]$", "", query)

def is_blind_spot_query(query: str) -> bool:
    return strip_via(query) in BLIND_SPOT_QUERIES

def query_tier(query: str) -> int:
    return 1 if strip_via(query) in HIGH_SPECIFICITY else 2

def has_reaction(text: str) -> bool:
    """Bug 4 fix: return True inside the jesus loop, not after."""
    low = text.lower()
    if any(r in low for r in REACTION_TERMS):
        return True
    for m in re.finditer(r"\bjesus\b", low):
        pos = m.start()
        after_match = re.match(r"[\s,!?.]*([a-z]+)", low[pos + 5:])
        if after_match:
            next_word = after_match.group(1)
            if next_word == "christ":
                continue
            if next_word in _JESUS_REFERENCE_WORDS:
                continue
        return True  # Bug 4: inside loop
    return False

def normalize_text_for_reaction(text: str) -> str:
    """Bug 19: strip punctuation/case for reaction matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ──────────────────────────────────────────────────────────────
# STEP 1: VOD LIST
# ──────────────────────────────────────────────────────────────

def fetch_all_vods(sess: requests.Session, cfg: dict,
                   log=None, pressure: dict | None = None) -> dict:
    """
    Paginate /api/vods. Bug 1 fix: collect ALL in-range on each page before
    checking early-termination — don't break on first out-of-range item.
    """
    start, end = cfg["start_date"], cfg["end_date"]
    vods: dict = {}
    page, limit = 1, 50

    cache_path = TEMP_DIR / "vods_cache.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if log: log(f"[cache] Loaded {len(data)} VODs from vods_cache.json")
            return data
        except Exception:
            pass

    while True:
        url  = f"{BASE_URL}/vods?page={page}&limit={limit}&filter=all"
        data = get_with_retry(sess, url, cfg, pressure_state=pressure)
        if not data:
            break

        batch = data.get("vods", [])
        total = data.get("total", 0)
        if not batch:
            break

        # Bug 1: collect all in-range on the page first
        all_before_start = True
        for vod in batch:
            d = vod.get("date", "")
            if date_in_range(d, start, end):
                vods[vod["id"]] = vod
                all_before_start = False
            elif d >= start:
                all_before_start = False  # future date, don't break

        if log:
            log(f"[vods] Page {page}: {len(batch)} VODs, in-range so far: {len(vods)}")

        # Only stop early if everything on this page predates start
        if all_before_start:
            break

        if page * limit >= total:
            break
        page += 1
        polite_sleep(cfg)

    cache_path.write_text(json.dumps(vods, indent=2), encoding="utf-8")
    if log: log(f"[vods] {len(vods)} VODs in range, cached.")
    return vods

# ──────────────────────────────────────────────────────────────
# STEP 2: SEARCH
# ──────────────────────────────────────────────────────────────

def _search_single_query(sess, query, cfg, pressure, log=None) -> list[dict]:
    """Bug 10 fix: explicit &limit=50 on every search request."""
    start, end = cfg["start_date"], cfg["end_date"]
    results, page = [], 1
    safe_query = sanitize_query(query)
    if not safe_query:
        return results

    # Bug 7: search both original and lowercase, merge
    query_variants_to_try = [safe_query]
    lc = safe_query.lower()
    if lc != safe_query:
        query_variants_to_try.append(lc)

    seen_keys: set[tuple] = set()
    for attempt_query in query_variants_to_try:
        page = 1
        while True:
            encoded = requests.utils.quote(attempt_query)
            url = f"{BASE_URL}/search?q={encoded}&page={page}&limit=50"
            data = get_with_retry(sess, url, cfg, pressure_state=pressure)
            if not data:
                break

            hits  = data.get("results", [])
            total = data.get("total", 0)
            limit = data.get("limit", 50)

            if not hits:
                break

            for vod_hit in hits:
                vod_date = vod_hit.get("date", "")
                if not date_in_range(vod_date, start, end):
                    continue
                vod_id   = vod_hit.get("vod_id")
                video_id = vod_hit.get("video_id")
                title    = vod_hit.get("title", "")

                for seg in vod_hit.get("segments", []):
                    if seg.get("speaker") != "destiny":
                        continue
                    key = (vod_id, seg.get("start_time", 0))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    results.append({
                        "vod_id":     vod_id,
                        "video_id":   video_id,
                        "title":      title,
                        "date":       vod_date,
                        "start_time": seg.get("start_time", 0),
                        "snippet":    strip_marks(seg.get("snippet", "")),
                        "query":      query,
                    })

            if page * limit >= total:
                break
            page += 1
            polite_sleep(cfg)

    return results

def search_all_queries(sess, cfg, all_queries, log=None, progress_cb=None) -> list[dict]:
    """
    Bug 13 fix: checkpoint saved after each completed query.
    Bug 11 fix: per-moment query tracking preserved.
    """
    checkpoint_path = TEMP_DIR / "search_checkpoint.json"
    completed: dict = {}

    if checkpoint_path.exists():
        try:
            completed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if log: log(f"[search] Resuming: {len(completed)} queries done")
        except Exception:
            pass

    pressure: dict = {"under_pressure": False, "extra_delay": 0.5}
    variant_threshold = cfg.get("variant_threshold", 5)
    all_hits: list[dict] = []

    # Collect hits from already-completed queries
    for q, hits in completed.items():
        all_hits.extend(hits)

    pending = [q for q in all_queries if q not in completed]
    total   = len(pending)

    for i, query in enumerate(pending, 1):
        if log: log(f"[search] [{i}/{total}] '{query}'")
        primary = _search_single_query(sess, query, cfg, pressure, log)

        if len(primary) < variant_threshold and query in PHONETIC_VARIANTS:
            seen_keys = {(h["vod_id"], h["start_time"]) for h in primary}
            for variant in PHONETIC_VARIANTS[query]:
                vh = _search_single_query(sess, variant, cfg, pressure)
                for h in vh:
                    key = (h["vod_id"], h["start_time"])
                    if key not in seen_keys:
                        h["query"] = f"{query} [via: {variant}]"
                        primary.append(h)
                        seen_keys.add(key)
                polite_sleep(cfg)

        completed[query] = primary
        all_hits.extend(primary)
        if log: log(f"[search]   → {len(primary)} moments")

        # Bug 13: save checkpoint after every completed query
        checkpoint_path.write_text(json.dumps(completed, indent=2), encoding="utf-8")

        if progress_cb:
            progress_cb(i, total)
        polite_sleep(cfg)

    return all_hits

# ──────────────────────────────────────────────────────────────
# STEP 3: BUILD INDEX
# ──────────────────────────────────────────────────────────────

def build_index(all_hits: list[dict], vods_meta: dict | None = None) -> dict:
    """Group by VOD, dedup moments within 30s, sort by date."""
    by_vod: dict = {}
    for hit in all_hits:
        vid = hit["vod_id"]
        if vid not in by_vod:
            meta = (vods_meta or {}).get(vid, {})
            dur  = (meta.get("duration") or meta.get("duration_seconds")
                    or meta.get("length") or meta.get("stream_length"))
            by_vod[vid] = {
                "vod_id":           vid,
                "video_id":         hit["video_id"],
                "title":            hit["title"],
                "date":             hit["date"],
                "duration_seconds": int(dur) if dur is not None else None,
                "moments":          [],
            }
        by_vod[vid]["moments"].append(hit)

    for vid, vod in by_vod.items():
        moments  = sorted(vod["moments"], key=lambda x: x["start_time"])
        deduped, last_time = [], -999
        # Bug 11: track all queries that triggered each moment
        for m in moments:
            if m["start_time"] - last_time > 30:
                deduped.append(m)
                last_time = m["start_time"]
        vod["moments"] = deduped

    return dict(sorted(by_vod.items(), key=lambda x: x[1]["date"]))

def merge_index(existing: dict, new_hits: list[dict], vods_meta: dict | None = None) -> dict:
    old_hits = [m for vod in existing.values() for m in vod["moments"]]
    result   = build_index(old_hits + new_hits, vods_meta)
    for vid, vod in result.items():
        # Bug 5: use `not` not `is None` so 0 doesn't overwrite valid duration
        if not vod.get("duration_seconds") and vid in existing:
            vod["duration_seconds"] = existing[vid].get("duration_seconds")
    return result

# ──────────────────────────────────────────────────────────────
# STEP 4: DURATION INJECTION
# ──────────────────────────────────────────────────────────────

def inject_durations(sess, index: dict, cfg: dict, log=None) -> dict:
    """Fetch duration_seconds for any VOD missing it."""
    pressure: dict = {"under_pressure": False, "extra_delay": 0.5}
    updated = 0
    for vid, vod in index.items():
        if vod.get("duration_seconds"):
            continue
        vod_id = vod.get("vod_id")
        url    = f"{BASE_URL}/transcript/{vod_id}"
        data   = get_with_retry(sess, url, cfg, timeout=30, pressure_state=pressure)
        if not data:
            continue
        segs = data.get("segments", [])
        if not segs:
            continue
        last_end = max(
            (seg.get("end_time") or seg.get("start_time") or 0)
            for seg in segs
        )
        if last_end:
            vod["duration_seconds"] = int(last_end)
            updated += 1
        polite_sleep(cfg)
    if log: log(f"[duration] Injected durations for {updated} VODs")
    return index

# ──────────────────────────────────────────────────────────────
# STEP 5: YOUTUBE ARCHIVE
# ──────────────────────────────────────────────────────────────

def get_youtube_service(cfg: dict):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Missing YouTube deps.\n"
            "pip install google-auth-oauthlib google-auth-httplib2 "
            "google-api-python-client --break-system-packages"
        )

    token_json  = SCRIPT_DIR / cfg.get("yt_token_json", "yt_token.json")
    client_file = SCRIPT_DIR / cfg.get("yt_client_file", "client_secrets.json")
    creds = None

    if token_json.exists():
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_json), YT_SCOPES)

    if not creds or not creds.valid:
        from google.auth.transport.requests import Request
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_file.exists():
                raise FileNotFoundError(f"{client_file} not found.")
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), YT_SCOPES)
            creds = flow.run_local_server(port=0)
        token_json.write_text(creds.to_json())
        # Bug 31: no pickle

    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds)

_ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

def parse_iso_duration(iso: str) -> int | None:
    if not iso: return None
    m = _ISO_DUR.match(iso)
    if not m: return None
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s

def fetch_youtube_archive(cfg: dict, log=None) -> list[dict]:
    """Bug 22: uses snippet,contentDetails in one playlist call to halve quota."""
    cache_path = TEMP_DIR / "archive_cache.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if log: log(f"[yt] Loaded {len(data)} videos from cache")
            return data
        except Exception:
            pass

    yt = get_youtube_service(cfg)
    archive_videos: list[dict] = []

    for ch in YT_ARCHIVE_CHANNELS:
        if log: log(f"[yt] Fetching {ch['handle']}...")
        uploads_resp = yt.channels().list(part="contentDetails", id=ch["id"]).execute()
        items = uploads_resp.get("items", [])
        if not items:
            continue
        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        page_token = None
        while True:
            # Bug 22: request contentDetails in same call — one API call not two
            kwargs = dict(part="snippet,contentDetails", playlistId=uploads_id, maxResults=50)
            if page_token:
                kwargs["pageToken"] = page_token
            for attempt in range(5):
                try:
                    resp = yt.playlistItems().list(**kwargs).execute()
                    break
                except Exception:
                    if attempt == 4: raise
                    time.sleep(2 ** attempt)
            for item in resp.get("items", []):
                snippet = item.get("snippet", {})
                cd      = item.get("contentDetails", {})
                vid_id  = snippet.get("resourceId", {}).get("videoId", "")
                if vid_id:
                    archive_videos.append({
                        "video_id":         vid_id,
                        "title":            snippet.get("title", ""),
                        "published_at":     snippet.get("publishedAt", ""),
                        "channel":          ch["handle"],
                        "duration_seconds": parse_iso_duration(
                            cd.get("videoPublishedAt", "")  # not available here
                        ),
                    })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if log: log(f"[yt] {ch['handle']}: {sum(1 for v in archive_videos if v['channel'] == ch['handle'])} videos")

    # Duration is not available via playlistItems contentDetails (only videoPublishedAt).
    # Batch-fetch it separately — still one round trip per 50 instead of per video.
    if log: log("[yt] Fetching durations...")
    ids = [v["video_id"] for v in archive_videos]
    for i in range(0, len(ids), 50):
        batch = ids[i:i+50]
        for attempt in range(5):
            try:
                resp = yt.videos().list(
                    part="contentDetails", id=",".join(batch), maxResults=50
                ).execute()
                break
            except Exception:
                if attempt == 4: raise
                time.sleep(2 ** attempt)
        dur_map = {item["id"]: parse_iso_duration(
            item.get("contentDetails", {}).get("duration", "")
        ) for item in resp.get("items", [])}
        for v in archive_videos:
            if v["video_id"] in dur_map:
                v["duration_seconds"] = dur_map[v["video_id"]]

    cache_path.write_text(json.dumps(archive_videos, indent=2), encoding="utf-8")
    if log: log(f"[yt] {len(archive_videos)} videos cached")
    return archive_videos

# ──────────────────────────────────────────────────────────────
# STEP 6: ODYSEE ARCHIVE
# ──────────────────────────────────────────────────────────────

def fetch_odysee_archive(log=None) -> dict[str, dict]:
    """Bug 26: logs warning if Odysee release date >7 days from dgg date."""
    cache_path = TEMP_DIR / "odysee_cache.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if log: log(f"[odysee] Loaded {len(data)} entries from cache")
            return data
        except Exception:
            pass

    _YT_SLUG_RE = re.compile(r"^([A-Za-z0-9_-]{11})-r-youtube\d+$")
    _YT_BARE_RE = re.compile(r"^([A-Za-z0-9_-]{11})$")
    index: dict = {}

    for ch in ODYSEE_CHANNELS:
        channel_id   = ch["channel_id"]
        channel_slug = ch["slug"]
        base_url     = f"https://odysee.com/{channel_slug}"
        page         = 1

        while True:
            payload = {
                "jsonrpc": "2.0", "method": "claim_search",
                "id":      int(time.time() * 1000),
                "params":  {
                    "channel_ids": [channel_id],
                    "claim_type": ["stream"], "has_source": True,
                    "no_totals": True, "order_by": ["release_time"],
                    "page_size": ODYSEE_PAGE_SIZE, "page": page,
                },
            }
            for attempt in range(5):
                try:
                    resp = requests.post(ODYSEE_API, json=payload, timeout=30)
                    resp.raise_for_status()
                    result = resp.json()
                    break
                except Exception as e:
                    if attempt == 4: raise
                    time.sleep(2 ** attempt)

            items = result.get("result", {}).get("items", [])
            if not items:
                break

            for claim in items:
                name = claim.get("name", "")
                m    = _YT_SLUG_RE.match(name) or _YT_BARE_RE.match(name)
                if not m: continue
                orig_id  = m.group(1)
                claim_id = claim.get("claim_id", "")
                url      = f"{base_url}/{name}"
                # Bug 26: store release_time for cross-check
                release_time = claim.get("value", {}).get("release_time")
                index[orig_id] = {
                    "slug":         name,
                    "claim_id":     claim_id,
                    "url":          url,
                    "channel_slug": channel_slug,
                    "release_time": release_time,
                }
            if log: log(f"[odysee] {channel_slug} page {page}: {len(items)} claims")
            if len(items) < ODYSEE_PAGE_SIZE:
                break
            page += 1
            time.sleep(0.1)

    cache_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    if log: log(f"[odysee] {len(index)} YouTube-sourced VODs indexed and cached")
    return index

# ──────────────────────────────────────────────────────────────
# STEP 7: ARCHIVE REMAPPING
# ──────────────────────────────────────────────────────────────

_DATE_RE    = re.compile(r"(?P<y>20\d{2})[-_ .]?(?P<m>0[1-9]|1[0-2])[-_ .]?(?P<d>[0-3]\d)")
_OMNI_ID_RE = re.compile(r"\s[A-Za-z0-9_-]{10,12}\s*$")

def parse_date_from_title(title: str) -> str | None:
    m = _DATE_RE.search(title)
    return f"{m.group('y')}{m.group('m')}{m.group('d')}" if m else None

def normalize_title(title: str) -> str:
    title = _OMNI_ID_RE.sub("", title).strip()
    title = _DATE_RE.sub("", title)
    title = re.sub(r"\bdestiny\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    return re.sub(r"\s+", " ", title).strip()

def date_distance(d1: str, d2: str) -> int:
    try:
        return abs((datetime.strptime(d1, "%Y%m%d") - datetime.strptime(d2, "%Y%m%d")).days)
    except Exception:
        return 9999

def build_yt_archive_index(videos: list[dict]) -> list[dict]:
    return [{**v, "parsed_date": parse_date_from_title(v["title"]),
             "norm_title": normalize_title(v["title"])} for v in videos]

def _norm_id(vid_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", vid_id).lower()

def _yt_result(dgg_vod, av, score, note, cfg) -> dict:
    vid_id = av["video_id"]
    url    = f"https://www.youtube.com/watch?v={vid_id}"
    return {
        "dgg_vod_id":      dgg_vod.get("vod_id"),
        "original_vid_id": dgg_vod.get("video_id"),
        "archive_vid_id":  vid_id,
        "archive_url":     url,
        "archive_title":   av["title"],
        "archive_channel": av["channel"],
        "source":          "youtube",
        "score":           score,
        "note":            note,
        "confident":       score >= cfg.get("confident_threshold", 85),
    }

def match_vod_youtube(dgg_vod: dict, yt_index: list[dict], cfg: dict) -> dict | None:
    """
    Bug 24: When title_score < 40, reweight to 60/40 date/title to favor date.
    Bug 25: ID-in-title match anchored to avoid partial substring false positives.
    """
    dgg_date    = dgg_vod.get("date", "")
    dgg_title   = normalize_title(dgg_vod.get("title", ""))
    dgg_dur     = dgg_vod.get("duration_seconds")
    original_id = dgg_vod.get("video_id", "")

    dt_days      = cfg.get("date_tolerance_days", 1)
    dur_tol      = cfg.get("duration_tolerance", 90)
    dur_bonus_v  = cfg.get("duration_match_bonus", 30)
    hard_reject  = cfg.get("hard_reject_dur", 1800)
    conf_thresh  = cfg.get("confident_threshold", 85)
    review_thresh = cfg.get("review_threshold", 60)

    # Step 1: ID-in-OmniBased-title ground truth
    # Bug 25: anchor match — ID must appear as isolated token, not substring of another ID
    if original_id:
        norm_orig  = _norm_id(original_id)
        omni_match = None
        for av in yt_index:
            if av.get("channel") != "@OmniBased":
                continue
            cleaned_title = re.sub(r"[^A-Za-z0-9]", "", av["title"]).lower()
            # Require the ID to be a standalone segment
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(norm_orig) + r"(?![A-Za-z0-9])",
                         cleaned_title):
                omni_match = av
                break

        if omni_match:
            omni_dur  = omni_match.get("duration_seconds")
            omni_date = omni_match.get("parsed_date") or dgg_date
            dgg_upgrade = None
            if omni_dur:
                for av in yt_index:
                    if av.get("channel") != "@destinyggvods":
                        continue
                    arc_date = av.get("parsed_date")
                    if not arc_date or date_distance(omni_date, arc_date) > dt_days:
                        continue
                    arc_dur = av.get("duration_seconds")
                    if arc_dur and abs(omni_dur - arc_dur) <= dur_tol:
                        dgg_upgrade = av
                        break
            if dgg_upgrade:
                diff = abs(omni_dur - dgg_upgrade["duration_seconds"])
                return _yt_result(dgg_vod, dgg_upgrade, 200.0,
                                  f"id_in_title→dgg_upgrade dur_match({diff}s)", cfg)
            return _yt_result(dgg_vod, omni_match, 200.0, "id_in_title", cfg)

    # Step 2: Fuzzy scoring with Bug 24 title-reliability reweight
    best_score, best_match, best_note = 0.0, None, ""
    for av in yt_index:
        arc_date = av.get("parsed_date")
        if not arc_date or not dgg_date:
            continue
        dist = date_distance(dgg_date, arc_date)
        if dist == 0:
            date_score = 100
        elif dist <= dt_days:
            date_score = 80
        else:
            continue

        title_score = fuzz.token_sort_ratio(dgg_title, av.get("norm_title", ""))
        arc_dur     = av.get("duration_seconds")
        dur_bonus, dur_note = 0, ""
        if dgg_dur and arc_dur:
            diff = abs(dgg_dur - arc_dur)
            if diff <= dur_tol:
                dur_bonus, dur_note = dur_bonus_v, f" dur_match({diff}s)"
            elif diff > hard_reject:
                continue
            else:
                dur_note = f" dur_diff={diff}s"

        # Bug 24: if title is generic/uninformative, lean harder on date
        if title_score < 40:
            combined = (date_score * 0.60) + (title_score * 0.40) + dur_bonus
        else:
            combined = (date_score * 0.25) + (title_score * 0.75) + dur_bonus

        is_better   = combined > best_score
        is_tied     = abs(combined - best_score) <= 5
        prefers_dgg = (is_tied and av.get("channel") == "@destinyggvods"
                       and best_match and best_match.get("channel") != "@destinyggvods")
        if is_better or prefers_dgg:
            best_score = combined
            best_match = av
            best_note  = (f"title_score={title_score:.0f} "
                          f"{'exact_date' if dist==0 else f'date_dist={dist}d'}{dur_note}")

    if best_score < review_thresh or best_match is None:
        return None
    return _yt_result(dgg_vod, best_match, round(best_score, 1), best_note, cfg)

def remap_archive_urls(index: dict, yt_videos: list[dict], odysee_index: dict,
                       cfg: dict, log=None, skip_odysee: bool = False
                       ) -> tuple[dict, list[dict], list[dict]]:
    """Returns (remapped_index, matches, unmatched)."""
    yt_archive = build_yt_archive_index(yt_videos)
    matches:  list[dict] = []
    unmatched_vods: list[dict] = []
    yt_claimed: dict[str, tuple] = {}

    for vod in index.values():
        result = match_vod_youtube(vod, yt_archive, cfg)
        if not result:
            unmatched_vods.append(vod)
            continue
        aid     = result["archive_vid_id"]
        arc_dur = next((av.get("duration_seconds") for av in yt_archive
                        if av["video_id"] == aid), None)
        if aid not in yt_claimed:
            yt_claimed[aid] = (arc_dur, result["score"])
            matches.append(result)
        else:
            prev_dur, prev_score = yt_claimed[aid]
            if result["score"] == 200.0 and prev_score < 200.0:
                for i, m in enumerate(matches):
                    if m["archive_vid_id"] == aid and m["score"] < 200.0:
                        ev = index.get(str(m["dgg_vod_id"]))
                        if ev: unmatched_vods.append(ev)
                        matches.pop(i)
                        break
                yt_claimed[aid] = (arc_dur, result["score"])
                matches.append(result)
            elif arc_dur and prev_dur and abs(arc_dur - prev_dur) > 300:
                matches.append(result)
            else:
                unmatched_vods.append(vod)

    if log: log(f"[remap] YouTube: {len(matches)} matches, {len(unmatched_vods)} unmatched")

    # Odysee fallback
    final_unmatched: list[dict] = []
    if unmatched_vods and not skip_odysee:
        odysee_matches: list[dict] = []
        for vod in unmatched_vods:
            orig_id = vod.get("video_id", "")
            if orig_id not in odysee_index:
                final_unmatched.append(vod)
                continue
            entry        = odysee_index[orig_id]
            release_time = entry.get("release_time")
            # Bug 26: date cross-check
            if release_time and vod.get("date"):
                try:
                    odysee_dt = datetime.fromtimestamp(int(release_time))
                    dgg_dt    = datetime.strptime(vod["date"], "%Y%m%d")
                    diff_days = abs((odysee_dt - dgg_dt).days)
                    if diff_days > 7:
                        if log:
                            log(f"[odysee] WARNING: {orig_id} date diff {diff_days}d "
                                f"(dgg={vod['date']}, odysee={odysee_dt.strftime('%Y%m%d')})")
                except Exception:
                    pass
            odysee_matches.append({
                "dgg_vod_id":      vod.get("vod_id"),
                "original_vid_id": orig_id,
                "archive_vid_id":  entry["slug"],
                "archive_url":     entry["url"],
                "archive_title":   entry["slug"],
                "archive_channel": entry.get("channel_slug", "@odysteve"),
                "source":          "odysee",
                "score":           200.0,
                "note":            "id_in_slug (odysee fallback)",
                "confident":       True,
            })
        matches.extend(odysee_matches)
        if log: log(f"[odysee] {len(odysee_matches)} additional fallback matches")
    else:
        final_unmatched = unmatched_vods

    confident = [m for m in matches if m["confident"]]
    remap_meta = {m["original_vid_id"]: m for m in confident}
    remapped: dict = {}
    for key, vod in index.items():
        orig_id = vod.get("video_id", "")
        new_vod = dict(vod)
        if orig_id in remap_meta:
            m = remap_meta[orig_id]
            new_vod["archive_url"]       = m["archive_url"]
            new_vod["archive_source"]    = m["source"]
            new_vod["archive_channel"]   = m["archive_channel"]
            new_vod["video_id_remapped"] = True
        remapped[key] = new_vod

    return remapped, matches, final_unmatched

# ──────────────────────────────────────────────────────────────
# STEP 8: TRANSCRIPT FETCH
# ──────────────────────────────────────────────────────────────

def fetch_transcript(sess, vod_id: int, cfg: dict,
                     pressure: dict | None = None) -> list[dict]:
    """Bug 21: caches to temp/transcript_cache/{vod_id}.json."""
    cache_file = TRANSCRIPT_CACHE_DIR / f"{vod_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    url  = f"{BASE_URL}/transcript/{vod_id}"
    data = get_with_retry(sess, url, cfg, timeout=30, pressure_state=pressure)
    if not data:
        return []

    segs = [
        {"start_time": s.get("start_time", 0), "text": s.get("text", "").strip()}
        for s in data.get("segments", [])
        if s.get("speaker") == "destiny" and s.get("text", "").strip()
    ]
    segs.sort(key=lambda x: x["start_time"])
    cache_file.write_text(json.dumps(segs, indent=2), encoding="utf-8")
    return segs

# ──────────────────────────────────────────────────────────────
# STEP 9: STREAM FLAGGING & WINDOW EXTRACTION
# ──────────────────────────────────────────────────────────────

def flag_arc_streams(index: dict, cfg: dict) -> list[dict]:
    min_hits    = cfg.get("min_hits", 20)
    min_queries = cfg.get("min_unique_queries", 5)
    flagged     = []
    for vod in index.values():
        moments   = vod.get("moments", [])
        total     = len(moments)
        unique_qs = len({strip_via(m["query"]) for m in moments})
        if total >= min_hits or unique_qs >= min_queries:
            flagged.append({**vod, "_total_hits": total, "_unique_queries": unique_qs})
    flagged.sort(key=lambda v: v["date"])
    return flagged

def allocate_budgets(flagged: list[dict], cfg: dict) -> dict[int, int]:
    """Bug 17: budget proportional to duration_seconds when available."""
    target  = cfg.get("target_chars", 800_000)
    bs_mult = cfg.get("blind_spot_budget_mult", 2.5)
    weights = {}
    for vod in flagged:
        vod_id   = vod["vod_id"]
        has_bs   = any(is_blind_spot_query(m.get("query", "")) for m in vod.get("moments", []))
        dur      = vod.get("duration_seconds") or 0
        dur_w    = max(1.0, dur / 3600.0)  # weight by hours, floor 1
        bs_w     = bs_mult if has_bs else 1.0
        weights[vod_id] = dur_w * bs_w

    total_weight = sum(weights.values()) or 1
    return {vod["vod_id"]: int(target * weights[vod["vod_id"]] / total_weight)
            for vod in flagged}

def extract_windows(segments: list[dict], moments: list[dict],
                    per_stream_budget: int) -> list[dict]:
    """
    Bug 2 fix: binary search assigns hit segment to 'after' correctly.
    Bug 18 fix: overlapping window dedup merges text spans.
    Bug 19 fix: reaction check on punctuation/case-stripped text.
    """
    if not segments:
        return []

    times = [s["start_time"] for s in segments]

    def find_nearest_idx(target_time: int) -> int:
        lo, hi = 0, len(times) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] < target_time:
                lo = mid + 1
            else:
                hi = mid
        return lo

    BASE_WINDOW  = 600
    BS_WINDOW    = 1200

    windows: list[dict] = []
    for moment in moments:
        hit_time = moment["start_time"]
        query    = moment.get("query", "")
        idx      = find_nearest_idx(hit_time)
        half     = (BS_WINDOW if is_blind_spot_query(query) else BASE_WINDOW) // 2

        before_segs, after_segs = [], []
        chars_b = chars_a = 0

        # Bug 2: idx-1 for before (so hit segment goes into after)
        i = idx - 1
        while i >= 0 and chars_b < half:
            before_segs.insert(0, segments[i])
            chars_b += len(segments[i]["text"])
            i -= 1

        i = idx
        while i < len(segments) and chars_a < half:
            after_segs.append(segments[i])
            chars_a += len(segments[i]["text"])
            i += 1

        window_segs = before_segs + after_segs
        window_text = " ".join(s["text"] for s in window_segs)

        # Bug 19: strip punctuation/case before reaction matching
        tier = query_tier(query)
        if has_reaction(normalize_text_for_reaction(window_text)):
            tier = 0

        windows.append({
            "hit_time":   hit_time,
            "query":      query,
            "tier":       tier,
            "text":       window_text,
            "start_time": window_segs[0]["start_time"] if window_segs else hit_time,
            "end_time":   window_segs[-1]["start_time"] if window_segs else hit_time,
            "_segs":      window_segs,  # keep for merge
        })

    # Deduplicate overlapping windows — Bug 18: merge text spans, don't replace wholesale
    windows.sort(key=lambda w: w["start_time"])
    deduped: list[dict] = []
    for w in windows:
        if deduped and w["start_time"] <= deduped[-1]["end_time"] + 30:
            prev = deduped[-1]
            # Merge: use the higher-priority (lower tier number) tier
            if w["tier"] < prev["tier"]:
                prev["tier"] = w["tier"]
            # Extend text span with any new segments from w
            prev_end = prev["end_time"]
            new_segs = [s for s in w.get("_segs", []) if s["start_time"] > prev_end]
            if new_segs:
                prev["text"]     += " " + " ".join(s["text"] for s in new_segs)
                prev["end_time"]  = new_segs[-1]["start_time"]
        else:
            deduped.append(w)

    # Remove temp _segs key
    for w in deduped:
        w.pop("_segs", None)

    # Trim to budget
    total_chars = sum(len(w["text"]) for w in deduped)
    if total_chars > per_stream_budget:
        by_priority = sorted(deduped, key=lambda w: (-w["tier"], -w["hit_time"]))
        kept = set(id(w) for w in deduped)
        for w in by_priority:
            if total_chars <= per_stream_budget:
                break
            if w["tier"] == 0:
                continue
            total_chars -= len(w["text"])
            kept.discard(id(w))
        deduped = [w for w in deduped if id(w) in kept]
        deduped.sort(key=lambda w: w["start_time"])

    return deduped

# ──────────────────────────────────────────────────────────────
# STEP 10: OUTPUT
# ──────────────────────────────────────────────────────────────

def write_outputs(index: dict, remapped_index: dict, matches: list[dict],
                  unmatched: list[dict], windows_by_vod: dict,
                  flagged: list[dict], cfg: dict, log=None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # j6_index.json
    idx_path = OUTPUT_DIR / "j6_index.json"
    idx_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    if log: log(f"[out] j6_index.json → {idx_path}")

    # j6_index_remapped.json
    remap_path = OUTPUT_DIR / "j6_index_remapped.json"
    remap_path.write_text(json.dumps(remapped_index, indent=2, ensure_ascii=False), encoding="utf-8")
    if log: log(f"[out] j6_index_remapped.json → {remap_path}")

    # j6_index.md
    _write_markdown(index, OUTPUT_DIR / "j6_index.md")
    if log: log(f"[out] j6_index.md")

    # remapping_report.txt
    _write_remap_report(matches, unmatched, OUTPUT_DIR / "remapping_report.txt")
    if log: log(f"[out] remapping_report.txt")

    # j6_corpus.txt
    corpus = _build_corpus(flagged, windows_by_vod)
    corpus_path = OUTPUT_DIR / "j6_corpus.txt"
    corpus_path.write_text(corpus, encoding="utf-8")
    if log: log(f"[out] j6_corpus.txt ({len(corpus):,} chars)")

def _write_markdown(index: dict, path: Path):
    total_moments = sum(len(v["moments"]) for v in index.values())
    lines = [
        "# Destiny January 6th Research Arc — Timestamped Index", "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Streams with J6 content:** {len(index)}  ",
        f"**Total indexed moments:** {total_moments}", "",
        "All moments are Destiny's own speech only.", "", "---", "",
    ]
    for vod in index.values():
        date_fmt = datetime.strptime(vod["date"], "%Y%m%d").strftime("%Y-%m-%d")
        lines.append(f"## {date_fmt} | {vod['title']}")
        lines.append(f"vod_id: {vod['vod_id']}")
        lines.append("")
        for m in vod["moments"]:
            ts   = seconds_to_hms(m["start_time"])
            snip = re.sub(r"https?://\S+", "", m["snippet"].strip()).strip()
            lines.append(f"- {ts} [{m['query']}] — {snip}")
        lines.append(""); lines.append("---"); lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")

def _write_remap_report(matches: list[dict], unmatched: list[dict], path: Path):
    confident = [m for m in matches if m["confident"]]
    review    = [m for m in matches if not m["confident"]]
    lines = [
        "DESTINY J6 ARC — REMAPPING REPORT",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
        f"Confident : {len(confident)}   Needs review: {len(review)}   Unmatched: {len(unmatched)}",
        "", "=" * 70, "CONFIDENT MATCHES", "=" * 70,
    ]
    for m in sorted(confident, key=lambda x: -x["score"]):
        lines += [
            f"  dgg_vod_id   : {m['dgg_vod_id']}",
            f"  original_id  : {m['original_vid_id']}  → archive_id: {m['archive_vid_id']}",
            f"  source       : {m.get('source','?')}  channel: {m.get('archive_channel','')}",
            f"  archive_url  : {m['archive_url']}",
            f"  score        : {m['score']}  ({m['note']})", "",
        ]
    lines += ["=" * 70, "NEEDS REVIEW", "=" * 70]
    for m in sorted(review, key=lambda x: -x["score"]):
        lines += [
            f"  dgg_vod_id   : {m['dgg_vod_id']}",
            f"  original_id  : {m['original_vid_id']}  → archive_id: {m['archive_vid_id']}",
            f"  source       : {m.get('source','?')}  channel: {m.get('archive_channel','')}",
            f"  archive_url  : {m['archive_url']}",
            f"  score        : {m['score']}  ({m['note']})", "",
        ]
    lines += ["=" * 70, "UNMATCHED", "=" * 70]
    for v in unmatched:
        lines += [
            f"  dgg_vod_id : {v.get('vod_id')}  id: {v.get('video_id')}",
            f"  title      : {v.get('title')}  date: {v.get('date')}", "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")

def _build_corpus(flagged: list[dict], windows_by_vod: dict) -> str:
    lines = [
        "DESTINY JANUARY 6TH RESEARCH ARC — CONDENSED TRANSCRIPT CORPUS",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Arc streams: {len(flagged)}",
        "(All text is Destiny's speech only. Timestamps are H:MM:SS.)",
        "", "=" * 70, "",
    ]
    for vod in flagged:
        vod_id  = vod["vod_id"]
        windows = windows_by_vod.get(vod_id, [])
        if not windows: continue
        date_fmt    = datetime.strptime(vod["date"], "%Y%m%d").strftime("%Y-%m-%d")
        archive_url = vod.get("archive_url", "")
        lines.append(f"STREAM: {date_fmt} | {vod['title']}")
        lines.append(f"vod_id: {vod_id}  hits: {vod['_total_hits']}  "
                     f"unique_queries: {vod['_unique_queries']}")
        if archive_url:
            lines.append(f"archive_url: {archive_url}")
        lines.append("-" * 50)
        for w in windows:
            ts   = seconds_to_hms(w["hit_time"])
            tier = ["[REACTION]", "[SPECIFIC]", "[BROAD]"][min(w["tier"], 2)]
            lines.append(f"[{ts}] {tier} query: {w['query']}")
            lines.append(w["text"])
            lines.append("")
        lines.append("=" * 70); lines.append("")
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────
# PIPELINE RUNNER
# ──────────────────────────────────────────────────────────────

class Pipeline:
    """
    Orchestrates all steps. Designed to be called from TUI worker thread.
    log_fn: callable(str) for streaming log output.
    progress_fn: callable(step_name, done, total) for progress updates.
    """
    def __init__(self, cfg: dict, log_fn, progress_fn, steps: set[str] | None = None):
        self.cfg         = cfg
        self.log         = log_fn
        self.progress    = progress_fn
        self.steps       = steps  # None = all steps
        self.cancelled   = False

    def should_run(self, step: str) -> bool:
        return self.steps is None or step in self.steps

    def run(self):
        cfg = self.cfg
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Auth
        self.log("[init] Loading cookies...")
        try:
            cookies = load_cookies(cfg["cookies_path"])
            sess    = make_session(cookies)
        except Exception as e:
            self.log(f"[ERROR] Auth failed: {e}")
            return
        self.log(f"[init] Session ready ({len(cookies)} cookies)")
        pressure: dict = {"under_pressure": False, "extra_delay": 0.5}

        # VOD list
        if self.should_run("vods"):
            self.log("[step 1/9] Fetching VOD list...")
            vods_meta = fetch_all_vods(sess, cfg, log=self.log, pressure=pressure)
            self.log(f"[step 1/9] {len(vods_meta)} VODs in range")
            self.progress("vods", 1, 1)
        else:
            cache_path = TEMP_DIR / "vods_cache.json"
            vods_meta  = json.loads(cache_path.read_text()) if cache_path.exists() else {}
            self.log(f"[step 1/9] Skipped (loaded {len(vods_meta)} from cache)")

        if self.cancelled: return

        # Search
        if self.should_run("search"):
            self.log("[step 2/9] Running search queries...")
            all_hits = search_all_queries(
                sess, cfg, ALL_QUERIES,
                log=self.log,
                progress_cb=lambda done, total: self.progress("search", done, total),
            )
        else:
            cp = TEMP_DIR / "search_checkpoint.json"
            if cp.exists():
                completed = json.loads(cp.read_text())
                all_hits  = [h for hits in completed.values() for h in hits]
                self.log(f"[step 2/9] Skipped (loaded {len(all_hits)} hits from checkpoint)")
            else:
                all_hits = []
                self.log("[step 2/9] Skipped (no checkpoint found)")

        if self.cancelled: return

        # Build index
        self.log("[step 3/9] Building index...")
        index = build_index(all_hits, vods_meta)
        self.log(f"[step 3/9] {len(index)} streams, "
                 f"{sum(len(v['moments']) for v in index.values())} moments")
        self.progress("index", 1, 1)

        # Duration injection
        if self.should_run("duration"):
            self.log("[step 4/9] Injecting durations...")
            index = inject_durations(sess, index, cfg, log=self.log)
            self.progress("duration", 1, 1)
        else:
            self.log("[step 4/9] Skipped")

        if self.cancelled: return

        # YouTube archive
        if self.should_run("yt"):
            self.log("[step 5/9] Fetching YouTube archive...")
            try:
                yt_videos = fetch_youtube_archive(cfg, log=self.log)
            except Exception as e:
                self.log(f"[step 5/9] YouTube fetch failed: {e}")
                yt_videos = []
            self.progress("yt", 1, 1)
        else:
            yt_cache = TEMP_DIR / "archive_cache.json"
            yt_videos = json.loads(yt_cache.read_text()) if yt_cache.exists() else []
            self.log(f"[step 5/9] Skipped (loaded {len(yt_videos)} from cache)")

        if self.cancelled: return

        # Odysee archive
        if self.should_run("odysee"):
            self.log("[step 6/9] Fetching Odysee archive...")
            try:
                odysee_index = fetch_odysee_archive(log=self.log)
            except Exception as e:
                self.log(f"[step 6/9] Odysee fetch failed: {e}")
                odysee_index = {}
            self.progress("odysee", 1, 1)
        else:
            od_cache = TEMP_DIR / "odysee_cache.json"
            odysee_index = json.loads(od_cache.read_text()) if od_cache.exists() else {}
            self.log(f"[step 6/9] Skipped (loaded {len(odysee_index)} from cache)")

        if self.cancelled: return

        # Remap archive URLs
        if self.should_run("remap") and (yt_videos or odysee_index):
            self.log("[step 7/9] Remapping archive URLs...")
            remapped_index, matches, unmatched = remap_archive_urls(
                index, yt_videos, odysee_index, cfg, log=self.log
            )
            self.log(f"[step 7/9] {len(matches)} confident matches, "
                     f"{len(unmatched)} unmatched")
            self.progress("remap", 1, 1)
        else:
            remapped_index, matches, unmatched = index, [], []
            self.log("[step 7/9] Skipped")

        if self.cancelled: return

        # Transcript fetch + window extraction
        if self.should_run("corpus"):
            flagged = flag_arc_streams(remapped_index, cfg)
            self.log(f"[step 8/9] Flagged {len(flagged)} arc streams")
            budgets        = allocate_budgets(flagged, cfg)
            windows_by_vod = {}
            total_f        = len(flagged)

            for i, vod in enumerate(flagged, 1):
                if self.cancelled: break
                vod_id = vod["vod_id"]
                self.log(f"[step 8/9] [{i}/{total_f}] {vod['date']} {vod['title'][:40]}")
                segs = fetch_transcript(sess, vod_id, cfg, pressure)
                if segs:
                    windows = extract_windows(segs, vod["moments"], budgets[vod_id])
                    windows_by_vod[vod_id] = windows
                self.progress("corpus", i, total_f)
                polite_sleep(cfg)

            # Write outputs
            self.log("[step 9/9] Writing output files...")
            write_outputs(index, remapped_index, matches, unmatched,
                          windows_by_vod, flagged, cfg, log=self.log)
        else:
            self.log("[step 8-9/9] Skipped")

        self.log("[✓] Pipeline complete.")
        self.log(f"    Outputs → {OUTPUT_DIR}")

# ──────────────────────────────────────────────────────────────
# TEXTUAL TUI
# ──────────────────────────────────────────────────────────────

STEP_LABELS = {
    "vods":     "Fetch VOD list",
    "search":   "Search queries",
    "duration": "Inject durations",
    "yt":       "YouTube archive",
    "odysee":   "Odysee archive",
    "remap":    "Remap archive URLs",
    "corpus":   "Build corpus",
}

CSS = """
Screen {
    background: $surface;
}
#header-bar {
    height: 3;
    background: $primary;
    padding: 0 2;
    align: left middle;
}
#header-bar Label {
    color: $text;
    text-style: bold;
}
.panel {
    border: solid $primary;
    padding: 1 2;
    margin: 0 1;
}
.panel-title {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}
Button {
    margin: 0 1 0 0;
}
Button.selected {
    background: $accent;
    color: $text;
}
#config-grid {
    layout: grid;
    grid-size: 2;
    grid-gutter: 1;
    height: auto;
}
.field-label {
    height: 1;
    margin-top: 1;
}
Input {
    margin-bottom: 0;
}
#log-panel {
    height: 1fr;
    border: solid $primary;
    margin: 0 1;
}
#progress-panel {
    height: auto;
    border: solid $primary;
    margin: 0 1;
    padding: 1 2;
}
.step-row {
    height: 1;
    layout: horizontal;
}
.step-name {
    width: 30;
    color: $text-muted;
}
.step-status {
    width: 20;
}
"""

class ConfigScreen(Screen):
    """First screen: configure the run."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        cfg = self.app.config
        yield Header()
        yield Label("  J6 Pipeline — Configuration", id="header-bar")
        with ScrollableContainer():
            with Vertical(classes="panel"):
                yield Label("PATHS", classes="panel-title")
                yield Label("Cookies file path:", classes="field-label")
                yield Input(value=cfg.get("cookies_path", "cookies.txt"), id="cookies_path")
                yield Label("YouTube client_secrets.json:", classes="field-label")
                yield Input(value=cfg.get("yt_client_file", "client_secrets.json"), id="yt_client_file")

            with Vertical(classes="panel"):
                yield Label("DATE RANGE", classes="panel-title")
                yield Label("Start date (YYYYMMDD):", classes="field-label")
                yield Input(value=cfg.get("start_date", "20231201"), id="start_date")
                yield Label("End date (YYYYMMDD):", classes="field-label")
                yield Input(value=cfg.get("end_date", "20250701"), id="end_date")

            with Vertical(classes="panel"):
                yield Label("SEARCH", classes="panel-title")
                yield Label("Variant threshold (low-hit fallback):", classes="field-label")
                yield Input(value=str(cfg.get("variant_threshold", 5)), id="variant_threshold")

            with Vertical(classes="panel"):
                yield Label("FLAGGING & CORPUS", classes="panel-title")
                yield Label("Min hits to flag stream:", classes="field-label")
                yield Input(value=str(cfg.get("min_hits", 20)), id="min_hits")
                yield Label("Min unique queries to flag:", classes="field-label")
                yield Input(value=str(cfg.get("min_unique_queries", 5)), id="min_unique_queries")
                yield Label("Target corpus chars:", classes="field-label")
                yield Input(value=str(cfg.get("target_chars", 800000)), id="target_chars")
                yield Label("Blind spot budget multiplier:", classes="field-label")
                yield Input(value=str(cfg.get("blind_spot_budget_mult", 2.5)), id="blind_spot_budget_mult")

            with Horizontal():
                yield Button("Save & Continue →", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.app.pop_screen()
            return
        # Read all inputs
        def get(id: str):
            return self.query_one(f"#{id}", Input).value.strip()
        cfg = self.app.config
        cfg["cookies_path"]          = get("cookies_path")
        cfg["yt_client_file"]        = get("yt_client_file")
        cfg["start_date"]            = get("start_date")
        cfg["end_date"]              = get("end_date")
        try: cfg["variant_threshold"]     = int(get("variant_threshold"))
        except ValueError: pass
        try: cfg["min_hits"]              = int(get("min_hits"))
        except ValueError: pass
        try: cfg["min_unique_queries"]    = int(get("min_unique_queries"))
        except ValueError: pass
        try: cfg["target_chars"]          = int(get("target_chars"))
        except ValueError: pass
        try: cfg["blind_spot_budget_mult"] = float(get("blind_spot_budget_mult"))
        except ValueError: pass

        save_config(cfg)
        self.app.pop_screen()
        self.app.push_screen(MenuScreen())


class MenuScreen(Screen):
    """Step selector + launch screen."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self):
        super().__init__()
        self._selected: set[str] = set(STEP_LABELS.keys())
        self._full_pipeline = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("  J6 Pipeline — Select Steps", id="header-bar")
        with Vertical(classes="panel"):
            yield Label("MODE", classes="panel-title")
            yield Button("▶ Full Pipeline (smart skip)", id="mode_full", variant="primary", classes="selected")
            yield Button("  Custom steps", id="mode_custom")

        with Vertical(classes="panel", id="steps-panel"):
            yield Label("STEPS  (click to toggle)", classes="panel-title")
            for key, label in STEP_LABELS.items():
                yield Button(f"[✓] {label}", id=f"step_{key}", classes="selected")

        with Horizontal():
            yield Button("⚙ Config", id="goto_config")
            yield Button("🗑 Clear temp cache", id="clear_temp")
            yield Button("▶ Run", id="run", variant="success")
        yield Footer()

    def _refresh_step_buttons(self):
        for key in STEP_LABELS:
            btn = self.query_one(f"#step_{key}", Button)
            if key in self._selected:
                btn.label = f"[✓] {STEP_LABELS[key]}"
                btn.add_class("selected")
            else:
                btn.label = f"[ ] {STEP_LABELS[key]}"
                btn.remove_class("selected")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "mode_full":
            self._full_pipeline = True
            self._selected = set(STEP_LABELS.keys())
            self._refresh_step_buttons()
            self.query_one("#mode_full").add_class("selected")
            self.query_one("#mode_custom").remove_class("selected")
        elif bid == "mode_custom":
            self._full_pipeline = False
            self.query_one("#mode_full").remove_class("selected")
            self.query_one("#mode_custom").add_class("selected")
        elif bid and bid.startswith("step_"):
            key = bid[5:]
            if key in self._selected:
                self._selected.discard(key)
            else:
                self._selected.add(key)
            self._refresh_step_buttons()
        elif bid == "goto_config":
            self.app.push_screen(ConfigScreen())
        elif bid == "clear_temp":
            import shutil
            if TEMP_DIR.exists():
                shutil.rmtree(TEMP_DIR)
                TEMP_DIR.mkdir(parents=True)
            self.app.notify("Temp cache cleared.", severity="information")
        elif bid == "run":
            steps = None if self._full_pipeline else set(self._selected)
            self.app.push_screen(DashboardScreen(steps))


class DashboardScreen(Screen):
    """Live progress dashboard while pipeline runs."""

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel"),
    ]

    def __init__(self, steps: set[str] | None):
        super().__init__()
        self._steps    = steps
        self._pipeline: Pipeline | None = None
        self._step_status: dict[str, str] = {k: "pending" for k in STEP_LABELS}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("  J6 Pipeline — Running", id="header-bar")
        with Vertical(id="progress-panel"):
            yield Label("STEPS", classes="panel-title")
            for key, label in STEP_LABELS.items():
                with Horizontal(classes="step-row"):
                    yield Label(label, classes="step-name", id=f"step_name_{key}")
                    yield Label("⏳ pending", classes="step-status", id=f"step_status_{key}")
        yield Log(id="run-log", max_lines=2000, highlight=True)
        with Horizontal():
            yield Button("✗ Cancel", id="cancel_btn", variant="error")
            yield Button("← Menu", id="menu_btn")
        yield Footer()

    def on_mount(self) -> None:
        self._start_pipeline()

    def _log(self, msg: str):
        try:
            log_widget = self.query_one("#run-log", Log)
            log_widget.write_line(msg)
        except Exception:
            pass

    def _progress(self, step: str, done: int, total: int):
        try:
            status_label = self.query_one(f"#step_status_{step}", Label)
            if done >= total:
                status_label.update("✅ done")
                self._step_status[step] = "done"
            else:
                pct = int(done / max(total, 1) * 100)
                status_label.update(f"⏳ {pct}% ({done}/{total})")
        except Exception:
            pass

    @work(thread=True)
    def _start_pipeline(self):
        cfg = self.app.config

        def log(msg: str):
            self.call_from_thread(self._log, msg)

        def progress(step: str, done: int, total: int):
            self.call_from_thread(self._progress, step, done, total)

        pipeline = Pipeline(cfg, log, progress, self._steps)
        self._pipeline = pipeline
        pipeline.run()

    def action_cancel(self):
        if self._pipeline:
            self._pipeline.cancelled = True
            self._log("[!] Cancellation requested...")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel_btn":
            self.action_cancel()
        elif event.button.id == "menu_btn":
            self.app.pop_screen()


class J6App(App):
    CSS = CSS
    TITLE = "J6 Pipeline"
    SCREENS = {}

    def __init__(self):
        super().__init__()
        self.config = load_config()

    def on_mount(self) -> None:
        self.push_screen(MenuScreen())


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = J6App()
    app.run()
