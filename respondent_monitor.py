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
- MAX_STUDIES_PER_RUN: optional, defaults to 20
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

DEFAULT_BROWSE_URL = "https://www.respondent.io/research-projects"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def resolve_db_path() -> str:
    """Return DB path from env, checking DB_PATH then DATABASE_PATH, falling back to /data or local dir."""
    env_path = os.getenv("DB_PATH", "").strip() or os.getenv("DATABASE_PATH", "").strip()
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
        return textwrap.shorten(line, width=300, placeholder="...")
    return ""


def build_telegram_message(studies: List[Study]) -> str:
    lines = [f"🔔 New Respondent studies: {len(studies)}"]
    
    for study in studies[:10]:
        title = study.title or "Untitled study"
        reward = study.reward or "Not specified"
        summary = study.summary or "No description"
        
        full_text = f"{study.title} {study.summary} {study.full_body_text}".lower()
        
        if "unmoderated study" in full_text:
            study_type = "Unmoderated Study"
        elif "diary study" in full_text:
            study_type = "Diary Study"
        elif "moderated" in full_text:
            study_type = "Moderated"
        else:
            study_type = "Unknown"
            
        study_block = (
            "🚨 New Respondent Study\n\n"
            f"• {title} | {reward}\n"
            f"🧪 Type: {study_type}\n\n"
            "📄 Description:\n"
            f"{summary}\n\n"
            f"🔗 {study.url}"
        )
        lines.append(study_block)
            
    if len(studies) > 10:
        lines.append(f"...and {len(studies) - 10} more")
        
    return "\n\n---\n\n".join(lines)


def get_telegram_chat_ids() -> List[str]:
    raw_ids = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if raw_ids:
        chat_ids = [c.strip() for c in raw_ids.split(",") if c.strip()]
        if chat_ids:
            return chat_ids
    fallback_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if fallback_id:
        return [fallback_id]
    return []


def send_telegram_alert(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_telegram_chat_ids()
    if not token or not chat_ids:
        logging.warning("Telegram credentials are not set; skipping alert.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
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
                logging.info("Telegram alert sent successfully to %s.", chat_id)
            else:
                logging.error(
                    "Telegram rejected message for chat_id %s. Status: %s, Response: %s. "
                    "Ensure bot token is correct, chat_id is valid, and bot has been started.",
                    chat_id, response.status_code, response.text
                )
        except Exception as exc:
            logging.error("Failed to send Telegram alert to %s (network/other issue): %s", chat_id, exc)


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



API_PROJECTS_URL = "https://www.respondent.io/api/projects"


def fetch_public_api_studies(max_studies: int = 50) -> List[Study]:
    """Fetch public unauthenticated study listings directly from Respondent's public API."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    studies: List[Study] = []
    page = 1
    
    while len(studies) < max_studies:
        url = f"{API_PROJECTS_URL}?page={page}"
        logging.info("Fetching public API page %d: %s", page, url)
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                logging.warning("API returned HTTP %d", resp.status_code)
                break
                
            data = resp.json()
            results = data.get("results", [])
            if not results:
                logging.info("No more results on API page %d", page)
                break
                
            now = utc_now_iso()
            for item in results:
                study_id = str(item.get("id", "")).strip()
                title = clean_text(item.get("name", ""))
                description = clean_text(item.get("description", ""))
                remuneration = item.get("respondentRemuneration")
                reward = f"${remuneration}" if remuneration is not None else "Not specified"
                kind = item.get("kindOfResearch")
                published_at = str(item.get("publishedAt", "")).strip()
                referral_link = str(item.get("referralLink", "")).strip()
                
                if referral_link:
                    study_url = referral_link
                elif study_id:
                    study_url = f"https://app.respondent.io/projects/view/{study_id}"
                else:
                    continue
                    
                full_body_text = f"{title} {description}"
                summary = textwrap.shorten(description, width=300, placeholder="...") if description else ""
                
                study_obj = Study(
                    url=study_url,
                    title=title,
                    reward=reward,
                    summary=summary,
                    full_body_text=full_body_text,
                    posted_hint=published_at,
                    source="public_api",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                setattr(study_obj, "kind_of_research", kind)
                studies.append(study_obj)
                
                if len(studies) >= max_studies:
                    break
                    
            page += 1
        except Exception as exc:
            logging.error("Failed to fetch public API studies: %s", exc)
            break
            
    logging.info("Fetched %d total studies from public API.", len(studies))
    return studies


def run_once() -> int:
    try:
        max_studies = int(os.getenv("MAX_STUDIES_PER_RUN", "50"))
    except ValueError:
        max_studies = 50

    db_path = resolve_db_path()
    logging.info("Using DB at: %s", db_path)

    store = StudyStore(db_path)
    new_studies: List[Study] = []

    studies = fetch_public_api_studies(max_studies=max_studies)

    for study in studies:
        filter_blob = f"{study.title} {study.summary} {study.full_body_text}"
        kind_of_research = getattr(study, "kind_of_research", None)
        
        # Accept if kindOfResearch == 4 (Unmoderated) or text detection matches
        is_unmod = (kind_of_research == 4) or is_unmoderated_study(filter_blob)
        if not is_unmod:
            logging.info("Skipped (not Unmoderated Study): %s", study.title)
            continue

        if is_diary_study(filter_blob):
            logging.info("Accepted (Unmoderated Study + Diary Study): %s", study.title)
        else:
            logging.info("Accepted (Unmoderated Study): %s", study.title)

        was_new = store.upsert(study)
        if was_new:
            new_studies.append(study)

    if new_studies:
        logging.info("%d new studies found.", len(new_studies))
        send_telegram_alert(build_telegram_message(new_studies))
    else:
        logging.info("No new studies found.")

    store.close()
    return 0


def check_telegram_config() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_telegram_chat_ids()
    if not token or not chat_ids:
        logging.warning("Startup check: TELEGRAM_BOT_TOKEN or chat IDs (TELEGRAM_CHAT_IDS / TELEGRAM_CHAT_ID) is missing. Alerts are disabled.")
    else:
        masked_token = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"
        logging.info("Startup check: Telegram configured for %d chat ID(s): %s (Token: %s)", len(chat_ids), ", ".join(chat_ids), masked_token)


def get_check_interval() -> int:
    raw = os.getenv("CHECK_INTERVAL_SECONDS", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return 600


if __name__ == "__main__":
    setup_logging()
    check_telegram_config()
    interval = get_check_interval()
    try:
        while True:
            logging.info("Starting monitoring cycle...")
            run_once()
            logging.info("Cycle complete. Sleeping for %d seconds...", interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        raise SystemExit(130)
