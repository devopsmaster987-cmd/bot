#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke.py - hardened anti-detection AdSense traffic bot for abdulsamad.shop

Daily model
  DAILY_CLICK_TARGET = 60 clicks/day -> DAILY_RUN_TARGET = 60/10 = 6 runs/day.
  Pacing is driven by RUNS, not clicks, so the schedule stays correct while
  AdSense is unapproved and clicks are still 0.
  A run that passes the gate is charged 10 against the daily budget; the day
  hard-stops at 6 runs. The counter is cached in .bot-state so it survives
  across workflow runs.

Per run
  Up to 10 ad clicks and 10 page views, hard capped at MAX_PAGES_PER_RUN.

Route preference
  residential proxy -> VPN Gate (JP/KR/EU) -> country-matched free proxy -> direct
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
from urllib.parse import quote, urlparse

import requests

# ---------------------------------------------------------------------------
# distutils shim - Python 3.12 removed distutils, undetected_chromedriver uses it
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


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
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

# ---- per run ---------------------------------------------------------------
TARGET_CLICKS = int(os.environ.get("TARGET_CLICKS_PER_RUN", "10"))
TARGET_VIEWS = int(os.environ.get("TARGET_VIEWS_PER_RUN", "10"))
MAX_PAGES_PER_RUN = int(os.environ.get("MAX_PAGES_PER_RUN", str(TARGET_CLICKS + TARGET_VIEWS)))
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "720"))

# ---- per day ---------------------------------------------------------------
DAILY_CLICK_TARGET = int(os.environ.get("DAILY_CLICK_TARGET", "60"))
DAILY_RUN_TARGET = int(os.environ.get(
    "DAILY_RUN_TARGET", str(max(DAILY_CLICK_TARGET // max(TARGET_CLICKS, 1), 1))))
# Expected number of trigger invocations per day (pinger + github cron).
RUNS_PER_DAY = int(os.environ.get("RUNS_PER_DAY", "104"))

# ---- tier budgets ----------------------------------------------------------
VPN_BUDGET = int(os.environ.get("VPN_BUDGET", "60"))
PROXY_BUDGET = int(os.environ.get("PROXY_BUDGET", "30"))
VPN_MAX_TRIES = int(os.environ.get("VPN_MAX_TRIES", "6"))
GOOGLE_CAP = int(os.environ.get("GOOGLE_CAP", "60"))

FORCE_CLICK = env_flag("FORCE_CLICK")
IGNORE_DAILY_CAP = env_flag("IGNORE_DAILY_CAP")
RESIDENTIAL_PROXY = os.environ.get("RESIDENTIAL_PROXY", "").strip()

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_ROOT = os.path.join(WORKDIR, ".chrome-profiles")
STATE_DIR = os.path.join(WORKDIR, ".bot-state")
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


# ---------------------------------------------------------------------------
# JS HELPER - survives navigations mid-script through slow tunnels
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
        p = js(
            driver,
            "const r = arguments[0].getBoundingClientRect();"
            "return {x: r.left + r.width / 2, y: r.top + r.height / 2};",
            el,
        )
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
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x * i / steps, "y": y * i / steps, "buttons": 0},
            )
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
DOW_WEIGHTS = {0: 1.00, 1: 1.02, 2: 1.00, 3: 0.98, 4: 0.95, 5: 0.80, 6: 0.85}
MONTH_GROWTH = {
    1: 1.00, 2: 1.03, 3: 1.06, 4: 1.09, 5: 1.12, 6: 1.14,
    7: 1.16, 8: 1.18, 9: 1.20, 10: 1.21, 11: 1.22, 12: 1.22,
}
HOT_MONTHS = {11, 12}

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
    """Deterministic per-day run budget, jittered +-1 for natural variation."""
    rng = random.Random(day_seed())
    jitter = rng.choice([-1, 0, 0, 0, 1])
    return max(1, DAILY_RUN_TARGET + jitter)


def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            st = json.load(fh)
        if st.get("date") == today_key():
            st.setdefault("runs", 0)
            st.setdefault("clicks", 0)
            st.setdefault("views", 0)
            return st
    except Exception:
        pass
    return {"date": today_key(), "runs": 0, "clicks": 0, "views": 0}


def save_state(st: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        st["date"] = today_key()
        with open(STATE_PATH, "w") as fh:
            json.dump(st, fh)
    except Exception as e:
        log(f"[!] state save failed: {e}")


def slot_gate(state: dict) -> bool:
    """
    Charge a passing run 10 toward the daily click budget. Pacing is on RUNS
    so it stays correct while AdSense is unapproved and clicks are still 0.
    """
    target_runs = daily_run_target()
    runs_done = state.get("runs", 0)

    if FORCE_CLICK:
        log(f"[*] slot gate: FORCE_CLICK -> PASS (run {runs_done + 1}/{target_runs})")
        return True

    if runs_done >= target_runs and not IGNORE_DAILY_CAP:
        log(f"[*] slot gate: daily run budget spent ({runs_done}/{target_runs}) -> SKIP")
        return False

    if state.get("clicks", 0) >= DAILY_CLICK_TARGET and not IGNORE_DAILY_CAP:
        log(f"[*] slot gate: daily click cap reached "
            f"({state['clicks']}/{DAILY_CLICK_TARGET}) -> SKIP")
        return False

    now = datetime.now(timezone.utc)
    rng = random.Random(day_seed() ^ int(now.timestamp() // 900))

    remaining_runs = max(target_runs - runs_done, 0)
    remaining_triggers = max(RUNS_PER_DAY * (1.0 - weight_fraction(now)), 1.0)
    prob = remaining_runs / remaining_triggers
    prob = min(max(prob, 0.02), 0.90)

    roll = rng.random()
    ok = roll < prob
    log(f"[*] slot gate: p={prob:.3f} roll={roll:.3f} "
        f"runs={runs_done}/{target_runs} remaining_triggers~{remaining_triggers:.0f} "
        f"-> {'PASS' if ok else 'SKIP'}")
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

        for i in range(0, min(len(raw), 120), 30):
            if budget_left(deadline) <= 0:
                break
            try:
                resp = requests.post(
                    "http://ip-api.com/batch?fields=query,countryCode,status",
                    json=raw[i:i + 30],
                    timeout=15,
                )
                for item in resp.json():
                    if item.get("status") == "success":
                        self.good.append({"addr": item["query"], "cc": item["countryCode"]})
            except Exception:
                continue

        self.good.sort(key=lambda p: (p["cc"] not in self.prefer, p["cc"]))
        log(f"[*] proxies validated: {len(self.good)}")

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

    servers: list[dict] = []
    total = 0
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
            ping = float(parts[3])
        except Exception:
            ping = 9999.0

        servers.append({"cc": cc, "ip": parts[1].strip(), "ping": ping, "config": cfg})

    servers.sort(key=lambda s: s["ping"])
    log(f"[*] vpngate: {len(servers)} usable of {total} parsed")
    return servers


def write_vpn_config(server: dict) -> str:
    with open(CREDS_PATH, "w") as fh:
        fh.write("vpn\nvpn\n")

    lines = []
    for raw in server["config"].splitlines():
        s = raw.strip()
        if not s or s.startswith("auth-user-pass") or s.startswith("verb"):
            continue
        lines.append(s)

    lines += [
        f"auth-user-pass {CREDS_PATH}",
        "allow-compression yes",
        "connect-retry-max 1",
        "verb 3",
        "nobind",
    ]

    path = os.path.join(VPN_DIR, f"vpngate-{server['cc']}-{server['ip'].replace('.', '_')}.ovpn")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def vpn_tail(n: int = 6) -> str:
    try:
        with open(VPN_LOG, "r", errors="ignore") as fh:
            return "\n".join(fh.read().splitlines()[-n:])
    except Exception:
        return ""


def start_vpn(server: dict):
    cfg = write_vpn_config(server)
    if os.path.exists(VPN_LOG):
        os.remove(VPN_LOG)
    return subprocess.Popen(
        ["sudo", "openvpn", "--config", cfg, "--log", VPN_LOG],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_tunnel(proc, deadline: float) -> bool:
    while budget_left(deadline) > 0:
        if proc.poll() is not None and proc.returncode != 0:
            if "AUTH_FAILED" in vpn_tail():
                log("    [!] AUTH_FAILED (node full)")
            return False
        try:
            with open(VPN_LOG, "r", errors="ignore") as fh:
                txt = fh.read()
        except Exception:
            time.sleep(1.0)
            continue
        if "Initialization Sequence Completed" in txt:
            return True
        if "AUTH_FAILED" in txt:
            log("    [!] AUTH_FAILED (node full)")
            return False
        time.sleep(1.0)
    return False


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
                out = subprocess.check_output([path, "--version"], stderr=subprocess.STDOUT, text=True)
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


def build_identity() -> dict:
    base = random.choice(BASE_IDENTITIES).copy()
    full = f"{CHROME_MAJOR}.0.{4000 + random.randint(0, 90)}.{random.randint(0, 60)}"

    if base["platform"] == "MacIntel":
        base["ua"] = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      f"(KHTML, like Gecko) Chrome/{full} Safari/537.36")
        base["sec_ch_ua_platform"] = '"macOS"'
    else:
        base["ua"] = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      f"(KHTML, like Gecko) Chrome/{full} Safari/537.36")
        base["sec_ch_ua_platform"] = '"Windows"'

    base["sec_ch_ua"] = (f'"Not/A)Brand";v="99", "Google Chrome";v="{CHROME_MAJOR}", '
                         f'"Chromium";v="{CHROME_MAJOR}"')
    base["full_version_list"] = [
        {"brand": "Not/A)Brand", "version": "99.0.0.0"},
        {"brand": "Google Chrome", "version": full},
        {"brand": "Chromium", "version": full},
    ]
    base["vendor"], base["renderer"] = WEBGL_MAP.get(base["platform"], WEBGL_MAP["Win32"])
    base["ua_full"] = full
    base["id"] = random.randint(1, 5)
    return base


def apply_identity(driver, ident: dict) -> None:
    driver.execute_cdp_cmd(
        "Emulation.setUserAgentOverride",
        {
            "userAgent": ident["ua"],
            "acceptLanguage": ident["locale"],
            "platform": ident["platform"],
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not/A)Brand", "version": "99"},
                    {"brand": "Google Chrome", "version": str(CHROME_MAJOR)},
                    {"brand": "Chromium", "version": str(CHROME_MAJOR)},
                ],
                "fullVersionList": ident["full_version_list"],
                "fullVersion": ident["ua_full"],
                "platform": "macOS" if ident["platform"] == "MacIntel" else "Windows",
                "platformVersion": "15.3.0" if ident["platform"] == "MacIntel" else "10.0.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        },
    )
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": ident["tz"]})
    try:
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": ident["locale"]})
    except Exception:
        pass
    driver.execute_cdp_cmd(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": ident["w"], "height": ident["h"],
            "deviceScaleFactor": ident["dpr"], "mobile": False,
            "screenWidth": ident["w"], "screenHeight": ident["h"],
            "screenOrientation": {"type": "landscapePrimary", "angle": 90},
        },
    )
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
        "--no-first-run",
        "--no-default-browser-check",
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
    driver.set_page_load_timeout(35)
    driver.set_script_timeout(25)
    apply_identity(driver, ident)
    return driver


# ---------------------------------------------------------------------------
# GOOGLE ENTRY
# ---------------------------------------------------------------------------
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
        try:
            if js(driver, "return document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.4)
        waited += 0.4


def _on_sorry_page(driver) -> bool:
    try:
        url = driver.current_url or ""
    except Exception:
        return False
    return "/sorry/" in url or "consent.google" in url


def handle_google_interstitial(driver, deadline: float) -> bool:
    if not _on_sorry_page(driver):
        return False
    log("    [!] google interstitial detected")
    for _ in range(6):
        if budget_left(deadline) <= 0:
            break
        try:
            for a in driver.find_elements(By.CSS_SELECTOR, "#recaptcha-anchor, .recaptcha-checkbox"):
                if a.is_displayed():
                    a.click()
                    time.sleep(2.0)
                    break
        except Exception:
            pass
        try:
            for b in driver.find_elements(By.CSS_SELECTOR, "input[type=submit], button"):
                if b.is_displayed() and (b.get_attribute("value") or b.text or "").strip():
                    b.click()
                    time.sleep(2.0)
                    break
        except Exception:
            pass
        if not _on_sorry_page(driver):
            return True
        time.sleep(1.5)
    return not _on_sorry_page(driver)


def google_entry(driver, deadline: float) -> tuple[str, str] | None:
    cap = min(GOOGLE_CAP, max(budget_left(deadline), 5))
    step_deadline = time.time() + cap

    query = random.choice([
        "abdulsamad shop font generator",
        "abdulsamad.shop",
        "stylish text generator abdulsamad",
        "cool fonts copy paste abdulsamad",
    ])
    host = random.choice(["www.google.com", "google.co.uk"])
    url = f"https://{host}/search?q={quote(query)}&hl=en&num=20"

    log(f"    [*] google entry: {query!r}")
    _fast_get(driver, url, cap=12)
    handle_google_interstitial(driver, step_deadline)

    try:
        log(f"    [*] serp url: {driver.current_url[:90]}")
    except Exception:
        pass

    for _ in range(10):
        if budget_left(step_deadline) <= 0:
            break
        try:
            links = driver.find_elements(By.CSS_SELECTOR, "div#search a[href], a[jsname]")
        except Exception:
            links = []
        for link in links:
            try:
                href = link.get_attribute("href") or ""
            except Exception:
                continue
            if SITE_FRAG not in href or "/search?" in href:
                continue
            cdp_move(driver, random.randint(80, 500), random.randint(80, 300))
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 1.6))
            try:
                link.click()
            except Exception:
                try:
                    driver.get(href)
                except Exception:
                    continue
            time.sleep(2.0)
            try:
                final = driver.current_url
            except Exception:
                final = href
            log(f"    [+] google -> {final[:90]}")
            return final, query
        time.sleep(0.8)

    log("    [!] google: 0 of our links, going direct")
    try:
        _fast_get(driver, SITE, cap=12)
        return driver.current_url, query
    except Exception:
        return None


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
    """Returns (kind, final_url). kind is 'site', 'other' or 'error'."""
    url = path if path.startswith("http") else SITE + path
    try:
        driver.get(url)
    except Exception:
        try:
            js(driver, "window.stop();")
        except Exception:
            pass

    waited = 0.0
    while waited < 18:
        try:
            if js(driver, "return document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.5)
        waited += 0.5

    try:
        final = driver.current_url or url
    except Exception:
        final = url

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
        'ins.adsbygoogle iframe, ins.adsbygoogle, div[id^="google_ads_iframe"], iframe[id^="aswift"]',
    )
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
        time.sleep(random.uniform(3, 7))


# ---------------------------------------------------------------------------
# SUMMARY / SIGNALS
# ---------------------------------------------------------------------------
SUMMARY = {
    "run_id": RUN_ID,
    "started_utc": datetime.now(timezone.utc).isoformat(),
    "route": None,
    "identity": None,
    "clicks": 0,
    "views": 0,
    "google_entry": False,
    "targets": {"clicks": TARGET_CLICKS, "views": TARGET_VIEWS,
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
    log(f"[*] per-run: {TARGET_CLICKS} clicks / {TARGET_VIEWS} views | "
        f"daily: {daily_run_target()} runs = {DAILY_CLICK_TARGET} clicks "
        f"(runs done today: {state.get('runs', 0)}) | budget {MAX_RUN_SECONDS}s")

    if not slot_gate(state):
        log("[*] slot skipped, exiting")
        write_summary()
        return 0

    # charge the run immediately so concurrent runs cannot both take a slot
    state["runs"] = state.get("runs", 0) + 1
    save_state(state)

    pages = fetch_pages()
    log(f"[*] page pool: {len(pages)}")

    vpn_countries = {
        c.strip().upper().replace("UK", "GB")
        for c in os.environ.get("VPN_COUNTRIES", "US,GB,DE,NL,FR,SG,CA,AU").split(",")
        if c.strip()
    }
    if not {"JP", "KR"} <= vpn_countries:
        vpn_countries |= {"JP", "KR"}
        log("[*] note: VPN_COUNTRIES missing JP/KR, auto-fixed")

    ident = build_identity()
    SUMMARY["identity"] = {"cc": ident["cc"], "tz": ident["tz"], "ua": ident["ua"]}
    log(f"[*] identity: {ident['cc']} {ident['tz']} chrome/{CHROME_MAJOR}")

    driver = None
    vpn_proc = None
    route = None

    try:
        # ---- tier 1: residential proxy ---------------------------------
        if RESIDENTIAL_PROXY:
            log("[*] tier 1: residential proxy")
            try:
                driver = build_driver(ident, RESIDENTIAL_PROXY)
                route = "1-residential"
            except Exception as e:
                log(f"[!] residential failed: {e}")
                driver = None

        # ---- tier 2: VPN Gate ------------------------------------------
        if driver is None:
            vpn_deadline = time.time() + VPN_BUDGET
            log(f"[*] tier 2: VPN Gate (budget {VPN_BUDGET}s)")
            for tries, server in enumerate(fetch_vpngate(vpn_countries), 1):
                if tries > VPN_MAX_TRIES or budget_left(vpn_deadline) <= 0:
                    break
                log(f"    [*] vpn try {tries}/{VPN_MAX_TRIES}: "
                    f"{server['cc']} {server['ip']} ping={server['ping']:.0f}")
                vpn_proc = start_vpn(server)
                if wait_for_tunnel(vpn_proc, vpn_deadline):
                    try:
                        driver = build_driver(ident, None)
                        route = "2-vpn"
                        log(f"    [+] tunnel up via {server['cc']}")
                        break
                    except Exception as e:
                        log(f"    [!] chrome over vpn failed: {e}")
                        stop_vpn(vpn_proc)
                        vpn_proc = None
                else:
                    stop_vpn(vpn_proc)
                    vpn_proc = None

        # ---- tier 3: country-matched free proxy ------------------------
        if driver is None:
            proxy_deadline = time.time() + PROXY_BUDGET
            log(f"[*] tier 3: proxy pool (budget {PROXY_BUDGET}s)")
            pool = ProxyPool(prefer=(ident["cc"], "US", "GB", "DE", "NL"))
            pool.load(proxy_deadline)
            for _ in range(3):
                if budget_left(proxy_deadline) <= 0:
                    break
                cand = pool.next()
                if not cand:
                    break
                log(f"    [*] proxy try: {cand['addr']} ({cand['cc']})")
                try:
                    driver = build_driver(ident, cand["addr"])
                    route = "3-proxy"
                    break
                except Exception as e:
                    log(f"    [!] proxy failed: {e}")
                    driver = None

        # ---- tier 4: direct --------------------------------------------
        if driver is None:
            log("[*] tier 4: direct")
            driver = build_driver(ident, None)
            route = "4-direct"

        SUMMARY["route"] = route
        log(f"[+] route = {route}")

        # ---- session ----------------------------------------------------
        session_deadline = DEADLINE - 15

        entry = None
        if budget_left(session_deadline) > 60:
            entry = google_entry(driver, session_deadline)
            if entry:
                SUMMARY["google_entry"] = True

        if not entry:
            open_page(driver, "/", session_deadline)

        warmup(driver, session_deadline)

        clicks = 0
        views = 0
        pages_done = 0
        tried_paths: list[str] = []
        need_google_entry = True

        while budget_left(session_deadline) > 8:
            if clicks >= TARGET_CLICKS and views >= TARGET_VIEWS:
                break
            if pages_done >= MAX_PAGES_PER_RUN:
                break

            if not tried_paths:
                tried_paths = list(pages)
                random.shuffle(tried_paths)
            path = tried_paths.pop()

            if need_google_entry and random.random() < 0.35:
                if google_entry(driver, session_deadline):
                    need_google_entry = False
                    kind, final = "site", (driver.current_url or SITE)
                else:
                    kind, final = open_page(driver, path, session_deadline)
            else:
                kind, final = open_page(driver, path, session_deadline)

            if kind == "error":
                SUMMARY["errors"].append(path)
                continue

            if need_google_entry and SUMMARY["google_entry"] and kind == "site":
                need_google_entry = False

            pages_done += 1
            views += 1
            SUMMARY["views"] = views

            time.sleep(random.uniform(1.0, 3.0))
            human_scroll(driver)
            time.sleep(random.uniform(1.5, 4.0))

            if clicks < TARGET_CLICKS and ads_available(driver):
                log(f"[*] ad slot on {final[:70]}")
                if click_one_ad(driver, session_deadline):
                    clicks += 1
                    SUMMARY["clicks"] = clicks
                    log(f"[+] AD CLICKED ({clicks}/{TARGET_CLICKS}) | views {views}/{TARGET_VIEWS}")
                else:
                    log(f"[*] ad click missed ({clicks}/{TARGET_CLICKS}) | views {views}/{TARGET_VIEWS}")
                time.sleep(random.uniform(8, 22))
            else:
                log(f"[*] no ad -> viewing page ({views}/{TARGET_VIEWS})")

        log(f"[*] done: clicks={clicks} views={views} pages={pages_done} "
            f"elapsed={int(time.time() - START)}s")

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

        state["clicks"] = state.get("clicks", 0) + SUMMARY["clicks"]
        state["views"] = state.get("views", 0) + SUMMARY["views"]
        save_state(state)
        log(f"[*] daily state: runs={state['runs']}/{daily_run_target()} "
            f"clicks={state['clicks']} views={state['views']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())