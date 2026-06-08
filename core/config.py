"""
core/config.py — Arc config loader and validator.

Loads a YAML arc file from arcs/, validates required fields,
and dynamically imports the query module if specified.
"""

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DateRange:
    start: str   # YYYYMMDD
    end: str     # YYYYMMDD


@dataclass
class Thresholds:
    # Archive matching
    confident: float = 85.0
    review: float = 60.0
    date_tolerance_days: int = 1
    duration_tolerance_sec: int = 90
    duration_match_bonus: float = 30.0
    # Arc flagging
    arc_flag_threshold: float = 3.0


@dataclass
class CorpusConfig:
    target_chars: int = 800_000
    window_seconds: int = 300       # context window around each hit (±150s)
    max_windows_per_stream: int = 10


@dataclass
class YouTubeChannelConfig:
    handle: str
    id: str
    resolution: int
    has_date_in_title: bool
    has_video_id_in_title: bool
    priority: int


@dataclass
class OdyseeChannelConfig:
    slug: str
    channel_id: str
    priority: int


@dataclass
class SourcesConfig:
    youtube_channels: list[YouTubeChannelConfig] = field(default_factory=list)
    odysee_channels: list[OdyseeChannelConfig] = field(default_factory=list)


@dataclass
class ArcConfig:
    arc_name: str
    display_name: str
    date_range: DateRange
    thresholds: Thresholds
    corpus: CorpusConfig
    sources: SourcesConfig

    # Query data (populated after load)
    query_groups: dict[str, list[str]] = field(default_factory=dict)
    reaction_terms: list[str] = field(default_factory=list)

    # Paths
    arc_dir: Optional[Path] = None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

ARCS_DIR = Path(__file__).parent.parent / "arcs"


def load_arc(arc_name: str) -> ArcConfig:
    """
    Load and validate an arc config by name.

    Looks for `arcs/{arc_name}.yaml`. If the YAML references a
    `query_module`, dynamically imports it and pulls QUERY_GROUPS
    and REACTION_TERMS from it.

    Raises FileNotFoundError if the YAML doesn't exist.
    Raises ValueError on schema violations.
    """
    yaml_path = ARCS_DIR / f"{arc_name}.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Arc config not found: {yaml_path}")

    log.info(f"[config] Loading arc: {yaml_path}")
    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    # --- Required top-level keys ---
    _require(raw, ['arc_name', 'display_name', 'date_range', 'thresholds', 'corpus'])

    # --- Date range ---
    dr = raw['date_range']
    _require(dr, ['start', 'end'], context='date_range')
    date_range = DateRange(start=str(dr['start']), end=str(dr['end']))

    # --- Thresholds ---
    th = raw.get('thresholds', {})
    thresholds = Thresholds(
        confident=float(th.get('confident', 85)),
        review=float(th.get('review', 60)),
        date_tolerance_days=int(th.get('date_tolerance_days', 1)),
        duration_tolerance_sec=int(th.get('duration_tolerance_sec', 90)),
        duration_match_bonus=float(th.get('duration_match_bonus', 30)),
        arc_flag_threshold=float(th.get('arc_flag_threshold', 3.0)),
    )

    # --- Corpus ---
    cp = raw.get('corpus', {})
    corpus = CorpusConfig(
        target_chars=int(cp.get('target_chars', 800_000)),
        window_seconds=int(cp.get('window_seconds', 300)),
        max_windows_per_stream=int(cp.get('max_windows_per_stream', 10)),
    )

    # --- Sources ---
    sources_raw = raw.get('sources', {})
    yt_channels = [
        YouTubeChannelConfig(
            handle=ch['handle'],
            id=ch['id'],
            resolution=int(ch.get('resolution', 1080)),
            has_date_in_title=bool(ch.get('has_date_in_title', True)),
            has_video_id_in_title=bool(ch.get('has_video_id_in_title', False)),
            priority=int(ch.get('priority', 99)),
        )
        for ch in sources_raw.get('youtube_channels', [])
    ]
    ody_channels = [
        OdyseeChannelConfig(
            slug=ch['slug'],
            channel_id=ch['channel_id'],
            priority=int(ch.get('priority', 99)),
        )
        for ch in sources_raw.get('odysee_channels', [])
    ]
    sources = SourcesConfig(
        youtube_channels=yt_channels,
        odysee_channels=ody_channels,
    )

    # --- Build config ---
    cfg = ArcConfig(
        arc_name=raw['arc_name'],
        display_name=raw['display_name'],
        date_range=date_range,
        thresholds=thresholds,
        corpus=corpus,
        sources=sources,
        arc_dir=ARCS_DIR,
    )

    # --- Load query data ---
    if 'query_module' in raw:
        _load_query_module(cfg, raw['query_module'])
    elif 'query_groups' in raw:
        cfg.query_groups = raw['query_groups']
    else:
        log.warning(f"[config] Arc '{arc_name}' has no query_groups or query_module defined.")

    log.info(
        f"[config] Arc loaded: {cfg.display_name}  "
        f"date={cfg.date_range.start}–{cfg.date_range.end}  "
        f"query_groups={len(cfg.query_groups)}  "
        f"total_queries={sum(len(v) for v in cfg.query_groups.values())}"
    )
    return cfg


def _load_query_module(cfg: ArcConfig, module_path: str):
    """
    Dynamically import a query module and pull QUERY_GROUPS + REACTION_TERMS.
    module_path is a Python dotted path, e.g. 'arcs.j6_queries'.
    """
    try:
        mod = importlib.import_module(module_path)
        cfg.query_groups = getattr(mod, 'QUERY_GROUPS', {})
        cfg.reaction_terms = getattr(mod, 'REACTION_TERMS', [])
        log.info(
            f"[config] Loaded query module '{module_path}': "
            f"{len(cfg.query_groups)} groups, "
            f"{sum(len(v) for v in cfg.query_groups.values())} queries, "
            f"{len(cfg.reaction_terms)} reaction terms"
        )
    except ImportError as e:
        raise ImportError(f"Could not import query module '{module_path}': {e}") from e


def _require(d: dict, keys: list[str], context: str = 'root'):
    for k in keys:
        if k not in d:
            raise ValueError(f"Arc config missing required field '{k}' in {context}")


# ---------------------------------------------------------------------------
# Convenience: list available arcs
# ---------------------------------------------------------------------------

def list_arcs() -> list[str]:
    """Return names of all available arc configs (without .yaml extension)."""
    return [
        p.stem for p in ARCS_DIR.glob("*.yaml")
        if p.stem != 'example'
    ]


def all_queries(cfg: ArcConfig) -> list[tuple[str, str]]:
    """
    Return a flat list of (group_name, query) tuples for all query groups.
    Includes reaction_terms as group 'reactions'.
    """
    result = []
    for group, queries in cfg.query_groups.items():
        for q in queries:
            result.append((group, q))
    for q in cfg.reaction_terms:
        result.append(('reactions', q))
    return result
