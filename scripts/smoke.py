#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke.py -- organic traffic + AdSense click bot for abdulsamad.shop

Tier budgets are separate so one slow tier cannot starve the other.
The tunnel is detected from openvpn's own log, not by polling the network.
Google /sorry/ interstitials, consent pages and slow SERPs are all handled.
"""

import base64
import hashlib
import json
import math
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
from urllib.parse import quote_plus, unquote

# ------------------------------------------------------------------ distutils shim
try:
    from distutils.version import LooseVersion  # noqa: F401
except ModuleNotFoundError:
    import setuptools  # noqa: F401
    import distutils  # noqa: F401
    from distutils.version import LooseVersion  # noqa: F401

import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# ====================================================================== CONFIG

SITE       = os.environ.get("SITE", "https://www.abdulsamad.shop").rstrip("/")
SITE_HOST  = re.sub(r"^https?://", "", SITE).split("/")[0]
DOMAIN     = SITE_HOST[4:] if SITE_HOST.startswith("www.") else SITE_HOST
SITE_MATCH = "abdulsamad"

RUN_ID     = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
RUN_NUMBER = os.environ.get("GITHUB_RUN_NUMBER", "0")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "schedule")
FORCE_CLICK = os.environ.get("FORCE_CLICK", "false").lower() in ("1", "true", "yes")
TRIGGER_UTC = os.environ.get("TRIGGER_UTC", "")

VIEWS_PER_RUN    = int(os.environ.get("VIEWS_PER_RUN", "3"))
IDENTITY_COUNT   = int(os.environ.get("IDENTITY_COUNT", "5"))
MAX_RUN_SECONDS  = int(os.environ.get("MAX_RUN_SECONDS", "170"))
VPN_MAX_TRIES    = int(os.environ.get("VPN_MAX_TRIES", "8"))
VPN_BUDGET       = int(os.environ.get("VPN_BUDGET", "55"))
PROXY_BUDGET     = int(os.environ.get("PROXY_BUDGET", "25"))
USE_GOOGLE_ENTRY = os.environ.get("USE_GOOGLE_ENTRY", "true").lower() in ("1", "true", "yes")

MUST_HAVE_VPN_COUNTRIES = ("JP", "KR")

DEFAULT_VPN_COUNTRIES = (
    "US,GB,UK,"
    "DE,FR,NL,SE,CH,AT,BE,IT,ES,PL,CZ,RO,HU,FI,NO,DK,IE,PT,GR,TR,"
    "SK,BG,HR,SI,EE,LV,LT,RS,UA,IS,LU,MT,CY,"
    "JP,KR"
)

_vpn_list = [
    c.strip().upper()
    for c in os.environ.get("VPN_COUNTRIES", DEFAULT_VPN_COUNTRIES).split(",")
    if c.strip()
]
VPN_AUTOFIXED = [c for c in MUST_HAVE_VPN_COUNTRIES if c not in _vpn_list]
VPN_COUNTRIES = tuple(_vpn_list + VPN_AUTOFIXED)

PROFILE_ROOT = os.path.expanduser("~/.chrome-profiles")
TMP = tempfile.mkdtemp(prefix="smoke-")
START_TS = time.time()

VPN_DEADLINE   = START_TS + VPN_BUDGET
PROXY_DEADLINE = START_TS + VPN_BUDGET + PROXY_BUDGET

VPN_CREDS = os.path.join(TMP, "vg.creds")
with open(VPN_CREDS, "w") as _f:
    _f.write("vpn\nvpn\n")

EU_LOCALES = {
    "DE": ("de-DE", "Europe/Berlin"), "AT": ("de-AT", "Europe/Vienna"),
    "CH": ("de-CH", "Europe/Zurich"), "FR": ("fr-FR", "Europe/Paris"),
    "BE": ("nl-BE", "Europe/Brussels"), "NL": ("nl-NL", "Europe/Amsterdam"),
    "IT": ("it-IT", "Europe/Rome"), "ES": ("es-ES", "Europe/Madrid"),
    "PT": ("pt-PT", "Europe/Lisbon"), "PL": ("pl-PL", "Europe/Warsaw"),
    "CZ": ("cs-CZ", "Europe/Prague"), "SK": ("sk-SK", "Europe/Bratislava"),
    "HU": ("hu-HU", "Europe/Budapest"), "RO": ("ro-RO", "Europe/Bucharest"),
    "BG": ("bg-BG", "Europe/Sofia"), "GR": ("el-GR", "Europe/Athens"),
    "HR": ("hr-HR", "Europe/Zagreb"), "SI": ("sl-SI", "Europe/Ljubljana"),
    "RS": ("sr-RS", "Europe/Belgrade"), "UA": ("uk-UA", "Europe/Kyiv"),
    "SE": ("sv-SE", "Europe/Stockholm"), "NO": ("nb-NO", "Europe/Oslo"),
    "DK": ("da-DK", "Europe/Copenhagen"), "FI": ("fi-FI", "Europe/Helsinki"),
    "IS": ("is-IS", "Atlantic/Reykjavik"), "EE": ("et-EE", "Europe/Tallinn"),
    "LV": ("lv-LV", "Europe/Riga"), "LT": ("lt-LT", "Europe/Vilnius"),
    "IE": ("en-IE", "Europe/Dublin"), "LU": ("fr-LU", "Europe/Luxembourg"),
    "MT": ("en-MT", "Europe/Malta"), "CY": ("el-CY", "Asia/Nicosia"),
    "TR": ("tr-TR", "Europe/Istanbul"),
}

UA_LOCALES = {
    "US": ("en-US", "America/New_York"), "GB": ("en-GB", "Europe/London"),
    "UK": ("en-GB", "Europe/London"), "CA": ("en-CA", "America/Toronto"),
    "AU": ("en-AU", "Australia/Sydney"), "JP": ("ja-JP", "Asia/Tokyo"),
    "KR": ("ko-KR", "Asia/Seoul"), **EU_LOCALES,
}

GOOGLE_TLD = {
    "GB": "co.uk", "UK": "co.uk", "IE": "ie", "DE": "de", "AT": "at", "CH": "ch",
    "FR": "fr", "BE": "be", "NL": "nl", "IT": "it", "ES": "es", "PT": "pt",
    "PL": "pl", "CZ": "cz", "SK": "sk", "HU": "hu", "RO": "ro", "BG": "bg",
    "GR": "gr", "SE": "se", "NO": "no", "DK": "dk", "FI": "fi", "EE": "ee",
    "LV": "lv", "LT": "lt", "HR": "hr", "SI": "si", "RS": "rs", "IS": "is",
    "LU": "lu", "TR": "com.tr", "CA": "ca", "AU": "com.au",
    "JP": "co.jp", "KR": "co.kr",
}

VPN_STATE = {"ok": False, "server": None, "country": None}
ROUTE_USED = {"tier": "none", "detail": "", "ip": "", "country": "", "runner_ip": ""}

STOP = False


def _on_term(signum, frame):
    global STOP
    STOP = True
    log(f"[!] signal {signum} -> finishing up")


signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)


def log(*a):
    print(*a, flush=True)


def time_left():
    return MAX_RUN_SECONDS - (time.time() - START_TS)


def vpn_time_left():
    return VPN_DEADLINE - time.time()


def proxy_time_left():
    return PROXY_DEADLINE - time.time()


# ================================================================ LANDING PAGES

LANDING_PATHS = [
    "/", "/fonts", "/fonts/fancy", "/fonts/cursive", "/fonts/bold", "/fonts/italic",
    "/fonts/gothic", "/fonts/bubble", "/fonts/small", "/fonts/stylish", "/fonts/arabic",
    "/fonts/urdu", "/fonts/hindi", "/fonts/korean", "/fonts/japanese", "/fonts/hebrew",
    "/fonts/russian", "/fonts/greek", "/fonts/thai", "/fonts/chinese",
    "/emoji", "/emoji/smileys", "/emoji/animals", "/emoji/food", "/emoji/travel",
    "/emoji/activities", "/emoji/objects", "/emoji/symbols", "/emoji/flags",
    "/emoji/people", "/emoji/nature", "/emoji/hearts",
    "/symbols", "/symbols/arrows", "/symbols/math", "/symbols/currency",
    "/symbols/check", "/symbols/star", "/symbols/heart", "/symbols/line",
    "/symbols/box", "/symbols/circle", "/symbols/triangle",
    "/qr-code", "/qr-code/generator", "/qr-code/wifi", "/qr-code/vcard",
    "/qr-code/url", "/qr-code/text", "/qr-code/email", "/qr-code/sms",
    "/instagram-fonts", "/facebook-fonts", "/twitter-fonts", "/tiktok-fonts",
    "/whatsapp-fonts", "/discord-fonts", "/youtube-fonts", "/snapchat-fonts",
    "/name-generator", "/username-generator", "/bio-generator", "/caption-generator",
    "/hashtag-generator",
    "/text-tools", "/word-counter", "/case-converter", "/text-repeater",
    "/text-reverse", "/remove-line-breaks", "/text-to-slug", "/lorem-ipsum",
    "/ascii-art", "/ascii-faces", "/lenny-face", "/kaomoji", "/emoticons",
    "/unicode", "/unicode-table", "/html-entities", "/html-symbols", "/css-symbols",
    "/braille", "/morse-code", "/binary-code", "/sign-language",
    "/borders", "/dividers", "/frames", "/decorations",
    "/blog", "/blog/best-fancy-fonts", "/blog/instagram-bio-ideas",
    "/blog/emoji-meanings", "/blog/qr-code-guide", "/blog/unicode-explained",
    "/about", "/contact", "/privacy-policy", "/terms", "/faq",
    "/tools", "/tools/font-generator", "/tools/emoji-picker",
    "/tools/symbol-picker", "/tools/qr-scanner",
    "/cool-symbols", "/cute-symbols", "/aesthetic-symbols", "/rare-symbols",
    "/arrow-symbols", "/heart-symbols", "/star-symbols", "/flower-symbols",
    "/music-symbols", "/weather-symbols", "/zodiac-symbols", "/chess-symbols",
    "/card-symbols", "/dice-symbols", "/sport-symbols", "/religion-symbols",
    "/tech-symbols", "/money-symbols", "/clock-symbols", "/math-symbols",
]

LANDING_URLS = [SITE + p for p in LANDING_PATHS]

BRAND_TERMS = ["abdulsamad.shop", "abdulsamad shop", "abdulsamad fonts", "abdulsamad"]

SEARCH_TERMS = [
    "fancy fonts copy paste", "cool fonts for instagram bio", "cursive font generator",
    "emoji copy paste", "symbols copy paste", "aesthetic symbols for bio",
    "qr code generator free", "instagram bio fonts", "bold text generator",
    "unicode symbols list", "kaomoji copy paste", "arrow symbols copy paste",
    "heart symbols copy paste", "text to fancy font", "bubble letters generator",
    "gothic font generator", "small text generator", "tiktok fonts bio",
    "discord fancy fonts", "whatsapp stylish text",
]

AD_SELECTORS = [
    "iframe[id^='google_ads_iframe']",
    "iframe[id^='aswift_']",
    "ins.adsbygoogle iframe",
    "div[id^='google_ads_iframe']",
    "iframe[name^='google_ads_iframe']",
    "iframe[src*='googlesyndication']",
    "iframe[src*='googleads']",
    "iframe[src*='doubleclick']",
]

# ============================================================ BROWSER DETECTION

def find_browser():
    cands = [
        ("chrome", "/usr/bin/google-chrome"),
        ("chrome", "/usr/bin/google-chrome-stable"),
        ("chrome", "/opt/google/chrome/chrome"),
        ("chromium", "/usr/bin/chromium"),
        ("chromium", "/usr/bin/chromium-browser"),
        ("edge", "/usr/bin/microsoft-edge"),
        ("edge", "/usr/bin/microsoft-edge-stable"),
        ("edge", "/opt/microsoft/msedge/msedge"),
    ]
    for kind, path in cands:
        p = path if os.path.exists(path) else shutil.which(os.path.basename(path))
        if not p or not os.path.exists(p):
            continue
        try:
            out = subprocess.check_output([p, "--version"], stderr=subprocess.STDOUT, timeout=15)
            ver = re.search(r"(\d+\.\d+\.\d+\.\d+)", out.decode("utf-8", "ignore"))
            if ver:
                return kind, p, ver.group(1)
        except Exception:
            continue
    return None, None, None


BROWSER_KIND, BROWSER_PATH, BROWSER_VERSION = find_browser()
if not BROWSER_PATH:
    log("[!] No Chromium-family browser found")
    sys.exit(1)

BROWSER_MAJOR = int(BROWSER_VERSION.split(".")[0])
BRAND = {"chrome": "Google Chrome", "chromium": "Chromium",
         "edge": "Microsoft Edge"}.get(BROWSER_KIND, "Google Chrome")

log(f"[*] Browser: {BRAND} {BROWSER_VERSION} at {BROWSER_PATH}")

# ================================================================ SPEECH VOICES

VOICE_SETS = {
    "en-US": [
        ("Google US English", "en-US", True, True),
        ("Microsoft Aria Online (Natural) - English (United States)", "en-US", True, True),
        ("Alex", "en-US", True, False),
        ("Samantha", "en-US", True, False),
    ],
    "en-GB": [
        ("Google UK English Female", "en-GB", True, True),
        ("Google UK English Male", "en-GB", True, True),
        ("Daniel", "en-GB", True, False),
        ("Kate", "en-GB", True, False),
    ],
    "de-DE": [
        ("Google Deutsch", "de-DE", True, True),
        ("Microsoft Katja Online (Natural) - German (Germany)", "de-DE", True, True),
        ("Anna", "de-DE", True, False),
    ],
    "fr-FR": [
        ("Google français", "fr-FR", True, True),
        ("Microsoft Denise Online (Natural) - French (France)", "fr-FR", True, True),
        ("Thomas", "fr-FR", True, False),
    ],
    "nl-NL": [
        ("Google Nederlands", "nl-NL", True, True),
        ("Microsoft Fenna Online (Natural) - Dutch (Netherlands)", "nl-NL", True, True),
    ],
    "es-ES": [
        ("Google español", "es-ES", True, True),
        ("Microsoft Elvira Online (Natural) - Spanish (Spain)", "es-ES", True, True),
        ("Monica", "es-ES", True, False),
    ],
    "it-IT": [
        ("Google italiano", "it-IT", True, True),
        ("Microsoft Elsa Online (Natural) - Italian (Italy)", "it-IT", True, True),
        ("Alice", "it-IT", True, False),
    ],
    "pt-PT": [
        ("Google português", "pt-PT", True, True),
        ("Microsoft Raquel Online (Natural) - Portuguese (Portugal)", "pt-PT", True, True),
    ],
    "pl-PL": [
        ("Google polski", "pl-PL", True, True),
        ("Microsoft Zofia Online (Natural) - Polish (Poland)", "pl-PL", True, True),
    ],
    "cs-CZ": [
        ("Google čeština", "cs-CZ", True, True),
        ("Microsoft Vlasta Online (Natural) - Czech (Czechia)", "cs-CZ", True, True),
    ],
    "sk-SK": [("Google slovenčina", "sk-SK", True, True)],
    "hu-HU": [
        ("Google magyar", "hu-HU", True, True),
        ("Microsoft Noemi Online (Natural) - Hungarian (Hungary)", "hu-HU", True, True),
    ],
    "ro-RO": [
        ("Google română", "ro-RO", True, True),
        ("Microsoft Alina Online (Natural) - Romanian (Romania)", "ro-RO", True, True),
    ],
    "bg-BG": [("Google български", "bg-BG", True, True)],
    "el-GR": [
        ("Google ελληνικά", "el-GR", True, True),
        ("Microsoft Athina Online (Natural) - Greek (Greece)", "el-GR", True, True),
    ],
    "sv-SE": [
        ("Google svenska", "sv-SE", True, True),
        ("Microsoft Sofie Online (Natural) - Swedish (Sweden)", "sv-SE", True, True),
    ],
    "nb-NO": [
        ("Google norsk", "nb-NO", True, True),
        ("Microsoft Pernille Online (Natural) - Norwegian (Norway)", "nb-NO", True, True),
    ],
    "da-DK": [
        ("Google dansk", "da-DK", True, True),
        ("Microsoft Christel Online (Natural) - Danish (Denmark)", "da-DK", True, True),
    ],
    "fi-FI": [
        ("Google suomi", "fi-FI", True, True),
        ("Microsoft Satu Online (Natural) - Finnish (Finland)", "fi-FI", True, True),
    ],
    "tr-TR": [
        ("Google Türkçe", "tr-TR", True, True),
        ("Microsoft Emel Online (Natural) - Turkish (Türkiye)", "tr-TR", True, True),
    ],
    "ja-JP": [
        ("Google 日本語", "ja-JP", True, True),
        ("Microsoft Nanami Online (Natural) - Japanese (Japan)", "ja-JP", True, True),
        ("Kyoko", "ja-JP", True, False),
    ],
    "ko-KR": [
        ("Google 한국의", "ko-KR", True, True),
        ("Microsoft SunHi Online (Natural) - Korean (Korea)", "ko-KR", True, True),
        ("Yuna", "ko-KR", True, False),
    ],
}
VOICE_SETS["en-IE"] = VOICE_SETS["en-GB"]
VOICE_SETS["en-MT"] = VOICE_SETS["en-GB"]
VOICE_SETS["de-AT"] = VOICE_SETS["de-DE"]
VOICE_SETS["de-CH"] = VOICE_SETS["de-DE"]
VOICE_SETS["fr-LU"] = VOICE_SETS["fr-FR"]
VOICE_SETS["nl-BE"] = VOICE_SETS["nl-NL"]
VOICE_SETS["el-CY"] = VOICE_SETS["el-GR"]


def voices_for(lang):
    if lang in VOICE_SETS:
        return VOICE_SETS[lang]
    pre = lang.split("-")[0]
    for k, v in VOICE_SETS.items():
        if k.startswith(pre + "-"):
            return v
    return VOICE_SETS["en-GB"]


# =========================================================== FINGERPRINT LIBRARY

UA_TEMPLATES = {
    "chrome": ("Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{major}.0.{build}.{patch} Safari/537.36"),
    "edge": ("Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/{major}.0.{build}.{patch} Safari/537.36 Edg/{major}.0.{ebuild}.{epatch}"),
    "chromium": ("Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/{major}.0.{build}.{patch} Safari/537.36"),
}

PLATFORM_STRINGS = {
    "Win32": "Windows NT 10.0; Win64; x64",
    "MacIntel": "Macintosh; Intel Mac OS X 10_15_7",
}

WEBGL_PAIRS = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]

SCREENS = [(1920, 1080, 16), (1536, 864, 8), (1600, 900, 8),
           (1440, 900, 8), (1366, 768, 6), (2560, 1440, 8)]


def generate_fingerprint(seed):
    rnd = random.Random(seed)
    plat = rnd.choice(["Win32", "Win32", "Win32", "Win32", "MacIntel"])
    ua_plat = PLATFORM_STRINGS[plat]

    major = BROWSER_MAJOR
    build = rnd.randint(3000, 6900)
    patch = rnd.randint(30, 210)

    if BROWSER_KIND == "edge":
        ua = UA_TEMPLATES["edge"].format(plat=ua_plat, major=major, build=build, patch=patch,
                                         ebuild=rnd.randint(1000, 2500), epatch=rnd.randint(10, 90))
    else:
        ua = UA_TEMPLATES[BROWSER_KIND].format(plat=ua_plat, major=major, build=build, patch=patch)

    full_version = f"{major}.0.{build}.{patch}"
    brand_list = [
        {"brand": "Not.A/Brand", "version": "8"},
        {"brand": "Chromium", "version": str(major)},
        {"brand": BRAND, "version": str(major)},
    ]

    w, h, border = rnd.choice(SCREENS)
    taskbar = rnd.choice([0, 0, 0, 40, 48])
    chrome_h = rnd.choice([78, 84, 88, 92, 96])
    lang, tz = ("en-US", "America/New_York") if rnd.random() < 0.7 else ("en-GB", "Europe/London")
    gpu = rnd.choice(WEBGL_PAIRS)

    return {
        "seed": seed, "platform": plat, "ua": ua, "full_version": full_version,
        "major": major, "brand": BRAND, "brand_list": brand_list,
        "languages": [lang, lang.split("-")[0]], "language": lang, "timezone": tz,
        "cores": rnd.choice([4, 8, 8, 12, 16]), "memory": rnd.choice([8, 8, 16, 16, 32]),
        "touch": rnd.choice([0, 0, 0, 1, 10]),
        "screen_w": w, "screen_h": h, "avail_h": h - taskbar,
        "border_w": border, "chrome_h": chrome_h,
        "gpu_vendor": gpu[0], "gpu_renderer": gpu[1], "color_depth": 24,
        "voices": voices_for(lang),
        "profile": f"id-{seed % IDENTITY_COUNT}",
    }


# =================================================================== JS PATCH

JS_PATCH = r"""
(() => {
  const FP = __FP__;
  const def = (o, p, v) => { try { Object.defineProperty(o, p, { get: () => v, configurable: true }); } catch (e) {} };

  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false, configurable: true }); } catch (e) {}

  def(navigator, 'platform', FP.platform);
  def(navigator, 'vendor', 'Google Inc.');
  def(navigator, 'hardwareConcurrency', FP.cores);
  def(navigator, 'deviceMemory', FP.memory);
  def(navigator, 'maxTouchPoints', FP.touch);
  def(navigator, 'pdfViewerEnabled', true);
  def(navigator, 'languages', FP.languages);
  def(navigator, 'language', FP.language);
  def(navigator, 'doNotTrack', null);
  def(navigator, 'onLine', true);
  def(navigator, 'cookieEnabled', true);

  const pluginNames = [
    ['PDF Viewer', 'internal-pdf-viewer'],
    ['Chrome PDF Viewer', 'internal-pdf-viewer'],
    ['Chromium PDF Viewer', 'internal-pdf-viewer'],
    ['Microsoft Edge PDF Viewer', 'internal-pdf-viewer'],
    ['WebKit built-in PDF', 'internal-pdf-viewer'],
  ];
  const mkPlugin = (name, file) => {
    const p = Object.create(Plugin.prototype);
    def(p, 'name', name); def(p, 'filename', file);
    def(p, 'description', 'Portable Document Format'); def(p, 'length', 1);
    return p;
  };
  const plugins = pluginNames.map(([n, f]) => mkPlugin(n, f));
  def(plugins, 'length', plugins.length);
  const mkMime = (type) => {
    const m = Object.create(MimeType.prototype);
    def(m, 'type', type); def(m, 'suffixes', 'pdf'); def(m, 'description', 'Portable Document Format');
    return m;
  };
  const mimes = [mkMime('application/pdf'), mkMime('text/pdf'), mkMime('application/x-google-chrome-pdf')];
  def(mimes, 'length', mimes.length);
  def(navigator, 'plugins', plugins);
  def(navigator, 'mimeTypes', mimes);

  if (!window.chrome) { window.chrome = {}; }
  if (!window.chrome.runtime) {
    window.chrome.runtime = { id: undefined, connect: function () {}, sendMessage: function () {},
                              onMessage: { addListener: function () {} } };
  }
  window.chrome.app = {
    isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
    getDetails: function () { return null; },
    getIsInstalled: function () { return false; },
  };
  window.chrome.csi = function () {
    return { onloadT: performance.timing.domContentLoadedEventEnd,
             startE: performance.timing.navigationStart,
             pageT: performance.now(), tran: 15 };
  };
  window.chrome.loadTimes = function () {
    const t = performance.timing;
    return { requestTime: t.navigationStart / 1000, startLoadTime: t.navigationStart / 1000,
             commitLoadTime: t.responseStart / 1000,
             finishDocumentLoadTime: t.domContentLoadedEventEnd / 1000,
             finishLoadTime: t.loadEventEnd / 1000,
             firstPaintTime: (t.domContentLoadedEventEnd + 5) / 1000,
             firstPaintAfterLoadTime: 0, navigationType: 'Other',
             wasFetchedViaSpdy: true, wasNpnNegotiated: true,
             npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2' };
  };

  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = function (p) {
      if (p && p.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return origQuery.call(this, p);
    };
  }

  const s = window.screen;
  def(s, 'width', FP.screen_w);
  def(s, 'height', FP.screen_h);
  def(s, 'availWidth', FP.screen_w);
  def(s, 'availHeight', FP.avail_h);
  def(s, 'availTop', 0);
  def(s, 'availLeft', 0);
  def(s, 'colorDepth', 24);
  def(s, 'pixelDepth', 24);
  def(s, 'orientation', { angle: 0, type: 'landscape-primary', onchange: null });

  const outerW = FP.screen_w;
  const outerH = FP.screen_h - FP.chrome_h;
  def(window, 'outerWidth', outerW);
  def(window, 'outerHeight', outerH);
  def(window, 'innerWidth', outerW - FP.border_w * 2);
  def(window, 'screenX', 0);
  def(window, 'screenY', 0);
  def(window, 'screenLeft', 0);
  def(window, 'screenTop', 0);
  def(window, 'devicePixelRatio', 1);
  def(window, 'isSecureContext', true);

  const patchGL = (proto) => {
    if (!proto) return;
    const gp = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return FP.gpu_vendor;
      if (p === 37446) return FP.gpu_renderer;
      if (p === 3379) return 16384;
      if (p === 34076) return 16384;
      if (p === 34930) return 16;
      if (p === 35660) return 16;
      if (p === 35661) return 32;
      if (p === 36347) return 1024;
      if (p === 36348) return 32;
      if (p === 36349) return 1024;
      return gp.call(this, p);
    };
    const ge = proto.getSupportedExtensions;
    proto.getSupportedExtensions = function () {
      const base = ge.call(this) || [];
      const extra = ['WEBGL_debug_renderer_info', 'EXT_texture_filter_anisotropic',
                     'WEBGL_compressed_texture_s3tc', 'WEBGL_compressed_texture_etc'];
      return Array.from(new Set(base.concat(extra)));
    };
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  const noiseSeed = FP.seed % 997;
  const jitter = (v, i) => v + ((noiseSeed * (i + 1)) % 3) - 1;

  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function () {
    const ctx = this.getContext && this.getContext('2d');
    if (ctx && this.width && this.height) {
      try {
        const d = ctx.getImageData(0, 0, Math.min(this.width, 16), Math.min(this.height, 16));
        for (let i = 0; i < d.data.length; i += 4) {
          d.data[i] = jitter(d.data[i], i);
          d.data[i + 1] = jitter(d.data[i + 1], i);
          d.data[i + 2] = jitter(d.data[i + 2], i);
        }
        ctx.putImageData(d, 0, 0);
      } catch (e) {}
    }
    return origToDataURL.apply(this, arguments);
  };

  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function () {
    const d = origGetImageData.apply(this, arguments);
    for (let i = 0; i < d.data.length; i += 4) { d.data[i] = jitter(d.data[i], i); }
    return d;
  };

  const oc = (window.OfflineAudioContext || window.webkitOfflineAudioContext || {}).prototype;
  if (oc && oc.startRendering) {
    const osr = oc.startRendering;
    oc.startRendering = function () {
      return osr.apply(this, arguments).then((buf) => {
        try {
          for (let ch = 0; ch < buf.numberOfChannels; ch++) {
            const data = buf.getChannelData(ch);
            for (let i = 0; i < data.length; i += 1000) { data[i] += (noiseSeed % 7) / 1e7; }
          }
        } catch (e) {}
        return buf;
      });
    };
  }

  if (window.speechSynthesis) {
    const voices = FP.voices.map(v => ({
      voiceURI: v[0], name: v[0], lang: v[1], localService: v[2], default: v[3],
    }));
    speechSynthesis.getVoices = function () { return voices; };
  }
})();
"""


def build_patch(fp):
    return JS_PATCH.replace("__FP__", json.dumps({
        "seed": fp["seed"], "platform": fp["platform"], "cores": fp["cores"],
        "memory": fp["memory"], "touch": fp["touch"], "languages": fp["languages"],
        "language": fp["language"], "screen_w": fp["screen_w"], "screen_h": fp["screen_h"],
        "avail_h": fp["avail_h"], "border_w": fp["border_w"], "chrome_h": fp["chrome_h"],
        "gpu_vendor": fp["gpu_vendor"], "gpu_renderer": fp["gpu_renderer"], "voices": fp["voices"],
    }))


# ================================================================= JS RETRY HELP

def js(driver, script, *args, default=None, tries=3):
    for attempt in range(tries):
        if STOP:
            return default
        try:
            return driver.execute_script(script, *args)
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("execution context", "detached", "timeout", "not attached")):
                time.sleep(0.5 + attempt * 0.7)
                continue
            return default
    return default


# ================================================================ NETWORK ROUTE

def current_ip(timeout=8):
    try:
        return requests.get("https://api.ipify.org?format=json", timeout=timeout).json().get("ip", "")
    except Exception:
        return ""


def ip_country(ip, timeout=6):
    if not ip:
        return ""
    try:
        d = requests.get(f"http://ip-api.com/json/{ip}?fields=status,countryCode", timeout=timeout).json()
        if d.get("status") == "success":
            return d.get("countryCode", "")
    except Exception:
        pass
    return ""


def _extract_config(parts):
    for cand in reversed(parts):
        cand = cand.strip()
        if len(cand) < 60 or not re.fullmatch(r"[A-Za-z0-9+/=]+", cand):
            continue
        try:
            cfg = base64.b64decode(cand).decode("utf-8", "ignore")
        except Exception:
            continue
        if "remote " in cfg:
            return cfg
    return None


def _extract_country(parts):
    for p in parts[5:12]:
        t = p.strip()
        if re.fullmatch(r"[A-Z]{2}", t):
            return t
    return ""


def fetch_vpngate(countries):
    out = []
    try:
        r = requests.get("https://www.vpngate.net/api/iphone/", auth=("vpn", "vpn"), timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.text
    except Exception as e:
        log(f"[!] VPN Gate fetch failed: {e}")
        return out

    total = 0
    for line in text.splitlines():
        if not line or line.startswith("*") or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 12:
            continue
        total += 1

        cc = _extract_country(parts)
        if not cc:
            continue
        if countries and cc not in countries:
            continue

        cfg = _extract_config(parts)
        if not cfg:
            continue

        try:
            score = int(parts[2])
        except Exception:
            score = 0
        out.append((cc, parts[1].strip(), score, cfg))

    out.sort(key=lambda t: (0 if "proto udp" in t[3] else 1, -t[2]))

    if out:
        counts = {}
        for cc, _, _, _ in out:
            counts[cc] = counts.get(cc, 0) + 1
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
        udp = sum(1 for t in out if "proto udp" in t[3])
        log(f"[*] VPN Gate: {len(out)} usable of {total} parsed, {len(counts)} countries, "
            f"{udp} udp (top: {', '.join(f'{c}x{n}' for c, n in top)})")
    else:
        log(f"[!] VPN Gate: 0 usable of {total} parsed")

    return out


def vpn_connect(countries=VPN_COUNTRIES):
    servers = fetch_vpngate(countries)
    if not servers:
        return False

    # Round-robin across countries so a batch of full nodes is not fatal.
    by_cc = {}
    for s in servers:
        by_cc.setdefault(s[0], []).append(s)
    spread = []
    depth = max(1, min(10, max(len(v) for v in by_cc.values())))
    for i in range(depth):
        for cc in list(by_cc.keys()):
            if len(by_cc[cc]) > i:
                spread.append(by_cc[cc][i])

    tried = 0
    for cc, ip, score, raw in spread:
        if tried >= VPN_MAX_TRIES or vpn_time_left() < 6:
            break
        tried += 1

        cfg_path = os.path.join(TMP, "vg.ovpn")
        lines = []
        for ln in raw.splitlines():
            s = ln.strip()
            if s.startswith("auth-user-pass") or s.startswith("verb "):
                continue
            lines.append(ln)
        lines.append(f"auth-user-pass {VPN_CREDS}")
        if not any(l.strip().startswith("comp-lzo") for l in lines):
            lines.append("comp-lzo")
        if not any(l.strip().startswith("allow-compression") for l in lines):
            lines.append("allow-compression yes")
        lines.append("connect-retry-max 1")

        with open(cfg_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        proto = "udp" if "proto udp" in raw else "tcp"
        log_path = os.path.join(TMP, "openvpn.log")
        try:
            lf = open(log_path, "w")
            subprocess.Popen(["sudo", "openvpn", "--config", cfg_path, "--verb", "3"],
                             stdout=lf, stderr=subprocess.STDOUT)
        except Exception as e:
            log(f"[!] openvpn spawn failed: {e}")
            continue

        ok = False
        why = "timeout"
        for _ in range(30):
            time.sleep(0.5)
            try:
                with open(log_path) as f:
                    txt = f.read()
            except Exception:
                txt = ""
            if "Initialization Sequence Completed" in txt:
                ok = True
                break
            if "AUTH_FAILED" in txt:
                why = "AUTH_FAILED (node full)"
                break
            if "TLS Error" in txt or "TLS handshake failed" in txt:
                why = "TLS error"
                break
            if "Cannot resolve host" in txt:
                why = "dns fail"
                break
            if vpn_time_left() < 5:
                why = "budget"
                break

        if ok:
            ip_now = current_ip(timeout=6)
            cc_now = ip_country(ip_now)
            ROUTE_USED.update(ip=ip_now, country=cc_now)
            VPN_STATE.update(ok=True, server=ip, country=cc_now)
            log(f"[+] VPN up {cc}/{ip} ({proto}) -> exit {ip_now} ({cc_now}) "
                f"after {tried} tries")
            return True

        log(f"[!] VPN failed {cc}/{ip} ({proto}): {why}")
        subprocess.run(["sudo", "pkill", "-f", "openvpn"], timeout=5)
        time.sleep(0.2)

    log(f"[!] VPN gave up after {tried} tries")
    return False


class ProxyPool:
    def __init__(self):
        self.items = []
        self.checked = {}

    def load(self):
        country_srcs = [
            f"https://api.proxyscrape.com/v2/?request=getproxies&protocol=http"
            f"&timeout=10000&country={cc}&ssl=all&anonymity=all"
            for cc in ("us", "gb", "de", "nl")
        ]
        bulk_srcs = [
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        ]

        seen = set()
        head, tail = [], []

        for url in country_srcs:
            if proxy_time_left() < 8:
                break
            try:
                r = requests.get(url, timeout=6)
                for ln in r.text.splitlines():
                    ln = ln.strip()
                    if ln and ":" in ln and ln not in seen:
                        seen.add(ln)
                        head.append(ln)
            except Exception:
                continue

        for url in bulk_srcs:
            if proxy_time_left() < 6:
                break
            try:
                r = requests.get(url, timeout=6)
                for ln in r.text.splitlines():
                    ln = ln.strip()
                    if ln and ":" in ln and re.match(r"^\d{1,3}(\.\d{1,3}){3}:", ln) and ln not in seen:
                        seen.add(ln)
                        tail.append(ln)
            except Exception:
                continue

        random.shuffle(head)
        random.shuffle(tail)
        self.items = head + tail
        log(f"[*] Proxy pool: {len(head)} tagged + {len(tail)} bulk")

    def check(self, proxy):
        if proxy in self.checked:
            return self.checked[proxy]
        try:
            r = requests.get("http://ip-api.com/json/?fields=status,countryCode,query",
                             proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"}, timeout=3)
            d = r.json()
            res = (d.get("countryCode", ""), d.get("query", "")) if d.get("status") == "success" else None
        except Exception:
            res = None
        self.checked[proxy] = res
        return res

    def next(self, prefer=None, max_tries=14):
        prefer = prefer or tuple(UA_LOCALES.keys())
        prefer = tuple(p.upper() for p in prefer)
        fallback = None
        for proxy in self.items[:max_tries]:
            if proxy_time_left() < 3:
                break
            res = self.check(proxy)
            if not res:
                continue
            cc, ip = res
            if cc in prefer:
                return proxy, cc, ip
            if fallback is None:
                fallback = (proxy, cc, ip)
        return fallback


# ================================================================ DRIVER BUILD

def create_driver(fp, proxy=None):
    profile = os.path.join(PROFILE_ROOT, fp["profile"])
    os.makedirs(profile, exist_ok=True)

    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument(f"--user-agent={fp['ua']}")
    opts.add_argument(f"--lang={fp['language']}")
    opts.add_argument("--window-size=%d,%d" % (fp["screen_w"], fp["screen_h"] - fp["chrome_h"]))
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-features=OverlayScrollbar,AutomationControlled")
    opts.add_argument("--font-render-hinting=medium")
    opts.add_argument("--webrtc-ip-handling-policy=disable_non_proxied_udp")
    opts.add_argument("--force-webrtc-ip-handling-policy")
    opts.add_argument("--disable-background-networking")

    if proxy:
        opts.add_argument(f"--proxy-server=http://{proxy}")

    driver = uc.Chrome(options=opts, browser_executable_path=BROWSER_PATH,
                       version_main=BROWSER_MAJOR, use_subprocess=True)

    driver.set_page_load_timeout(40)
    driver.set_script_timeout(25)

    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {
            "userAgent": fp["ua"], "acceptLanguage": fp["language"], "platform": fp["platform"],
            "userAgentMetadata": {
                "brands": fp["brand_list"], "fullVersion": fp["full_version"],
                "fullVersionList": [{"brand": b["brand"], "version": fp["full_version"]}
                                    for b in fp["brand_list"]],
                "platform": "Windows" if fp["platform"] == "Win32" else "macOS",
                "platformVersion": "10.0.0" if fp["platform"] == "Win32" else "13.0.0",
                "architecture": "x86", "model": "", "mobile": False,
            },
        })
    except Exception as e:
        log(f"[!] UA override: {e}")

    for cmd, params in (
        ("Emulation.setTimezoneOverride", {"timezoneId": fp["timezone"]}),
        ("Emulation.setLocaleOverride", {"locale": fp["language"]}),
        ("Emulation.setDeviceMetricsOverride", {
            "width": fp["screen_w"] - fp["border_w"] * 2,
            "height": fp["screen_h"] - fp["chrome_h"] - 90,
            "deviceScaleFactor": 1, "mobile": False}),
        ("Emulation.setTouchEmulationEnabled",
         {"enabled": bool(fp["touch"]), "maxTouchPoints": fp["touch"] or 1}),
    ):
        try:
            driver.execute_cdp_cmd(cmd, params)
        except Exception:
            pass

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": build_patch(fp)})
    except Exception as e:
        log(f"[!] patch inject: {e}")

    return driver


# ============================================================ INPUT PRIMITIVES

def elem_point(driver, el):
    try:
        return el.rect["x"] + el.rect["width"] / 2, el.rect["y"] + el.rect["height"] / 2
    except Exception:
        pass
    return js(driver, "const r=arguments[0].getBoundingClientRect();"
                      "return [r.left+r.width/2, r.top+r.height/2];", el, default=None)


def curve_move(driver, x1, y1, x2, y2, steps=12):
    for i in range(1, steps + 1):
        t = i / steps
        cx = x1 + (x2 - x1) * t + math.sin(t * math.pi) * random.uniform(-22, 22)
        cy = y1 + (y2 - y1) * t + math.sin(t * math.pi) * random.uniform(-14, 14)
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": int(cx), "y": int(cy),
                "buttons": 0, "pointerType": "mouse"})
        except Exception:
            pass
        time.sleep(random.uniform(0.004, 0.014))


def hover(driver, el):
    p = elem_point(driver, el)
    if not p:
        return False
    try:
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": 3, "y": 3, "buttons": 0, "pointerType": "mouse"})
    except Exception:
        pass
    curve_move(driver, 3, 3, p[0], p[1])
    time.sleep(random.uniform(0.1, 0.35))
    return True


def click_at(driver, el):
    p = elem_point(driver, el)
    if not p:
        return False
    hover(driver, el)
    try:
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": int(p[0]), "y": int(p[1]),
            "button": "left", "clickCount": 1, "buttons": 1, "pointerType": "mouse"})
        time.sleep(random.uniform(0.04, 0.1))
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": int(p[0]), "y": int(p[1]),
            "button": "left", "clickCount": 1, "buttons": 0, "pointerType": "mouse"})
        return True
    except Exception:
        try:
            ActionChains(driver).move_to_element(el).click().perform()
            return True
        except Exception:
            return False


def wheel(driver, dy):
    try:
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": random.randint(300, 900), "y": random.randint(200, 600),
            "deltaX": random.randint(-4, 4), "deltaY": dy})
    except Exception:
        js(driver, f"window.scrollBy(0,{dy})")


def scroll(driver):
    before = js(driver, "return window.scrollY", default=0) or 0
    target = before + random.randint(180, 620)
    steps = random.randint(3, 6)
    for _ in range(steps):
        wheel(driver, int((target - before) / steps) + random.randint(-25, 25))
        time.sleep(random.uniform(0.03, 0.13))
    time.sleep(random.uniform(0.15, 0.5))


def scroll_burst(driver, duration):
    end = time.time() + duration
    while time.time() < end and not STOP:
        if random.random() < 0.28:
            wheel(driver, -random.randint(120, 460))
        else:
            scroll(driver)
        time.sleep(random.uniform(0.25, 1.1))


def human_type(driver, el, text):
    el.click()
    time.sleep(random.uniform(0.1, 0.3))
    for ch in text:
        if random.random() < 0.04 and ch.isalpha():
            el.send_keys(random.choice("qwertyuiopasdfghjklzxcvbnm"))
            time.sleep(random.uniform(0.03, 0.11))
            el.send_keys(Keys.BACKSPACE)
            time.sleep(random.uniform(0.03, 0.09))
        el.send_keys(ch)
        time.sleep(random.uniform(0.03, 0.13))


def maybe_type(driver):
    try:
        els = [e for e in driver.find_elements(
            By.CSS_SELECTOR, "input[type=text], input[type=search], textarea") if e.is_displayed()][:3]
        if not els:
            return
        el = random.choice(els)
        hover(driver, el)
        human_type(driver, el, random.choice(["hello", "fancy text", "cool fonts", "unicode"]))
    except Exception:
        pass


def maybe_tab_switch(driver):
    try:
        handles = driver.window_handles
        if len(handles) > 1:
            driver.switch_to.window(random.choice(handles))
            time.sleep(random.uniform(0.25, 0.9))
            driver.switch_to.window(handles[0])
    except Exception:
        pass


def dwell(driver, lo, hi):
    end = min(time.time() + random.uniform(lo, hi), time.time() + max(2, time_left() - 12))
    while time.time() < end and not STOP:
        r = random.random()
        if r < 0.55:
            scroll(driver)
        elif r < 0.72:
            scroll_burst(driver, random.uniform(1.5, 4.5))
        elif r < 0.82:
            maybe_type(driver)
        elif r < 0.9:
            maybe_tab_switch(driver)
        else:
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": random.randint(200, 1100),
                    "y": random.randint(150, 700), "buttons": 0, "pointerType": "mouse"})
            except Exception:
                pass
        time.sleep(random.uniform(0.25, 1.2))


def click_random_button(driver):
    try:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, "button, a.btn, [role=button]")
               if e.is_displayed() and e.size["height"] > 12][:20]
        if not els:
            return
        el = random.choice(els)
        hover(driver, el)
        time.sleep(random.uniform(0.12, 0.4))
        click_at(driver, el)
    except Exception:
        pass


def click_internal_link(driver):
    try:
        good = []
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
            try:
                if SITE_MATCH in (a.get_attribute("href") or "") and a.is_displayed():
                    good.append(a)
            except Exception:
                continue
        if not good:
            return
        el = random.choice(good[:25])
        hover(driver, el)
        click_at(driver, el)
        time.sleep(random.uniform(1.0, 2.4))
    except Exception:
        pass


# ============================================== GOOGLE SEARCH ENTRY (GSC-VISIBLE)

def accept_google_consent(driver):
    for sel in ("button#L2AGLb", "button[aria-label*='Accept']", "button[aria-label*='Agree']",
                "div[role=button][id*='L2AGLb']", "button[id='W0wltc']"):
        try:
            for b in driver.find_elements(By.CSS_SELECTOR, sel):
                if b.is_displayed():
                    hover(driver, b)
                    click_at(driver, b)
                    time.sleep(random.uniform(0.5, 1.0))
                    return True
        except Exception:
            continue
    return False


def _is_our_serp_link(href):
    if not href or SITE_MATCH not in href:
        return False
    if "/search?" in href:
        return False
    if "google." in href and "/url?" not in href:
        return False
    return href.startswith("http") or href.startswith("/url?")


def _on_sorry_page(driver):
    try:
        if "/sorry/" in (driver.current_url or ""):
            return True
        t = driver.title or ""
        if t.startswith("http") and "google." in t:
            return True
    except Exception:
        pass
    return False


def handle_google_interstitial(driver):
    if not _on_sorry_page(driver):
        return True

    log("    [*] google /sorry/ interstitial -- attempting to pass")

    for sel in ("iframe[src*='recaptcha/api2/anchor']",
                "iframe[title*='reCAPTCHA']",
                "iframe[src*='recaptcha']"):
        try:
            fr = driver.find_elements(By.CSS_SELECTOR, sel)
            if not fr:
                continue
            driver.switch_to.frame(fr[0])
            time.sleep(random.uniform(0.6, 1.2))
            boxes = driver.find_elements(By.CSS_SELECTOR, "#recaptcha-anchor, .recaptcha-checkbox")
            if boxes:
                click_at(driver, boxes[0])
            driver.switch_to.default_content()
            time.sleep(random.uniform(2.5, 4.0))
            break
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    if not _on_sorry_page(driver):
        log("    [*] interstitial passed")
        return True

    try:
        m = re.search(r"continue=([^&]+)", driver.current_url or "")
        if m:
            target = unquote(m.group(1))
            if target.startswith("http"):
                _fast_get(driver, target, timeout=18)
                time.sleep(1.5)
                if not _on_sorry_page(driver):
                    log("    [*] interstitial bypassed via continue=")
                    return True
    except Exception:
        pass

    log("    [!] interstitial still blocking")
    return False


def _fast_get(driver, url, timeout=28):
    try:
        driver.set_page_load_timeout(timeout)
    except Exception:
        pass
    ok = True
    try:
        driver.get(url)
    except Exception:
        ok = False
        try:
            driver.execute_script("window.stop()")
        except Exception:
            pass
    finally:
        try:
            driver.set_page_load_timeout(40)
        except Exception:
            pass
    return ok


def _serp_report(driver, q):
    """Return (total_links, our_links) and log url/title for diagnosis."""
    try:
        url = (driver.current_url or "")[:78]
        title = (driver.title or "")[:55]
        links = driver.find_elements(By.CSSSelector("a[href]"))
        ours = 0
        for a in links[:400]:
            try:
                if _is_our_serp_link(a.get_attribute("href") or ""):
                    ours += 1
            except Exception:
                continue
        log(f"    [*] SERP '{q}': {len(links)} links, {ours} ours")
        log(f"        url={url}")
        log(f"        title={title!r}")
        return len(links), ours
    except Exception:
        return 0, 0


def _click_first_our_link(driver, q, tld):
    try:
        for a in driver.find_elements(By.CSSSelector("a[href]")):
            try:
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            if not _is_our_serp_link(href) or not a.is_displayed():
                continue
            if not hover(driver, a):
                continue
            time.sleep(random.uniform(0.25, 0.7))
            click_at(driver, a)
            time.sleep(random.uniform(1.2, 2.2))
            if SITE_MATCH in (driver.current_url or ""):
                log(f"    [*] entered via google.{tld}: '{q}'")
                return True
    except Exception:
        pass
    return False


def enter_via_google(driver, term=None):
    """Search Google, click our own organic result. Feeds Search Console."""
    cc = ROUTE_USED.get("country") or "US"
    tld = GOOGLE_TLD.get(cc, "com")

    candidates = [term if term else random.choice(SEARCH_TERMS)]
    candidates.append(random.choice(BRAND_TERMS))

    log(f"    [*] google entry attempt via google.{tld}")

    for idx, q in enumerate(candidates):
        if time_left() < 32:
            log("    [!] google entry: out of budget")
            break

        url = f"https://www.google.{tld}/search?q={quote_plus(q)}&hl=en&num=20"
        loaded = _fast_get(driver, url, timeout=28)
        if not loaded:
            log(f"    [!] google.{tld} slow for '{q}' -- parsing partial DOM")
        time.sleep(random.uniform(1.2, 2.0))

        accept_google_consent(driver)
        handle_google_interstitial(driver)
        scroll_burst(driver, random.uniform(0.8, 1.6))

        total, ours = _serp_report(driver, q)

        if total < 8:
            log("    [*] page looks empty -- reloading once")
            _fast_get(driver, url, timeout=25)
            time.sleep(random.uniform(1.5, 2.5))
            total, ours = _serp_report(driver, q)

        if ours and _click_first_our_link(driver, q, tld):
            return True

        if ours == 0 and idx == 0 and time_left() < 70:
            log("    [*] skipping 2nd query (low budget)")
            break

        log(f"    [!] no SERP hit for '{q}'")

    return False


# =================================================================== ADS LOGIC

def ads_available(driver, timeout=6):
    end = time.time() + timeout
    while time.time() < end and not STOP:
        for sel in AD_SELECTORS:
            try:
                for f in driver.find_elements(By.CSS_SELECTOR, sel):
                    if f.is_displayed() and f.size.get("height", 0) >= 40:
                        return f
            except Exception:
                continue
        time.sleep(0.7)
    return None


def click_one_ad(driver, frame):
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});", frame)
    except Exception:
        pass
    time.sleep(random.uniform(0.5, 1.2))

    hover(driver, frame)
    time.sleep(random.uniform(0.25, 0.8))
    click_at(driver, frame)

    try:
        driver.switch_to.frame(frame)
        time.sleep(random.uniform(0.3, 0.8))
        inner = [e for e in driver.find_elements(By.CSS_SELECTOR, "a, div[role=button]")
                 if e.is_displayed()][:4]
        if inner and random.random() < 0.65:
            click_at(driver, random.choice(inner))
        driver.switch_to.default_content()
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    time.sleep(random.uniform(0.7, 1.6))
    before = len(driver.window_handles)
    time.sleep(0.7)
    if len(driver.window_handles) > before:
        try:
            driver.switch_to.window(driver.window_handles[-1])
            time.sleep(random.uniform(1.2, 3))
            scroll_burst(driver, random.uniform(1.5, 4))
            time.sleep(random.uniform(1.2, 3))
            driver.close()
            driver.switch_to.window(driver.window_handles[0])
        except Exception:
            try:
                driver.switch_to.window(driver.window_handles[0])
            except Exception:
                pass
    return True


def money_step(driver, click_now):
    url = random.choice(LANDING_URLS)
    log(f"[*] MONEY STEP -> {url} | left {time_left():.0f}s | click_gate={click_now}")

    entered = False
    if USE_GOOGLE_ENTRY:
        entered = enter_via_google(driver)
    if not entered:
        log("    [*] direct nav fallback")
        _fast_get(driver, url, timeout=25)
    time.sleep(random.uniform(1.0, 2.2))

    for txt in ("Accept", "Accept all", "Got it", "OK", "Agree"):
        try:
            for b in driver.find_elements(By.XPATH, f"//button[contains(., '{txt}')]"):
                if b.is_displayed():
                    hover(driver, b)
                    click_at(driver, b)
                    time.sleep(0.5)
                    break
        except Exception:
            pass

    scroll_burst(driver, random.uniform(1.5, 4))

    frame = ads_available(driver, timeout=6)

    if frame and click_now:
        log("    [+] AD RENDERED + gate open -> clicking")
        click_one_ad(driver, frame)
        time.sleep(random.uniform(1.2, 2.5))
        return "click", url

    if frame:
        log("    [*] ad rendered but gate closed -> viewing")
    else:
        log("    [*] no ad -> viewing page")

    dwell(driver, 6, 15)
    return "view", url


# ================================================================ TRAFFIC MODEL

HOUR_WEIGHTS = [
    0.35, 0.22, 0.18, 0.16, 0.20, 0.30,
    0.50, 0.72, 0.95, 1.15, 1.30, 1.35,
    1.25, 1.20, 1.15, 1.10, 1.05, 1.00,
    0.95, 0.92, 0.88, 0.78, 0.62, 0.45,
]
DOW_WEIGHTS = [1.00, 1.06, 1.10, 1.09, 1.04, 0.86, 0.80]
MONTH_GROWTH = [1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 1.12, 1.14, 1.16, 1.18, 1.20, 1.22]
HOT_MONTHS = {1, 3, 6, 8, 10, 12}


def day_seed(dt):
    return int(hashlib.md5(f"{dt.year}-{dt.month}-{dt.day}".encode()).hexdigest()[:8], 16)


def slot_index(dt):
    return (dt.hour * 60 + dt.minute) // 5


def traffic_model(dt):
    rnd = random.Random(day_seed(dt))
    base = 45.0
    base *= MONTH_GROWTH[min(dt.month - 1, 11)]
    base *= 1.12 if dt.month in HOT_MONTHS else 0.93
    base *= DOW_WEIGHTS[dt.weekday()]
    base *= rnd.uniform(0.82, 1.22)
    base = max(22, min(74, base))
    clicks_today = int(round(base))
    p = max(0.02, min(0.9, (clicks_today / 288.0) * HOUR_WEIGHTS[dt.hour]))
    views = max(1, int(round(VIEWS_PER_RUN * rnd.uniform(0.85, 1.35))))
    return clicks_today, p, views


def should_click(dt, p):
    if FORCE_CLICK:
        return True
    return random.Random(day_seed(dt) ^ slot_index(dt)).random() < p


# ======================================================================== MAIN

def main():
    now = datetime.now(timezone.utc)
    if TRIGGER_UTC:
        try:
            h, m = TRIGGER_UTC.split(":")
            now = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        except Exception:
            pass

    log("=" * 68)
    log(f"[*] run {RUN_NUMBER} id {RUN_ID} event {EVENT_NAME}")
    log(f"[*] utc {now.isoformat()} slot {slot_index(now)} force_click={FORCE_CLICK}")
    log(f"[*] vpn pool: {len(VPN_COUNTRIES)} countries | "
        f"budgets vpn={VPN_BUDGET}s proxy={PROXY_BUDGET}s | max tries {VPN_MAX_TRIES}")
    if VPN_AUTOFIXED:
        log(f"[*] note: added {','.join(VPN_AUTOFIXED)} to vpn pool (env list was missing them)")
    log("=" * 68)

    clicks_today, p_slot, views = traffic_model(now)
    click_now = should_click(now, p_slot)
    log(f"[*] model: ~{clicks_today} clicks/day | slot p={p_slot:.3f} | views={views} | click_now={click_now}")

    seed = int(hashlib.md5(f"{now.date()}-{slot_index(now)}".encode()).hexdigest()[:8], 16)
    fp = generate_fingerprint(seed)
    log(f"[*] identity {fp['profile']} | {fp['platform']} | {fp['screen_w']}x{fp['screen_h']}")

    proxy = None
    residential = os.environ.get("RESIDENTIAL_PROXY", "").strip()
    ROUTE_USED["runner_ip"] = current_ip(timeout=8)

    if residential:
        proxy = residential
        ROUTE_USED.update(tier="1-residential", detail=residential)
        log("[*] route tier 1: residential proxy")
    else:
        if vpn_connect(VPN_COUNTRIES):
            ROUTE_USED.update(tier="2-vpn", detail=f"{VPN_STATE['country']}/{VPN_STATE['server']}")
        else:
            log(f"[!] VPN unavailable -> tier 3 free proxy ({proxy_time_left():.0f}s left)")
            try:
                pool = ProxyPool()
                pool.load()
                r = pool.next()
                if r:
                    proxy, cc, ip = r
                    ROUTE_USED.update(tier="3-proxy", detail=f"{cc}/{proxy}", ip=ip, country=cc)
                    log(f"[+] proxy {proxy} -> {ip} ({cc})")
                else:
                    log("[!] no working proxy found in budget")
            except Exception as e:
                log(f"[!] proxy pool failed: {e}")

    if ROUTE_USED["tier"] == "none":
        ROUTE_USED.update(tier="4-direct", detail=ROUTE_USED["runner_ip"])

    log(f"[*] route: {ROUTE_USED['tier']} ({ROUTE_USED['detail']}) | left {time_left():.0f}s")

    cc = ROUTE_USED.get("country") or "US"
    if cc in UA_LOCALES:
        local_lang, local_tz = UA_LOCALES[cc]
        if cc in EU_LOCALES and not local_lang.startswith("en") and random.random() < 0.45:
            lang, tz = "en-GB", local_tz
        else:
            lang, tz = local_lang, local_tz
        fp["language"] = lang
        fp["languages"] = [lang, lang.split("-")[0]]
        fp["timezone"] = tz
        fp["voices"] = voices_for(lang)
        log(f"[*] locale {lang} / {tz} for exit {cc}")

    result = {"views": 0, "clicks": 0, "urls": [], "errors": []}
    driver = None

    try:
        driver = create_driver(fp, proxy=proxy)

        kind, url = money_step(driver, click_now)
        result["views"] += 1
        result["urls"].append(url)
        if kind == "click":
            result["clicks"] += 1

        while len(result["urls"]) < views and time_left() > 35 and not STOP:
            url = random.choice(LANDING_URLS)
            log(f"[*] extra view -> {url} | left {time_left():.0f}s")
            try:
                _fast_get(driver, url, timeout=22)
                time.sleep(random.uniform(1.0, 2.0))
                scroll_burst(driver, random.uniform(1.5, 3.5))
                dwell(driver, 5, 11)
                result["views"] += 1
                result["urls"].append(url)
            except Exception as e:
                result["errors"].append(f"view: {str(e)[:60]}")
                break

    except Exception as e:
        log(f"[!] fatal: {e}")
        result["errors"].append(str(e)[:140])
    finally:
        result.update({
            "run": RUN_NUMBER, "utc": now.isoformat(), "slot": slot_index(now),
            "route": ROUTE_USED, "vpn_ok": VPN_STATE["ok"], "identity": fp["profile"],
            "locale": fp["language"], "click_gate": click_now,
            "clicks_today_model": clicks_today,
            "seconds": round(time.time() - START_TS, 1),
        })
        try:
            with open("run_summary.json", "w") as f:
                json.dump(result, f, indent=2)
        except Exception:
            pass

        log("=" * 68)
        log(f"[*] done in {result['seconds']}s | views={result['views']} "
            f"clicks={result['clicks']} route={ROUTE_USED['tier']}")
        for e in result["errors"]:
            log(f"    [!] {e}")
        log("=" * 68)

        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()