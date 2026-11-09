"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


def _gcs_uri(gcs: Optional[str]) -> str:
    """Resolve and validate the GCS destination (flag or APPLYPILOT_GCS_URI)."""
    import os

    uri = (gcs or os.environ.get("APPLYPILOT_GCS_URI", "")).strip()
    if uri and (not uri.startswith("gs://") or len(uri) <= 5):
        console.print(f"[red]Invalid GCS location:[/red] {uri!r} (expected gs://BUCKET or gs://BUCKET/PREFIX)")
        raise typer.Exit(code=1)
    return uri


def _do_export(min_score: int, output: Optional[str], no_copy: bool, gcs: Optional[str],
               quiet: bool = False) -> int:
    """Export the shortlist (and optionally sync to GCS). Returns job count."""
    from pathlib import Path

    from applypilot.export import export_shortlist, sync_to_gcs

    result = export_shortlist(
        min_score=min_score,
        output_dir=Path(output).expanduser() if output else None,
        copy_files=not no_copy,
    )

    if not quiet and result["records"]:
        table = Table(title=f"Shortlist ({result['count']} jobs)", header_style="bold cyan")
        table.add_column("Score", justify="center")
        table.add_column("Company", style="bold")
        table.add_column("Role")
        table.add_column("Location")
        for r in result["records"]:
            table.add_row(str(r["fit_score"]), r["company"], r["title"], r["location"])
        console.print(table)
    if not result["count"]:
        console.print(
            f"[yellow]No tailored resumes with score >= {min_score} yet.[/yellow] "
            "Run: applypilot cycle"
        )
    console.print(f"[green]Shortlist ({result['count']} jobs) saved to[/green] {result['csv']}")

    gcs_uri = _gcs_uri(gcs)
    if gcs_uri:
        uploaded = sync_to_gcs(result["dir"], gcs_uri)
        console.print(f"[green]Synced to[/green] {gcs_uri} ({uploaded} file(s) uploaded)")
    return result["count"]


_GCS_HELP = "Also mirror the shortlist folder to gs://BUCKET/PREFIX (or set APPLYPILOT_GCS_URI)."


@app.command()
def export(
    min_score: int = typer.Option(7, "--min-score", help="Only include jobs scored at least this."),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="Output folder (default: ~/.applypilot/shortlist or APPLYPILOT_EXPORT_DIR).",
    ),
    no_copy: bool = typer.Option(
        False, "--no-copy", help="Only write shortlist.csv/.md; don't copy resumes into per-job folders.",
    ),
    gcs: Optional[str] = typer.Option(None, "--gcs", help=_GCS_HELP),
) -> None:
    """Export companies + tailored resumes for applying manually."""
    _bootstrap()
    _do_export(min_score, output, no_copy, gcs)


@app.command()
def cycle(
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score to tailor and export."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment."),
    cover: bool = typer.Option(False, "--cover", help="Also generate cover letters (more LLM calls)."),
    validation: str = typer.Option("normal", "--validation", help="strict | normal | lenient"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Shortlist folder."),
    gcs: Optional[str] = typer.Option(None, "--gcs", help=_GCS_HELP),
    loop_hours: float = typer.Option(
        0, "--loop-hours",
        help="Repeat forever every N hours (0 = run once; use with systemd/cron instead if possible).",
    ),
) -> None:
    """Headless run for servers: discover -> enrich -> score -> tailor -> pdf -> export.

    Never applies to anything. Safe to run from cron/systemd; overlapping runs
    are prevented with a lock file.
    """
    import fcntl
    import time as _time

    _bootstrap()

    from applypilot.config import APP_DIR, check_tier
    from applypilot.pipeline import run_pipeline

    check_tier(2, "AI scoring/tailoring")
    _gcs_uri(gcs)  # fail fast on a bad bucket address before hours of work

    from applypilot.config import PROFILE_PATH, RESUME_PATH
    missing = [str(p) for p in (PROFILE_PATH, RESUME_PATH) if not p.exists()]
    if missing:
        console.print("[red]Missing required file(s):[/red] " + ", ".join(missing))
        console.print("Copy config/profile.example.json and your resume.txt there first (see README).")
        raise typer.Exit(code=1)
    stages = ["discover", "enrich", "score", "tailor"] + (["cover"] if cover else []) + ["pdf"]

    lock_file = open(APP_DIR / ".cycle.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[yellow]Another 'applypilot cycle' is already running; exiting.[/yellow]")
        raise typer.Exit(code=0)

    while True:
        started = _time.time()
        log.info("Cycle start: stages=%s", ",".join(stages))
        try:
            result = run_pipeline(
                stages=stages, min_score=min_score, workers=workers,
                validation_mode=validation,
            )
            if result.get("errors"):
                log.warning("Some stages reported errors: %s", result.get("errors"))
        except Exception:
            log.exception("Pipeline run failed; exporting whatever is ready")

        # Always refresh the CSV, even if a stage failed part-way.
        try:
            _do_export(min_score, output, no_copy=False, gcs=gcs, quiet=True)
        except Exception:
            log.exception("Export/sync failed")
            if not loop_hours:
                raise typer.Exit(code=1)

        if not loop_hours:
            break
        wait = max(0.0, loop_hours * 3600 - (_time.time() - started))
        log.info("Cycle done in %.0f min; next run in %.0f min",
                 (_time.time() - started) / 60, wait / 60)
        _time.sleep(wait)


@app.command()
def companies(
    all_levels: bool = typer.Option(False, "--all-levels", help="Don't drop senior/manager titles."),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Also write the roles to this CSV file."),
    save: bool = typer.Option(False, "--save", help="Store the roles in the database for scoring/tailoring."),
) -> None:
    """List open roles at your target companies (companies.yaml) that someone in India can take."""
    _bootstrap()

    import csv as _csv
    from collections import defaultdict

    from applypilot.discovery.companies import collect, companies_config_path, store_postings

    console.print(f"[dim]Company list: {companies_config_path()}[/dim]")
    postings, report = collect(all_levels=all_levels)

    summary = Table(title="Target companies", header_style="bold cyan")
    for col, just in (("Company", "left"), ("Board", "left"), ("Openings", "right"),
                      ("Open to India", "right"), ("Matching roles", "right"), ("Note", "left")):
        summary.add_column(col, justify=just)
    for r in report:
        note = r["error"] or ("no openings right now" if r["source"] and not r["total"] else "")
        summary.add_row(r["company"], r["source"] or "-", str(r["total"]), str(r["eligible"]),
                        str(r["kept"]), f"[yellow]{note}[/yellow]" if note else "")
    console.print(summary)

    by_company: dict[str, list] = defaultdict(list)
    for p in postings:
        by_company[p.company].append(p)
    for name, items in by_company.items():
        t = Table(title=f"{name} ({len(items)})", header_style="bold", title_justify="left")
        t.add_column("Role")
        t.add_column("Eligibility")
        t.add_column("Link", overflow="fold")
        for p in sorted(items, key=lambda x: (x.eligibility, x.title)):
            t.add_row(p.title, p.eligibility, p.url)
        console.print(t)

    if csv_path:
        from pathlib import Path
        out = Path(csv_path).expanduser()
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(["company", "title", "eligibility", "location", "url", "apply_url", "posted_at"])
            for p in postings:
                w.writerow([p.company, p.title, p.eligibility, p.location, p.url, p.apply_url, p.posted_at or ""])
        console.print(f"[green]Wrote {len(postings)} roles to[/green] {out}")

    if save:
        from applypilot.database import get_connection
        new, existing = store_postings(get_connection(), postings)
        console.print(f"[green]Saved:[/green] {new} new, {existing} already in the database. "
                      "Run [bold]applypilot cycle[/bold] (or run score tailor pdf) to tailor resumes.")


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
