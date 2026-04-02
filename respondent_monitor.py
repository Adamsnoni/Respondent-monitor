#!/usr/bin/env python3
"""
Respondent public study monitor.

What it does
- Opens Respondent's public research projects page in a real browser (Playwright)
- Collects visible study links from the browse page
- Visits each study page and extracts title / reward / summary
- Stores seen studies in SQLite
- Sends Telegram alerts only for new studies

Environment variables
- TELEGRAM_BOT_TOKEN: Telegram bot token (required for alerts)
- TELEGRAM_CHAT_ID: Telegram chat ID (required for alerts)
- RESPONDENT_BROWSE_URL: optional, defaults to https://www.respondent.io/research-projects
- HEADLESS: optional, 1 or 0, defaults to 1
- MAX_STUDIES_PER_RUN: optional, defaults to 40
- LOG_LEVEL: optional, defaults to INFO
- DB_PATH: optional, path to SQLite file, defaults to /data/respondent_studies.db
            (falls back to ./respondent_studies.db if /data is not writable)

Usage
  python respondent_monitor.py
"""

from __future__ import annotations

import gc
import logging
import os
import re
import sqlite3
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_BROWSE_URL = "https://www.respondent.io/research-projects"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def resolve_db_path() -> str:
    """Return DB path from env, falling back to /data or local dir."""
    env_path = os.getenv("DB_PATH", "").strip()
    if env_path:
        return env_path
    # Prefer /data (Render persistent disk mount point)
    data_dir = "/data"
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        return os.path.join(data_dir, "respondent_studies.db")
    # Fallback: same directory as this script
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "respondent_studies.db")


@dataclass
class Study:
    url: str
    title: str = ""
    reward: str = ""
    summary: str = ""
    full_body_text: str = ""
    posted_hint: str = ""
    source: str = "public"
    first_seen_at: str = ""
    last_seen_at: str = ""


class StudyStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS studies (
                url TEXT PRIMARY KEY,
                title TEXT,
                reward TEXT,
                summary TEXT,
                posted_hint TEXT,
                source TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT
            )
            """
        )
        self.conn.commit()

    def has(self, url: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM studies WHERE url = ?", (url,)).fetchone()
        return row is not None

    def upsert(self, study: Study) -> bool:
        """Returns True if inserted for the first time."""
        existing = self.conn.execute(
            "SELECT url FROM studies WHERE url = ?", (study.url,)
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO studies (
                    url, title, reward, summary, posted_hint, source, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    study.url,
                    study.title,
                    study.reward,
                    study.summary,
                    study.posted_hint,
                    study.source,
                    study.first_seen_at,
                    study.last_seen_at,
                ),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            """
            UPDATE studies
               SET title = ?, reward = ?, summary = ?, posted_hint = ?, source = ?, last_seen_at = ?
             WHERE url = ?
            """,
            (
                study.title,
                study.reward,
                study.summary,
                study.posted_hint,
                study.source,
                study.last_seen_at,
                study.url,
            ),
        )
        self.conn.commit()
        return False

    def close(self) -> None:
        self.conn.close()


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_url(url: str, base: str) -> str:
    if not url:
        return ""
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    # Keep only scheme, host, path; drop referral params to dedupe better.
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def extract_reward(text: str) -> str:
    patterns = [
        r"(?:\$|£|€|₦)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:-|to)\s?(?:\$|£|€|₦)?\s?\d[\d,]*(?:\.\d+)?)?",
        r"\b\d+\s?(?:USD|EUR|GBP|NGN)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(0))
    return ""


def extract_posted_hint(text: str) -> str:
    hints = [
        r"\b\d+\s+(?:minute|minutes|hour|hours|day|days|week|weeks)\s+ago\b",
        r"\b(?:today|yesterday)\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s+\d{4})?\b",
    ]
    for pattern in hints:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(0))
    return ""


def extract_summary_from_body(body_text: str, title: str) -> str:
    lines = [clean_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    for line in lines:
        if line == title:
            continue
        if len(line) < 40:
            continue
        if "cookie" in line.lower() or "privacy" in line.lower():
            continue
        return textwrap.shorten(line, width=220, placeholder="...")
    return ""


def build_telegram_message(studies: List[Study]) -> str:
    lines = [f"🔔 New Respondent studies: {len(studies)}"]
    for study in studies[:10]:
        title = study.title or "Untitled study"
        reward = f" | {study.reward}" if study.reward else ""
        
        full_text = f"{study.title} {study.summary}"
        if is_diary_study(full_text):
            lines.append(f"• {title}{reward}\n  Type: Diary Study\n  {study.url}")
        else:
            lines.append(f"• {title}{reward}\n  {study.url}")
            
    if len(studies) > 10:
        lines.append(f"...and {len(studies) - 10} more")
    return "\n".join(lines)


def send_telegram_alert(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logging.warning("Telegram credentials are not set; skipping alert.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    logging.info("Attempting to send Telegram alert to chat_id: %s", chat_id)
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False},
            timeout=30,
        )
        data = response.json() if response.text else {}
        is_ok = data.get("ok")
        if response.status_code == 200 and is_ok:
            logging.info("Telegram alert sent successfully.")
        else:
            logging.error(
                "Telegram rejected message. Status: %s, Response: %s. "
                "Ensure bot token is correct, chat_id is valid, and bot has been started.",
                response.status_code, response.text
            )
    except Exception as exc:
        logging.error("Failed to send Telegram alert (network/other issue): %s", exc)


def is_unmoderated_study(text: str) -> bool:
    text = text.lower()
    
    # Negative indicators
    negatives = ["zoom", "call", "live session", "1:1", "one-on-one", "one on one"]
    for neg in negatives:
        if neg in text:
            return False
            
    # Strong positive indicators
    strong_positives = [
        "unmoderated study", "unmoderated research study", 
        "this is an unmoderated study", "participate in an unmoderated study", 
        "self-paced unmoderated study"
    ]
    # Optional supporting indicators
    support_positives = [
        "self-paced", "self guided", "self-guided", "at your own pace", 
        "complete on your own time", "take at your convenience"
    ]
    
    for pos in strong_positives + support_positives:
        if pos in text:
            return True
            
    return False

def is_diary_study(text: str) -> bool:
    text = text.lower()
    
    phrases = [
        "diary study", "diary", "daily log", "daily logs", "daily check-in", 
        "daily check in", "journal", "journaling", "track your", "tracking your", 
        "record your", "log your", "over multiple days", "over several days", 
        "multiple days", "for 3 days", "for 5 days", "for 7 days", "for 1 week", 
        "for one week", "week-long", "longitudinal", "ongoing participation"
    ]
    
    for phrase in phrases:
        if phrase in text:
            return True
            
    return False


def harvest_study_links(page, browse_url: str, max_links: int) -> List[str]:
    logging.info("Opening browse page: %s", browse_url)
    page.goto(browse_url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(4000)

    # Scroll to reveal lazy-loaded results (Limit to 1 to save massive amount of memory)
    for _ in range(1):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1500)

    raw_links: Set[str] = set()
    hrefs = page.locator("a").evaluate_all(
        "elements => elements.map(el => el.getAttribute('href')).filter(Boolean)"
    )
    for href in hrefs:
        if "/projects/view/" in href:
            normalized = normalize_url(href, browse_url)
            if normalized:
                raw_links.add(normalized)

    links = sorted(raw_links)
    logging.info("Found %d candidate study links.", len(links))
    return links[:max_links]


def scrape_study_page(page, url: str) -> Optional[Study]:
    logging.info("Scraping study page: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2500)
    except PlaywrightTimeoutError:
        logging.warning("Timed out loading %s", url)
        return None

    title = ""
    for selector in ["h1", "main h1", "header h1", "meta[property='og:title']"]:
        try:
            if selector.startswith("meta"):
                content = page.locator(selector).get_attribute("content")
                title = clean_text(content or "")
            else:
                title = clean_text(page.locator(selector).first.inner_text(timeout=3000))
            if title:
                break
        except Exception:
            continue

    # Fetch body text once and reuse (avoids double evaluate)
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""

    if not title and not body_text:
        return None

    reward = extract_reward(body_text)
    posted_hint = extract_posted_hint(body_text)
    summary = extract_summary_from_body(body_text, title)
    now = utc_now_iso()

    return Study(
        url=url,
        title=title,
        reward=reward,
        summary=summary,
        full_body_text=body_text,
        posted_hint=posted_hint,
        source="public",
        first_seen_at=now,
        last_seen_at=now,
    )


def run_once() -> int:
    browse_url = os.getenv("RESPONDENT_BROWSE_URL", DEFAULT_BROWSE_URL).strip() or DEFAULT_BROWSE_URL
    headless = os.getenv("HEADLESS", "1").strip() != "0"
    try:
        max_studies = int(os.getenv("MAX_STUDIES_PER_RUN", "15"))
    except ValueError:
        max_studies = 15

    db_path = resolve_db_path()
    logging.info("Using DB at: %s", db_path)

    store = StudyStore(db_path)
    new_studies: List[Study] = []

    # Phase 1: Harvest links using a temporary context
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--js-flags=--max-old-space-size=128"]
        )
        browse_context = browser.new_context(user_agent=USER_AGENT)
        
        # Block heavy visual resources
        def intercept_route(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet", "websocket"]:
                route.abort()
            else:
                route.continue_()
                
        browse_context.route("**/*", intercept_route)
        main_page = browse_context.new_page()
        links = harvest_study_links(main_page, browse_url, max_studies)
        browser.close()

    if not links:
        logging.warning("No study links found on browse page.")

    # Phase 2: Scrape in small batches to guarantee Chromium memory is fully wiped
    batch_size = 5
    for i in range(0, len(links), batch_size):
        chunk = links[i : i + batch_size]
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox", 
                    "--disable-setuid-sandbox", 
                    "--disable-dev-shm-usage", 
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-extensions",
                    "--mute-audio",
                    "--js-flags=--max-old-space-size=128"
                ],
            )
            
            for url in chunk:
                study_context = browser.new_context(user_agent=USER_AGENT)
                study_context.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet", "websocket"] else route.continue_())
                study_page = study_context.new_page()
                
                try:
                    study = scrape_study_page(study_page, url)
                    if not study:
                        continue
                    
                    # Combine everything to guarantee we don't miss metadata
                    filter_blob = f"{study.title} {study.summary} {study.full_body_text}"
                    
                    if "unmoderated study" in filter_blob.lower():
                        logging.info("DEBUG: 'unmoderated study' matched in text for %s", study.title)
                        
                    if not is_unmoderated_study(filter_blob):
                        logging.info("Skipped (not Unmoderated Study): %s", study.title)
                        excerpt = study.full_body_text[:120].replace('\n', ' ')
                        logging.info("DEBUG Excerpt: %s...", excerpt)
                        continue

                    if is_diary_study(filter_blob):
                        logging.info("Accepted (Unmoderated Study + Diary Study): %s", study.title)
                    else:
                        logging.info("Accepted (Unmoderated Study): %s", study.title)

                    was_new = store.upsert(study)
                    if was_new:
                        new_studies.append(study)
                    time.sleep(1.5)
                except Exception as exc:
                    logging.exception("Failed to process %s: %s", url, exc)
                    continue
                finally:
                    study_context.close()
            
            browser.close()
            
        # Force Python to deep clean memory between batches
        gc.collect()

    if new_studies:
        logging.info("%d new studies found.", len(new_studies))
        send_telegram_alert(build_telegram_message(new_studies))
    else:
        logging.info("No new studies found.")

    store.close()
    return 0


def check_telegram_config() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logging.warning("Startup check: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing. Alerts are disabled.")
    else:
        masked_token = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"
        logging.info("Startup check: Telegram configured (Chat ID: %s, Token: %s)", chat_id, masked_token)


if __name__ == "__main__":
    setup_logging()
    check_telegram_config()
    try:
        while True:
            logging.info("Starting monitoring cycle...")
            run_once()
            logging.info("Cycle complete. Sleeping for 10 minutes...")
            time.sleep(600)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        raise SystemExit(130)
