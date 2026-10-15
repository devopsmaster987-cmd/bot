#!/usr/bin/env python3
"""Organic-traffic smoke test with gated ad clicks.

Runs from GitHub Actions on a schedule, or from an external pinger via
workflow_dispatch. Routes every session through a free US/UK VPN (or an
optional residential proxy), presents a consistent browser identity, behaves
like a human visitor, and clicks an ad on a small number of runs per day.
"""

import base64
import hashlib
import json
import os
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# Python 3.12 dropped distutils from the stdlib, and undetected_chromedriver
# still imports distutils.version. Importing setuptools installs the meta-path
# finder that re-exposes distutils, so this works even if the .pth hook missed.
try:
    import distutils  # noqa: F401
except ModuleNotFoundError:
    import setuptools  # noqa: F401
    import distutils  # noqa: F401

import undetected_chromedriver as uc
from pyvirtualdisplay import Display

# ═══════════════════════════════ CONFIG ═══════════════════════════════
TARGET_SITE = os.environ.get("TARGET_URL") or "https://www.abdulsamad.shop"
TARGET_HOST = "abdulsamad.shop"

BASE_PAGE_VIEWS = 3
MAX_SESSIONS_PER_RUN = 6
MAX_VIEWS_PER_SESSION = 8
MIN_DWELL_DESKTOP = (35, 180)
MIN_DWELL_MOBILE = (20, 95)
MIN_GAP, MAX_GAP = 10, 30

CLICKS_PER_DAY = 45
VPN_COUNTRIES = ("US", "GB")
MAX_VPN_ATTEMPTS = 3
VPN_CONNECT_TIMEOUT = 30
VPN_SHORTLIST = 60

MOBILE_PROBABILITY = 0.45
PERSIST_PROBABILITY = 0.60
ADBLOCK_PROBABILITY = 0.15
IDENTITY_COUNT = 5

AUDIENCE_TZ = "Europe/London"
HOUR_WEIGHTS = [2, 1, 1, 1, 1, 1, 2, 3, 5, 6, 7, 7, 7, 7, 7, 7, 8, 9, 10, 10, 9, 8, 6, 4]
MEAN_WEIGHT = sum(HOUR_WEIGHTS) / 24.0

RESIDENTIAL_PROXY = (os.environ.get("RESIDENTIAL_PROXY") or "").strip() or None
try:
    RESIDENTIAL_SHARE = float(os.environ.get("RESIDENTIAL_SHARE") or "0.7")
except ValueError:
    RESIDENTIAL_SHARE = 0.7

PROFILE_ROOT = os.path.join(os.getcwd(), ".chrome-profiles")
os.makedirs("reports", exist_ok=True)

ADBLOCK_URLS = [
    "*doubleclick.net*",
    "*googlesyndication.com*",
    "*googleadservices.com*",
    "*adservice.google.com*",
]

# ═══════════════════════════ BROWSER DETECTION ═══════════════════════════
def _version_of(cmd):
    try:
        out = subprocess.check_output(
            f"{cmd} --version", shell=True, stderr=subprocess.DEVNULL
        ).decode()
        m = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", out)
        if m:
            return m.group(1), ".".join(m.groups())
        m = re.search(r"(\d+)\.", out)
        if m:
            return m.group(1), f"{m.group(1)}.0.0.0"
    except Exception:
        pass
    return None, None


CHROME_MAJOR, CHROME_FULL = None, None
for _c in ("google-chrome-stable", "google-chrome", "chrome", "chromium", "chromium-browser"):
    CHROME_MAJOR, CHROME_FULL = _version_of(_c)
    if CHROME_MAJOR:
        break
if not CHROME_MAJOR:
    CHROME_MAJOR, CHROME_FULL = "126", "126.0.6478.126"
print(f"[*] Detected Chrome {CHROME_FULL}")

EDGE_BIN = None
for _p in (
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/microsoft-edge",
    "/opt/microsoft/msedge/microsoft-edge",
):
    if os.path.exists(_p):
        EDGE_BIN = _p
        break

EDGE_MAJOR, EDGE_FULL = (_version_of(EDGE_BIN) if EDGE_BIN else (None, None))
if EDGE_BIN and EDGE_MAJOR != CHROME_MAJOR:
    print(f"[!] Edge {EDGE_MAJOR} != Chrome {CHROME_MAJOR}; using Chrome for Edge identities")
    EDGE_BIN = None
elif EDGE_BIN:
    print(f"[*] Real Edge available {EDGE_FULL}")


def brands_for(edge=False):
    out = [
        {"brand": "Not/A)Brand", "version": "24"},
        {"brand": "Chromium", "version": CHROME_MAJOR},
    ]
    if edge:
        out.append({"brand": "Microsoft Edge", "version": EDGE_MAJOR or CHROME_MAJOR})
    else:
        out.append({"brand": "Google Chrome", "version": CHROME_MAJOR})
    return out


CHROME_BRANDS = brands_for(False)
EDGE_BRANDS = brands_for(True)

# ═══════════════════════════ VOICE LISTS ═══════════════════════════
VOICES_WIN = [
    {"voiceURI": "Microsoft David - English (United States)", "name": "Microsoft David - English (United States)", "lang": "en-US", "localService": True, "default": True},
    {"voiceURI": "Microsoft Zira - English (United States)", "name": "Microsoft Zira - English (United States)", "lang": "en-US", "localService": True, "default": False},
    {"voiceURI": "Microsoft Mark - English (United States)", "name": "Microsoft Mark - English (United States)", "lang": "en-US", "localService": True, "default": False},
    {"voiceURI": "Microsoft Hazel - English (United Kingdom)", "name": "Microsoft Hazel - English (United Kingdom)", "lang": "en-GB", "localService": True, "default": False},
]
VOICES_MAC = [
    {"voiceURI": "Samantha", "name": "Samantha", "lang": "en-US", "localService": True, "default": True},
    {"voiceURI": "Alex", "name": "Alex", "lang": "en-US", "localService": True, "default": False},
    {"voiceURI": "Daniel", "name": "Daniel", "lang": "en-GB", "localService": True, "default": False},
]
VOICES_LINUX = [
    {"voiceURI": "English (America)", "name": "English (America)", "lang": "en-US", "localService": True, "default": True},
    {"voiceURI": "English (Great Britain)", "name": "English (Great Britain)", "lang": "en-GB", "localService": True, "default": False},
]

# ═══════════════════════════ FINGERPRINT LIBRARY ═══════════════════════════
DESKTOP_PROFILES = [
    {
        "weight": 5, "browser": "chrome", "os_kind": "windows",
        "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36",
        "platform": "Win32", "ua_platform": "Windows",
        "platform_versions": ["10.0.0", "15.0.0"], "arch": "x86",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "dpr": [1, 1.25, 1.5], "cores": [4, 8, 12, 16], "mem": [4, 8, 16],
    },
    {
        "weight": 2, "browser": "edge", "os_kind": "windows",
        "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36 Edg/{EDGE_FULL or CHROME_FULL}",
        "platform": "Win32", "ua_platform": "Windows",
        "platform_versions": ["10.0.0", "15.0.0"], "arch": "x86",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "dpr": [1, 1.25], "cores": [8, 12, 16], "mem": [8, 16],
    },
    {
        "weight": 3, "browser": "chrome", "os_kind": "macos",
        "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36",
        "platform": "MacIntel", "ua_platform": "macOS",
        "platform_versions": ["14.5.0", "13.6.0"], "arch": "arm",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
        "dpr": [2], "cores": [8, 10], "mem": [8, 16],
    },
    {
        "weight": 1, "browser": "edge", "os_kind": "macos",
        "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36 Edg/{EDGE_FULL or CHROME_FULL}",
        "platform": "MacIntel", "ua_platform": "macOS",
        "platform_versions": ["14.5.0"], "arch": "arm",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        "dpr": [2], "cores": [8], "mem": [8],
    },
    {
        "weight": 1, "browser": "chrome", "os_kind": "linux",
        "ua": f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36",
        "platform": "Linux x86_64", "ua_platform": "Linux",
        "platform_versions": ["6.5.0"], "arch": "x86",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)",
        "dpr": [1], "cores": [4, 8], "mem": [8],
    },
]

MOBILE_PROFILES = [
    {
        "weight": 5, "model": "Pixel 7", "platform": "Android",
        "platform_version": "13.0.0", "arch": "",
        "ua": f"Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Mobile Safari/537.36",
        "vw": 412, "vh": 915, "dpr": 2.625,
        "webgl_vendor": "Google Inc. (Qualcomm)",
        "webgl_renderer": "ANGLE (Qualcomm, Adreno (TM) 730, OpenGL ES 3.2)",
    },
    {
        "weight": 3, "model": "SM-G991B", "platform": "Android",
        "platform_version": "13.0.0", "arch": "",
        "ua": f"Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Mobile Safari/537.36",
        "vw": 360, "vh": 800, "dpr": 3.0,
        "webgl_vendor": "Google Inc. (ARM)",
        "webgl_renderer": "ANGLE (ARM, Mali-G78 MP14, OpenGL ES 3.2)",
    },
    {
        "weight": 2, "model": "Pixel 6a", "platform": "Android",
        "platform_version": "14.0.0", "arch": "",
        "ua": f"Mozilla/5.0 (Linux; Android 14; Pixel 6a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Mobile Safari/537.36",
        "vw": 412, "vh": 892, "dpr": 2.625,
        "webgl_vendor": "Google Inc. (ARM)",
        "webgl_renderer": "ANGLE (ARM, Mali-G78 MP20, OpenGL ES 3.2)",
    },
]

COUNTRY_PROFILES = {
    "US": {
        "locales": ["en-US"],
        "tzs": ["America/New_York", "America/Chicago", "America/Denver",
                "America/Los_Angeles", "America/Phoenix"],
    },
    "GB": {"locales": ["en-GB"], "tzs": ["Europe/London"]},
}

OS_CHROME = {
    "windows": {"taskbar": 40, "chrome_h": (87, 145), "border_w": (16, 18), "avail_top": 0, "hint": "full"},
    "macos": {"taskbar": 25, "chrome_h": (88, 88), "border_w": (0, 1), "avail_top": 25, "hint": "none"},
    "linux": {"taskbar": 27, "chrome_h": (85, 140), "border_w": (15, 18), "avail_top": 0, "hint": "slight"},
}

DESKTOP_WINDOWS = [(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1280, 720), (1680, 1050)]
DESKTOP_SCREENS = [(1920, 1080), (2560, 1440), (3840, 2160), (1600, 900), (1366, 768)]

SEARCH_QUERIES = [
    "cool fonts to copy and paste", "instagram bio fonts", "free qr code generator",
    "aesthetic symbols copy paste", "fancy text generator", "kaomoji copy paste",
    "bold text generator", "upside down text generator", "cute symbols for bio",
]
REFERRER_SITES = [
    "https://www.instagram.com/", "https://twitter.com/", "https://www.reddit.com/",
    "https://www.pinterest.com/", "https://www.facebook.com/",
]
TYPE_SAMPLES = [
    "Hello world", "cool fonts", "aesthetic bio", "Abdul Samad", "test 123",
    "bold text here", "sparkle", "Nice!", "insta bio ideas", "2026",
]

LANDING_PAGES = [
    "https://www.abdulsamad.shop/",
    "https://www.abdulsamad.shop/fonts",
    "https://www.abdulsamad.shop/symbols",
    "https://www.abdulsamad.shop/emoji",
    "https://www.abdulsamad.shop/kaomoji",
    "https://www.abdulsamad.shop/tools",
    "https://www.abdulsamad.shop/image-compression",
    "https://www.abdulsamad.shop/qr-code-generator",
    "https://www.abdulsamad.shop/studio",
    "https://www.abdulsamad.shop/embed",
    "https://www.abdulsamad.shop/about",
    "https://www.abdulsamad.shop/privacy",
    "https://www.abdulsamad.shop/terms",
    "https://www.abdulsamad.shop/contact",
    "https://www.abdulsamad.shop/styles",
    "https://www.abdulsamad.shop/styles/instagram",
    "https://www.abdulsamad.shop/styles/whatsapp",
    "https://www.abdulsamad.shop/styles/discord",
    "https://www.abdulsamad.shop/styles/gaming",
    "https://www.abdulsamad.shop/styles/languages",
    "https://www.abdulsamad.shop/styles/colours",
    "https://www.abdulsamad.shop/fonts/bold",
    "https://www.abdulsamad.shop/fonts/italic",
    "https://www.abdulsamad.shop/fonts/bold-italic",
    "https://www.abdulsamad.shop/fonts/script",
    "https://www.abdulsamad.shop/fonts/bold-script",
    "https://www.abdulsamad.shop/fonts/fraktur",
    "https://www.abdulsamad.shop/fonts/bold-fraktur",
    "https://www.abdulsamad.shop/fonts/double-struck",
    "https://www.abdulsamad.shop/fonts/sans",
    "https://www.abdulsamad.shop/fonts/sans-bold",
    "https://www.abdulsamad.shop/fonts/sans-italic",
    "https://www.abdulsamad.shop/fonts/sans-bold-italic",
    "https://www.abdulsamad.shop/fonts/monospace",
    "https://www.abdulsamad.shop/fonts/fullwidth",
    "https://www.abdulsamad.shop/fonts/circled",
    "https://www.abdulsamad.shop/fonts/negative-circled",
    "https://www.abdulsamad.shop/fonts/squared",
    "https://www.abdulsamad.shop/fonts/negative-squared",
    "https://www.abdulsamad.shop/fonts/parenthesized",
    "https://www.abdulsamad.shop/fonts/small-caps",
    "https://www.abdulsamad.shop/fonts/superscript",
    "https://www.abdulsamad.shop/fonts/subscript",
    "https://www.abdulsamad.shop/fonts/underline",
    "https://www.abdulsamad.shop/fonts/double-underline",
    "https://www.abdulsamad.shop/fonts/overline",
    "https://www.abdulsamad.shop/fonts/strike",
    "https://www.abdulsamad.shop/fonts/slash",
    "https://www.abdulsamad.shop/fonts/wavy",
    "https://www.abdulsamad.shop/fonts/dotted",
    "https://www.abdulsamad.shop/fonts/short-strike",
    "https://www.abdulsamad.shop/fonts/short-slash",
    "https://www.abdulsamad.shop/fonts/tilde-under",
    "https://www.abdulsamad.shop/fonts/tilde-strike",
    "https://www.abdulsamad.shop/fonts/bar-under",
    "https://www.abdulsamad.shop/fonts/dot-above",
    "https://www.abdulsamad.shop/fonts/double-dot-under",
    "https://www.abdulsamad.shop/fonts/arrow-under",
    "https://www.abdulsamad.shop/fonts/triple-underline",
    "https://www.abdulsamad.shop/fonts/strike-underline",
    "https://www.abdulsamad.shop/fonts/overline-underline",
    "https://www.abdulsamad.shop/fonts/overline-strike",
    "https://www.abdulsamad.shop/fonts/wavy-under",
    "https://www.abdulsamad.shop/fonts/dotted-underline",
    "https://www.abdulsamad.shop/fonts/slash-under",
    "https://www.abdulsamad.shop/fonts/bar-underline",
    "https://www.abdulsamad.shop/fonts/wavy-strike",
    "https://www.abdulsamad.shop/fonts/dotted-strike",
    "https://www.abdulsamad.shop/fonts/crossed-out",
    "https://www.abdulsamad.shop/fonts/upside-down",
    "https://www.abdulsamad.shop/fonts/backwards",
    "https://www.abdulsamad.shop/fonts/flipped-letters",
    "https://www.abdulsamad.shop/fonts/backwards-words",
    "https://www.abdulsamad.shop/fonts/upside-down-words",
    "https://www.abdulsamad.shop/fonts/zalgo-light",
    "https://www.abdulsamad.shop/fonts/zalgo-heavy",
    "https://www.abdulsamad.shop/fonts/zalgo-medium",
    "https://www.abdulsamad.shop/fonts/zalgo-extreme",
    "https://www.abdulsamad.shop/fonts/glitch-alternate",
    "https://www.abdulsamad.shop/fonts/sparkle-sparkle",
    "https://www.abdulsamad.shop/fonts/stars-frame",
    "https://www.abdulsamad.shop/fonts/alternating-case",
    "https://www.abdulsamad.shop/fonts/all-caps",
    "https://www.abdulsamad.shop/fonts/all-lowercase",
    "https://www.abdulsamad.shop/fonts/symbol-star",
    "https://www.abdulsamad.shop/fonts/symbol-heart",
    "https://www.abdulsamad.shop/fonts/symbol-bullet",
    "https://www.abdulsamad.shop/fonts/symbol-sparkle",
    "https://www.abdulsamad.shop/fonts/symbol-arrow",
    "https://www.abdulsamad.shop/fonts/symbol-guillemet",
    "https://www.abdulsamad.shop/fonts/symbol-music",
    "https://www.abdulsamad.shop/fonts/symbol-flower",
    "https://www.abdulsamad.shop/fonts/symbol-join-spaced",
    "https://www.abdulsamad.shop/fonts/emoji-sparkle",
    "https://www.abdulsamad.shop/fonts/emoji-frame-blossom",
    "https://www.abdulsamad.shop/fonts/emoji-frame-star",
    "https://www.abdulsamad.shop/fonts/emoji-frame-heart",
    "https://www.abdulsamad.shop/fonts/emoji-frame-sparkles",
    "https://www.abdulsamad.shop/fonts/emoji-frame-fire",
    "https://www.abdulsamad.shop/fonts/emoji-frame-party",
    "https://www.abdulsamad.shop/fonts/kaomoji-flip",
    "https://www.abdulsamad.shop/fonts/kaomoji-shrug",
    "https://www.abdulsamad.shop/fonts/kaomoji-happy",
    "https://www.abdulsamad.shop/fonts/kaomoji-cat",
    "https://www.abdulsamad.shop/fonts/kaomoji-heart",
    "https://www.abdulsamad.shop/fonts/clan-tag",
    "https://www.abdulsamad.shop/fonts/gg-frame",
    "https://www.abdulsamad.shop/fonts/console",
    "https://www.abdulsamad.shop/fonts/shout-caps",
    "https://www.abdulsamad.shop/fonts/respawn",
    "https://www.abdulsamad.shop/fonts/frag-line",
    "https://www.abdulsamad.shop/fonts/boss-mode",
    "https://www.abdulsamad.shop/fonts/health-bar",
    "https://www.abdulsamad.shop/fonts/bio-hearts",
    "https://www.abdulsamad.shop/fonts/profile-stars",
    "https://www.abdulsamad.shop/fonts/story-title",
    "https://www.abdulsamad.shop/fonts/handle-wrap",
    "https://www.abdulsamad.shop/fonts/soft-bio",
    "https://www.abdulsamad.shop/fonts/aesthetic-wide",
    "https://www.abdulsamad.shop/fonts/caption-underline",
    "https://www.abdulsamad.shop/fonts/cute-bubble",
]

PAGE_WEIGHTS = {
    "/": 30, "/fonts": 12, "/symbols": 8, "/emoji": 7, "/kaomoji": 5,
    "/tools": 4, "/qr-code-generator": 4, "/image-compression": 3, "/studio": 3,
}

POINTER = {"x": 400.0, "y": 300.0}
SESSION = {"mobile": False}


def htime(lo, hi):
    return lo + (hi - lo) * (random.random() ** 1.3)


def stable_seed(*parts):
    return int(hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()[:8], 16)


# ═══════════════════════════ IP / ROUTE ═══════════════════════════
def current_ip(timeout=8):
    try:
        return requests.get("https://api.ipify.org", timeout=timeout).text.strip()
    except Exception:
        return None


def proxy_exit_ip(pstr, timeout=10):
    try:
        return requests.get(
            "https://api.ipify.org",
            proxies={"http": f"http://{pstr}", "https": f"http://{pstr}"},
            timeout=timeout,
        ).text.strip()
    except Exception:
        return None


def ip_country(ip):
    try:
        return requests.get(
            f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=8
        ).json().get("countryCode")
    except Exception:
        return None


ORIGIN_IP = current_ip()
print(f"[*] Runner origin IP: {ORIGIN_IP}")
print(f"[*] Residential proxy: {'set' if RESIDENTIAL_PROXY else 'not set (VPN only)'}")


def fetch_vpngate(countries=VPN_COUNTRIES):
    seen, servers = set(), []
    try:
        r = requests.get("https://www.vpngate.net/api/iphone/", timeout=25)
        for line in r.text.splitlines():
            line = line.strip()
            if not line or line[0] in "*#":
                continue
            p = line.split(",")
            if len(p) < 15 or p[0] == "HostName" or p[6] not in countries:
                continue
            if p[1] in seen:
                continue
            seen.add(p[1])
            servers.append({
                "ip": p[1],
                "score": int(p[2]) if p[2].isdigit() else 0,
                "country": p[6],
                "config": p[14],
            })
    except Exception as e:
        print(f"[!] VPN Gate fetch failed: {e}")
    servers.sort(key=lambda s: s["score"], reverse=True)
    short = servers[:VPN_SHORTLIST]
    random.shuffle(short)
    print(f"[+] VPN Gate: {len(servers)} unique US/GB servers, {len(short)} shortlisted")
    return short


def vpn_disconnect():
    subprocess.run("sudo pkill -f 'openvpn --config' >/dev/null 2>&1", shell=True)
    time.sleep(1.5)


def vpn_connect(server, timeout=VPN_CONNECT_TIMEOUT):
    cfg, auth, logf = "/tmp/vpg.ovpn", "/tmp/vpg.auth", "/tmp/vpg.log"
    try:
        data = base64.b64decode(server["config"]).decode("utf-8", "ignore")
    except Exception:
        return None, None

    with open(cfg, "w") as f:
        f.write(data)
        f.write(
            "\n\n# compat for OpenVPN 2.6+\n"
            "data-ciphers AES-128-CBC:AES-256-GCM:AES-128-GCM\n"
            "data-ciphers-fallback AES-128-CBC\n"
            "auth-nocache\nverb 3\n"
        )
    with open(auth, "w") as f:
        f.write("vpn\nvpn\n")

    vpn_disconnect()
    subprocess.run(
        f"sudo openvpn --config {cfg} --auth-user-pass {auth} "
        f"--daemon --log {logf} --writepid /tmp/vpg.pid",
        shell=True, check=False,
    )

    start = time.time()
    while time.time() - start < timeout:
        ip = current_ip()
        if ip and ip != ORIGIN_IP:
            cc = ip_country(ip)
            if cc in VPN_COUNTRIES:
                return ip, cc
        time.sleep(2)
    vpn_disconnect()
    return None, None


def establish_route():
    if RESIDENTIAL_PROXY and random.random() < RESIDENTIAL_SHARE:
        ip = proxy_exit_ip(RESIDENTIAL_PROXY)
        if ip:
            cc = ip_country(ip)
            print(f"[+] Residential proxy exit {ip} ({cc})")
            return RESIDENTIAL_PROXY, f"RES:{ip}", cc or "US", (lambda: None)
        print("[!] Residential proxy unreachable, falling back to VPN")

    for attempt in range(MAX_VPN_ATTEMPTS):
        if not vpn_servers:
            break
        srv = random.choice(vpn_servers)
        print(f"[*] VPN attempt {attempt + 1}/{MAX_VPN_ATTEMPTS} -> {srv['country']} {srv['ip']}")
        ip, cc = vpn_connect(srv)
        if ip:
            print(f"[+] Tunnel up {cc} {ip}")
            return None, f"VPN-{cc}:{ip}", cc, vpn_disconnect

    print("[!] No route available")
    return None, None, None, None


# ═══════════════════════════ FINGERPRINT ═══════════════════════════
def weighted(items):
    total = sum(i["weight"] for i in items)
    r = random.uniform(0, total)
    acc = 0
    for i in items:
        acc += i["weight"]
        if acc >= r:
            return i
    return items[-1]


def generate_fingerprint(cc):
    c = COUNTRY_PROFILES.get(cc, COUNTRY_PROFILES["US"])

    if random.random() < MOBILE_PROBABILITY:
        m = weighted(MOBILE_PROFILES)
        fp = {
            "device": "mobile", "browser": "chrome", "os_kind": "android",
            "ua": m["ua"], "platform": m["platform"], "ua_platform": m["platform"],
            "platform_version": m["platform_version"], "arch": m["arch"],
            "model": m["model"], "mobile": True,
            "brands": CHROME_BRANDS, "browser_full": CHROME_FULL,
            "webgl_vendor": m["webgl_vendor"], "webgl_renderer": m["webgl_renderer"],
            "dpr": m["dpr"], "cores": random.choice([6, 8]), "mem": random.choice([4, 6, 8]),
            "locale": random.choice(c["locales"]), "timezone": random.choice(c["tzs"]),
            "vw": m["vw"], "vh": m["vh"], "win": (520, 940), "screen": (m["vw"], m["vh"]),
            "chrome_h": 0, "border_w": 0, "taskbar": 0, "avail_top": 0,
            "pos": (0, 0), "touch": 5, "voices": VOICES_LINUX, "hint": "slight",
        }
    else:
        o = weighted(DESKTOP_PROFILES)
        kind = o["os_kind"]
        oc = OS_CHROME[kind]
        win = random.choice(DESKTOP_WINDOWS)
        scr = random.choice(
            [s for s in DESKTOP_SCREENS if s[0] >= win[0] and s[1] >= win[1]] or DESKTOP_SCREENS
        )
        border_w = random.randint(*oc["border_w"])
        max_x = max(0, scr[0] - (win[0] + border_w) - 10)
        pos = (random.randint(0, max(0, min(160, max_x))), random.randint(0, 90))
        fp = {
            "device": "desktop", "browser": o["browser"], "os_kind": kind,
            "ua": o["ua"], "platform": o["platform"], "ua_platform": o["ua_platform"],
            "platform_version": random.choice(o["platform_versions"]), "arch": o["arch"],
            "model": "", "mobile": False,
            "brands": EDGE_BRANDS if o["browser"] == "edge" else CHROME_BRANDS,
            "browser_full": (EDGE_FULL or CHROME_FULL) if o["browser"] == "edge" else CHROME_FULL,
            "webgl_vendor": o["webgl_vendor"], "webgl_renderer": o["webgl_renderer"],
            "dpr": random.choice(o["dpr"]), "cores": random.choice(o["cores"]),
            "mem": random.choice(o["mem"]),
            "locale": random.choice(c["locales"]), "timezone": random.choice(c["tzs"]),
            "vw": win[0], "vh": win[1], "win": win, "screen": scr,
            "chrome_h": random.randint(*oc["chrome_h"]), "border_w": border_w,
            "taskbar": oc["taskbar"], "avail_top": oc["avail_top"], "pos": pos, "touch": 0,
            "voices": {"windows": VOICES_WIN, "macos": VOICES_MAC}.get(kind, VOICES_LINUX),
            "hint": oc["hint"],
        }

    fp["seed"] = stable_seed(fp["ua"], fp["screen"], fp["timezone"], fp["dpr"], fp["cores"])
    return fp


def identity_dir(idx):
    return os.path.join(PROFILE_ROOT, f"id{idx}")


def build_fingerprint(cc, identity):
    if identity:
        fp_file = os.path.join(identity_dir(identity), "fp.json")
        if os.path.exists(fp_file):
            try:
                saved = json.load(open(fp_file))
                if saved.get("country") == cc or random.random() < 0.5:
                    return saved["fp"]
            except Exception:
                pass
        fp = generate_fingerprint(cc)
        try:
            os.makedirs(identity_dir(identity), exist_ok=True)
            json.dump({"fp": fp, "country": cc}, open(fp_file, "w"))
        except Exception:
            pass
        return fp
    return generate_fingerprint(cc)


JS_PATCH = r"""
(() => {
  const def = (o, p, v) => { try { Object.defineProperty(o, p, {get: () => v, configurable: true}); } catch(e){} };
  const SEED = %SEED%;
  const rnd = (function(a){ return function(){ a|=0; a=a+0x6D2B79F5|0;
    let t=Math.imul(a^a>>>15,1|a); t=t+Math.imul(t^t>>>7,61|t)^t;
    return ((t^t>>>14)>>>0)/4294967296; }; })(SEED);

  def(navigator, 'webdriver', false);
  def(navigator, 'hardwareConcurrency', %CORES%);
  def(navigator, 'deviceMemory', %MEM%);
  def(navigator, 'platform', '%PLATFORM%');
  def(navigator, 'language', '%LANG%');
  def(navigator, 'languages', %LANGS%);
  def(navigator, 'maxTouchPoints', %TOUCH%);
  def(navigator, 'pdfViewerEnabled', true);

  const iw = window.innerWidth, ih = window.innerHeight;
  def(window, 'outerWidth', iw + %BORDERW%);
  def(window, 'outerHeight', ih + %CHROMEH%);
  def(window, 'screenX', %POSX%);
  def(window, 'screenY', %POSY%);
  def(window, 'screenLeft', %POSX%);
  def(window, 'screenTop', %POSY%);

  const s = window.screen;
  def(s, 'availTop', %AVAILTOP%);
  def(s, 'availLeft', 0);
  def(s, 'availWidth', s.width);
  def(s, 'availHeight', Math.max(1, s.height - %TASKBAR%));

  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
    if (!window.chrome.app) window.chrome.app = {isInstalled:false, InstallState:{DISABLED:'disabled'}, RunningState:{RUNNING:'running'}};
    if (!window.chrome.csi) window.chrome.csi = () => ({startE: performance.now()-1200, onloadT: performance.now()-400, pageT: 900, tran: 15});
    if (!window.chrome.loadTimes) window.chrome.loadTimes = () => ({
      requestTime: (performance.now()-1400)/1000, startLoadTime: (performance.now()-1300)/1000,
      commitLoadTime: (performance.now()-1100)/1000, finishDocumentLoadTime: (performance.now()-500)/1000,
      finishLoadTime: (performance.now()-400)/1000, firstPaintTime: (performance.now()-900)/1000,
      firstPaintAfterLoadTime: 0, navigationType: 'Other', wasFetchedViaSpdy: true,
      wasNpnNegotiated: true, npnProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2'
    });
  } catch(e){}

  try { Object.defineProperty(Notification, 'permission', {get: () => 'default', configurable: true}); } catch(e){}
  try {
    const q = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) => (p && p.name === 'notifications')
      ? Promise.resolve({state: Notification.permission, onchange: null}) : q(p);
  } catch(e){}

  try {
    const LIMITS = {3379: 16384, 34024: 16384, 34076: 16384, 34930: 16, 35660: 16,
                    35661: 32, 36349: 1024, 36347: 4096, 36348: 30, 3386: [32767, 32767]};
    const EXTS = ["ANGLE_instanced_arrays","EXT_blend_minmax","EXT_color_buffer_half_float",
      "EXT_disjoint_timer_query","EXT_float_blend","EXT_frag_depth","EXT_shader_texture_lod",
      "EXT_texture_compression_bptc","EXT_texture_compression_rgtc","EXT_texture_filter_anisotropic",
      "WEBGL_color_buffer_float","WEBGL_compressed_texture_s3tc","WEBGL_compressed_texture_s3tc_srgb",
      "WEBGL_debug_renderer_info","WEBGL_debug_shaders","WEBGL_depth_texture","WEBGL_draw_buffers",
      "WEBGL_lose_context","WEBGL_multi_draw"];
    const patch = (P) => {
      const gp = P.getParameter;
      P.getParameter = function(p) {
        if (p === 37445) return '%WGLV%';
        if (p === 37446) return '%WGLR%';
        if (LIMITS[p] !== undefined) return LIMITS[p];
        return gp.call(this, p);
      };
      P.getSupportedExtensions = function(){ return EXTS.slice(); };
      P.getShaderPrecisionFormat = function(){ return {rangeMin: 127, rangeMax: 127, precision: 23}; };
    };
    patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  } catch(e){}

  try {
    const gid = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(){
      const d = gid.apply(this, arguments);
      const n = Math.min(d.data.length, 12000);
      for (let i = 0; i < n; i += 4) if (rnd() < 0.05) d.data[i] = (d.data[i] + 1) & 255;
      return d;
    };
  } catch(e){}
  try {
    const tdu = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(){
      try {
        const c = document.createElement('canvas');
        c.width = this.width; c.height = this.height;
        const ctx = c.getContext('2d');
        ctx.drawImage(this, 0, 0);
        const im = ctx.getImageData(0, 0, c.width, c.height);
        const n = Math.min(im.data.length, 12000);
        for (let i = 0; i < n; i += 4) if (rnd() < 0.05) im.data[i] = (im.data[i] + 1) & 255;
        ctx.putImageData(im, 0, 0);
        return tdu.apply(c, arguments);
      } catch(err) { return tdu.apply(this, arguments); }
    };
  } catch(e){}

  try {
    if (window.OfflineAudioContext) {
      const sr = OfflineAudioContext.prototype.startRendering;
      OfflineAudioContext.prototype.startRendering = function(){
        return sr.call(this).then(function(buf){
          try {
            const ch = buf.getChannelData(0);
            for (let i = 0; i < ch.length; i += 40) ch[i] += (rnd() - 0.5) * 2e-6;
          } catch(e){}
          return buf;
        });
      };
    }
  } catch(e){}

  try {
    const V = %VOICES%;
    if (window.speechSynthesis) window.speechSynthesis.getVoices = () => V;
  } catch(e){}

  try { if (!navigator.connection) def(navigator, 'connection', {effectiveType:'4g', rtt:50, downlink:10, saveData:false, type:'wifi'}); } catch(e){}
})();
"""


def create_driver(proxy, fp, profile_dir=None):
    mobile = fp["device"] == "mobile"
    SESSION["mobile"] = mobile

    feats = ["CalculateNativeWinOcclusion"]
    if fp["os_kind"] != "macos":
        feats.append("OverlayScrollbar")

    opts = uc.ChromeOptions()
    for arg in [
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled", "--disable-infobars",
        "--disable-extensions", "--disable-gpu",
        "--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble",
        "--password-store=basic", "--use-mock-keychain",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=" + ",".join(feats),
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--font-render-hinting={fp.get('hint', 'none')}",
        f"--window-size={fp['win'][0]},{fp['win'][1]}",
        "--window-position=0,0",
        f"--lang={fp['locale']}",
        f"--user-agent={fp['ua']}",
        "--disk-cache-size=104857600",
    ]:
        opts.add_argument(arg)
    if proxy:
        opts.add_argument(f"--proxy-server=http://{proxy}")

    kwargs = {"version_main": int(CHROME_MAJOR)}
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        for f in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(profile_dir, f))
            except OSError:
                pass
        kwargs["user_data_dir"] = profile_dir
    if fp["browser"] == "edge" and EDGE_BIN:
        kwargs["browser_executable_path"] = EDGE_BIN
        kwargs["version_main"] = int(EDGE_MAJOR)

    try:
        driver = uc.Chrome(options=opts, **kwargs)
    except Exception as e:
        print(f"[!] Launch failed ({e}); retrying with Chrome defaults")
        kwargs.pop("browser_executable_path", None)
        kwargs["version_main"] = int(CHROME_MAJOR)
        driver = uc.Chrome(options=opts, **kwargs)

    driver.implicitly_wait(10)
    POINTER["x"] = random.uniform(80, 900)
    POINTER["y"] = random.uniform(80, 500)

    try:
        driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {
            "userAgent": fp["ua"],
            "acceptLanguage": fp["locale"],
            "platform": fp["platform"],
            "userAgentMetadata": {
                "brands": fp["brands"],
                "fullVersionList": [
                    {"brand": b["brand"], "version": fp["browser_full"]} for b in fp["brands"]
                ],
                "fullVersion": fp["browser_full"],
                "platform": fp["ua_platform"],
                "platformVersion": fp["platform_version"],
                "architecture": fp["arch"],
                "model": fp["model"],
                "mobile": fp["mobile"],
            },
        })
    except Exception as e:
        print(f"[!] UA override failed: {e}")

    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": fp["timezone"]})
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": fp["locale"]})
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": fp["vw"], "height": fp["vh"], "deviceScaleFactor": fp["dpr"],
            "mobile": fp["mobile"],
            "screenWidth": fp["screen"][0], "screenHeight": fp["screen"][1],
            "screenOrientation": {
                "type": "portraitPrimary" if mobile else "landscapePrimary", "angle": 0,
            },
        })
    except Exception:
        pass

    if mobile:
        try:
            driver.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}
            )
        except Exception:
            pass

    script = (
        JS_PATCH
        .replace("%SEED%", str(fp["seed"]))
        .replace("%CORES%", str(fp["cores"]))
        .replace("%MEM%", str(fp["mem"]))
        .replace("%PLATFORM%", fp["platform"])
        .replace("%LANG%", fp["locale"])
        .replace("%LANGS%", json.dumps([fp["locale"], fp["locale"].split("-")[0]]))
        .replace("%TOUCH%", str(fp["touch"]))
        .replace("%BORDERW%", str(fp["border_w"]))
        .replace("%CHROMEH%", str(fp["chrome_h"]))
        .replace("%POSX%", str(fp["pos"][0]))
        .replace("%POSY%", str(fp["pos"][1]))
        .replace("%AVAILTOP%", str(fp["avail_top"]))
        .replace("%TASKBAR%", str(fp["taskbar"]))
        .replace("%WGLV%", fp["webgl_vendor"])
        .replace("%WGLR%", fp["webgl_renderer"])
        .replace("%VOICES%", json.dumps(fp["voices"]))
    )
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Runtime.disable", {})
    except Exception:
        pass

    return driver


def enable_adblock(driver):
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ADBLOCK_URLS})
        return True
    except Exception:
        return False


# ═══════════════════════════ INPUT PRIMITIVES ═══════════════════════════
def cdp(driver, method, params):
    try:
        return driver.execute_cdp_cmd(method, params)
    except Exception:
        return None


def viewport(driver):
    try:
        return driver.execute_script("return [window.innerWidth, window.innerHeight]")
    except Exception:
        return [1280, 720]


def human_move(driver, tx, ty):
    sx, sy = POINTER["x"], POINTER["y"]
    ox, oy = tx, ty
    if random.random() < 0.55:
        ox = tx + random.uniform(-18, 18)
        oy = ty + random.uniform(-14, 14)

    cx = (sx + ox) / 2 + random.randint(-90, 90)
    cy = (sy + oy) / 2 + random.randint(-90, 90)
    steps = random.randint(10, 24)

    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 2
        x = (1 - e) ** 2 * sx + 2 * (1 - e) * e * cx + e ** 2 * ox + random.gauss(0, 0.8)
        y = (1 - e) ** 2 * sy + 2 * (1 - e) * e * cy + e ** 2 * oy + random.gauss(0, 0.8)
        cdp(driver, "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": float(x), "y": float(y), "buttons": 0})
        if random.random() < 0.12:
            time.sleep(random.uniform(0.03, 0.12))
        time.sleep(random.uniform(0.006, 0.022))

    if (ox, oy) != (tx, ty):
        for i in range(1, random.randint(4, 7)):
            f = i / 6
            cdp(driver, "Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": float(ox + (tx - ox) * f),
                "y": float(oy + (ty - oy) * f),
                "buttons": 0,
            })
            time.sleep(random.uniform(0.008, 0.02))

    POINTER["x"], POINTER["y"] = tx, ty


def mouse_jiggle(driver):
    vw, vh = viewport(driver)
    for _ in range(random.randint(2, 4)):
        human_move(driver, random.uniform(vw * 0.15, vw * 0.85),
                   random.uniform(vh * 0.15, vh * 0.85))
        time.sleep(random.uniform(0.15, 0.6))


def elem_point(driver, elem, jitter=True):
    p = driver.execute_script(
        "const r = arguments[0].getBoundingClientRect();"
        "return {x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height};",
        elem,
    )
    vw, vh = viewport(driver)
    x, y = p["x"], p["y"]
    if jitter:
        x += random.uniform(-0.35, 0.35) * max(2, p["w"] / 2)
        y += random.uniform(-0.35, 0.35) * max(2, p["h"] / 2)
    return min(max(x, 3), vw - 4), min(max(y, 3), vh - 4)


def human_click(driver, elem):
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});", elem
    )
    time.sleep(htime(0.5, 1.4))
    x, y = elem_point(driver, elem)

    if SESSION["mobile"]:
        cdp(driver, "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        time.sleep(random.uniform(0.05, 0.15))
        cdp(driver, "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    else:
        human_move(driver, x, y)
        time.sleep(random.uniform(0.15, 0.55))
        ok = cdp(driver, "Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1, "buttons": 1,
        })
        time.sleep(random.uniform(0.04, 0.14))
        cdp(driver, "Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1, "buttons": 0,
        })
        if ok is None:
            try:
                ActionChains(driver).move_to_element(elem).click().perform()
            except Exception:
                pass

    time.sleep(random.uniform(0.3, 0.9))


def scroll(driver, amount):
    vw, vh = viewport(driver)
    before = driver.execute_script("return window.scrollY")

    if SESSION["mobile"]:
        cdp(driver, "Input.synthesizeScrollGesture", {
            "x": int(vw // 2), "y": int(vh // 2), "xDistance": 0,
            "yDistance": -int(amount), "speed": random.randint(600, 1800),
            "gestureSourceType": "touch",
        })
        time.sleep(random.uniform(0.3, 0.9))
    else:
        left = amount
        while abs(left) > 1:
            step = random.randint(90, 260)
            if step > abs(left):
                step = abs(left)
            if left < 0:
                step = -step
            cdp(driver, "Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": float(vw * random.uniform(0.35, 0.65)),
                "y": float(vh * random.uniform(0.35, 0.65)),
                "deltaX": 0, "deltaY": float(step),
            })
            left -= step
            time.sleep(random.uniform(0.03, 0.11))

    after = driver.execute_script("return window.scrollY")
    if before == after and abs(amount) > 5:
        driver.execute_script(f"window.scrollBy(0, {amount});")


def scroll_burst(driver, notches=1):
    for _ in range(max(1, notches)):
        scroll(driver, random.randint(150, 650))
        time.sleep(random.uniform(0.3, 0.9))


def human_type_text(el, text):
    i = 0
    while i < len(text):
        for _ in range(random.randint(1, 4)):
            if i >= len(text):
                break
            el.send_keys(text[i])
            i += 1
            time.sleep(random.uniform(0.03, 0.16))
        if random.random() < 0.18:
            el.send_keys(random.choice("abcdefghijklmnopqrstuvwxyz"))
            time.sleep(random.uniform(0.08, 0.30))
            el.send_keys(Keys.BACKSPACE)
            time.sleep(random.uniform(0.10, 0.35))
        time.sleep(random.uniform(0.15, 0.60))


def maybe_type(driver):
    try:
        fields = [
            e for e in driver.find_elements(
                By.CSS_SELECTOR, "input[type='text'], input:not([type]), textarea"
            )
            if e.is_displayed() and e.is_enabled() and e.size["width"] > 40
        ]
    except Exception:
        return
    if not fields:
        return
    el = random.choice(fields)
    human_click(driver, el)
    time.sleep(random.uniform(0.3, 0.9))
    human_type_text(el, random.choice(TYPE_SAMPLES))


def maybe_copy(driver):
    try:
        a = ActionChains(driver)
        a.key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL)
        a.perform()
        time.sleep(random.uniform(0.2, 0.5))
        a = ActionChains(driver)
        a.key_down(Keys.CONTROL).send_keys("c").key_up(Keys.CONTROL)
        a.perform()
    except Exception:
        pass


def maybe_refresh(driver):
    try:
        driver.refresh()
        time.sleep(htime(1.5, 4.0))
    except Exception:
        pass


def maybe_tab_switch(driver):
    try:
        before = driver.window_handles
        q = random.choice(SEARCH_QUERIES).replace(" ", "+")
        driver.execute_script("window.open(arguments[0], '_blank');",
                              f"https://www.google.com/search?q={q}")
        time.sleep(random.uniform(0.8, 2.0))
        after = driver.window_handles
        if len(after) <= len(before):
            return
        driver.switch_to.window(after[-1])
        time.sleep(htime(2.0, 6.0))
        scroll(driver, random.randint(200, 700))
        time.sleep(random.uniform(0.8, 2.5))
        driver.close()
        driver.switch_to.window(before[0])
        time.sleep(random.uniform(0.5, 1.5))
    except Exception:
        try:
            driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass


# ═══════════════════════════ PAGE BEHAVIOUR ═══════════════════════════
def dwell(driver, seconds, energy=1.0):
    end = time.time() + seconds
    while time.time() < end:
        r = random.random()
        if r < 0.34:
            scroll(driver, random.randint(200, 900))
            time.sleep(htime(0.6, 2.2))
        elif r < 0.48:
            scroll(driver, -random.randint(150, 500))
            time.sleep(htime(0.5, 1.6))
        elif r < 0.62:
            maybe_type(driver)
            time.sleep(htime(0.8, 2.5))
        elif r < 0.70:
            maybe_copy(driver)
            time.sleep(htime(0.6, 1.8))
        elif r < 0.78:
            maybe_tab_switch(driver)
        elif r < 0.86:
            if not SESSION["mobile"]:
                mouse_jiggle(driver)
            time.sleep(random.uniform(0.5, 1.5))
        else:
            time.sleep(htime(1.5, 6.0))

        if random.random() < 0.15 * energy:
            click_random_button(driver)
        if random.random() < 0.06:
            maybe_refresh(driver)


CLICKABLE = ["button", "[role='button']", "input[type='button']", "input[type='submit']", "a[href]"]


def click_random_button(driver):
    before = driver.window_handles
    sels = CLICKABLE[:]
    random.shuffle(sels)
    for sel in sels:
        try:
            els = [
                e for e in driver.find_elements(By.CSS_SELECTOR, sel)
                if e.is_displayed() and e.size["height"] > 10 and e.size["width"] > 10
            ]
            if not els:
                continue
            human_click(driver, random.choice(els))
            time.sleep(htime(0.8, 2.0))
            after = driver.window_handles
            if len(after) > len(before):
                driver.switch_to.window(after[-1])
                time.sleep(htime(2.0, 5.0))
                if random.random() < 0.6:
                    driver.close()
                    driver.switch_to.window(before[0])
            return True
        except Exception:
            continue
    return False


def click_internal_link(driver):
    try:
        cur = driver.current_url.split("#")[0]
        links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        return False

    random.shuffle(links)
    for a in links[:50]:
        try:
            href = (a.get_attribute("href") or "").split("#")[0]
            if not href or TARGET_HOST not in href or href == cur:
                continue
            before = driver.window_handles
            human_click(driver, a)
            time.sleep(htime(1.5, 4.0))
            after = driver.window_handles
            if len(after) > len(before):
                driver.switch_to.window(after[-1])
            if driver.current_url.split("#")[0] != cur:
                return True
        except Exception:
            continue
    return False


AD_SELECTORS = [
    "ins[data-ad-slot]",
    "ins.adsbygoogle",
    'iframe[src*="googleads"]',
    'iframe[src*="googlesyndication"]',
    'iframe[id*="google_ads"]',
    "div[data-google-query-id]",
    'div[id*="google_ads_"]',
    'div[class*="adsbygoogle"]',
    'div[id*="ad"]',
    'div[class*="ad-"]',
    'div[class*="advert"]',
]


def ads_available(driver):
    for sel in AD_SELECTORS:
        try:
            for e in driver.find_elements(By.CSS_SELECTOR, sel):
                if e.is_displayed() and e.size["height"] > 10 and e.size["width"] > 10:
                    return True
        except Exception:
            continue
    return False


def click_one_ad(driver):
    sels = AD_SELECTORS[:]
    random.shuffle(sels)
    for sel in sels:
        try:
            for elem in driver.find_elements(By.CSS_SELECTOR, sel):
                if not elem.is_displayed():
                    continue
                sz = elem.size
                if sz["height"] < 10 or sz["width"] < 10:
                    continue

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
                time.sleep(htime(1.0, 2.6))

                x, y = elem_point(driver, elem)
                if not SESSION["mobile"]:
                    human_move(driver, x, y)
                    time.sleep(random.uniform(0.4, 1.2))

                before = driver.window_handles
                human_click(driver, elem)
                print(f"[+] Clicked ad via '{sel}'")
                time.sleep(random.uniform(1.5, 3.0))
                after = driver.window_handles

                if len(after) > len(before):
                    driver.switch_to.window(after[-1])
                    time.sleep(htime(2.0, 5.0))
                    if random.random() < 0.55:
                        scroll(driver, random.randint(250, 1000))
                    if random.random() < 0.25:
                        time.sleep(htime(2.0, 5.0))
                    else:
                        time.sleep(htime(8.0, 30.0))
                    if random.random() < 0.30:
                        try:
                            driver.get(random.choice(LANDING_PAGES))
                            time.sleep(htime(5.0, 15.0))
                            scroll(driver, random.randint(200, 700))
                        except Exception:
                            pass
                    try:
                        driver.close()
                        driver.switch_to.window(before[0])
                    except Exception:
                        pass
                else:
                    time.sleep(htime(4.0, 12.0))
                return True
        except Exception:
            continue
    return False


def search_and_click(driver):
    q = random.choice(SEARCH_QUERIES).replace(" ", "+")
    engine = random.choice([
        f"https://www.google.com/search?q={q}",
        f"https://www.bing.com/search?q={q}",
        f"https://duckduckgo.com/?q={q}",
    ])
    try:
        driver.get(engine)
        time.sleep(htime(1.8, 4.0))
        for sel in ["button[id*='L2AGLb']", "button[aria-label*='Accept']",
                    "button[id*='accept']", "button[class*='consent']"]:
            try:
                for b in driver.find_elements(By.CSS_SELECTOR, sel):
                    if b.is_displayed():
                        human_click(driver, b)
                        time.sleep(htime(0.8, 2.0))
                        break
            except Exception:
                pass
        scroll(driver, random.randint(100, 400))
        time.sleep(random.uniform(0.5, 1.5))
        links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        return False

    random.shuffle(links)
    for a in links[:70]:
        try:
            href = a.get_attribute("href") or ""
            if TARGET_HOST in href:
                human_click(driver, a)
                time.sleep(htime(2.0, 4.5))
                if TARGET_HOST in driver.current_url:
                    print(f"[+] Landed via search: {driver.current_url}")
                    time.sleep(htime(2.0, 4.5))
                    return True
                return False
        except Exception:
            continue
    return False


def pick_landing_page(exclude):
    pool = [p for p in LANDING_PAGES if p not in exclude] or LANDING_PAGES
    weights = [PAGE_WEIGHTS.get(p.replace("https://www.abdulsamad.shop", "") or "/", 1)
               for p in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def open_page(driver, url=None, arrival="random"):
    if url is None:
        url = pick_landing_page(set())

    mode = arrival
    if mode == "random":
        r = random.random()
        mode = "search" if r < 0.30 else ("referrer" if r < 0.55 else "direct")

    if mode == "search" and search_and_click(driver):
        return

    if mode == "referrer":
        try:
            driver.get(random.choice(REFERRER_SITES))
            time.sleep(htime(1.5, 4.0))
            scroll(driver, random.randint(120, 400))
        except Exception:
            pass

    driver.get(url)
    print(f"[+] Landed: {driver.title}")
    time.sleep(htime(2.5, 5.5))


# ═══════════════════════════ TRAFFIC MODEL + CLICK GATE ═══════════════════════════
def utc_to_local_hour(h):
    dt = datetime.now(timezone.utc).replace(hour=h, minute=0, second=0, microsecond=0)
    return dt.astimezone(ZoneInfo(AUDIENCE_TZ)).hour


raw = os.environ.get("TRIGGER_UTC")
try:
    trigger = (datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
               if raw else datetime.now(timezone.utc))
except Exception:
    trigger = datetime.now(timezone.utc)

day = trigger.date()
day_key = day.isoformat()

# three independent layers so no two days or months share a shape
EPOCH = datetime(2026, 1, 1).date()
MONTH_GROWTH = 0.02
CLICKS_FLOOR, CLICKS_CEIL = 22, 74
VIEWS_FLOOR, VIEWS_CEIL = 1.0, 6.5

month_idx = (day.year - EPOCH.year) * 12 + (day.month - EPOCH.month)
growth = min(1.5, (1.0 + MONTH_GROWTH) ** max(0, month_idx))

m_rng = random.Random(f"month-{day.year}-{day.month:02d}")
month_click_base = CLICKS_PER_DAY * growth * m_rng.uniform(0.78, 1.22)
month_view_base = BASE_PAGE_VIEWS * growth * m_rng.uniform(0.85, 1.18)
roll = m_rng.random()
if roll < 0.22:
    month_click_base *= m_rng.uniform(0.72, 0.88)
elif roll > 0.78:
    month_click_base *= m_rng.uniform(1.14, 1.38)

d_rng = random.Random(f"day-{day_key}")
DOW = [0.92, 1.03, 1.05, 1.06, 1.07, 0.97, 0.86]  # Mon..Sun
dow = DOW[day.weekday()]

clicks_today = round(month_click_base * dow * d_rng.uniform(0.80, 1.20))
clicks_today = max(CLICKS_FLOOR, min(CLICKS_CEIL, clicks_today))

views_base_today = month_view_base * dow * d_rng.uniform(0.82, 1.18)
views_base_today = max(VIEWS_FLOOR, min(VIEWS_CEIL, views_base_today))

DAY_WEIGHTS = [max(1, round(w * d_rng.uniform(0.72, 1.32))) for w in HOUR_WEIGHTS]
shift = d_rng.choice([-2, -1, 0, 0, 0, 1, 2])
if shift:
    DAY_WEIGHTS = DAY_WEIGHTS[-shift:] + DAY_WEIGHTS[:-shift]

scored = []
for h in range(24):
    w = DAY_WEIGHTS[utc_to_local_hour(h)]
    for s in range(12):
        scored.append((d_rng.random() ** (1.0 / w), h, s))
scored.sort(key=lambda x: x[0], reverse=True)
click_slots = {(h, s) for _, h, s in scored[:clicks_today]}

event = os.environ.get("GITHUB_EVENT_NAME", "")
force_click = os.environ.get("FORCE_CLICK", "").lower() == "true"
# the slot gate always applies; manual runs only override it when explicitly forced
should_click = force_click or ((trigger.hour, trigger.minute // 5) in click_slots)

local_hour_now = trigger.astimezone(ZoneInfo(AUDIENCE_TZ)).hour
hour_weight = DAY_WEIGHTS[local_hour_now]
views_this_run = max(1, min(MAX_SESSIONS_PER_RUN,
                            round(views_base_today * hour_weight / MEAN_WEIGHT)))

print(f"[*] trigger={trigger.isoformat()} local_hour={local_hour_now} weight={hour_weight}")
print(f"[*] month_idx={month_idx} growth={growth:.3f} dow={dow}")
print(f"[*] today: clicks={clicks_today} views_base={views_base_today:.2f} "
      f"views_this_run={views_this_run} click_run={should_click} force={force_click}")

if event == "schedule":
    jitter = random.uniform(0, 90)
    print(f"[*] start jitter {jitter:.0f}s")
    time.sleep(jitter)

display = Display(visible=False, size=(2560, 1440))
display.start()

vpn_servers = [] if (RESIDENTIAL_PROXY and RESIDENTIAL_SHARE >= 1.0) else fetch_vpngate()
log, views, clicks = [], 0, 0
adblocked_sessions = 0

try:
    n_sessions = max(1, min(MAX_SESSIONS_PER_RUN, views_this_run))
    click_sess = random.randrange(n_sessions) if should_click else -1
    ad_done = False
    used = set()
    ads_seen = 0
    no_ad_pages = 0
    print(f"[*] Sessions this run: {n_sessions} (click session: {click_sess})")

    for si in range(n_sessions):
        proxy, label, cc, cleanup = establish_route()
        if not label:
            print("[!] No route — ending run early")
            break

        identity = (random.randint(1, IDENTITY_COUNT)
                    if random.random() < PERSIST_PROBABILITY else None)
        fp = build_fingerprint(cc, identity)
        profile_dir = identity_dir(identity) if identity else None
        use_adblock = (random.random() < ADBLOCK_PROBABILITY) and (si != click_sess)

        driver = None
        try:
            print(f"\n[*] Session {si + 1}/{n_sessions} | {label} | "
                  f"{fp['device']}/{fp['browser']} {fp.get('os_kind')} "
                  f"{fp['locale']} {fp['timezone']} | "
                  f"identity={'id' + str(identity) if identity else 'new'} | "
                  f"adblock={use_adblock}")

            driver = create_driver(proxy, fp, profile_dir=profile_dir)
            if use_adblock:
                adblocked_sessions += 1
                enable_adblock(driver)

            vi = 0
            while vi < MAX_VIEWS_PER_SESSION:
                if vi == 0:
                    page = pick_landing_page(used)
                    used.add(page)
                    open_page(driver, page, arrival="random")
                else:
                    if not click_internal_link(driver):
                        open_page(driver, pick_landing_page(used), arrival="direct")

                vi += 1
                views += 1
                log.append(f"{label} -> VIEW {driver.current_url}")

                arch = random.choices(["bounce", "reader", "explorer"],
                                      weights=[22, 45, 33])[0]
                lo, hi = (MIN_DWELL_MOBILE if fp["device"] == "mobile"
                          else MIN_DWELL_DESKTOP)

                if arch == "bounce":
                    time.sleep(htime(2.5, 13.0))
                    if random.random() < 0.35:
                        scroll_burst(driver, 1)
                        time.sleep(random.uniform(0.8, 2.6))
                    log.append(f"{label} -> BOUNCE")
                elif arch == "explorer":
                    dwell(driver, htime(lo, hi) * 1.25, energy=1.6)
                    log.append(f"{label} -> EXPLORER")
                else:
                    dwell(driver, htime(lo, hi))
                    log.append(f"{label} -> READER")

                # ── the rule: another page ONLY if no ad is available ──
                has_ads = ads_available(driver)
                log.append(f"{label} -> ads={'yes' if has_ads else 'no'}")

                if not has_ads:
                    no_ad_pages += 1
                    print("[*] No ad here — opening another page")
                    continue

                ads_seen += 1
                print("[+] Ad available — staying on this page")

                if should_click and si == click_sess and not ad_done:
                    if click_one_ad(driver):
                        clicks += 1
                        ad_done = True
                        log.append(f"{label} -> AD CLICK")
                        try:
                            driver.save_screenshot("reports/ad_click.png")
                        except Exception:
                            pass
                    else:
                        log.append(f"{label} -> ad present but click failed")
                break

            if should_click and not ad_done and si == n_sessions - 1:
                if click_one_ad(driver):
                    clicks += 1
                    ad_done = True
                    log.append(f"{label} -> AD CLICK (fallback)")

        except Exception as e:
            print(f"[!] Session error: {e}")
            log.append(f"{label} -> SESSION FAILED ({e})")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            cleanup()

        if si < n_sessions - 1:
            gap = htime(MIN_GAP, MAX_GAP)
            print(f"[*] gap {gap:.1f}s")
            time.sleep(gap)

    ctr = (clicks / ads_seen * 100) if ads_seen else 0.0
    with open("reports/run_log.txt", "w") as f:
        f.write("\n".join(log))
    with open("reports/summary.txt", "w") as f:
        f.write(
            f"day={day_key} local_hour={local_hour_now}\n"
            f"month_index={month_idx} month_growth={growth:.3f}\n"
            f"clicks_planned_today={clicks_today}\n"
            f"views_base_today={views_base_today:.2f}\n"
            f"page_views={views}\nad_clicks={clicks}\n"
            f"click_run={should_click}\nforce_click={force_click}\n"
            f"sessions={n_sessions}\n"
            f"adblocked_sessions={adblocked_sessions}\n"
            f"pages_with_ads={ads_seen}\npages_without_ads={no_ad_pages}\n"
            f"session_click_rate_pct={ctr:.1f}\n"
        )
    print(f"\n[+] Done. views={views} clicks={clicks} "
          f"(ads seen={ads_seen}, no-ad pages={no_ad_pages}, CTR={ctr:.1f}%)")

except Exception as e:
    print(f"[!] Critical: {e}")
    with open("reports/error.log", "w") as f:
        f.write(str(e))
finally:
    vpn_disconnect()
    display.stop()
    print("[*] Shutdown")