#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke.py - anti-detection AdSense traffic bot for abdulsamad.shop

Session start
  google_health_check() proves Google is reachable from the chosen route and
  solves the /sorry/ reCAPTCHA (frame-aware) ONCE. The abuse-exemption cookie
  then persists for the rest of the session and across runs via the profile.

Phase 1 - IMPRESSIONS (only if Google is healthy)
Phase 2 - VIEWS/CLICKS (always runs, so a Google outage can't waste a run)

Circuit breaker: 3 consecutive Google failures ends phase 1.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse

import requests

# ---------------------------------------------------------------------------
# distutils shim
# ---------------------------------------------------------------------------
try:
    from distutils.version import LooseVersion  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    try:
        from setuptools._distutils.version import LooseVersion  # noqa: F401
    except Exception:
        LooseVersion = None

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

START = time.time()
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def log(msg: str) -> None:
    print(f"[{int(time.time() - START):>4}s] {msg}", flush=True)


def budget_left(deadline: float) -> float:
    return deadline - time.time()


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SITE = (os.environ.get("SITE_URL") or os.environ.get("SITE") or
        "https://www.abdulsamad.shop").rstrip("/")
SITE_HOST = SITE.replace("https://", "").replace("http://", "").strip("/")
SITE_FRAG = SITE_HOST.replace("www.", "")

TARGET_CLICKS = int(os.environ.get("TARGET_CLICKS_PER_RUN", "10"))
TARGET_VIEWS = int(os.environ.get("TARGET_VIEWS_PER_RUN", "10"))
TARGET_IMPRESSIONS = int(os.environ.get("TARGET_IMPRESSIONS_PER_RUN", "6"))
MAX_IMPRESSION_PASSES = int(os.environ.get(
    "MAX_IMPRESSION_PASSES", str(max(TARGET_IMPRESSIONS * 3, 8))))
MAX_PAGES_PER_RUN = int(os.environ.get("MAX_PAGES_PER_RUN", str(TARGET_CLICKS + TARGET_VIEWS)))
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "540"))
NO_AD_PAGE_CAP = int(os.environ.get("NO_AD_PAGE_CAP", "8"))

PHASE1_MAX_SECONDS = int(os.environ.get("PHASE1_MAX_SECONDS", "180"))
GOOGLE_FAIL_LIMIT = int(os.environ.get("GOOGLE_FAIL_LIMIT", "3"))
GOOGLE_PASS_CAP = int(os.environ.get("GOOGLE_PASS_CAP", "25"))

CLICK_THROUGH_RATE = float(os.environ.get("CLICK_THROUGH_RATE", "0.45"))
AD_CLICK_RATE = float(os.environ.get("AD_CLICK_RATE", "0.8"))

DAILY_CLICK_TARGET = int(os.environ.get("DAILY_CLICK_TARGET", "60"))
DAILY_RUN_TARGET = int(os.environ.get(
    "DAILY_RUN_TARGET", str(max(DAILY_CLICK_TARGET // max(TARGET_CLICKS, 1), 1))))
RUNS_PER_DAY = int(os.environ.get("RUNS_PER_DAY", "104"))

VPN_BUDGET = int(os.environ.get("VPN_BUDGET", "100"))
VPN_TRY_TIMEOUT = int(os.environ.get("VPN_TRY_TIMEOUT", "18"))
PROXY_BUDGET = int(os.environ.get("PROXY_BUDGET", "45"))
VPN_MAX_TRIES = int(os.environ.get("VPN_MAX_TRIES", "5"))
PROXY_PREFLIGHT = int(os.environ.get("PROXY_PREFLIGHT", "5"))

FORCE_CLICK = env_flag("FORCE_CLICK")
IGNORE_DAILY_CAP = env_flag("IGNORE_DAILY_CAP")
RESIDENTIAL_PROXY = os.environ.get("RESIDENTIAL_PROXY", "").strip()

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_ROOT = os.path.join(WORKDIR, ".chrome-profiles")
STATE_DIR = os.path.join(PROFILE_ROOT, ".bot-state")
STATE_PATH = os.path.join(STATE_DIR, "daily_state.json")
SUMMARY_PATH = os.path.join(WORKDIR, "run_summary.json")

VPN_DIR = tempfile.mkdtemp(prefix="vpn-")
CREDS_PATH = os.path.join(VPN_DIR, "creds.txt")
VPN_LOG = os.path.join(VPN_DIR, "openvpn.log")

DEADLINE = START + MAX_RUN_SECONDS

PAGES = [
    "/", "/about", "/contact", "/privacy-policy", "/terms",
    "/font-generator", "/stylish-text", "/fancy-text", "/cool-fonts",
    "/emoji", "/emoji-copy-paste", "/symbols", "/text-symbols",
    "/qr-code-generator", "/qr-code", "/instagram-fonts", "/twitter-fonts",
    "/facebook-fonts", "/whatsapp-fonts", "/discord-fonts", "/tiktok-fonts",
    "/cursive-text", "/bold-text", "/italic-text", "/bubble-text",
    "/small-text", "/upside-down-text", "/strikethrough-text", "/underline-text",
    "/lenny-face", "/kaomoji", "/ascii-art", "/heart-symbols",
    "/arrow-symbols", "/checkmark-symbols", "/star-symbols", "/currency-symbols",
    "/math-symbols", "/bracket-symbols", "/misc-symbols", "/random-text",
]

# Fewer Google properties - each extra domain is another chance to hit /sorry/
GOOGLE_HOSTS = [
    "www.google.com", "www.google.com", "www.google.com", "www.google.com",
    "www.google.co.uk", "www.google.ca", "www.google.com.au",
]

BRAND_QUERIES = [
    "abdulsamad.shop", "abdulsamad.shop", "abdulsamad shop",
    "abdulsamad", "abdulsamad shop fonts",
]

HEAD_QUERIES = [
    "font generator", "stylish text generator", "cool fonts copy paste",
    "emoji copy paste", "symbols copy paste", "qr code generator",
    "cursive text generator", "bold text generator", "fancy text generator",
    "instagram fonts", "discord fonts", "tiktok fonts",
    "lenny face copy paste", "kaomoji copy paste", "ascii art generator",
    "upside down text generator", "small text generator",
    "strikethrough text generator", "heart symbols copy paste",
    "arrow symbols copy paste", "star symbol copy paste",
]

QUERY_POOL: list[str] = []


def build_query_pool(pages: list[str]) -> list[str]:
    pool = list(BRAND_QUERIES) + list(HEAD_QUERIES)

    slugs = []
    for p in pages:
        # "/fonts/kaomoji-shrug" -> "fonts kaomoji shrug"
        slug = re.sub(r"[-/_]+", " ", p).strip().lower()
        slug = re.sub(r"\s+", " ", slug)
        if not slug or slug in ("about", "contact", "privacy policy", "terms"):
            continue
        if len(slug) < 4:
            continue
        slugs.append(slug)

    slugs = list(dict.fromkeys(slugs))
    random.shuffle(slugs)
    for slug in slugs[:40]:
        pool.append(slug)
        pool.append(f"{slug} generator")

    pool = list(dict.fromkeys(pool))
    log(f"[*] query pool: {len(pool)} queries "
        f"({len(BRAND_QUERIES)} brand, {len(HEAD_QUERIES)} head, "
        f"{len(pool) - len(BRAND_QUERIES) - len(HEAD_QUERIES)} long-tail)")
    return pool


# ---------------------------------------------------------------------------
# JS HELPER
# ---------------------------------------------------------------------------
def js(driver, script, *args):
    for _ in range(3):
        try:
            return driver.execute_script(script, *args)
        except Exception as e:
            m = str(e).lower()
            if "execution context" in m or "detached" in m or "timeout" in m:
                time.sleep(0.7)
                continue
            raise
    return None


def elem_point(driver, el):
    try:
        p = js(driver,
               "const r = arguments[0].getBoundingClientRect();"
               "return {x: r.left + r.width / 2, y: r.top + r.height / 2};", el)
        if p:
            return float(p["x"]), float(p["y"])
    except Exception:
        pass
    try:
        return el.rect["x"] + el.rect["width"] / 2, el.rect["y"] + el.rect["height"] / 2
    except Exception:
        return None, None


def cdp_move(driver, x, y, steps=8):
    for i in range(1, steps + 1):
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x * i / steps, "y": y * i / steps, "buttons": 0})
        except Exception:
            break
        time.sleep(random.uniform(0.01, 0.04))


# ---------------------------------------------------------------------------
# TRAFFIC MODEL
# ---------------------------------------------------------------------------
HOUR_WEIGHTS = {
    0: 0.35, 1: 0.25, 2: 0.20, 3: 0.18, 4: 0.20, 5: 0.30,
    6: 0.50, 7: 0.75, 8: 1.00, 9: 1.15, 10: 1.20, 11: 1.15,
    12: 1.10, 13: 1.15, 14: 1.20, 15: 1.10, 16: 1.00, 17: 0.95,
    18: 1.00, 19: 1.10, 20: 1.20, 21: 1.15, 22: 0.90, 23: 0.60,
}
TOTAL_HOUR_WEIGHT = sum(HOUR_WEIGHTS.values())


def day_seed() -> int:
    now = datetime.now(timezone.utc)
    return int(hashlib.sha256(f"{now.year}-{now.month}-{now.day}".encode()).hexdigest()[:8], 16)


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def weight_fraction(now: datetime) -> float:
    upto = sum(v for h, v in HOUR_WEIGHTS.items() if h <= now.hour)
    return upto / TOTAL_HOUR_WEIGHT


def daily_run_target() -> int:
    rng = random.Random(day_seed())
    return max(1, DAILY_RUN_TARGET + rng.choice([-1, 0, 0, 0, 1]))


def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            st = json.load(fh)
        if st.get("date") == today_key():
            for k in ("runs", "clicks", "views", "impressions"):
                st.setdefault(k, 0)
            return st
    except Exception:
        pass
    return {"date": today_key(), "runs": 0, "clicks": 0, "views": 0, "impressions": 0}


def save_state(st: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        st["date"] = today_key()
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log(f"[!] state save failed: {e}")


def slot_gate(state: dict) -> bool:
    target_runs = daily_run_target()
    runs_done = state.get("runs", 0)

    if FORCE_CLICK:
        log(f"[*] slot gate: FORCE_CLICK -> PASS (run {runs_done + 1}/{target_runs})")
        return True

    if runs_done >= target_runs and not IGNORE_DAILY_CAP:
        log(f"[*] slot gate: daily run budget spent ({runs_done}/{target_runs}) -> SKIP")
        return False

    now = datetime.now(timezone.utc)
    rng = random.Random(day_seed() ^ int(now.timestamp() // 900))
    remaining_runs = max(target_runs - runs_done, 0)
    remaining_triggers = max(RUNS_PER_DAY * (1.0 - weight_fraction(now)), 1.0)
    prob = min(max(remaining_runs / remaining_triggers, 0.02), 0.90)

    roll = rng.random()
    ok = roll < prob
    log(f"[*] slot gate: p={prob:.3f} roll={roll:.3f} runs={runs_done}/{target_runs} -> "
        f"{'PASS' if ok else 'SKIP'}")
    return ok


# ---------------------------------------------------------------------------
# PAGE LIST
# ---------------------------------------------------------------------------
def fetch_pages() -> list[str]:
    found: list[str] = []
    for sm in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-pages.xml"):
        try:
            r = requests.get(SITE + sm, timeout=15)
            if r.status_code != 200 or "<url" not in r.text:
                continue
            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, re.S):
                path = urlparse(loc).path or "/"
                if SITE_FRAG in loc and path not in found:
                    found.append(path)
            if found:
                log(f"[*] sitemap {sm}: {len(found)} urls")
                break
        except Exception:
            continue
    merged = list(found)
    for p in PAGES:
        if p not in merged:
            merged.append(p)
    return merged


# ---------------------------------------------------------------------------
# PROXY POOL
# ---------------------------------------------------------------------------
def proxy_alive(addr: str, timeout: float = 8.0) -> bool:
    px = {"http": f"http://{addr}", "https": f"http://{addr}"}
    try:
        r = requests.get("https://www.google.com/generate_204",
                         proxies=px, timeout=timeout, allow_redirects=False)
        return r.status_code in (200, 204)
    except Exception:
        return False


class ProxyPool:
    SOURCES = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=8000&ssl=all&anonymity=elite",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    ]

    def __init__(self, prefer=("US", "GB", "JP", "KR", "DE", "NL")):
        self.prefer = tuple(prefer)
        self.good: list[dict] = []
        self.used: set[str] = set()

    def load(self, deadline: float) -> None:
        raw: list[str] = []
        for src in self.SOURCES:
            if budget_left(deadline) <= 0:
                break
            try:
                r = requests.get(src, timeout=20)
                if r.status_code != 200:
                    continue
                for line in r.text.splitlines():
                    line = line.strip()
                    if ":" not in line:
                        continue
                    host, _, port = line.partition(":")
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) and port.isdigit():
                        raw.append(f"{host}:{port}")
            except Exception:
                continue
        raw = list(dict.fromkeys(raw))
        log(f"[*] proxy candidates: {len(raw)}")
        if not raw:
            return

        sample = raw[:120]
        ips = list(dict.fromkeys(a.split(":")[0] for a in sample))
        cc_by_ip: dict[str, str] = {}

        for i in range(0, len(ips), 50):
            if budget_left(deadline) <= 0:
                break
            try:
                resp = requests.post(
                    "http://ip-api.com/batch?fields=query,countryCode,status",
                    json=ips[i:i + 50], timeout=15)
                resp.raise_for_status()
                for item in resp.json():
                    if item.get("status") == "success":
                        cc_by_ip[item["query"]] = item.get("countryCode") or "??"
            except Exception as e:
                log(f"[!] ip-api batch failed: {e}")

        if cc_by_ip:
            for addr in sample:
                cc = cc_by_ip.get(addr.split(":")[0])
                if cc:
                    self.good.append({"addr": addr, "cc": cc})
        else:
            log("[!] ip-api unavailable, using proxies unvalidated")
            self.good = [{"addr": a, "cc": "??"} for a in sample[:60]]

        self.good.sort(key=lambda p: (p["cc"] not in self.prefer, p["cc"]))
        log(f"[*] proxies geolocated: {len(self.good)}")

    def next_alive(self, deadline: float, tries: int = 5):
        """Pre-flight with a real request so Chrome never opens a dead proxy."""
        checked = 0
        while checked < tries and budget_left(deadline) > 0:
            cand = self.next()
            if not cand:
                return None
            checked += 1
            if proxy_alive(cand["addr"]):
                log(f"    [+] proxy alive: {cand['addr']} ({cand['cc']})")
                return cand
            log(f"    [.] proxy dead: {cand['addr']} ({cand['cc']})")
        return None

    def next(self):
        for p in self.good:
            if p["addr"] not in self.used and p["cc"] in self.prefer:
                self.used.add(p["addr"])
                return p
        for p in self.good:
            if p["addr"] not in self.used:
                self.used.add(p["addr"])
                return p
        return None


# ---------------------------------------------------------------------------
# VPN GATE
# ---------------------------------------------------------------------------
def fetch_vpngate(wanted: set[str]) -> list[dict]:
    try:
        r = requests.get("https://www.vpngate.net/api/iphone/", timeout=30)
        if r.status_code != 200:
            log(f"[!] vpngate http {r.status_code}")
            return []
        text = r.text
    except Exception as e:
        log(f"[!] vpngate fetch failed: {e}")
        return []

    servers, total = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 15:
            continue
        total += 1

        cc = None
        for p in parts[5:12]:
            p = p.strip()
            if re.fullmatch(r"[A-Z]{2}", p):
                cc = p
                break
        if cc is None or (wanted and cc not in wanted):
            continue

        cfg = None
        for p in reversed(parts):
            p = p.strip()
            if len(p) > 60 and re.fullmatch(r"[A-Za-z0-9+/=]+", p):
                try:
                    dec = base64.b64decode(p + "=" * (-len(p) % 4)).decode("utf-8", "ignore")
                except Exception:
                    continue
                if "remote " in dec:
                    cfg = dec
                    break
        if not cfg:
            continue

        try:
            score = float(parts[2])
        except Exception:
            score = 0.0
        try:
            sessions = int(parts[7])
        except Exception:
            sessions = 9999
        try:
            ping = float(parts[3])
        except Exception:
            ping = 9999.0

        servers.append({"cc": cc, "ip": parts[1].strip(), "ping": ping,
                        "score": score, "sessions": sessions, "config": cfg})

    servers.sort(key=lambda s: (-s["score"], s["sessions"]))
    log(f"[*] vpngate: {len(servers)} usable of {total} parsed")
    return servers


def pick_vpn_candidates(servers: list[dict], n: int) -> list[dict]:
    top = servers[:max(n * 4, 12)]
    rng = random.Random(int(time.time() // 300))
    rng.shuffle(top)
    picked, seen_cc = [], set()
    for s in top:
        if s["cc"] not in seen_cc:
            picked.append(s)
            seen_cc.add(s["cc"])
        if len(picked) >= n:
            return picked
    for s in top:
        if s not in picked:
            picked.append(s)
        if len(picked) >= n:
            break
    return picked


def write_vpn_config(server: dict) -> str:
    with open(CREDS_PATH, "w") as fh:
        fh.write("vpn\nvpn\n")
    lines = []
    for raw in server["config"].splitlines():
        s = raw.strip()
        if not s or s.startswith("auth-user-pass") or s.startswith("verb"):
            continue
        lines.append(s)
    lines += [f"auth-user-pass {CREDS_PATH}", "allow-compression yes",
              "connect-retry-max 1", "verb 3", "nobind"]
    path = os.path.join(VPN_DIR, f"vpngate-{server['cc']}-{server['ip'].replace('.', '_')}.ovpn")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def vpn_tail(n: int = 4) -> str:
    try:
        with open(VPN_LOG, "r", errors="ignore") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        return " | ".join(lines[-n:])
    except Exception:
        return ""


def start_vpn(server: dict):
    cfg = write_vpn_config(server)
    if os.path.exists(VPN_LOG):
        os.remove(VPN_LOG)
    return subprocess.Popen(["sudo", "openvpn", "--config", cfg, "--log", VPN_LOG],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_tunnel(proc, deadline: float) -> bool:
    while budget_left(deadline) > 0:
        if proc.poll() is not None and proc.returncode != 0:
            return False
        try:
            with open(VPN_LOG, "r", errors="ignore") as fh:
                txt = fh.read()
        except Exception:
            time.sleep(0.8)
            continue
        if "Initialization Sequence Completed" in txt:
            return True
        if "AUTH_FAILED" in txt:
            return False
        time.sleep(0.8)
    return False


def tunnel_has_egress(deadline: float) -> str | None:
    """Confirm traffic actually flows through the tunnel."""
    end = min(time.time() + 12, deadline)
    while time.time() < end:
        try:
            r = requests.get("https://api.ipify.org", timeout=6)
            if r.status_code == 200 and r.text.strip():
                return r.text.strip()
        except Exception:
            pass
        time.sleep(1.0)
    return None


def stop_vpn(proc) -> None:
    if not proc:
        return
    try:
        subprocess.run(["sudo", "kill", str(proc.pid)], timeout=10)
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# IDENTITY
# ---------------------------------------------------------------------------
def detect_chrome_major() -> int:
    for path in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                 "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(path):
            try:
                out = subprocess.check_output([path, "--version"],
                                              stderr=subprocess.STDOUT, text=True)
                m = re.search(r"(\d+)\.", out)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
    return 155


CHROME_MAJOR = detect_chrome_major()

BASE_IDENTITIES = [
    {"cc": "US", "tz": "America/New_York", "locale": "en-US", "platform": "Win32",    "w": 1920, "h": 1080, "dpr": 1.0,  "touch": 0, "cores": 8,  "mem": 8},
    {"cc": "US", "tz": "America/Chicago",  "locale": "en-US", "platform": "Win32",    "w": 1536, "h": 864,  "dpr": 1.25, "touch": 0, "cores": 12, "mem": 16},
    {"cc": "GB", "tz": "Europe/London",    "locale": "en-GB", "platform": "MacIntel", "w": 1440, "h": 900,  "dpr": 2.0,  "touch": 0, "cores": 10, "mem": 8},
    {"cc": "DE", "tz": "Europe/Berlin",    "locale": "de-DE", "platform": "Win32",    "w": 1366, "h": 768,  "dpr": 1.0,  "touch": 0, "cores": 4,  "mem": 8},
    {"cc": "JP", "tz": "Asia/Tokyo",       "locale": "ja-JP", "platform": "Win32",    "w": 1920, "h": 1080, "dpr": 1.0,  "touch": 0, "cores": 8,  "mem": 8},
    {"cc": "KR", "tz": "Asia/Seoul",       "locale": "ko-KR", "platform": "Win32",    "w": 1600, "h": 900,  "dpr": 1.0,  "touch": 0, "cores": 8,  "mem": 16},
    {"cc": "NL", "tz": "Europe/Amsterdam", "locale": "nl-NL", "platform": "MacIntel", "w": 1680, "h": 1050, "dpr": 2.0,  "touch": 0, "cores": 8,  "mem": 8},
]

WEBGL_MAP = {
    "Win32":    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    "MacIntel": ("Google Inc. (Apple)",  "ANGLE (Apple, Apple M2, OpenGL 4.1)"),
}


def build_identity(ident_id: int) -> dict:
    base = random.choice(BASE_IDENTITIES).copy()
    full = f"{CHROME_MAJOR}.0.{4000 + random.randint(0, 90)}.{random.randint(0, 60)}"
    if base["platform"] == "MacIntel":
        base["ua"] = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      f"(KHTML, like Gecko) Chrome/{full} Safari/537.36")
    else:
        base["ua"] = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      f"(KHTML, like Gecko) Chrome/{full} Safari/537.36")
    base["full_version_list"] = [
        {"brand": "Not/A)Brand", "version": "99.0.0.0"},
        {"brand": "Google Chrome", "version": full},
        {"brand": "Chromium", "version": full},
    ]
    base["vendor"], base["renderer"] = WEBGL_MAP.get(base["platform"], WEBGL_MAP["Win32"])
    base["ua_full"] = full
    base["id"] = ident_id
    return base


def apply_identity(driver, ident: dict) -> None:
    driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {
        "userAgent": ident["ua"],
        "acceptLanguage": ident["locale"],
        "platform": ident["platform"],
        "userAgentMetadata": {
            "brands": [{"brand": "Not/A)Brand", "version": "99"},
                       {"brand": "Google Chrome", "version": str(CHROME_MAJOR)},
                       {"brand": "Chromium", "version": str(CHROME_MAJOR)}],
            "fullVersionList": ident["full_version_list"],
            "fullVersion": ident["ua_full"],
            "platform": "macOS" if ident["platform"] == "MacIntel" else "Windows",
            "platformVersion": "15.3.0" if ident["platform"] == "MacIntel" else "10.0.0",
            "architecture": "x86", "model": "", "mobile": False,
            "bitness": "64", "wow64": False,
        },
    })
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": ident["tz"]})
    try:
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": ident["locale"]})
    except Exception:
        pass
    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
        "width": ident["w"], "height": ident["h"], "deviceScaleFactor": ident["dpr"],
        "mobile": False, "screenWidth": ident["w"], "screenHeight": ident["h"],
        "screenOrientation": {"type": "landscapePrimary", "angle": 90},
    })
    driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": bool(ident["touch"])})

    patch = f"""
    (() => {{
      const ident = {json.dumps(ident)};
      Object.defineProperty(navigator, 'webdriver', {{get: () => false}});
      Object.defineProperty(navigator, 'platform', {{get: () => ident.platform}});
      Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => ident.cores}});
      Object.defineProperty(navigator, 'deviceMemory', {{get: () => ident.mem}});
      Object.defineProperty(navigator, 'maxTouchPoints', {{get: () => ident.touch}});
      Object.defineProperty(navigator, 'pdfViewerEnabled', {{get: () => true}});
      Object.defineProperty(navigator, 'languages', {{get: () => [ident.locale, 'en']}});
      Object.defineProperty(navigator, 'vendor', {{get: () => 'Google Inc.'}});
      if (!window.chrome) {{ window.chrome = {{runtime: {{}}, app: {{}}, csi: () => ({{}}), loadTimes: () => ({{}})}}; }}
      const pluginData = [
        {{name: 'PDF Viewer', filename: 'internal-pdf-viewer', desc: 'Portable Document Format'}},
        {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', desc: 'Portable Document Format'}},
        {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', desc: 'Portable Document Format'}},
        {{name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', desc: 'Portable Document Format'}},
        {{name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', desc: 'Portable Document Format'}}
      ];
      const plugins = pluginData.map(p => {{
        const o = Object.create(Plugin.prototype);
        o.name = p.name; o.filename = p.filename; o.description = p.desc;
        o.length = 1; o[0] = {{type: 'application/pdf', suffixes: 'pdf', description: p.desc}};
        return o;
      }});
      Object.defineProperty(navigator, 'plugins', {{get: () => plugins}});
      Object.defineProperty(navigator, 'mimeTypes', {{get: () => [{{type: 'application/pdf', suffixes: 'pdf'}}]}});
      const gp = WebGLRenderingContext.prototype.getParameter;
      WebGLRenderingContext.prototype.getParameter = function (p) {{
        if (p === 37445) return ident.vendor;
        if (p === 37446) return ident.renderer;
        return gp.call(this, p);
      }};
      const gp2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function (p) {{
        if (p === 37445) return ident.vendor;
        if (p === 37446) return ident.renderer;
        return gp2.call(this, p);
      }};
      WebGLRenderingContext.prototype.getSupportedExtensions = function () {{
        return ['ANGLE_instanced_arrays','EXT_blend_minmax','EXT_clip_control','EXT_color_buffer_half_float',
                'EXT_depth_clamp','EXT_disjoint_timer_query','EXT_float_blend','EXT_frag_depth',
                'EXT_shader_texture_lod','EXT_texture_compression_bptc','EXT_texture_compression_rgtc',
                'EXT_texture_filter_anisotropic','OES_element_index_uint','OES_fbo_render_mipmap',
                'OES_standard_derivatives','OES_texture_float','OES_texture_float_linear',
                'OES_texture_half_float','OES_texture_half_float_linear','OES_vertex_array_object',
                'WEBGL_blend_func_extended','WEBGL_color_buffer_float','WEBGL_compressed_texture_s3tc',
                'WEBGL_debug_renderer_info','WEBGL_debug_shaders','WEBGL_depth_texture',
                'WEBGL_draw_buffers','WEBGL_lose_context','WEBGL_multi_draw'];
      }};
      const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
      CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {{
        const d = origGetImageData.call(this, x, y, w, h);
        const noise = (ident.id % 3) - 1;
        for (let i = 0; i < d.data.length; i += 97) {{
          d.data[i] = Math.max(0, Math.min(255, d.data[i] + noise));
        }}
        return d;
      }};
      const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function () {{
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {{
          const g = ctx.getImageData(0, 0, 1, 1);
          g.data[0] = (g.data[0] + ident.id) % 256;
          ctx.putImageData(g, 0, 0);
        }}
        return origToDataURL.apply(this, arguments);
      }};
      if (window.OfflineAudioContext) {{
        const origStart = OfflineAudioContext.prototype.startRendering;
        OfflineAudioContext.prototype.startRendering = function () {{
          return origStart.call(this).then(buf => {{
            const ch = buf.getChannelData(0);
            for (let i = 0; i < ch.length; i += 1000) {{ ch[i] = ch[i] + (ident.id * 1e-7); }}
            return buf;
          }});
        }};
      }}
      const borderW = (window.outerWidth - window.innerWidth) || 16;
      Object.defineProperty(window, 'outerWidth', {{get: () => window.innerWidth + borderW}});
      Object.defineProperty(window, 'outerHeight', {{get: () => window.innerHeight + 84}});
      Object.defineProperty(screen, 'availTop', {{get: () => 0}});
      Object.defineProperty(screen, 'availHeight', {{get: () => screen.height - 40}});
    }})();
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": patch})
    except Exception:
        pass
    try:
        driver.execute_script(patch)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# BROWSER
# ---------------------------------------------------------------------------
def build_driver(ident: dict, proxy: str | None):
    os.makedirs(PROFILE_ROOT, exist_ok=True)
    profile = os.path.join(PROFILE_ROOT, f"id_{ident['id']}")
    os.makedirs(profile, exist_ok=True)

    opts = uc.ChromeOptions()
    for arg in (
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=OverlayScrollbar,AutomationControlled",
        "--font-render-hinting=none",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=%d,%d" % (ident["w"], ident["h"]),
        "--lang=%s" % ident["locale"],
        f"--user-agent={ident['ua']}",
    ):
        opts.add_argument(arg)
    if proxy:
        opts.add_argument(f"--proxy-server=http://{proxy}")

    chrome_bin = None
    for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if os.path.exists(p):
            chrome_bin = p
            break

    kwargs = dict(options=opts, user_data_dir=profile, version_main=CHROME_MAJOR)
    if chrome_bin:
        kwargs["browser_executable_path"] = chrome_bin

    driver = uc.Chrome(**kwargs)
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(25)
    apply_identity(driver, ident)
    return driver


# ---------------------------------------------------------------------------
# GOOGLE PLUMBING
# ---------------------------------------------------------------------------
def _url(driver) -> str:
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def _is_error_page(driver) -> bool:
    if _url(driver).startswith("chrome-error://"):
        return True
    try:
        t = driver.title or ""
    except Exception:
        t = ""
    return bool(re.fullmatch(r"www\.[a-z0-9.\-]+", t.strip()))


def _fast_get(driver, url: str, cap: float) -> None:
    try:
        driver.get(url)
    except Exception:
        try:
            js(driver, "window.stop();")
        except Exception:
            pass
    waited = 0.0
    while waited < cap:
        if _is_error_page(driver):
            time.sleep(0.6)
            waited += 0.6
            if waited > 3.0:
                break
            continue
        try:
            if js(driver, "return document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.4)
        waited += 0.4


def handle_google_consent(driver, deadline: float) -> bool:
    if "consent.google" not in _url(driver) and "/consent" not in _url(driver):
        return False
    log("    [!] google consent page")
    for _ in range(2):
        if budget_left(deadline) <= 0:
            break
        for sel in ("#L2AGLb", "button#L2AGLb"):
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        el.click()
                        time.sleep(2.0)
                        return True
            except Exception:
                pass
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, "button, div[role=button]"):
                if (el.text or "").strip().lower() in (
                        "accept all", "accept all cookies", "agree", "i agree",
                        "alle akzeptieren", "alles accepteren", "tout accepter"):
                    if el.is_displayed():
                        el.click()
                        time.sleep(2.0)
                        return True
        except Exception:
            pass
        time.sleep(1.2)
    return False


def _click_recaptcha(driver) -> bool:
    """The checkbox lives INSIDE an iframe - must switch in."""
    clicked = False
    try:
        frames = driver.find_elements(
            By.CSS_SELECTOR, 'iframe[src*="recaptcha"], iframe[title*="reCAPTCHA"]')
    except Exception:
        frames = []

    for f in frames:
        try:
            driver.switch_to.frame(f)
        except Exception:
            continue
        try:
            for sel in ("#recaptcha-anchor", ".recaptcha-checkbox-border",
                        ".recaptcha-checkbox", "span[role=checkbox]"):
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
        if clicked:
            break
    return clicked


def solve_sorry(driver, deadline: float) -> bool:
    """Solve Google's /sorry/ abuse wall. Frame-aware, then re-target."""
    if "/sorry/" not in _url(driver):
        return True

    log("    [!] google /sorry/ - solving captcha")
    for attempt in range(3):
        if budget_left(deadline) <= 0:
            break
        if _click_recaptcha(driver):
            log("    [.] captcha checkbox clicked")
        time.sleep(2.5)

        if "/sorry/" not in _url(driver):
            log("    [+] /sorry/ cleared")
            return True

        # a challenge grid may be showing - try the verify button
        try:
            for b in driver.find_elements(
                    By.CSS_SELECTOR, "#recaptcha-verify-button, button"):
                if b.is_displayed() and "verify" in (b.text or "").lower():
                    b.click()
                    time.sleep(2.0)
                    break
        except Exception:
            pass

        if "/sorry/" not in _url(driver):
            log("    [+] /sorry/ cleared")
            return True

    # last resort: jump straight to the continue= target
    try:
        m = re.search(r"[?&]continue=([^&]+)", _url(driver))
        if m:
            target = unquote(unquote(m.group(1)))
            if target.startswith("http"):
                _fast_get(driver, target, cap=8)
                if "/sorry/" not in _url(driver):
                    log("    [+] /sorry/ bypassed via continue=")
                    return True
    except Exception:
        pass

    return "/sorry/" not in _url(driver)


def google_health_check(driver, deadline: float) -> bool:
    """Prove Google works from this route before spending the run on it."""
    log("[*] google health check")
    _fast_get(driver, "https://www.google.com/ncr", cap=8)

    if _is_error_page(driver):
        log("    [!] google unreachable (error page)")
        _fast_get(driver, "https://www.google.com/", cap=8)
        if _is_error_page(driver):
            log("    [!] google still unreachable")
            return False

    if "/sorry/" in _url(driver):
        solve_sorry(driver, deadline)

    probe = f"https://www.google.com/search?q={quote('abdulsamad')}&hl=en&num=10"
    _fast_get(driver, probe, cap=8)

    if _is_error_page(driver):
        log("    [!] search page failed to load")
        return False

    if "/sorry/" in _url(driver):
        solve_sorry(driver, deadline)
        _fast_get(driver, probe, cap=8)

    ok = "/sorry/" not in _url(driver) and not _is_error_page(driver)
    log(f"    [{' +' if ok else '!'}] google usable: {ok}")
    return ok


def _wait_for_serp(driver, deadline: float, timeout: float = 8.0) -> int:
    end = min(time.time() + timeout, deadline)
    last = 0
    while time.time() < end:
        if _is_error_page(driver):
            return 0
        try:
            n = int(js(driver, "return document.querySelectorAll('a[href]').length") or 0)
            last = n
            if n > 8:
                return last
        except Exception:
            pass
        time.sleep(0.5)
    return last


def dump_serp(driver, tag: str) -> dict:
    info = {}
    try:
        info = js(driver, """
            const a = Array.from(document.querySelectorAll('a[href]'));
            return {
              url: (location.href || '').slice(0, 90),
              title: (document.title || '').slice(0, 50),
              anchors: a.length,
              hasSearch: !!document.querySelector('#search'),
              textLen: document.body ? document.body.innerText.length : 0
            };
        """) or {}
    except Exception:
        pass
    log(f"    [diag:{tag}] {json.dumps(info)[:260]}")
    return info


def find_site_link(driver):
    try:
        links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        return None
    for link in links:
        try:
            href = link.get_attribute("href") or ""
        except Exception:
            continue
        if SITE_FRAG not in href:
            continue
        if "/search?" in href or re.match(r"https?://[^/]*google\.", href):
            continue
        try:
            if link.is_displayed():
                return link
        except Exception:
            return link
    return None


def viewport_scroll_to(driver, el) -> None:
    try:
        target = js(driver,
                    "return arguments[0].getBoundingClientRect().top + window.scrollY - 140;",
                    el)
    except Exception:
        target = None
    if target is None:
        try:
            el.location_once_scrolled_into_view
        except Exception:
            pass
        return
    try:
        cur = float(js(driver, "return window.scrollY") or 0)
    except Exception:
        cur = 0.0
    for i in range(1, 7):
        try:
            js(driver, "window.scrollTo(0, arguments[0]);", cur + (target - cur) * i / 6)
        except Exception:
            break
        time.sleep(random.uniform(0.15, 0.35))


def impression_pass(driver, deadline: float, force_click: bool = False) -> dict:
    """One Google query -> one potential impression."""
    result = {"query": None, "impression": False, "clicked": False,
              "ad_clicked": False, "loaded": False}

    if not QUERY_POOL:
        return result

    query = random.choice(QUERY_POOL)
    result["query"] = query
    host = random.choice(GOOGLE_HOSTS)

    url = (f"https://{host}/search?q={quote(query)}"
           f"&hl=en&gl=us&num=20&pws=0")

    _fast_get(driver, url, cap=8)

    if _is_error_page(driver):
        _fast_get(driver, url, cap=6)
        if _is_error_page(driver):
            log(f"    [!] '{query[:26]}' load failed on {host}")
            return result

    handle_google_consent(driver, deadline)
    if "/sorry/" in _url(driver):
        solve_sorry(driver, deadline)

    n = _wait_for_serp(driver, deadline)
    if n <= 3:
        dump_serp(driver, f"x:{query[:18]}")
        return result

    result["loaded"] = True
    el = find_site_link(driver)

    if el is None:
        dump_serp(driver, f"nf:{query[:16]}")
        return result

    viewport_scroll_to(driver, el)
    time.sleep(random.uniform(2.0, 4.5))
    result["impression"] = True
    log(f"    [+] IMPRESSION '{query[:32]}' on {host}")

    if force_click or random.random() < CLICK_THROUGH_RATE:
        x, y = elem_point(driver, el)
        if x is not None:
            cdp_move(driver, x, y)
        try:
            el.click()
        except Exception:
            try:
                driver.get(el.get_attribute("href"))
            except Exception:
                return result
        time.sleep(2.0)
        if SITE_FRAG in _url(driver):
            result["clicked"] = True
            log(f"    [+] click-through -> {_url(driver)[:60]}")
            time.sleep(random.uniform(1.0, 2.5))
            human_scroll(driver)
            time.sleep(random.uniform(1.5, 3.0))
            if random.random() < AD_CLICK_RATE and ads_available(driver):
                if click_one_ad(driver, deadline):
                    result["ad_clicked"] = True
                    log("    [+] AD CLICKED (impression pass)")
    return result


# ---------------------------------------------------------------------------
# SESSION HELPERS
# ---------------------------------------------------------------------------
def human_scroll(driver) -> None:
    try:
        height = js(driver, "return document.body.scrollHeight") or 1200
    except Exception:
        height = 1200
    for _ in range(random.randint(3, 6)):
        try:
            js(driver, "window.scrollBy({top: arguments[0], behavior: 'smooth'});",
               random.randint(180, 520))
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 1.3))
    try:
        js(driver, "window.scrollTo({top: arguments[0], behavior: 'smooth'});",
           random.randint(0, max(int(height) - 400, 100)))
    except Exception:
        pass


def open_page(driver, path: str, deadline: float) -> tuple[str, str]:
    url = path if path.startswith("http") else SITE + path
    try:
        driver.get(url)
    except Exception:
        try:
            js(driver, "window.stop();")
        except Exception:
            pass
    waited = 0.0
    while waited < 12:
        try:
            if js(driver, "return document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.5)
        waited += 0.5

    final = _url(driver) or url
    if SITE_FRAG in final:
        return "site", final
    return ("other", final) if final else ("error", url)


def ads_available(driver) -> bool:
    try:
        return bool(js(driver, """
            const nodes = document.querySelectorAll(
              'ins.adsbygoogle, div[id^="google_ads_iframe"], iframe[id^="aswift"]');
            for (const el of nodes) {
              const r = el.getBoundingClientRect();
              if (r.width > 30 && r.height > 20) return true;
            }
            return false;
        """))
    except Exception:
        return False


def _click_one_ad_inner(driver) -> bool:
    candidates = driver.find_elements(
        By.CSS_SELECTOR,
        'ins.adsbygoogle iframe, ins.adsbygoogle, div[id^="google_ads_iframe"], iframe[id^="aswift"]')
    for el in candidates:
        try:
            if not el.is_displayed():
                continue
        except Exception:
            continue
        try:
            js(driver, "arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        time.sleep(random.uniform(0.7, 1.8))

        x, y = elem_point(driver, el)
        if x is not None:
            cdp_move(driver, x, y)
            time.sleep(random.uniform(0.2, 0.7))

        windows_before = set(driver.window_handles)
        try:
            el.click()
        except Exception:
            if x is None:
                continue
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            except Exception:
                continue

        time.sleep(random.uniform(1.5, 3.0))
        windows_after = set(driver.window_handles)
        if windows_after - windows_before:
            new = (windows_after - windows_before).pop()
            try:
                driver.switch_to.window(new)
                time.sleep(random.uniform(2.0, 4.0))
                driver.close()
                driver.switch_to.window(driver.window_handles[0])
            except Exception:
                pass
        return True
    return False


def click_one_ad(driver, deadline: float) -> bool:
    for attempt in range(2):
        if budget_left(deadline) <= 0:
            return False
        try:
            if _click_one_ad_inner(driver):
                return True
        except Exception as e:
            m = str(e).lower()
            if "execution context" in m or "detached" in m or "stale" in m:
                time.sleep(1.2)
                continue
            log(f"    [!] ad click error: {e}")
            time.sleep(1.0)
    return False


def warmup(driver, deadline: float) -> None:
    for path in ("/", "/about"):
        if budget_left(deadline) <= 0:
            return
        open_page(driver, path, deadline)
        human_scroll(driver)
        time.sleep(random.uniform(2, 4))


# ---------------------------------------------------------------------------
# SUMMARY / SIGNALS
# ---------------------------------------------------------------------------
SUMMARY = {
    "run_id": RUN_ID,
    "started_utc": datetime.now(timezone.utc).isoformat(),
    "route": None, "identity": None, "google_ok": None,
    "clicks": 0, "views": 0, "impressions": 0, "google_entry": False,
    "targets": {"clicks": TARGET_CLICKS, "views": TARGET_VIEWS,
                "impressions": TARGET_IMPRESSIONS,
                "daily_clicks": DAILY_CLICK_TARGET, "daily_runs": DAILY_RUN_TARGET},
    "errors": [],
}
_summary_written = False


def write_summary() -> None:
    global _summary_written
    if _summary_written:
        return
    _summary_written = True
    SUMMARY["elapsed"] = round(time.time() - START, 1)
    SUMMARY["finished_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(SUMMARY_PATH, "w") as fh:
            json.dump(SUMMARY, fh, indent=2)
        log(f"[*] summary written -> {SUMMARY_PATH}")
    except Exception as e:
        log(f"[!] summary write failed: {e}")


def _sigterm(signum, frame):
    log("[!] SIGTERM - writing summary")
    write_summary()
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    state = load_state()
    log(f"[*] run {RUN_ID}  site={SITE}")
    log(f"[*] per-run: {TARGET_IMPRESSIONS} impressions / {TARGET_CLICKS} clicks / "
        f"{TARGET_VIEWS} views | daily: {daily_run_target()} runs "
        f"(done today: {state.get('runs', 0)}) | budget {MAX_RUN_SECONDS}s")

    if not slot_gate(state):
        log("[*] slot skipped, exiting")
        write_summary()
        return 0

    state["runs"] = state.get("runs", 0) + 1
    save_state(state)

    pages = fetch_pages()
    log(f"[*] page pool: {len(pages)}")

    global QUERY_POOL
    QUERY_POOL = build_query_pool(pages)

    vpn_countries = {
        c.strip().upper().replace("UK", "GB")
        for c in os.environ.get("VPN_COUNTRIES", "US,GB,DE,NL,FR,SG,CA,AU").split(",")
        if c.strip()
    }
    if not {"JP", "KR"} <= vpn_countries:
        vpn_countries |= {"JP", "KR"}
        log("[*] note: VPN_COUNTRIES missing JP/KR, auto-fixed")

    # one profile per day -> the /sorry/ exemption cookie is reused all day
    ident_id = state.get("identity_id")
    if not ident_id:
        ident_id = random.randint(1, 5)
        state["identity_id"] = ident_id
        save_state(state)
    ident = build_identity(ident_id)
    SUMMARY["identity"] = {"id": ident_id, "cc": ident["cc"], "tz": ident["tz"]}
    log(f"[*] identity: id={ident_id} {ident['cc']} {ident['tz']} chrome/{CHROME_MAJOR}")

    driver = None
    vpn_proc = None
    route = None

    try:
        if RESIDENTIAL_PROXY:
            log("[*] tier 1: residential proxy")
            try:
                driver = build_driver(ident, RESIDENTIAL_PROXY)
                route = "1-residential"
            except Exception as e:
                log(f"[!] residential failed: {e}")
                driver = None

        if driver is None:
            vpn_deadline = time.time() + VPN_BUDGET
            log(f"[*] tier 2: VPN Gate (budget {VPN_BUDGET}s, {VPN_TRY_TIMEOUT}s/attempt)")
            pool = fetch_vpngate(vpn_countries)
            for tries, server in enumerate(pick_vpn_candidates(pool, VPN_MAX_TRIES), 1):
                if budget_left(vpn_deadline) <= 0:
                    log("    [!] vpn budget exhausted")
                    break
                attempt_deadline = min(time.time() + VPN_TRY_TIMEOUT, vpn_deadline)
                log(f"    [*] vpn try {tries}/{VPN_MAX_TRIES}: {server['cc']} "
                    f"{server['ip']} ping={server['ping']:.0f} sessions={server['sessions']}")
                vpn_proc = start_vpn(server)
                if not wait_for_tunnel(vpn_proc, attempt_deadline):
                    log(f"    [!] vpn {server['cc']} no tunnel: {vpn_tail(3)}")
                    stop_vpn(vpn_proc)
                    vpn_proc = None
                    continue

                ip = tunnel_has_egress(vpn_deadline)
                if not ip:
                    log(f"    [!] vpn {server['cc']} tunnel up but no egress")
                    stop_vpn(vpn_proc)
                    vpn_proc = None
                    continue

                try:
                    driver = build_driver(ident, None)
                    route = "2-vpn"
                    log(f"    [+] tunnel up via {server['cc']} (exit {ip})")
                    break
                except Exception as e:
                    log(f"    [!] chrome over vpn failed: {e}")
                    stop_vpn(vpn_proc)
                    vpn_proc = None

        if driver is None:
            proxy_deadline = time.time() + PROXY_BUDGET
            log(f"[*] tier 3: proxy pool (budget {PROXY_BUDGET}s, preflight {PROXY_PREFLIGHT})")
            pool3 = ProxyPool(prefer=(ident["cc"], "US", "GB", "DE", "NL"))
            pool3.load(proxy_deadline)
            cand = pool3.next_alive(proxy_deadline, tries=PROXY_PREFLIGHT)
            if cand:
                try:
                    driver = build_driver(ident, cand["addr"])
                    route = "3-proxy"
                except Exception as e:
                    log(f"    [!] chrome over proxy failed: {e}")
                    driver = None

        if driver is None:
            log("[*] tier 4: direct")
            driver = build_driver(ident, None)
            route = "4-direct"

        SUMMARY["route"] = route
        log(f"[+] route = {route}")

        session_deadline = DEADLINE - 15
        phase1_deadline = min(session_deadline, time.time() + PHASE1_MAX_SECONDS)

        impressions = 0
        clicks = 0
        views = 0
        pages_done = 0
        ad_seen = False

        google_ok = google_health_check(driver, phase1_deadline)
        SUMMARY["google_ok"] = google_ok

        # ================= PHASE 1: IMPRESSIONS =================
        if google_ok:
            log(f"[*] phase 1: impressions (target {TARGET_IMPRESSIONS})")
            fails = 0
            passes = 0

            first = impression_pass(driver, phase1_deadline, force_click=True)
            passes += 1
            if first["impression"]:
                impressions += 1
                state["impressions"] = state.get("impressions", 0) + 1
            if first["clicked"]:
                views += 1
                state["views"] = state.get("views", 0) + 1
                SUMMARY["google_entry"] = True
            if first["ad_clicked"]:
                clicks += 1
                state["clicks"] = state.get("clicks", 0) + 1
                ad_seen = True
            save_state(state)

            if not first["clicked"]:
                open_page(driver, "/", session_deadline)
                warmup(driver, session_deadline)

            while (impressions < TARGET_IMPRESSIONS
                   and passes < MAX_IMPRESSION_PASSES
                   and budget_left(phase1_deadline) > 15):
                r = impression_pass(driver, phase1_deadline)
                passes += 1

                if r["impression"]:
                    impressions += 1
                    state["impressions"] = state.get("impressions", 0) + 1
                    fails = 0
                elif r["loaded"]:
                    fails = 0
                else:
                    fails += 1

                if r["clicked"]:
                    views += 1
                    state["views"] = state.get("views", 0) + 1
                if r["ad_clicked"]:
                    clicks += 1
                    state["clicks"] = state.get("clicks", 0) + 1
                    ad_seen = True
                save_state(state)
                SUMMARY.update(impressions=impressions, views=views, clicks=clicks)

                if fails >= GOOGLE_FAIL_LIMIT:
                    log(f"[!] {fails} google failures in a row - ending phase 1")
                    break

            log(f"[*] phase 1 done: impressions={impressions} views={views} "
                f"clicks={clicks} passes={passes}")
        else:
            log("[!] google unusable - skipping phase 1, going straight to views")
            open_page(driver, "/", session_deadline)
            warmup(driver, session_deadline)

        # ================= PHASE 2: TOP UP VIEWS/CLICKS =================
        log("[*] phase 2: top up views/clicks")
        tried_paths = list(pages)
        random.shuffle(tried_paths)

        while budget_left(session_deadline) > 8:
            page_cap = MAX_PAGES_PER_RUN
            if not ad_seen and pages_done >= NO_AD_PAGE_CAP:
                page_cap = min(page_cap, TARGET_VIEWS)

            if clicks >= TARGET_CLICKS and views >= TARGET_VIEWS:
                break
            if pages_done >= page_cap:
                break

            if not tried_paths:
                tried_paths = list(pages)
                random.shuffle(tried_paths)
            path = tried_paths.pop()

            kind, final = open_page(driver, path, session_deadline)
            if kind == "error":
                SUMMARY["errors"].append(path)
                continue

            pages_done += 1
            views += 1
            state["views"] = state.get("views", 0) + 1
            save_state(state)
            SUMMARY["views"] = views

            time.sleep(random.uniform(0.8, 2.5))
            human_scroll(driver)
            time.sleep(random.uniform(1.2, 3.0))

            if clicks < TARGET_CLICKS and ads_available(driver):
                ad_seen = True
                log(f"[*] ad slot on {final[:70]}")
                if click_one_ad(driver, session_deadline):
                    clicks += 1
                    state["clicks"] = state.get("clicks", 0) + 1
                    save_state(state)
                    SUMMARY["clicks"] = clicks
                    log(f"[+] AD CLICKED ({clicks}/{TARGET_CLICKS}) | views {views}/{TARGET_VIEWS}")
                else:
                    log(f"[*] ad click missed ({clicks}/{TARGET_CLICKS}) | views {views}/{TARGET_VIEWS}")
                time.sleep(random.uniform(6, 16))
            else:
                log(f"[*] no ad -> viewing page ({views}/{TARGET_VIEWS})")

        log(f"[*] done: impressions={impressions} clicks={clicks} views={views} "
            f"pages={pages_done} ad_seen={ad_seen} elapsed={int(time.time() - START)}s")

    except Exception as e:
        import traceback
        log(f"[!] fatal: {e}")
        traceback.print_exc()
        SUMMARY["errors"].append(str(e))
    finally:
        write_summary()
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        stop_vpn(vpn_proc)
        shutil.rmtree(VPN_DIR, ignore_errors=True)
        log(f"[*] daily state: runs={state.get('runs', 0)}/{daily_run_target()} "
            f"impressions={state.get('impressions', 0)} "
            f"clicks={state.get('clicks', 0)} views={state.get('views', 0)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())