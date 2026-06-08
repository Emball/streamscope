"""
core/utils.py — Shared utilities for StreamScope.

Covers:
- Date parsing and normalization to YYYYMMDD
- Title normalization for fuzzy matching
- YouTube video_id extraction from titles
- Timestamp formatting
- Fuzzy title similarity scoring
"""

import re
import logging
from typing import Optional

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# YouTube video_id: 11 chars, alphanumeric + hyphen + underscore
_YT_ID_RE = re.compile(r'\b([A-Za-z0-9_-]{11})\b')

# Title normalization: strip everything that isn't alphanumeric or space
_STRIP_RE = re.compile(r'[^a-z0-9\s]')
_MULTI_SPACE_RE = re.compile(r'\s+')

# Date patterns: each tuple is (regex, parser_fn → YYYYMMDD str or None)
# Ordered from most-specific to least.
_DATE_PATTERNS: list[tuple[re.Pattern, callable]] = []


def _register(pattern: str):
    """Decorator to register a date parser."""
    def decorator(fn):
        _DATE_PATTERNS.append((re.compile(pattern, re.IGNORECASE), fn))
        return fn
    return decorator


# --- Date parsers ---

MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'june': '06', 'july': '07', 'august': '08', 'september': '09',
    'october': '10', 'november': '11', 'december': '12',
}


@_register(r'(\d{4})[.\-_/](\d{2})[.\-_/](\d{2})')
def _parse_iso(m) -> Optional[str]:
    """YYYY-MM-DD or YYYY.MM.DD etc."""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if _valid_date(y, mo, d):
        return f"{y}{mo}{d}"
    return None


@_register(r'(\d{2})[.\-_/](\d{2})[.\-_/](\d{4})')
def _parse_dmy(m) -> Optional[str]:
    """DD-MM-YYYY or MM-DD-YYYY — prefer MM-DD-YYYY (US convention)."""
    a, b, y = m.group(1), m.group(2), m.group(3)
    # Try MM-DD-YYYY first
    if _valid_date(y, a, b):
        return f"{y}{a}{b}"
    # Try DD-MM-YYYY
    if _valid_date(y, b, a):
        return f"{y}{b}{a}"
    return None


@_register(r'(\d{8})')
def _parse_compact(m) -> Optional[str]:
    """YYYYMMDD compact."""
    s = m.group(1)
    y, mo, d = s[:4], s[4:6], s[6:8]
    if _valid_date(y, mo, d):
        return s
    return None


@_register(r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
           r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|'
           r'dec(?:ember)?)[.\s,_\-]+(\d{1,2})[,\s.]+(\d{4})')
def _parse_month_name_mdy(m) -> Optional[str]:
    """Month DD, YYYY (e.g. 'January 6, 2021')."""
    mo = MONTH_MAP.get(m.group(1).lower())
    d = m.group(2).zfill(2)
    y = m.group(3)
    if mo and _valid_date(y, mo, d):
        return f"{y}{mo}{d}"
    return None


@_register(r'(\d{1,2})[.\s,_\-]+'
           r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
           r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|'
           r'dec(?:ember)?)[,\s.]+(\d{4})')
def _parse_month_name_dmy(m) -> Optional[str]:
    """DD Month YYYY (e.g. '6 January 2021')."""
    d = m.group(1).zfill(2)
    mo = MONTH_MAP.get(m.group(2).lower())
    y = m.group(3)
    if mo and _valid_date(y, mo, d):
        return f"{y}{mo}{d}"
    return None


def _valid_date(y: str, mo: str, d: str) -> bool:
    try:
        yi, mi, di = int(y), int(mo), int(d)
        return 2000 <= yi <= 2030 and 1 <= mi <= 12 and 1 <= di <= 31
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Public date API
# ---------------------------------------------------------------------------

def parse_date(text: str) -> Optional[str]:
    """
    Extract and normalize the first recognizable date in `text` to YYYYMMDD.
    Returns None if no date found.
    """
    for pattern, parser in _DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            result = parser(m)
            if result:
                return result
    return None


def dates_within(a: str, b: str, tolerance_days: int) -> bool:
    """
    Return True if two YYYYMMDD strings are within `tolerance_days` of each other.
    Returns False if either string is None or unparseable.
    """
    if not a or not b:
        return False
    try:
        from datetime import date
        da = date(int(a[:4]), int(a[4:6]), int(a[6:8]))
        db = date(int(b[:4]), int(b[4:6]), int(b[6:8]))
        return abs((da - db).days) <= tolerance_days
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Title normalization
# ---------------------------------------------------------------------------

# Words to strip from titles before comparison (common Destiny stream filler)
_STOP_WORDS = frozenset({
    'destiny', 'stream', 'vod', 'live', 'clip', 'highlight',
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'is',
    'with', 'on', 'at', 'for', 'from', 'by', 'pt', 'part',
    'full', 'reupload', 'reup', 'watch',
})


def normalize_title(title: str) -> str:
    """
    Normalize a stream title for fuzzy comparison:
    - Lowercase
    - Strip non-alphanumeric (except spaces)
    - Remove stop words
    - Collapse whitespace
    - Strip leading/trailing whitespace
    """
    t = title.lower()
    t = _STRIP_RE.sub(' ', t)
    tokens = t.split()
    tokens = [tok for tok in tokens if tok not in _STOP_WORDS and not tok.isdigit()]
    t = ' '.join(tokens)
    t = _MULTI_SPACE_RE.sub(' ', t).strip()
    return t


def title_similarity(a: str, b: str) -> float:
    """
    Return a 0–100 similarity score between two (already normalized) titles.
    Uses token_set_ratio to handle word-order differences and partial overlaps.
    """
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b)


# ---------------------------------------------------------------------------
# YouTube video_id extraction
# ---------------------------------------------------------------------------

def extract_video_id(text: str) -> Optional[str]:
    """
    Extract the first YouTube video_id (11-char alphanumeric+_-) from a string.
    Returns None if not found.

    Skips IDs that are all-digit or all-alpha (too generic) — real YT IDs
    always contain a mix.
    """
    for m in _YT_ID_RE.finditer(text):
        candidate = m.group(1)
        has_digit = any(c.isdigit() for c in candidate)
        has_alpha = any(c.isalpha() for c in candidate)
        has_special = any(c in '-_' for c in candidate)
        # Require at least digits + (alpha or special) to reduce false positives
        if has_digit and (has_alpha or has_special):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def durations_match(a: Optional[int], b: Optional[int], tolerance_sec: int) -> bool:
    """Return True if two durations (in seconds) are within tolerance."""
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance_sec


def parse_iso8601_duration(duration_str: str) -> Optional[int]:
    """
    Parse an ISO 8601 duration string (e.g. 'PT1H23M45S') to total seconds.
    Used for YouTube Data API contentDetails.duration.
    """
    if not duration_str:
        return None
    pattern = re.compile(
        r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    )
    m = pattern.match(duration_str)
    if not m:
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def fmt_timestamp(seconds: int) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def yt_url_with_timestamp(video_id: str, seconds: int) -> str:
    """Build a YouTube watch URL with a ?t= timestamp."""
    return f"https://www.youtube.com/watch?v={video_id}&t={seconds}"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_step(step: str, msg: str, **kwargs):
    """Emit a structured log line for a pipeline step."""
    parts = [f"[{step}] {msg}"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    log.info("  ".join(parts))
