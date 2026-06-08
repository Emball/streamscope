"""
StreamScope — multi-source stream corpus builder and arc classifier.
Version: 0.1.1.1
"""

import argparse
import logging
import os
import sys
from pathlib import Path

VERSION = "0.1.1.1"

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    for lib in ("urllib3", "requests", "googleapiclient"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def _resolve_cookie(args) -> str:
    """
    Resolve DGGVods session cookie from (in priority order):
      1. --dgg-cookie CLI arg
      2. DGG_COOKIE environment variable
      3. data/dgg_cookie.txt file
    Exits with an error message if none found.
    """
    from ingest.dggvods import get_cookie, set_cookie_file
    cookie = getattr(args, 'dgg_cookie', None) or os.environ.get("DGG_COOKIE", "").strip()
    if cookie:
        # Persist it for future runs
        set_cookie_file(cookie)
        return cookie
    cookie = get_cookie()
    if cookie:
        return cookie
    print(
        "ERROR: DGGVods session cookie required.\n"
        "  Option 1: python streamscope.py set-cookie \"your_cookie_string\"\n"
        "  Option 2: set DGG_COOKIE environment variable\n"
        "  Option 3: save cookie to data/dgg_cookie.txt\n\n"
        "To get your cookie: open dggvods.dev in browser → F12 → Network tab →\n"
        "  refresh → click any /api/ request → copy the Cookie: header value."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def cmd_set_cookie(args):
    """Save DGGVods session cookie for future runs."""
    from ingest.dggvods import set_cookie_file
    set_cookie_file(args.cookie)
    print(f"[set-cookie] Cookie saved to data/dgg_cookie.txt")


def cmd_ingest(args):
    """Phase 1: Fetch VOD metadata from DGGVods and write to DB."""
    from core import load_arc, DB
    from ingest.dggvods import fetch_all_vods

    cookie = _resolve_cookie(args)
    cfg = load_arc(args.arc)
    with DB() as db:
        streams = fetch_all_vods(
            date_start=cfg.date_range.start,
            date_end=cfg.date_range.end,
            force_refresh=args.force_refresh,
            cookie=cookie,
        )
        ins, upd = db.upsert_streams(streams)
        print(f"[ingest] VODs: {len(streams)} in range  inserted={ins} updated={upd}")


def cmd_archive(args):
    """Phase 3: Fetch YouTube and Odysee archive metadata."""
    from core import load_arc, DB
    from ingest.youtube import fetch_all_channels as yt_fetch
    from ingest.odysee import fetch_all_channels as ody_fetch
    from pipeline.unify import unify_sources

    cfg = load_arc(args.arc)

    api_key = args.yt_api_key or os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YouTube API key required. Pass --yt-api-key or set YOUTUBE_API_KEY env var.")
        sys.exit(1)

    with DB() as db:
        streams = db.get_streams_in_range(cfg.date_range.start, cfg.date_range.end)
        print(f"[archive] Loaded {len(streams)} streams from DB")

        yt_videos = yt_fetch(
            channel_configs=cfg.sources.youtube_channels,
            api_key=api_key,
            date_start=cfg.date_range.start,
            date_end=cfg.date_range.end,
            force_refresh=args.force_refresh,
        )
        print(f"[archive] YouTube: {len(yt_videos)} videos")

        ody_videos = ody_fetch(
            channel_configs=cfg.sources.odysee_channels,
            date_start=cfg.date_range.start,
            date_end=cfg.date_range.end,
            force_refresh=args.force_refresh,
        )
        print(f"[archive] Odysee: {len(ody_videos)} videos")

        all_archive = yt_videos + ody_videos
        stats = unify_sources(cfg, db, streams, all_archive)
        print(f"[archive] Unification: {stats}")


def cmd_search(args):
    """Phase 2: Run keyword search across DGGVods transcripts."""
    from core import load_arc, DB
    from pipeline.search import run_search, clear_checkpoint

    cookie = _resolve_cookie(args)
    cfg = load_arc(args.arc)

    if args.reset_checkpoint:
        clear_checkpoint()
        print("[search] Checkpoint cleared")

    with DB() as db:
        streams = db.get_streams_in_range(cfg.date_range.start, cfg.date_range.end)
        stream_ids = {s.dgg_vod_id for s in streams}
        print(f"[search] Searching across {len(stream_ids)} streams in date range")

        stats = run_search(
            cfg=cfg,
            db=db,
            stream_ids=stream_ids,
            resume=not args.reset_checkpoint,
            dry_run=args.dry_run,
            cookie=cookie,
        )
        print(f"[search] Done: {stats}")


def cmd_classify(args):
    """Phase 5: Score and flag streams as arc-relevant."""
    from core import load_arc, DB
    from pipeline.classify import classify_streams, print_classification_report

    cfg = load_arc(args.arc)
    with DB() as db:
        streams = db.get_streams_in_range(cfg.date_range.start, cfg.date_range.end)
        stats = classify_streams(cfg, db, streams)
        print(f"[classify] {stats}")
        if args.report:
            print_classification_report(cfg, db)


def cmd_transcripts(args):
    """Phase 6: Acquire transcripts for flagged streams."""
    from core import load_arc, DB
    from pipeline.transcripts import acquire_transcripts

    cfg = load_arc(args.arc)
    with DB() as db:
        flagged = db.get_flagged_streams(cfg.arc_name)
        streams = [s for s, _ in flagged]
        print(f"[transcripts] Acquiring transcripts for {len(streams)} flagged streams")
        stats = acquire_transcripts(db, streams, force_refresh=args.force_refresh)
        print(f"[transcripts] {stats}")


def cmd_corpus(args):
    """Phase 7: Build LLM corpus from flagged streams and transcripts."""
    from core import load_arc, DB
    from pipeline.corpus import build_corpus

    cfg = load_arc(args.arc)
    with DB() as db:
        stats = build_corpus(cfg, db)
        print(f"[corpus] {stats}")


def cmd_run(args):
    """Run all phases end-to-end."""
    print(f"[run] StreamScope v{VERSION} — full pipeline for arc: {args.arc}")
    cmd_ingest(args)
    cmd_search(args)

    api_key = getattr(args, 'yt_api_key', None) or os.environ.get("YOUTUBE_API_KEY")
    if api_key:
        cmd_archive(args)
    else:
        print("[run] Skipping archive phase (no YouTube API key)")

    cmd_classify(args)
    cmd_transcripts(args)
    cmd_corpus(args)
    print(f"[run] Pipeline complete. Check output/ directory.")


def cmd_status(args):
    """Print DB summary stats."""
    from core import DB, load_arc
    from pipeline.classify import get_flagged_summary

    with DB() as db:
        summary = db.summary()
        print(f"\nStreamScope DB Status")
        print(f"  streams:     {summary['streams']}")
        print(f"  sources:     {summary['sources']}")
        print(f"  hits:        {summary['hits']}")
        print(f"  arc_results: {summary['arc_results']}")

        if args.arc:
            cfg = load_arc(args.arc)
            counts = db.arc_result_count(cfg.arc_name)
            flagged = get_flagged_summary(cfg, db)
            print(f"\nArc: {cfg.display_name}")
            print(f"  Scored:  {counts['total']}")
            print(f"  Flagged: {counts['flagged']}")
            if flagged:
                print(f"\nTop 10 flagged streams:")
                for entry in flagged[:10]:
                    print(
                        f"  [{entry['date']}] density={entry['density_score']:.2f} "
                        f"hits={entry['total_hits']:3d}  {entry['title'][:50]}"
                    )


# ---------------------------------------------------------------------------
# CLI setup
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamscope",
        description=f"StreamScope v{VERSION} — stream arc corpus builder",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # Shared arg helpers
    def arc_arg(p):
        p.add_argument("--arc", required=True, help="Arc name (e.g. j6)")
    def refresh_arg(p):
        p.add_argument("--force-refresh", action="store_true", help="Bypass cache")
    def cookie_arg(p):
        p.add_argument("--dgg-cookie", help="DGGVods session cookie string (overrides env/file)")

    # set-cookie
    p_sc = sub.add_parser("set-cookie", help="Save DGGVods session cookie for future runs")
    p_sc.add_argument("cookie", help="Full Cookie: header value from browser")
    p_sc.set_defaults(func=cmd_set_cookie)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Phase 1: Fetch DGGVods VOD metadata")
    arc_arg(p_ingest); refresh_arg(p_ingest); cookie_arg(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    # archive
    p_archive = sub.add_parser("archive", help="Phase 3: Fetch YouTube + Odysee archive metadata")
    arc_arg(p_archive); refresh_arg(p_archive)
    p_archive.add_argument("--yt-api-key", help="YouTube Data API key")
    p_archive.set_defaults(func=cmd_archive)

    # search
    p_search = sub.add_parser("search", help="Phase 2: Run keyword search")
    arc_arg(p_search); cookie_arg(p_search)
    p_search.add_argument("--reset-checkpoint", action="store_true",
                           help="Start fresh (ignore previous progress)")
    p_search.add_argument("--dry-run", action="store_true",
                           help="Run search but don't write to DB")
    p_search.set_defaults(func=cmd_search)

    # classify
    p_classify = sub.add_parser("classify", help="Phase 5: Score and flag streams")
    arc_arg(p_classify)
    p_classify.add_argument("--report", action="store_true", help="Print full report")
    p_classify.set_defaults(func=cmd_classify)

    # transcripts
    p_transcripts = sub.add_parser("transcripts", help="Phase 6: Acquire transcripts")
    arc_arg(p_transcripts); refresh_arg(p_transcripts)
    p_transcripts.set_defaults(func=cmd_transcripts)

    # corpus
    p_corpus = sub.add_parser("corpus", help="Phase 7: Build LLM corpus")
    arc_arg(p_corpus)
    p_corpus.set_defaults(func=cmd_corpus)

    # run (full pipeline)
    p_run = sub.add_parser("run", help="Run full pipeline end-to-end")
    arc_arg(p_run); refresh_arg(p_run); cookie_arg(p_run)
    p_run.add_argument("--yt-api-key", help="YouTube Data API key")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="Print DB summary")
    p_status.add_argument("--arc", help="Arc name for arc-specific stats")
    p_status.set_defaults(func=cmd_status)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(getattr(args, 'verbose', False))

    if not hasattr(args, 'func'):
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
