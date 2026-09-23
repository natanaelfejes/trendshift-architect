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
import json
import os
import random
import subprocess
import sys
import argparse
import shutil
from datetime import datetime
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

def ensure_playwright_browsers():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Failed to install Playwright browsers:[/bold red] {e.stderr}")
        sys.exit(1)

class TrendshiftWizard(App):
    TITLE = "Trendshift Architect (v1.0)"
    CSS = """
    #app-grid {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        padding: 0 2;
    }
    .column {
        padding: 1 2;
        margin: 0 1;
        border: solid cyan;
    }
    .section-title {
        text-style: bold;
        color: cyan;
        margin-bottom: 1;
        margin-top: 1;
    }
    .help-box {
        color: #888888;
        padding: 1;
        border-top: dashed #444444;
        margin-top: 1;
    }
    #btn_start {
        margin-top: 2;
        width: 100%;
        text-style: bold;
    }
    """
    BINDINGS = [
        ("escape", "quit", "Quit / Cancel"),
        ("f5", "start_scrape", "Start Scraping")
    ]

    def compose(self) -> ComposeResult:
        now = datetime.now()
        cur_yr, cur_mo = now.year, now.month
        iso_yr, iso_wk, _ = now.isocalendar()

        yield Header()
        with Container(id="app-grid"):
            with VerticalScroll(classes="column"):
                yield Label("📊 Active Feeds", classes="section-title")
                yield Checkbox("Daily Rankings (/)", id="ep_daily", value=True, tooltip="Today's top trending repositories.")
                yield Checkbox("Weekly Rankings (/weekly)", id="ep_weekly", value=True, tooltip="Top repositories over the last 7 days.")
                yield Checkbox("Monthly Rankings (/monthly)", id="ep_monthly", value=True, tooltip="Top repositories over the last 30 days.")
                yield Checkbox("Yearly Rankings (/yearly)", id="ep_yearly", value=True, tooltip="Top repositories over the last 365 days.")
                yield Checkbox("Live Mentions", id="ep_live", value=True, tooltip="Real-time mentions feed.")
                yield Checkbox("GitHub Trending", id="ep_ghtrending", tooltip="GitHub's official trending page aggregated.")
                yield Checkbox("Trending Developers", id="ep_devs", tooltip="Ranks individual developers by momentum.")
                yield Checkbox("Repo Engagements", id="ep_repoeng", tooltip="Repositories with sustained contributor activity.")
                
                yield Label("🕰️ Historical Archives", classes="section-title")
                yield Label("Type 'all' or separate specific dates with commas.", classes="help-box")
                yield Input(placeholder=f"Years (e.g., {cur_yr-1}, {cur_yr-2} OR type 'all')", id="arc_year")
                yield Input(placeholder=f"Months (e.g., {cur_yr}/{cur_mo:02d}, {cur_yr-1}/01 OR type 'all')", id="arc_month")
                yield Input(placeholder=f"Weeks (e.g., {iso_yr}/{iso_wk}, {iso_yr-1}/42 OR type 'all')", id="arc_week")

            with VerticalScroll(classes="column"):
                yield Label("⚙️ Extraction Strategy", classes="section-title")
                yield RadioSet(
                    RadioButton("Deep Extraction (Slower)", id="depth_deep", value=True, tooltip="Visits EVERY repository page. Gets precise metrics and timestamps."),
                    RadioButton("Shallow Snapshot (Fast)", id="depth_shallow", tooltip="Only scrapes list pages. Gets rankings, names, and URLs instantly."),
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
                    ("Unlimited (Fetch Everything Available)", 0),
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
        headers = ["Rank Contexts", "Name", "GitHub URL", "Trendshift URL", "Stars", "Forks", "Contributors", "Likes", "Bookmarks", "Created At", "Last Commit"]
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in records:
                metrics, timestamps = r.get("metrics", {}), r.get("timestamps", {})
                writer.writerow([
                    ", ".join(r.get("rank_contexts", [])), r.get("name", ""), r.get("github_url", ""), r.get("trendshift_url", ""),
                    metrics.get("stars", ""), metrics.get("forks", ""), metrics.get("contributors", ""),
                    metrics.get("likes", ""), metrics.get("bookmarks", ""), timestamps.get("created_at", ""), timestamps.get("last_commit", "")
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

async def extract_links_from_list(page):
    links = await page.evaluate('''() => {
        return Array.from(document.querySelectorAll('a'))
            .map(a => a.getAttribute('href'))
            .filter(href => href && (href.includes('/repositories/') || href.includes('/developers/')));
    }''')
    return list(dict.fromkeys(links))

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
                progress.console.print(f"[bold yellow]⚠ WAF block on {url}. Re-queuing for retry...[/bold yellow]")
                retry_queue.append((url, contexts))
                await asyncio.sleep(6)
                return

            # Robust Regex-Powered DOM Parser
            data = await page.evaluate('''() => {
                const rawTitle = document.title || '';
                const cleanName = rawTitle.split(' — ')[0].trim();
                const githubLink = cleanName.includes('/') ? `https://github.com/${cleanName}` : null;
                
                const getStat = (label) => {
                    const cards = Array.from(document.querySelectorAll('div, section, article'));
                    for (let card of cards) {
                        const txt = card.innerText || '';
                        if (txt.toLowerCase().includes(label.toLowerCase())) {
                            const regex = new RegExp(label + `[:\\s]*([\\d,]+)`, 'i');
                            const match = txt.match(regex);
                            if (match && match[1]) return match[1];
                        }
                    }
                    return null;
                };

                return {
                    name: cleanName,
                    github_url: githubLink,
                    metrics: {
                        stars: getStat('Stars') || getStat('Star'),
                        forks: getStat('Forks') || getStat('Fork'),
                        contributors: getStat('Contributors'),
                        likes: getStat('Likes') || getStat('Upvotes'),
                        bookmarks: getStat('Bookmarks') || getStat('Bookmark')
                    },
                    timestamps: {
                        created_at: getStat('Created at') || getStat('Created'),
                        last_commit: getStat('Last commit') || getStat('Updated')
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
            progress.console.print(f"[bold red]✗ Error extracting {url}: {e}[/bold red]")
        finally:
            progress.advance(task_id)
            await page.close()

async def run_scraper(config):
    console.print("\n[dim]Initializing stealth engine...[/dim]")
    ensure_playwright_browsers()
    
    cache_status = get_scraped_cache_status()
    repo_to_contexts = {} 

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        
        page = await context.new_page()
        limit_text = "Infinite" if config['limit'] == 0 else config['limit']
        console.print(f"\n[bold green]Phase 1: Discovering endpoints (Limit: {limit_text})[/bold green]")
        
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
            for label, url_path in config["endpoints"].items():
                url = urljoin(BASE_URL, url_path)
                task = progress.add_task(f"Scanning {label}...", total=None)
                
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(1000)
                    
                    if config["limit"] == 0 or config["limit"] > 25:
                        prev_height = 0
                        for _ in range(12):
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page.wait_for_timeout(1200)
                            curr_height = await page.evaluate("document.body.scrollHeight")
                            if curr_height == prev_height:
                                break
                            prev_height = curr_height
                            
                    links = await extract_links_from_list(page)
                    if config["limit"] > 0: links = links[:config["limit"]]
                        
                    for idx, link in enumerate(links):
                        fl = urljoin(BASE_URL, link)
                        if fl not in repo_to_contexts: repo_to_contexts[fl] = []
                        repo_to_contexts[fl].append(f"#{idx + 1} {label}")
                    progress.console.print(f"[dim]✓ {label}: Found {len(links)} items[/dim]")
                except Exception as e:
                    progress.console.print(f"[red]✗ Failed {label}: {e}[/red]")
                progress.remove_task(task)
        await page.close()

        if config["shallow"]:
            console.print("\n[bold cyan]Shallow Mode: Writing rankings to cache...[/bold cyan]")
            async with file_lock:
                with open(STATE_FILE, 'a', encoding='utf-8') as f:
                    for url, contexts in repo_to_contexts.items():
                        if url not in cache_status:
                            f.write(json.dumps({"trendshift_url": url, "rank_contexts": contexts, "scrape_mode": "shallow"}) + "\n")
            export_formats(config["formats"])
            await browser.close()
            return

        urls_to_scrape = {}
        for url, contexts in repo_to_contexts.items():
            if url not in cache_status or cache_status[url] == "shallow":
                urls_to_scrape[url] = contexts

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
            tasks = [fetch_repo_worker(context, url, contexts, semaphore, progress, task_id, retry_queue) for url, contexts in urls_to_scrape.items()]
            await asyncio.gather(*tasks)

            if retry_queue:
                console.print(f"\n[bold yellow]Retrying {len(retry_queue)} WAF-blocked URLs...[/bold yellow]")
                retry_task_id = progress.add_task("[yellow]Retrying blocked items...", total=len(retry_queue))
                retry_tasks = [fetch_repo_worker(context, url, contexts, semaphore, progress, retry_task_id, []) for url, contexts in retry_queue]
                await asyncio.gather(*retry_tasks)

        await browser.close()
        console.print("\n[bold green]Phase 3: Generating Artifacts[/bold green]")
        export_formats(config["formats"])
        console.print("[bold green]🎉 Done![/bold green]")

def get_cli_config():
    parser = argparse.ArgumentParser(description="Trendshift Advanced Scraper")
    parser.add_argument("--interactive", action="store_true", help="Force interactive mode")
    parser.add_argument("--shallow", action="store_true", help="Skip deep scraping")
    parser.add_argument("--limit", type=int, default=0, help="Max items per category")
    parser.add_argument("--concurrency", type=int, default=2, help="Concurrent browser tabs")
    parser.add_argument("--format", choices=["all", "json", "csv", "jsonl"], default="all")
    parser.add_argument("--endpoints", nargs="*", help="Direct URL paths")
    
    args = parser.parse_args()
    if any([args.shallow, args.limit, args.endpoints, args.concurrency != 2, args.format != "all"]) and not args.interactive:
        return {
            "shallow": args.shallow, "limit": args.limit, "concurrency": args.concurrency,
            "formats": [args.format] if args.format != "all" else ["json", "csv", "jsonl"],
            "endpoints": {p: p for p in (args.endpoints or ["/"])}
        }
    return None

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

if __name__ == "__main__":
    main()