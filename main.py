"""
main.py — Orchestrator for the craft school scholarship finder.

Usage:
    python main.py

Reads config.yaml, crawls all target sites, parses with LLM,
stores results in SQLite, and sends a weekly email digest.
"""

import argparse
import asyncio
import os
import sys
from datetime import date

import yaml

from crawler import crawl_all_sites
from database import (
    DB_PATH,
    init_db,
    upsert_opportunity,
    mark_inactive_if_not_seen_today,
    get_new_today,
    get_upcoming_deadlines,
    get_all_active,
    get_recently_deactivated,
    make_id,
)
from email_digest import build_html_email, send_digest
from llm_parser import parse_and_filter_pages


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main(dry_run: bool = False):
    print("=" * 60)
    print("Craft Scholarship Finder — starting run")
    print(f"Date: {date.today().isoformat()}")
    if dry_run:
        print("MODE: DRY RUN (first 3 sites only, DB and email skipped)")
    print("=" * 60)

    # ── Load config ──────────────────────────────────────────────
    config = load_config()
    # targets lives at the top level of config.yaml
    targets = config.get("targets") or config.get("crawl", {}).get("targets", [])
    max_depth = config["crawl"].get("max_depth", 3)
    internal_only = config["crawl"].get("internal_links_only", True)
    keyword_flags = config["crawl"].get("keyword_flags", [])
    site_timeout = config["crawl"].get("per_site_timeout_seconds", 90)
    page_cap = config["crawl"].get("per_site_page_cap", 20)
    global_timeout = config["crawl"].get("global_timeout_seconds", 2700)
    recipient = config["email"]["recipient"]
    sender = config["email"]["sender"]

    if not targets:
        print("ERROR: No target URLs found in config.yaml under 'targets'")
        sys.exit(1)
    if dry_run:
        targets = targets[:3]
        print(f"[dry-run] Limiting to first 3 targets")

    print(f"Targets:          {len(targets)} sites")
    print(f"Max crawl depth:  {max_depth}")
    print(f"Per-site timeout: {site_timeout}s")
    print(f"Per-site page cap:{page_cap} pages")
    print(f"Global timeout:   {global_timeout/3600:.1f}h")

    # ── Init DB ───────────────────────────────────────────────────
    if not os.path.exists(DB_PATH):
        print(f"No existing database found — creating fresh DB at {DB_PATH}")
    else:
        print(f"Existing database found at {DB_PATH}")
    init_db(DB_PATH)  # CREATE TABLE IF NOT EXISTS — safe on both new and existing DBs
    print(f"Database ready: {DB_PATH}")

    # ── Crawl ─────────────────────────────────────────────────────
    print("\n── CRAWLING ──────────────────────────────────────────────")
    site_pages = asyncio.run(
        crawl_all_sites(
            targets,
            max_depth=max_depth,
            internal_links_only=internal_only,
            keyword_flags=keyword_flags,
            site_timeout=site_timeout,
            page_cap=page_cap,
            global_timeout=global_timeout,
        )
    )

    # ── Parse & Filter ────────────────────────────────────────────
    print("\n── LLM PARSING ───────────────────────────────────────────")
    all_pages = [p for pages in site_pages.values() for p in pages]
    print(f"[DEBUG] Crawl complete, starting LLM phase")
    print(f"[DEBUG] Number of flagged pages to parse: {len(all_pages)}")

    try:
        print(f"[DEBUG] Calling asyncio.run(parse_and_filter_pages)...")
        all_opportunities = asyncio.run(parse_and_filter_pages(all_pages))
        print(f"[DEBUG] asyncio.run completed successfully")
    except Exception:
        import traceback
        print("[DEBUG] EXCEPTION in LLM phase — full traceback:")
        traceback.print_exc()
        all_opportunities = []

    print(f"\nTotal opportunities found: {len(all_opportunities)}")

    if dry_run:
        print("\n── DRY RUN RESULTS ───────────────────────────────────────")
        for opp in all_opportunities:
            print(f"  [{opp.get('eligibility_match', '?')}] {opp.get('school')} — {opp.get('name')} ({opp.get('type')})")
        print(f"\nDry run complete. {len(all_opportunities)} opportunities found. DB and email skipped.")
        return

    # ── Store to DB ───────────────────────────────────────────────
    print("\n── DATABASE ──────────────────────────────────────────────")
    seen_ids = []
    counts = {"new": 0, "updated": 0}

    for opp in all_opportunities:
        if opp.get("eligibility_match") == "not eligible":
            continue
        result = upsert_opportunity(opp)
        counts[result] += 1
        opp_id = make_id(
            opp.get("school", ""),
            opp.get("name", ""),
            opp.get("url", ""),
        )
        seen_ids.append(opp_id)

    deactivated_count = mark_inactive_if_not_seen_today(seen_ids)
    print(f"New: {counts['new']} | Updated: {counts['updated']} | Deactivated: {deactivated_count}")

    # ── Build Email ───────────────────────────────────────────────
    print("\n── EMAIL DIGEST ──────────────────────────────────────────")
    new_today = get_new_today()
    upcoming = get_upcoming_deadlines(days=30)
    all_active = get_all_active()
    deactivated = get_recently_deactivated()

    print(f"[EMAIL] {len(all_active)} opportunities found in DB")
    print(f"[EMAIL] New this week: {len(new_today)}")
    print(f"[EMAIL] Upcoming deadlines (30d): {len(upcoming)}")
    print(f"[EMAIL] Recently removed: {len(deactivated)}")

    html_body = build_html_email(new_today, upcoming, all_active, deactivated)

    # ── Send Email ────────────────────────────────────────────────
    print(f"[EMAIL] Preparing to send digest to {recipient}")

    if not os.environ.get("SENDGRID_API_KEY"):
        print("[EMAIL] WARNING: SENDGRID_API_KEY not set — saving digest locally instead")
        with open("digest_preview.html", "w") as f:
            f.write(html_body)
        print("[EMAIL] Digest saved to digest_preview.html")
    else:
        try:
            success = send_digest(html_body, recipient=recipient, sender=sender)
            if success:
                print(f"[EMAIL] Digest sent successfully to {recipient}")
            else:
                print(f"[EMAIL] Send failed — check SendGrid response above")
        except Exception as e:
            print(f"[EMAIL] Exception during send: {type(e).__name__}: {e}")

    # ── Summary ───────────────────────────────────────────────────
    print("\n── SUMMARY ───────────────────────────────────────────────")
    print(f"Sites crawled:    {len(targets)}")
    print(f"Pages flagged:    {len(all_pages)}")
    print(f"Opps extracted:   {len(all_opportunities)}")
    print(f"New this week:    {counts['new']}")
    print(f"Updated:          {counts['updated']}")
    print(f"Deactivated:      {deactivated_count}")
    print(f"Active in DB:     {len(all_active)}")
    print("=" * 60)
    print("Run complete.")


if __name__ == "__main__":
    import traceback

    parser = argparse.ArgumentParser(description="Craft school scholarship crawler")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl only the first 3 sites and print results; skip DB writes and email",
    )
    args = parser.parse_args()

    # Check required env vars
    missing = [v for v in ["GEMINI_API_KEY"] if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        print("  GEMINI_API_KEY  — get a free key at https://aistudio.google.com")
        sys.exit(1)

    sendgrid_present = bool(os.environ.get("SENDGRID_API_KEY"))
    print(f"[env] GEMINI_API_KEY: set")
    print(f"[env] SENDGRID_API_KEY: {'set' if sendgrid_present else 'NOT SET — digest will be saved locally'}")

    try:
        main(dry_run=args.dry_run)
    except Exception:
        print("\n" + "=" * 60)
        print("FATAL ERROR — full traceback:")
        print("=" * 60)
        traceback.print_exc()
        sys.exit(1)
