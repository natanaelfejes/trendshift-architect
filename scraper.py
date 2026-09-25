# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "playwright",
#     "rich",
#     "textual>=0.50.0,<1.0.0"
# ]
# ///

import asyncio
import csv
import email
import json
import os
import random
import re
import subprocess
import sys
import argparse
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from playwright.async_api import async_playwright

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Checkbox, Button, Label, Input, RadioSet, RadioButton, Select
from textual.containers import VerticalScroll, Container

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

console = Console()

APP_DIR = Path.home() / ".trendshift_scraper"
APP_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = APP_DIR / "state.jsonl"
OUTPUT_DIR = Path.cwd()
BASE_FILENAME = "trendshift_data"
BASE_URL = "https://trendshift.io"
file_lock = asyncio.Lock()

# Fixed weekly-job views: list pages only. Deep per-repo pages are opt-in (--deep)
# because they're what triggered Cloudflare rate-limiting in the past.
VIEWS = {
    "daily": "/",
    "weekly": "/weekly",
    "monthly": "/monthly",
    "yearly": "/yearly",
    "yearly-2025": "/yearly/2025",
    "yearly-2024": "/yearly/2024",
    "live-mentions": "/live-mentions",
    "github-trending-repositories": "/github-trending-repositories",
}
SNAPSHOT_DIR = Path.cwd() / "snapshots"
RUNS_DIR = Path.cwd() / "runs"
BLOCK_TITLE_MARKERS = ("just a moment", "cloudflare", "attention required", "checking your browser", "access denied")
ANCHOR_RE = re.compile(r'<a[^>]*href="([^"]*?/repositories/\d+)"[^>]*>([^<]*)</a>', re.I)
STAR_RE = re.compile(r'lucide-star.*?font-medium">([\d,.]+[kKmM]?)</span>', re.S)


class BlockDetected(Exception):
    """Raised when a Cloudflare/challenge page or a non-200/empty list page is hit. Aborts the whole run."""


def is_challenge_title(title):
    return any(marker in (title or "").lower() for marker in BLOCK_TITLE_MARKERS)


def is_block_page(html, title="", status=200):
    if status is not None and status != 200:
        return True
    if is_challenge_title(title):
        return True
    if not ANCHOR_RE.search(html or ""):
        return True
    return False


def slugify(label):
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def parse_view_html(html):
    """Extract (rank, owner/repo, trendshift_url, list_stars) tuples from a rendered list page, in document order."""
    entries = []
    seen = set()
    for m in ANCHOR_RE.finditer(html):
        url, name = m.group(1), m.group(2).strip()
        if "/" not in name or name in seen:
            continue
        seen.add(name)
        star_match = STAR_RE.search(html, m.end(), m.end() + 1000)
        entries.append({
            "rank": len(entries) + 1,
            "name": name,
            "trendshift_url": url,
            "list_stars": star_match.group(1) if star_match else None,
        })
    return entries


def read_html_file(path):
    if path.suffix.lower() == ".mhtml":
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode("utf-8", errors="replace")
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_snapshots_dir(dir_path):
    """Parse every .html/.mhtml snapshot in a directory. Returns {trendshift_url: [contexts]} and raises BlockDetected on a block page."""
    repo_to_contexts = {}
    dir_path = Path(dir_path)
    for path in sorted(dir_path.glob("*.html")) + sorted(dir_path.glob("*.mhtml")):
        html = read_html_file(path)
        title_match = re.search(r"<title[^>]*>([^<]*)</title>", html, re.I)
        title = title_match.group(1) if title_match else ""
        if is_challenge_title(title):
            raise BlockDetected(f"Block page detected while parsing snapshot {path}")
        if not ANCHOR_RE.search(html or ""):
            if path.suffix.lower() == ".html":
                # Our own saved snapshots are always list pages; zero links means a missed block.
                raise BlockDetected(f"Block page detected while parsing snapshot {path}")
            console.print(f"[dim]Skipping {path.name}: no repo links (not a list page)[/dim]")
            continue
        label = path.stem
        for entry in parse_view_html(html):
            url = entry["trendshift_url"]
            repo_to_contexts.setdefault(url, {"contexts": [], "list_stars": None, "name": entry["name"]})
            repo_to_contexts[url]["contexts"].append(f"#{entry['rank']} {label}")
            if repo_to_contexts[url]["list_stars"] is None:
                repo_to_contexts[url]["list_stars"] = entry["list_stars"]
    return repo_to_contexts


def github_enrich_repo(name, token=None):
    """Fetch created_at, pushed_at, license, stars, forks, open_issues, topics, archived + ~2KB of README for owner/repo."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "trendshift-scraper"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{name}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        info = {
            "created_at": data.get("created_at"),
            "pushed_at": data.get("pushed_at"),
            "license": (data.get("license") or {}).get("spdx_id"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "topics": data.get("topics", []),
            "archived": data.get("archived"),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        return {"error": str(e)}

    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{name}/readme", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            readme_data = json.loads(resp.read())
        import base64
        readme_bytes = base64.b64decode(readme_data.get("content", ""))
        info["readme_excerpt"] = readme_bytes[:2048].decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        info["readme_excerpt"] = None
    return info


def compute_diff(current_repos, previous_repos):
    """current_repos/previous_repos: {name: {"views": {view: rank}, ...}}. Returns new/dropped/risers."""
    current_names, previous_names = set(current_repos), set(previous_repos)
    new_entries = sorted(current_names - previous_names)
    dropped_out = sorted(previous_names - current_names)

    risers = []
    for name in current_names & previous_names:
        curr_best = min(current_repos[name]["views"].values(), default=None)
        prev_best = min(previous_repos[name]["views"].values(), default=None)
        if curr_best is None or prev_best is None:
            continue
        delta = prev_best - curr_best
        if delta > 0:
            risers.append({"name": name, "prev_rank": prev_best, "curr_rank": curr_best, "delta": delta})
    risers.sort(key=lambda r: r["delta"], reverse=True)

    return {"new_entries": new_entries, "dropped_out": dropped_out, "biggest_risers": risers[:10]}


def latest_run_file():
    if not RUNS_DIR.exists():
        return None
    runs = sorted(RUNS_DIR.glob("[0-9]" * 4 + "-" + "[0-9]" * 2 + "-" + "[0-9]" * 2 + ".json"))
    return runs[-1] if runs else None

def ensure_playwright_browsers():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Failed to install Playwright browsers:[/bold red] {e.stderr}")
        sys.exit(1)

class TrendshiftWizard(App):
    TITLE = "Trendshift Architect (v1.0)"
    CSS = """
    #app-grid { layout: grid; grid-size: 2; grid-columns: 1fr 1fr; padding: 0 2; }
    .column { padding: 1 2; margin: 0 1; border: solid cyan; }
    .section-title { text-style: bold; color: cyan; margin-bottom: 1; margin-top: 1; }
    .help-box { color: #888888; padding: 1; border-top: dashed #444444; margin-top: 1; }
    #btn_start { margin-top: 2; width: 100%; text-style: bold; }
    """
    BINDINGS = [("escape", "quit", "Quit / Cancel"), ("f5", "start_scrape", "Start Scraping")]

    def compose(self) -> ComposeResult:
        now = datetime.now()
        cur_yr, cur_mo = now.year, now.month
        iso_yr, iso_wk, _ = now.isocalendar()

        yield Header()
        with Container(id="app-grid"):
            with VerticalScroll(classes="column"):
                yield Label("📊 Active Feeds", classes="section-title")
                yield Checkbox("Daily Rankings (/)", id="ep_daily", value=True)
                yield Checkbox("Weekly Rankings (/weekly)", id="ep_weekly", value=True)
                yield Checkbox("Monthly Rankings (/monthly)", id="ep_monthly", value=True)
                yield Checkbox("Yearly Rankings (/yearly)", id="ep_yearly", value=True)
                yield Checkbox("Live Mentions", id="ep_live", value=True)
                yield Checkbox("GitHub Trending", id="ep_ghtrending")
                yield Checkbox("Trending Developers", id="ep_devs")
                yield Checkbox("Repo Engagements", id="ep_repoeng")
                
                yield Label("🕰️ Historical Archives", classes="section-title")
                yield Label("Type 'all' or separate specific dates with commas.", classes="help-box")
                yield Input(placeholder=f"Years (e.g., {cur_yr-1}, {cur_yr-2} OR type 'all')", id="arc_year")
                yield Input(placeholder=f"Months (e.g., {cur_yr}/{cur_mo:02d}, {cur_yr-1}/01 OR type 'all')", id="arc_month")
                yield Input(placeholder=f"Weeks (e.g., {iso_yr}/{iso_wk}, {iso_yr-1}/42 OR type 'all')", id="arc_week")

            with VerticalScroll(classes="column"):
                yield Label("⚙️ Extraction Strategy", classes="section-title")
                yield RadioSet(
                    RadioButton("Deep Extraction (Slower, triggers rate-limiting)", id="depth_deep", tooltip="Visits EVERY repository page. Gets precise metrics and timestamps."),
                    RadioButton("Shallow Snapshot (Fast)", id="depth_shallow", value=True, tooltip="Only scrapes list pages. Gets rankings, names, and URLs instantly."),
                    id="rs_depth"
                )
                
                yield Label("📦 Artifact Exports", classes="section-title")
                yield Checkbox("JSON Array (.json)", id="fmt_json", value=True)
                yield Checkbox("Spreadsheet CSV (.csv)", id="fmt_csv", value=True)
                yield Checkbox("JSON Lines Pipeline (.jsonl)", id="fmt_jsonl", value=True)
                
                yield Label("🚀 Execution Limits", classes="section-title")
                yield Label("Network Concurrency (Tabs running simultaneously):")
                yield Select([
                    ("1 Tab (Safest, Slowest)", 1),
                    ("2 Tabs (Safe & Recommended)", 2),
                    ("3 Tabs (Moderate)", 3),
                    ("4 Tabs (Fast)", 4),
                    ("5 Tabs (Aggressive)", 5),
                ], value=2, id="sel_concurrency")
                
                yield Label("Max Items Per Category:")
                yield Select([
                    ("Top 10 (Quick Snapshot)", 10),
                    ("Top 30 (Default Tracker)", 30),
                    ("Top 50 (Standard)", 50),
                    ("Top 100 (Deep Dive)", 100),
                    ("Unlimited (Fetch Everything)", 0),
                ], value=30, id="sel_limit")
                
                yield Button("START SCRAPING (F5)", variant="success", id="btn_start")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_start":
            self.action_start_scrape()

    def action_start_scrape(self):
        config = {"endpoints": {}, "formats": []}
        rs_depth = self.query_one("#rs_depth", RadioSet)
        config["shallow"] = rs_depth.pressed_button.id == "depth_shallow" if rs_depth.pressed_button else False
        
        config["limit"] = self.query_one("#sel_limit", Select).value
        config["concurrency"] = self.query_one("#sel_concurrency", Select).value
        
        if self.query_one("#ep_daily", Checkbox).value: config["endpoints"]["Daily"] = "/"
        if self.query_one("#ep_weekly", Checkbox).value: config["endpoints"]["Weekly"] = "/weekly"
        if self.query_one("#ep_monthly", Checkbox).value: config["endpoints"]["Monthly"] = "/monthly"
        if self.query_one("#ep_yearly", Checkbox).value: config["endpoints"]["Yearly"] = "/yearly"
        if self.query_one("#ep_live", Checkbox).value: config["endpoints"]["Live Mentions"] = "/live-mentions"
        if self.query_one("#ep_ghtrending", Checkbox).value: config["endpoints"]["GitHub Trending"] = "/github-trending-repositories"
        if self.query_one("#ep_devs", Checkbox).value: config["endpoints"]["Trending Developers"] = "/trending/developers"
        if self.query_one("#ep_repoeng", Checkbox).value: config["endpoints"]["Repo Engagements"] = "/repository-engagements"
        
        now = datetime.now()
        cur_yr, cur_mo = now.year, now.month
        iso_yr, iso_wk, _ = now.isocalendar()

        arc_y_raw = self.query_one("#arc_year", Input).value.strip().lower()
        years = [str(y) for y in range(2023, cur_yr + 1)] if arc_y_raw == "all" else [x.strip() for x in arc_y_raw.split(",") if x.strip()]
        for y in years: config["endpoints"][f"Year {y}"] = f"/yearly/{y}"

        arc_m_raw = self.query_one("#arc_month", Input).value.strip().lower()
        if arc_m_raw == "all":
            months = [f"{y}/{m:02d}" for y in range(2023, cur_yr + 1) for m in range(1, (cur_mo if y == cur_yr else 12) + 1)]
        else:
            months = [x.strip() for x in arc_m_raw.split(",") if x.strip()]
        for m in months: config["endpoints"][f"Month {m}"] = f"/monthly/{m}"

        arc_w_raw = self.query_one("#arc_week", Input).value.strip().lower()
        if arc_w_raw == "all":
            weeks = [f"{y}/{w}" for y in range(2023, cur_yr + 1) for w in range(1, (iso_wk if y == iso_yr else 52) + 1)]
        else:
            weeks = [x.strip() for x in arc_w_raw.split(",") if x.strip()]
        for w in weeks: config["endpoints"][f"Week {w}"] = f"/weekly/{w}"
        
        if self.query_one("#fmt_json", Checkbox).value: config["formats"].append("json")
        if self.query_one("#fmt_csv", Checkbox).value: config["formats"].append("csv")
        if self.query_one("#fmt_jsonl", Checkbox).value: config["formats"].append("jsonl")
        
        if not config["endpoints"]:
            return
        self.exit(config)

def export_formats(selected_formats):
    if not STATE_FILE.exists(): return
    records = []
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try: records.append(json.loads(line.strip()))
                except Exception: continue
    if not records: return

    if "json" in selected_formats or "all" in selected_formats:
        json_path = OUTPUT_DIR / f"{BASE_FILENAME}.json"
        with open(json_path, "w", encoding="utf-8") as f: json.dump(records, f, indent=2, ensure_ascii=False)
        console.print(f"[bold green]✓ Exported JSON:[/bold green] [cyan]{json_path}[/cyan]")

    if "csv" in selected_formats or "all" in selected_formats:
        csv_path = OUTPUT_DIR / f"{BASE_FILENAME}.csv"
        headers = ["Rank Contexts", "Name", "GitHub URL", "Trendshift URL", "Stars", "Forks", "Contributors", "Likes", "Bookmarks", "Tags", "Created At", "Last Commit"]
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in records:
                metrics, timestamps, tags = r.get("metrics", {}), r.get("timestamps", {}), r.get("tags", [])
                writer.writerow([
                    ", ".join(r.get("rank_contexts", [])), r.get("name", ""), r.get("github_url", ""), r.get("trendshift_url", ""),
                    metrics.get("stars", ""), metrics.get("forks", ""), metrics.get("contributors", ""),
                    metrics.get("likes", ""), metrics.get("bookmarks", ""), ", ".join(tags), 
                    timestamps.get("created_at", ""), timestamps.get("last_commit", "")
                ])
        console.print(f"[bold green]✓ Exported CSV:[/bold green] [cyan]{csv_path}[/cyan]")
        
    if "jsonl" in selected_formats or "all" in selected_formats:
        jsonl_path = OUTPUT_DIR / f"{BASE_FILENAME}.jsonl"
        shutil.copyfile(STATE_FILE, jsonl_path)
        console.print(f"[bold green]✓ Exported JSONL:[/bold green] [cyan]{jsonl_path}[/cyan]")

def get_scraped_cache_status():
    cache = {}
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    url = record.get("trendshift_url")
                    mode = record.get("scrape_mode", "shallow")
                    if url:
                        if mode == "deep" or url not in cache:
                            cache[url] = mode
                except Exception:
                    continue
    return cache

async def block_media(route):
    if route.request.resource_type in ["image", "media", "font"]: await route.abort()
    else: await route.continue_()

async def fetch_repo_worker(browser_context, url, contexts, semaphore, progress, task_id, retry_queue):
    async with semaphore:
        page = await browser_context.new_page()
        await page.route("**/*", block_media)
        try:
            await asyncio.sleep(random.uniform(0.6, 1.5))
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1000)

            title = await page.title()
            if "Just a moment..." in title or "Cloudflare" in title:
                progress.console.print(f"[bold yellow]⚠ WAF block on {url}. Re-queuing...[/bold yellow]")
                retry_queue.append((url, contexts))
                await asyncio.sleep(6)
                return

            # Precision DOM & JSON-LD Extraction
            data = await page.evaluate(r'''() => {
                const rawTitle = document.title || '';
                const cleanName = rawTitle.split(' — ')[0].trim();
                const githubLink = cleanName.includes('/') ? `https://github.com/${cleanName}` : null;

                // 1. Extract from JSON-LD Schema if available
                let ldCreated = null;
                let ldModified = null;
                try {
                    const ldScript = document.querySelector('script[type="application/ld+json"]');
                    if (ldScript) {
                        const parsed = JSON.parse(ldScript.textContent);
                        if (parsed) {
                            ldCreated = parsed.dateCreated || null;
                            ldModified = parsed.dateModified || null;
                        }
                    }
                } catch (e) {}

                // 2. Extract stats using Lucide icon classes in the stats bar
                let stars = null;
                let forks = null;
                let contributors = null;
                let lastCommit = null;
                let createdAt = null;

                const statDivs = Array.from(document.querySelectorAll('.flex.items-center.gap-1'));
                for (let div of statDivs) {
                    const txt = div.innerText.trim();
                    if (div.querySelector('.lucide-star')) {
                        stars = txt.replace(/[^0-9.,kKmM]/g, '');
                    } else if (div.querySelector('.lucide-git-fork')) {
                        forks = txt.replace(/[^0-9.,kKmM]/g, '');
                    } else if (txt.includes('contributors')) {
                        const m = txt.match(/([0-9]+)/);
                        if (m) contributors = m[1];
                    } else if (txt.startsWith('last commit')) {
                        lastCommit = txt.replace('last commit', '').trim();
                    } else if (txt.startsWith('created')) {
                        createdAt = txt.replace('created', '').trim();
                    }
                }

                // Fallback buttons for likes/bookmarks
                const likeBtn = document.querySelector('button[aria-label*="likes"]');
                const bookmarkBtn = document.querySelector('button[aria-label*="bookmarks"]');
                
                const getTags = () => {
                    return [...new Set(Array.from(document.querySelectorAll('a'))
                        .filter(a => a.href.includes('/tags/') || a.href.includes('/categories/'))
                        .map(a => a.textContent.trim())
                        .filter(t => t.length > 0))];
                };

                return {
                    name: cleanName,
                    github_url: githubLink,
                    tags: getTags(),
                    metrics: {
                        stars: stars,
                        forks: forks,
                        contributors: contributors,
                        likes: likeBtn ? likeBtn.innerText.trim().replace(/[^0-9]/g, '') : null,
                        bookmarks: bookmarkBtn ? bookmarkBtn.innerText.trim().replace(/[^0-9]/g, '') : null
                    },
                    timestamps: {
                        created_at: ldCreated || createdAt,
                        last_commit: ldModified || lastCommit
                    }
                }
            }''')

            data['trendshift_url'] = url
            data['rank_contexts'] = contexts
            data['scrape_mode'] = 'deep'
            
            async with file_lock:
                with open(STATE_FILE, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(data) + "\n")
                    
        except Exception as e:
            if "TargetClosedError" not in str(e) and "Target page, context or browser has been closed" not in str(e):
                progress.console.print(f"[bold red]✗ Error extracting {url}: {e}[/bold red]")
        finally:
            try:
                progress.advance(task_id)
                await page.close()
            except Exception:
                pass

def write_run_and_diff(repo_to_contexts, cache_status, github_token):
    """Enrich brand-new repos, write runs/<date>.json, diff it against the previous run, print the summary."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    prev_path = latest_run_file()
    previous_repos = {}
    if prev_path:
        previous_repos = {r["name"]: r for r in json.loads(prev_path.read_text(encoding="utf-8"))["repos"]}

    current_repos = {}
    for url, info in repo_to_contexts.items():
        contexts = info["contexts"]
        list_stars = info["list_stars"]
        name = info["name"]
        views = {}
        for ctx in contexts:
            m = re.match(r"#(\d+)\s+(.+)", ctx)
            if m:
                views[m.group(2)] = min(int(m.group(1)), views.get(m.group(2), int(m.group(1))))
        current_repos[name] = {"name": name, "trendshift_url": url, "views": views, "list_stars": list_stars}

    new_names = [n for n, r in current_repos.items() if r["trendshift_url"] not in cache_status]
    if new_names:
        console.print(f"\n[bold cyan]Enriching {len(new_names)} new repo(s) via GitHub API...[/bold cyan]")
    for name in new_names:
        gh_name = name if "/" in name else None
        current_repos[name]["enrichment"] = github_enrich_repo(gh_name, github_token) if gh_name else None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_path = RUNS_DIR / f"{today}.json"
    run_path.write_text(json.dumps({"date": today, "repos": list(current_repos.values())}, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"[bold green]✓ Run snapshot:[/bold green] [cyan]{run_path}[/cyan]")

    diff = compute_diff(current_repos, previous_repos)
    diff_path = RUNS_DIR / f"{today}-diff.json"
    diff_path.write_text(json.dumps({"date": today, "compared_to": prev_path.stem if prev_path else None, **diff}, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"[bold green]✓ Diff:[/bold green] [cyan]{diff_path}[/cyan] "
                  f"([green]{len(diff['new_entries'])} new[/green], [red]{len(diff['dropped_out'])} dropped[/red], "
                  f"[yellow]{len(diff['biggest_risers'])} risers[/yellow])")


def finalize_run(config, repo_to_contexts, cache_status):
    write_run_and_diff(repo_to_contexts, cache_status, os.environ.get("GITHUB_TOKEN"))
    if config["shallow"]:
        console.print("\n[bold cyan]Shallow Mode: Writing rankings to cache...[/bold cyan]")
        with open(STATE_FILE, 'a', encoding='utf-8') as f:
            for url, info in repo_to_contexts.items():
                if url not in cache_status:
                    f.write(json.dumps({"trendshift_url": url, "rank_contexts": info["contexts"], "scrape_mode": "shallow"}) + "\n")
        export_formats(config["formats"])
        return True
    return False


async def run_scraper(config):
    cache_status = get_scraped_cache_status()

    if config.get("from_dir"):
        console.print(f"\n[bold cyan]Offline mode: parsing snapshots from {config['from_dir']}[/bold cyan]")
        try:
            repo_to_contexts = parse_snapshots_dir(config["from_dir"])
        except BlockDetected as e:
            console.print(f"[bold red]✗ BLOCKED: {e}[/bold red]")
            sys.exit(1)
        finalize_run(config, repo_to_contexts, cache_status)
        console.print("[bold green]🎉 Done![/bold green]")
        return

    console.print("\n[dim]Initializing stealth engine...[/dim]")
    ensure_playwright_browsers()

    repo_to_contexts = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
        
        page = await context.new_page()
        date_dir = SNAPSHOT_DIR / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"\n[bold green]Phase 1: Fetching list pages sequentially into {date_dir}[/bold green]")

        endpoints = list(config["endpoints"].items())
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
            for i, (label, url_path) in enumerate(endpoints):
                if i > 0:
                    await asyncio.sleep(random.uniform(20, 30))
                url = urljoin(BASE_URL, url_path)
                slug = slugify(label)
                task = progress.add_task(f"Fetching {label}...", total=None)

                resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1000)

                prev_height = 0
                for _ in range(12):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1200)
                    curr_height = await page.evaluate("document.body.scrollHeight")
                    if curr_height == prev_height:
                        break
                    prev_height = curr_height

                if slug == "live-mentions":
                    for _ in range(5):
                        btn = page.locator("button:has-text('Load more')").first
                        if await btn.count() == 0:
                            break
                        await btn.click()
                        await page.wait_for_timeout(1200)

                status = resp.status if resp else None
                title = await page.title()
                html = await page.content()
                if is_block_page(html, title, status):
                    raise BlockDetected(f"{label} ({url}) returned status={status}, title='{title}'")

                (date_dir / f"{slug}.html").write_text(html, encoding="utf-8")
                for entry in parse_view_html(html):
                    fl = entry["trendshift_url"]
                    repo_to_contexts.setdefault(fl, {"contexts": [], "list_stars": None, "name": entry["name"]})
                    repo_to_contexts[fl]["contexts"].append(f"#{entry['rank']} {label}")
                    if repo_to_contexts[fl]["list_stars"] is None:
                        repo_to_contexts[fl]["list_stars"] = entry["list_stars"]
                progress.console.print(f"[dim]✓ {label}: found {len(parse_view_html(html))} items[/dim]")
                progress.remove_task(task)
        await page.close()

        if finalize_run(config, repo_to_contexts, cache_status):
            await browser.close()
            return

        urls_to_scrape = {}
        for url, info in repo_to_contexts.items():
            if url not in cache_status or cache_status[url] == "shallow":
                urls_to_scrape[url] = info["contexts"]

        total_urls = len(urls_to_scrape)
        if total_urls == 0:
            console.print(f"\n[bold green]✓ All endpoints are fully deep-scraped in cache. Compiling exports...[/bold green]")
            export_formats(config["formats"])
            await browser.close()
            return

        console.print(f"\n[bold green]Phase 2: Deep Extraction[/bold green]")
        console.print(f"[dim]Queue: {total_urls} | Up-to-date in cache: {len(repo_to_contexts) - total_urls} | Concurrency: {config['concurrency']}[/dim]")
        
        semaphore = asyncio.Semaphore(config["concurrency"])
        retry_queue = []
        
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(complete_style="green"), TaskProgressColumn(), TimeRemainingColumn()) as progress:
            task_id = progress.add_task("[cyan]Scraping repositories...", total=total_urls)
            
            try:
                tasks = [fetch_repo_worker(context, url, contexts, semaphore, progress, task_id, retry_queue) for url, contexts in urls_to_scrape.items()]
                await asyncio.gather(*tasks)

                if retry_queue:
                    console.print(f"\n[bold yellow]Retrying {len(retry_queue)} WAF-blocked URLs...[/bold yellow]")
                    retry_task_id = progress.add_task("[yellow]Retrying blocked items...", total=len(retry_queue))
                    retry_tasks = [fetch_repo_worker(context, url, contexts, semaphore, progress, retry_task_id, []) for url, contexts in retry_queue]
                    await asyncio.gather(*retry_tasks)
            except asyncio.CancelledError:
                pass

        try:
            await browser.close()
        except Exception:
            pass
            
        console.print("\n[bold green]Phase 3: Generating Artifacts[/bold green]")
        export_formats(config["formats"])
        console.print("[bold green]🎉 Done![/bold green]")

def get_cli_config():
    parser = argparse.ArgumentParser(description="Trendshift Advanced Scraper")
    parser.add_argument("--interactive", action="store_true", help="Launch the TUI wizard instead of the weekly-job pipeline")
    parser.add_argument("--deep", action="store_true", help="Also visit every repo's detail page (slower, caused rate-limiting in the past)")
    parser.add_argument("--from-dir", help="Parse snapshots (.html/.mhtml) from this directory instead of fetching live")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent browser tabs for --deep")
    parser.add_argument("--format", choices=["all", "json", "csv", "jsonl"], default="all")
    parser.add_argument("--endpoints", nargs="*", help="Direct URL paths (default: the fixed weekly view list)")

    args = parser.parse_args()
    if args.interactive:
        return None
    return {
        "shallow": not args.deep, "limit": 0, "concurrency": args.concurrency,
        "formats": [args.format] if args.format != "all" else ["json", "csv", "jsonl"],
        "endpoints": {p: p for p in args.endpoints} if args.endpoints else dict(VIEWS),
        "from_dir": args.from_dir,
    }

def main():
    config = get_cli_config()
    if not config:
        app = TrendshiftWizard()
        config = app.run()

    if not config:
        console.print("[bold yellow]Setup aborted by user. Exiting.[/bold yellow]")
        return
        
    try:
        asyncio.run(run_scraper(config))
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Execution aborted by user. Partial data saved to cache.[/bold yellow]")
        sys.exit(1)
    except BlockDetected as e:
        console.print(f"\n[bold red]✗ BLOCKED, aborting run: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    main()