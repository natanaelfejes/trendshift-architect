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

Zero manual setup, runs the weekly job (see below):

```bash
uv run scraper.py
```

For the old point-and-click TUI wizard, use `uv run scraper.py --interactive`.

## Weekly Job (default, no flags)

Running `uv run scraper.py` with no flags fetches the 8 tracked list pages (daily, weekly,
monthly, yearly, yearly/2025, yearly/2024, live-mentions, github-trending-repositories)
sequentially, 20-30s apart, saves each raw page to `snapshots/YYYY-MM-DD/<view>.html`, then
parses from disk. Every page is checked for a non-200 status, a Cloudflare/challenge title, or
zero repo links — any of those **aborts the whole run** with a clear error instead of caching a
block page as data.

Repos not seen in any previous run are enriched via the GitHub REST API (stars, forks, license,
topics, README excerpt, etc). Output lands in `runs/YYYY-MM-DD.json` plus a
`runs/YYYY-MM-DD-diff.json` (new entries, biggest risers, drop-outs vs. the previous run) — the
diff is what feeds the AI-stack watchlist threads.

### GitHub token (optional, but you'll want it)

Enrichment works without a token, but GitHub caps unauthenticated API requests at 60/hour —
easy to blow through on a single run, since each new repo makes 2 requests (repo + README). A
token raises that to 5000/hour. **Never commit a token** — this repo is public.

1. GitHub → Settings → Developer settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new)
2. **Repository access:** "Public Repositories (read-only)" — no access to your private repos, no write scopes.
3. Set an expiration (90 days is fine, it's read-only and public-only), generate, copy the token.
4. Put it in a `.env` file in the project root (copy `.env.example` → `.env`):
   ```
   GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   `.env` is gitignored — the scraper loads it automatically, no shell setup needed. It's only
   ever read from the environment, never logged or written to `runs/`/`snapshots/`.

Alternatively, export it in your shell instead of using `.env`:

```powershell
# PowerShell, this session only
$env:GITHUB_TOKEN = "github_pat_..."

# PowerShell, persists across new terminals for your user account
[System.Environment]::SetEnvironmentVariable("GITHUB_TOKEN", "github_pat_...", "User")
```
```bash
# bash, this session only
export GITHUB_TOKEN=github_pat_...
```

```bash
uv run scraper.py                    # live weekly run
uv run scraper.py --from-dir snapshots/2026-09-25   # re-parse a saved run, no network
uv run scraper.py --from-dir sample_sites           # parse the offline sample pages
```

## Deep Mode (off by default)

`--deep` additionally visits every repository's detail page for precise stars/forks/contributor
counts, tags, and JSON-LD created/updated timestamps. **This is what triggered Cloudflare
rate-limiting in the past** — it's opt-in and not part of the weekly job. Possible future work:
a lower-frequency deep pass with longer backoff.

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
