"""
ingest — StreamScope data ingestion layer.
Handles DGGVods, YouTube archive channels, and Odysee.
"""

from ingest.dggvods import fetch_all_vods, fetch_transcript, fetch_transcripts_batch
from ingest.youtube import fetch_channel_videos, fetch_all_channels as fetch_yt_channels
from ingest.odysee import fetch_channel_claims, fetch_all_channels as fetch_odysee_channels

__all__ = [
    "fetch_all_vods",
    "fetch_transcript",
    "fetch_transcripts_batch",
    "fetch_channel_videos",
    "fetch_yt_channels",
    "fetch_channel_claims",
    "fetch_odysee_channels",
]
