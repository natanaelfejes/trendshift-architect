# Trendshift Architect 🚀

A high-performance, asynchronous CLI scraper and TUI dashboard for extracting trending open-source intelligence and repository metrics.

## Features

- **Interactive TUI Dashboard:** Built with Textual for a clean, mouse-supported configuration wizard.

- **Multi-Format Exports:** Instantly export data to JSON, CSV (Excel / Google Sheets), and JSON Lines (Data Pipelines).

- **Intelligent Caching Engine:** Idempotent SQLite/JSONL-backed state machine that skips already-scraped items.

- **Dynamic Archives:** Access historical data by year, month (YYYY/MM), or ISO week (YYYY/WW), with an `"all"` wildcard option.

- **Stealth & Resilience:** Built-in WAF backoff re-queues, stealth flags, and asset-blocking for fast execution.

## Requirements

- Python `>=3.11`

- [`uv`](https://github.com/astral-sh/uv) (Recommended package runner)

## Quick Start

Run the interactive dashboard directly with zero manual setup:

```bash
uv run scraper.py
```

## Weekly Job (default, no flags)

Running `uv run scraper.py` with no flags fetches the 8 tracked list pages (daily, weekly,
monthly, yearly, yearly/2025, yearly/2024, live-mentions, github-trending-repositories)
sequentially, 20-30s apart, saves each raw page to `snapshots/YYYY-MM-DD/<view>.html`, then
parses from disk. Every page is checked for a non-200 status, a Cloudflare/challenge title, or
zero repo links — any of those **aborts the whole run** with a clear error instead of caching a
block page as data.

Repos not seen in any previous run are enriched via the GitHub REST API (stars, forks, license,
topics, README excerpt, etc). Set `GITHUB_TOKEN` in the environment to raise the rate limit;
it's optional. Output lands in `runs/YYYY-MM-DD.json` plus a `runs/YYYY-MM-DD-diff.json` (new
entries, biggest risers, drop-outs vs. the previous run) — the diff is what feeds the AI-stack
watchlist threads.

```bash
uv run scraper.py                    # live weekly run
uv run scraper.py --from-dir snapshots/2026-09-25   # re-parse a saved run, no network
uv run scraper.py --from-dir sample_sites           # parse the offline sample pages
```

## Deep Mode (off by default)

`--deep` additionally visits every repository's detail page for precise stars/forks/contributor
counts. **This is what triggered Cloudflare rate-limiting in the past** — it's opt-in and not
part of the weekly job. Possible future work: a lower-frequency deep pass with longer backoff.

## CLI Power-User Flags

```bash
uv run scraper.py --deep --concurrency 1 --format json
uv run scraper.py --endpoints /weekly /monthly
uv run scraper.py --interactive   # old TUI wizard for ad-hoc archive digging
```

## Offline Test

```bash
uv run --with playwright --with rich --with "textual>=0.50.0,<1.0.0" python tests/test_scraper.py
```

## License

Distributed under the MIT License. See `LICENSE` for more information.
