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

## CLI Power-User Flags

```bash
# Export top 50 daily repos as JSON using 3 concurrent tabs

uv run scraper.py --interactive --limit 50 --concurrency 3 --format json
```

## License

Distributed under the MIT License. See `LICENSE` for more information.
