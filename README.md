# StreamScope

Multi-source stream corpus builder and arc classifier.

Given a configurable arc definition (keyword groups, thresholds, date range), StreamScope ingests VOD metadata from DGGVods, YouTube archive channels, and Odysee, cross-references everything into a unified stream index, searches transcripts for arc-relevant keyword hits, and packs a ~800K-char LLM-ready corpus file.

## Setup

```bash
pip install -r requirements.txt
```

Place `client_secrets.json` (YouTube Data API v3 OAuth2) in the project root.

## Usage

```bash
python streamscope.py --arc arcs/j6.yaml
```

See `AGENTS.md` for full architecture and pipeline documentation.
