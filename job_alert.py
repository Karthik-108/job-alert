#!/usr/bin/env python3
"""Fresher Job Alert Bot
Checks many sources, filters for fresher roles, sends new ones to Telegram.

  python job_alert.py --test        send a test Telegram message
  python job_alert.py --dry-run     show what would match right now (nothing saved/sent)
  python job_alert.py               run forever, check every interval_minutes
  python job_alert.py --once        check once and exit (for cron / GitHub Actions)
"""
import argparse
import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
log = logging.getLogger("jobalert")


@dataclass
class Job:
    source: str
    title: str
    url: str
    company: str = ""
    location: str = ""
    text: str = ""

    @property
    def id(self):
        key = self.url or (self.title + self.company)
        return hashlib.sha1(key.encode()).hexdigest()[:16]


# ----------------------------------------------------------------- helpers
def dig(obj, path):
    """dig({'a': [{'b': 1}]}, 'a.0.b') -> 1"""
    for k in filter(None, str(path).split(".")):
        if obj is None:
            return None
        obj = obj[int(k)] if isinstance(obj, list) else obj.get(k)
    return obj


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------- fetchers
def fetch_google_news(cfg):
    jobs = []
    for q in cfg["queries"]:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(q + " when:2d")
               + "&hl=en-IN&gl=IN&ceid=IN:en")
        for e in feedparser.parse(url).entries:
            jobs.append(Job(cfg["name"], e.get("title", ""), e.get("link", ""),
                            text=BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")))
    return jobs


def fetch_rss(cfg):
    return [Job(cfg["name"], e.get("title", ""), e.get("link", ""),
                text=BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" "))
            for e in feedparser.parse(cfg["url"]).entries]


def fetch_telegram(cfg):
    jobs = []
    for ch in cfg.get("channels") or []:
        soup = BeautifulSoup(get(f"https://t.me/s/{ch}").text, "html.parser")
        for m in soup.select(".tgme_widget_message_wrap"):
            t = m.select_one(".tgme_widget_message_text")
            if not t:
                continue
            text = t.get_text(" ", strip=True)
            post = m.select_one("a.tgme_widget_message_date")
            ext = next((a["href"] for a in t.select("a[href]")
                        if a["href"].startswith("http") and "t.me" not in a["href"]), "")
            jobs.append(Job(f"{cfg['name']}:{ch}", text[:140], ext or (post["href"] if post else ""),
                            text=text))
    return jobs


def fetch_workday(cfg):
    jobs, limit = [], 20
    api = f"https://{cfg['host']}/wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs"
    for page in range(cfg.get("pages", 3)):
        body = {"appliedFacets": {}, "limit": limit, "offset": page * limit,
                "searchText": cfg.get("search", "fresher")}
        r = requests.post(api, json=body, headers=UA, timeout=25)
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        for p in posts:
            jobs.append(Job(cfg["name"], p.get("title", ""),
                            f"https://{cfg['host']}/{cfg['site']}{p.get('externalPath', '')}",
                            company=cfg["name"], location=p.get("locationsText", "")))
        if len(posts) < limit:
            break
    return jobs


def fetch_greenhouse(cfg):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs").json()
    return [Job(cfg["name"], j["title"], j["absolute_url"], company=cfg["name"],
                location=(j.get("location") or {}).get("name", "")) for j in data.get("jobs", [])]


def fetch_lever(cfg):
    data = get(f"https://api.lever.co/v0/postings/{cfg['company']}?mode=json").json()
    return [Job(cfg["name"], j["text"], j["hostedUrl"], company=cfg["name"],
                location=(j.get("categories") or {}).get("location", "")) for j in data]


def fetch_json_api(cfg):
    r = requests.request(cfg.get("method", "GET"), cfg["url"], json=cfg.get("body") or None,
                         headers={**UA, **(cfg.get("headers") or {})}, timeout=25)
    r.raise_for_status()
    items = dig(r.json(), cfg["list_path"]) if cfg.get("list_path") else r.json()
    jobs = []
    for it in items or []:
        url = cfg["url_template"].format_map(it) if cfg.get("url_template") else dig(it, cfg.get("url_field", "url"))
        jobs.append(Job(cfg["name"], str(dig(it, cfg.get("title_field", "title"))), url or "",
                        company=cfg["name"], location=str(dig(it, cfg.get("location_field", "location")) or "")))
    return jobs


def fetch_html_links(cfg):
    soup = BeautifulSoup(get(cfg["url"]).text, "html.parser")
    return [Job(cfg["name"], a.get_text(" ", strip=True), urljoin(cfg["url"], a["href"]))
            for a in soup.select("a[href]") if len(a.get_text(strip=True)) > 15]


FETCHERS = {"google_news": fetch_google_news, "rss": fetch_rss, "telegram": fetch_telegram,
            "workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "json_api": fetch_json_api, "html_links": fetch_html_links}
LENIENT_BY_DEFAULT = {"workday", "greenhouse", "lever", "json_api"}


# ----------------------------------------------------------------- filtering
def is_fresher(job, f, lenient):
    title = job.title.lower()
    text = f"{job.title} {job.text} {job.location}".lower()
    if any(w in title for w in f.get("exclude_title", [])):
        return False
    if any(re.search(p, text) for p in f.get("exclude_regex", [])):
        return False
    locs = [l.lower() for l in f.get("locations", [])]
    if locs and job.location and not any(l in job.location.lower() for l in locs):
        return False
    if any(str(k).lower() in text for k in f.get("include", [])):
        return True
    return lenient  # company career sites: no senior/exp red flags = keep


def is_watched(job, cfg):
    blob = f"{job.title} {job.company} {job.source} {job.text}".lower()
    return any(c.lower() in blob for c in cfg.get("watch_companies", []))


# ----------------------------------------------------------------- notify
def send_telegram(msg):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[no telegram configured]\n" + msg + "\n")
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=25)
    if not r.ok:
        log.error("Telegram error: %s", r.text)
    return r.ok


def format_job(job, star):
    lines = [("⭐ " if star else "🆕 ") + job.title[:200]]
    if job.company:
        lines.append("🏢 " + job.company)
    if job.location:
        lines.append("📍 " + job.location)
    lines.append("🔗 " + job.url)
    lines.append("via " + job.source)
    return "\n".join(lines)


# ----------------------------------------------------------------- main loop
def run_once(cfg, dry=False, notify_first=False):
    db = sqlite3.connect(cfg.get("db", "seen.db"))
    db.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, title TEXT, url TEXT, ts REAL)")
    first_run = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    sent = 0
    for src in cfg["sources"]:
        kind = src["type"]
        try:
            jobs = FETCHERS[kind](src)
        except Exception as e:  # one broken source must never kill the rest
            log.warning("source %s (%s) failed: %s", src.get("name"), kind, e)
            continue
        lenient = src.get("lenient", kind in LENIENT_BY_DEFAULT)
        log.info("%s: %d items", src.get("name"), len(jobs))
        for job in jobs:
            if not job.url or db.execute("SELECT 1 FROM seen WHERE id=?", (job.id,)).fetchone():
                continue
            ok = is_fresher(job, cfg["filters"], lenient)
            if dry:
                if ok:
                    print(format_job(job, is_watched(job, cfg)), "\n")
                continue
            if first_run and not notify_first:
                pass  # first run: just remember what already exists, don't spam
            elif ok:
                send_telegram(format_job(job, is_watched(job, cfg)))
                sent += 1
                time.sleep(0.5)
            db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?)", (job.id, job.title, job.url, time.time()))
        db.commit()
    if first_run and not dry and not notify_first:
        log.info("First run: existing jobs saved silently. New ones will be alerted from now on.")
    log.info("done, %d alerts sent", sent)
    db.close()


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--notify-first", action="store_true", help="alert on the very first run too")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_env()
    if a.test:
        print("sent" if send_telegram("✅ Job alert bot is working, bro!") else "failed / not configured")
        return
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    if a.dry_run or a.once:
        return run_once(cfg, dry=a.dry_run, notify_first=a.notify_first)
    while True:
        run_once(cfg, notify_first=a.notify_first)
        time.sleep(cfg.get("interval_minutes", 10) * 60)


if __name__ == "__main__":
    main()
