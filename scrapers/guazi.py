"""Guazi.com (English export site) scraper.

Lists at https://en.guazi.com/used-cars/, detail at /products/<slug>.html.
Geo-redirect: www.guazi.com/<city>/buy/ -> en.guazi.com for non-CN IPs.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import StealthyFetcher

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Session cookies harvested via Playwright login; reused on every Stealthy
# fetch in the run via the `cookies=` param. Empty dict = no auth.
SESSION_COOKIES: dict[str, str] = {}
COOKIE_CACHE = Path(".cache/guazi-cookies.json")


def _cookies_for_stealthy() -> list[dict] | None:
    """Scrapling expects cookies as a list of {name, value, domain, path}
    dicts (Playwright shape), not a flat name->value dict. Convert."""
    if not SESSION_COOKIES:
        return None
    return [
        {"name": k, "value": v, "domain": ".guazi.com", "path": "/"}
        for k, v in SESSION_COOKIES.items()
    ]

# Persistent (brand_idx, page) cursor across self-chained runs. Each batch
# starts where the previous one left off, advancing the brand pointer when
# a brand runs out of pages. Wraps at end of brand list so we re-scan as
# new listings appear.
PAGE_CURSOR = Path(".cache/guazi-page.txt")
PAGE_MAX_PER_BRAND = 50  # safety cap per brand — most have <30 real pages

# Brand slug list — discovered at runtime from any working brand page's
# nav, cached, and reused across self-chained runs. Hardcoded list is the
# fallback when discovery returns too few entries (parse fail / nav change).
BRAND_LIST_CACHE = Path(".cache/guazi-brands.json")
BRAND_LIST_TTL_S = 7 * 24 * 3600
BRAND_LIST_MIN = 30  # below this we keep the fallback list
BRAND_DISCOVERY_SEED = "/used-cars/toyota/page1/"  # known-good page for nav

# Non-brand slugs to exclude when sniffing /used-cars/<slug>/ anchors.
NON_BRAND_SLUGS = {
    "sedan", "suv", "mini-van", "hatchback", "wagon", "pick-up",
    "van", "truck", "buy", "sell", "search", "tag", "city",
    "convertible", "coupe", "mpv", "minivan",
}

# Fallback brand list — used when runtime discovery fails or yields too few
# entries. Sourced from poisk_avto's working scraper.
BRAND_SLUGS_FALLBACK: list[str] = [
    "toyota", "volkswagen", "bmw", "mercedes-benz", "audi", "honda",
    "nissan", "hyundai", "kia", "byd", "geely-auto", "chery",
    "haval", "great-wall", "changan", "buick", "chevrolet", "ford",
    "cadillac", "lincoln", "mazda", "subaru", "mitsubishi",
    "tesla", "nio", "xpeng", "li-auto", "zeekr", "xiaomi-auto",
    "volvo", "lexus", "infiniti", "porsche", "land-rover", "jaguar",
    "bentley", "rolls-royce", "maserati", "ferrari", "lamborghini",
    "peugeot", "citroen", "skoda", "renault", "smart", "mini", "mg",
    "jetour", "tank", "lynk-co", "wey", "hongqi", "denza",
    "voyah", "aion", "ora", "leapmotor", "neta", "arcfox",
    "roewe", "wuling", "baojun", "dongfeng", "jac", "foton",
    "gac-trumpchi", "aito", "jeep", "dodge", "chrysler",
    "genesis", "acura", "alfa-romeo", "aston-martin", "mclaren",
    "lotus", "suzuki", "ds",
]

# Populated at first call to get_brand_slugs(); rest of the module references
# this via the getter so the discovery side-effect happens once per run.
BRAND_SLUGS: list[str] = BRAND_SLUGS_FALLBACK

# Parallel detail-fetch worker count. Each worker is a separate Chromium
# context (~600-800 MB RAM with disable_resources=True), so 4 fits inside
# GitHub Actions' 7GB runner with headroom. Bumping higher risks OOM
# kills and also raises the chance of guazi seeing a burst pattern.
DETAIL_WORKERS = 4

BASE = "https://en.guazi.com"
LIST_PATH = "/used-cars/"
# Detail-link extraction. Guazi has flipped between SSR <a href="..."> and
# Next.js-streamed JSON paths over time, so we match the `/products/<slug>.html`
# path anywhere in the body (HTML attribute, JSON string, JS literal).
DETAIL_HREF_RE = re.compile(r'/products/[a-z0-9\-]+\.html')
SLUG_RE = re.compile(
    r"^(?P<brand>[a-z\-]+?)-(?P<model>[a-z0-9\-]+?)-(?P<year>(?:19|20)\d{2})-"
    r"(?P<engine>[\d.]+l)-(?:[a-z]+-)?(?P<mileage>\d+)km-"
    r"(?P<gear>at|mt|cvt|amt|dct)(?:-(?P<drive>2wd|4wd|awd))?-(?P<seats>\d+)-seats-"
    r"(?P<id>[a-z0-9]+)$"
)

MULTI_WORD_BRANDS = {
    # Western & Korean
    "land-rover", "mercedes-benz", "alfa-romeo", "aston-martin", "rolls-royce",
    "lynk-co", "smart-brabus",
    # Chinese conglomerates with shared parent prefix
    "great-wall", "wuling-hongguang", "geely-auto",
    "saic-roewe", "saic-maxus",
    "gac-trumpchi", "gac-aion", "gac-hyptec",
    "dongfeng-aeolus", "dongfeng-fengxing", "dongfeng-voyah", "dongfeng-m-hero",
    "faw-bestune", "faw-hongqi",
    "chery-jetour", "chery-exeed", "chery-omoda", "chery-jaecoo", "chery-icar",
    "byd-denza", "byd-yangwang", "byd-fang-cheng-bao",
    "changan-deepal", "changan-avatr",
    "nio-onvo", "nio-firefly",
    "huawei-aito", "huawei-luxeed", "huawei-stelato", "huawei-maextro",
    "xiaomi-auto",
    "beijing-auto",
    "great-wall-haval", "great-wall-tank", "great-wall-wey", "great-wall-ora",
}


@dataclass
class Listing:
    site: str = "guazi"
    listing_id: str = ""
    url: str = ""
    slug: str = ""
    title: str = ""
    brand: str = ""
    model: str = ""
    year: int | None = None
    engine_l: float | None = None
    engine_code: str = ""
    horsepower_ps: float | None = None
    mileage_km: int | None = None
    gearbox: str = ""
    drive: str = ""
    drive_train: str = ""
    seats: int | None = None
    doors: int | None = None
    color: str = ""
    body_type: str = ""
    dimension_mm: str = ""
    curb_weight_kg: int | None = None
    steering: str = ""
    fuel: str = ""
    production_date: str = ""
    registration_date: str = ""
    model_date: str = ""
    grade: str = ""
    vin: str = ""
    vin_mask: str = ""
    accident_free: bool | None = None
    water_damage_free: bool | None = None
    fire_damage_free: bool | None = None
    has_inspection_report: bool | None = None
    inspection_categories: list[dict] = field(default_factory=list)
    inspection_status: str = ""
    price_raw: str = ""
    price_amount: float | None = None
    currency: str = "USD"
    is_auction: bool = False
    location: str = ""
    spec: dict = field(default_factory=dict)
    photos: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _to_float(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def _normalize_engine(raw: str) -> float | None:
    """'25l' -> 2.5, '00l' -> 0.0, '2.0l' -> 2.0."""
    raw = raw.rstrip("l")
    if "." in raw:
        return _to_float(raw)
    if len(raw) >= 2:
        return _to_float(f"{raw[:-1]}.{raw[-1]}")
    return _to_float(raw)


def parse_slug(slug: str) -> dict:
    m = SLUG_RE.match(slug)
    if not m:
        return {}
    g = m.groupdict()
    brand = g["brand"]
    model = g["model"]
    if "-" in brand:
        for mw in MULTI_WORD_BRANDS:
            if slug.startswith(mw + "-"):
                rest = slug[len(mw) + 1 :]
                if (m2 := SLUG_RE.match("x-" + rest)):
                    brand = mw
                    model = m2.group("model")
                break
    return {
        "brand": brand.replace("-", " "),
        "model": model.replace("-", " "),
        "year": int(g["year"]),
        "engine_l": _normalize_engine(g["engine"]),
        "mileage_km": int(g["mileage"]),
        "gearbox": g["gear"].upper(),
        "drive": (g["drive"] or "").upper(),
        "seats": int(g["seats"]),
        "listing_id": g["id"],
    }


def _load_cached_cookies() -> dict[str, str]:
    """Reuse cookies from a previous run if the cache file exists and isn't
    older than 18 hours (guazi's session cookies usually live ~24h)."""
    if not COOKIE_CACHE.exists():
        return {}
    try:
        age_s = time.time() - COOKIE_CACHE.stat().st_mtime
        if age_s > 18 * 3600:
            return {}
        return json.loads(COOKIE_CACHE.read_text()) or {}
    except Exception:
        return {}


def _save_cookies(cookies: dict[str, str]) -> None:
    try:
        COOKIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_CACHE.write_text(json.dumps(cookies))
    except Exception as e:
        print(f"[guazi] cookie save failed: {e}", file=sys.stderr)


def _login_and_capture_cookies(email: str, password: str) -> dict[str, str]:
    """Spin up Playwright once, run the login flow from poisk_avto, and
    return all cookies the browser captured. Empty dict on failure — the
    caller falls back to unauthenticated fetch, which guazi may CAPTCHA."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"[guazi] playwright not installed: {e}", file=sys.stderr)
        return {}

    cookies: dict[str, str] = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=UA, locale="en-US")
            page = context.new_page()
            print(f"[guazi] login: opening {BASE}", file=sys.stderr)
            page.goto(BASE, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2500)

            login_btn = page.query_selector(
                'button:has-text("Log"), a:has-text("Log"), '
                'button:has-text("Sign"), a:has-text("Sign")'
            )
            if not login_btn:
                print("[guazi] login: no Log/Sign button found", file=sys.stderr)
            else:
                login_btn.click()
                page.wait_for_timeout(2000)
                email_input = page.query_selector(
                    'input[type="email"], input[name="email"], '
                    'input[placeholder*="email" i], input[placeholder*="Email"]'
                )
                pass_input = page.query_selector(
                    'input[type="password"], input[name="password"]'
                )
                if email_input and pass_input:
                    email_input.fill(email)
                    pass_input.fill(password)
                    page.wait_for_timeout(500)
                    submit = page.query_selector(
                        'button[type="submit"], button:has-text("Log in"), '
                        'button:has-text("Sign in")'
                    )
                    if submit:
                        submit.click()
                    else:
                        pass_input.press("Enter")
                    page.wait_for_timeout(3500)
                    if "login" not in page.url.lower():
                        print("[guazi] login: success", file=sys.stderr)
                    else:
                        print("[guazi] login: still on /login — likely failed",
                              file=sys.stderr)
                else:
                    print("[guazi] login: form not found", file=sys.stderr)

            for c in context.cookies() or []:
                cookies[c["name"]] = c["value"]
            print(f"[guazi] captured {len(cookies)} cookies", file=sys.stderr)
            browser.close()
    except Exception as e:
        print(f"[guazi] login error: {e}", file=sys.stderr)
    return cookies


def _read_page_cursor() -> tuple[int, int]:
    """Return (brand_idx, page_num). Accepts legacy bare-int format
    (treats it as page on brand 0) so old caches don't crash a new run."""
    if PAGE_CURSOR.exists():
        try:
            raw = PAGE_CURSOR.read_text().strip()
            if ":" in raw:
                bi, pn = raw.split(":", 1)
                bi_i = max(0, min(len(BRAND_SLUGS) - 1, int(bi)))
                pn_i = max(1, min(PAGE_MAX_PER_BRAND, int(pn)))
                return bi_i, pn_i
            # legacy format: bare page number → start at brand 0
            return 0, max(1, min(PAGE_MAX_PER_BRAND, int(raw)))
        except Exception:
            pass
    return 0, 1


def _save_page_cursor(brand_idx: int, page: int) -> None:
    try:
        PAGE_CURSOR.parent.mkdir(parents=True, exist_ok=True)
        # Wrap brand_idx at end of list so we restart the sweep periodically.
        bi = brand_idx % max(1, len(BRAND_SLUGS))
        pn = max(1, min(PAGE_MAX_PER_BRAND, page))
        PAGE_CURSOR.write_text(f"{bi}:{pn}")
    except Exception as e:
        print(f"[guazi] page cursor save failed: {e}", file=sys.stderr)


def _ensure_session() -> None:
    """Populate SESSION_COOKIES from cache, or run the login flow once."""
    global SESSION_COOKIES
    if SESSION_COOKIES:
        return
    cached = _load_cached_cookies()
    if cached:
        SESSION_COOKIES = cached
        print(f"[guazi] reused {len(cached)} cached cookies", file=sys.stderr)
        return
    email = os.getenv("GUAZI_EMAIL", "").strip()
    password = os.getenv("GUAZI_PASSWORD", "").strip()
    if email and password:
        SESSION_COOKIES = _login_and_capture_cookies(email, password)
        if SESSION_COOKIES:
            _save_cookies(SESSION_COOKIES)
    else:
        print("[guazi] no GUAZI_EMAIL/PASSWORD env, running anon (CAPTCHA risk)",
              file=sys.stderr)


# ──────────────────── JSON facade API · fresh mode (AUT-73/74) ───────────────
# Listing comes from the JSON endpoint the site's own front-end calls. One
# EdgeOne-cleared browser page is opened (StealthyFetcher's page_action hook);
# all list queries run as in-page fetch() — one Chromium load per run.
#
# Fresh mode: a single global query sorted by listing date (sort=created_at
# desc) returns the newest-posted cars. We page through the newest ~500 and
# value-filter in-page; the (source, source_id) upsert dedups, so a re-seen car
# just bumps last_seen and a genuinely new one inserts. The ~500 offset cap is a
# non-issue — anything older than the newest ~500 was caught by an earlier run.
API_LIST_PATH = "/os/facade/search/product/list?language=en"
LIST_PAGE_SIZE = 30      # hard server max (>=31 → 每页数超过最大查询阈值)
OFFSET_CAP = 480         # server exposes only ~first 500 rows of any one query
# sort=created_at desc → newest by listing/posting date (verified: every item
# carries the "Newly listed" label, mfg years mixed).

# Runs inside the EdgeOne-cleared page: page the newest-first listing, value-
# filter against list fields, collect up to `limit`. Returns {items, pages}.
_JS_FRESH = r"""
async ({limit, filters, guid, did, PS, CAP}) => {
  const f = filters || {};
  const post = async (pn) => {
    const body = {language:'en', businessType:5, clientScene:'cars', sourceFrom:'wap',
      countryCode:'', guid, did, pageSize:PS, pageNum:pn, sort:'created_at desc'};
    const r = await fetch('/os/facade/search/product/list?language=en', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify(body), credentials:'include'});
    const j = await r.json();
    return (j.data && j.data.list) || [];
  };
  const yearOf = (m) => (m && /^\d{4}/.test(m)) ? Number(m.slice(0,4)) : null;
  const gradeOf = (labels) => {
    for (const l of labels || []) if (l.type === 2) {
      const m = /^Grade\s+(\S+)/.exec(l.name || ''); if (m) return m[1];
    }
    return '';
  };
  const keep = (it) => {
    const y = yearOf(it.mfgDate);
    if (f.minYear != null && (y == null || y < f.minYear)) return false;
    if (f.maxYear != null && (y == null || y > f.maxYear)) return false;
    if (f.maxMileage != null) {
      const km = it.mileage ? Number(String(it.mileage).replace(/,/g,'')) : null;
      if (km != null && km > f.maxMileage) return false;
    }
    if (f.minPrice != null) {
      const p = it.price ? Number(String(it.price).replace(/[^0-9.]/g,'')) : null;
      if (p == null || p < f.minPrice) return false;   // also drops auction lots (price null)
    }
    if (f.grades && f.grades.length && !f.grades.includes((gradeOf(it.labels) || '').toUpperCase())) return false;
    if (f.sources && f.sources.length) {
      const pl = it.productLabels || [];
      if (!pl.some(x => f.sources.includes(String(x)))) return false;
    }
    return true;
  };

  const items = [], seen = new Set();
  let pn = 1, pages = 0;
  while (items.length < limit && (pn - 1) * PS < CAP) {
    let lst;
    try { lst = await post(pn); } catch (e) { break; }
    pages++;
    if (!lst.length) break;
    for (const it of lst) {
      const id = it.productId || it.seoUri;
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      if (keep(it)) items.push(it);
      if (items.length >= limit) break;
    }
    if (lst.length < PS) break;
    pn++;
  }
  return {items, pages};
}
"""


def _parse_guid_did(cookie_str: str) -> tuple[str, str]:
    """Pull guid (cookie `uuid`) and did (cookie `global_did`) out of a
    document.cookie string. Both are required, non-empty, by the list API."""
    ck: dict[str, str] = {}
    for part in (cookie_str or "").split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            ck[k] = v
    return ck.get("uuid", ""), ck.get("global_did", "")


def _run_in_page(job_fn):
    """Open one EdgeOne-cleared guazi page and run job_fn(page) inside it.

    StealthyFetcher's stealth Chromium clears the TencentEdgeOne bot wall on
    navigation; the page_action hook then runs our in-page automation on the
    live Playwright page. page_action's own return value is discarded, so
    job_fn's result is stashed via a closure holder.
    """
    holder: dict = {}

    def _driver(page):
        try:
            try:
                page.wait_for_timeout(800)  # let guazi JS mint uuid/global_did
            except Exception:
                pass
            holder["result"] = job_fn(page)
        except Exception as e:
            holder["error"] = e
            print(f"[guazi] page_action error: {e}", file=sys.stderr)
        return page

    StealthyFetcher.fetch(
        BASE + LIST_PATH,
        headless=True,
        network_idle=True,
        humanize=True,
        wait=2500,
        timeout=90000,
        cookies=_cookies_for_stealthy(),
        page_action=_driver,
    )
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


BRAND_HREF_RE = re.compile(r'/used-cars/([a-z][a-z0-9-]{1,40})/')
# Suffix tokens that mean "this isn't a pure-brand URL" — filter URLs,
# transmission/body/price/mileage facets that guazi exposes as SEO landing
# pages. Matches anywhere in the slug after a dash.
BRAND_NOISE_RE = re.compile(
    r'-(?:automatic|manual|mt|at|cvt|amt|dct|miles?|price|color|sedan|suv|'
    r'hatchback|wagon|coupe|convertible|pickup|pick-up|mpv|van|truck|under|'
    r'over|from|to|years?|model|million|dollar|fuel|cylinder|engine|hp|'
    r'horsepower|displacement|liter|litre|seater|seats?|drive|awd|4wd|2wd|'
    r'fwd|rwd)(?:-|$)'
)
# Color / cosmetic prefixes that indicate a "color filter" SEO page
# (e.g. /used-cars/gray-toyota-gac-toyota-bz4x/).
BRAND_PREFIX_NOISE_RE = re.compile(
    r'^(?:white|black|red|blue|silver|gray|grey|green|gold|brown|yellow|'
    r'orange|purple|beige|pearl|champagne|bronze|copper|pink|navy|ivory)-'
)


def _load_cached_brands() -> list[str]:
    if not BRAND_LIST_CACHE.exists():
        return []
    try:
        age = time.time() - BRAND_LIST_CACHE.stat().st_mtime
        if age > BRAND_LIST_TTL_S:
            return []
        data = json.loads(BRAND_LIST_CACHE.read_text())
        return [s for s in data if isinstance(s, str)]
    except Exception:
        return []


def _save_brands(slugs: list[str]) -> None:
    try:
        BRAND_LIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        BRAND_LIST_CACHE.write_text(json.dumps(slugs))
    except Exception as e:
        print(f"[guazi] brand cache save failed: {e}", file=sys.stderr)


def _discover_brands() -> list[str]:
    """Fetch a known-good brand page (toyota), extract every /used-cars/<slug>/
    anchor in its nav, filter out body-type / non-brand slugs. Returns sorted
    unique slug list. Empty list on failure — caller falls back."""
    url = urljoin(BASE, BRAND_DISCOVERY_SEED)
    print(f"[guazi] brand discovery: {url}", file=sys.stderr)
    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            humanize=True,
            wait=2500,
            disable_resources=True,
            timeout=30000,
            cookies=_cookies_for_stealthy(),
        )
        body = page.body.decode("utf-8", "replace")
    except Exception as e:
        print(f"[guazi] discovery fetch failed: {e}", file=sys.stderr)
        return []

    # Raw-body regex: brand links live inside Next.js JSON-streamed payload
    # (self.__next_f.push), not as actual <a href> tags, so DOM lookup misses
    # them. Match any /used-cars/<slug>/ in the body and filter the noise.
    raw = set(BRAND_HREF_RE.findall(body))

    def _is_brand(slug: str) -> bool:
        if slug in NON_BRAND_SLUGS:
            return False
        if BRAND_PREFIX_NOISE_RE.match(slug):
            return False
        if BRAND_NOISE_RE.search(slug):
            return False
        return True

    brands = sorted(s for s in raw if _is_brand(s))
    print(f"[guazi] discovered {len(brands)} brand candidates "
          f"(raw {len(raw)})", file=sys.stderr)
    if brands:
        print(f"[guazi]   sample: {brands[:15]}", file=sys.stderr)
    else:
        sample = sorted(raw)[:30]
        print(f"[guazi]   DEBUG raw body /used-cars/ matches ({len(raw)}): "
              f"{sample}", file=sys.stderr)
    return brands


def get_brand_slugs() -> list[str]:
    """Lazy populate the BRAND_SLUGS module global with cache → discovery
    → fallback resolution. Called once at the top of fetch_list()."""
    global BRAND_SLUGS
    cached = _load_cached_brands()
    if len(cached) >= BRAND_LIST_MIN:
        BRAND_SLUGS = cached
        print(f"[guazi] using {len(cached)} cached brand slugs", file=sys.stderr)
        return BRAND_SLUGS
    discovered = _discover_brands()
    if len(discovered) >= BRAND_LIST_MIN:
        BRAND_SLUGS = discovered
        _save_brands(discovered)
        print(f"[guazi] using {len(discovered)} discovered brand slugs",
              file=sys.stderr)
        return BRAND_SLUGS
    # Cache anything we got, even small, so we don't re-discover next run
    # if the page is consistently sparse; but use fallback for the sweep.
    if discovered:
        _save_brands(discovered)
    BRAND_SLUGS = BRAND_SLUGS_FALLBACK
    print(f"[guazi] discovery yielded {len(discovered)} (<{BRAND_LIST_MIN}), "
          f"using {len(BRAND_SLUGS)} fallback brand slugs", file=sys.stderr)
    return BRAND_SLUGS


def _build_list_url(
    path: str = LIST_PATH,
    page_num: int = 1,
    params: dict[str, str] | None = None,
    brand_slug: str | None = None,
) -> str:
    """Build a guazi list URL.

    When `brand_slug` is set, use the per-brand pagination format
    (/used-cars/{brand}/page{N}/) — these URLs render SSR cards without
    triggering the EdgeOne CAPTCHA wall that blocks the /used-cars/ root.
    Page goes in the path, not as ?page=, to match guazi's actual SEO URLs.
    """
    from urllib.parse import urlencode
    q = dict(params or {})
    if brand_slug:
        # Per-brand path. Page goes in the URL path like /used-cars/bmw/page2/.
        path = f"/used-cars/{brand_slug}/page{max(1, page_num)}/"
    else:
        # Fallback: legacy /used-cars/?page=N (CAPTCHA-walled — kept for
        # explicit --path /used-cars/<body-type>/ overrides).
        if page_num and page_num != 1:
            q["page"] = str(page_num)
    base = urljoin(BASE, path)
    return base + (("?" + urlencode(q, safe=",")) if q else "")


def _fetch_list_page(url: str, seen: set[str]) -> list[str]:
    """One list-page fetch. Returns new hrefs not in `seen`. Logs blocking
    state on empty result."""
    page = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,
        humanize=True,
        wait=2500,
        disable_resources=True,
        timeout=30000,
        cookies=_cookies_for_stealthy(),
    )
    body = page.body.decode("utf-8", "replace")
    # Strategy 1: hydrated DOM via CSS selector. Strategy 2: regex over raw
    # body. Per-brand pages SSR the anchors so both usually agree.
    dom_hrefs: list[str] = []
    try:
        for el in page.css('a[href*="/products/"]'):
            h = el.attrib.get("href") if hasattr(el, "attrib") else None
            if h is None and hasattr(el, "get"):
                h = el.get("href")
            if h and h.endswith(".html"):
                dom_hrefs.append(h if h.startswith("/") else "/" + h.lstrip("/"))
    except Exception as e:
        print(f"[guazi] DOM selector failed: {e}", file=sys.stderr)
    regex_hrefs = DETAIL_HREF_RE.findall(body)
    local_seen: set[str] = set()
    hrefs: list[str] = []
    for h in dom_hrefs + regex_hrefs:
        if h in seen or h in local_seen:
            continue
        local_seen.add(h)
        hrefs.append(h)
    print(f"[guazi]   body {len(body)} bytes, dom={len(dom_hrefs)} "
          f"regex={len(regex_hrefs)} unique={len(hrefs)}", file=sys.stderr)
    if not hrefs:
        head = body[:400].replace("\n", " ").replace("\r", " ")
        print(f"[guazi]   DEBUG body head: {head}", file=sys.stderr)
        if "captcha" in body.lower() or "verification" in body.lower():
            print(f"[guazi]   DEBUG blocked (captcha/verification)", file=sys.stderr)
    return hrefs


def fetch_list(
    limit: int = 10,
    path: str = LIST_PATH,
    params: dict[str, str] | None = None,
    max_pages: int = PAGE_MAX_PER_BRAND,
    max_mileage_km: int | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
) -> list[Listing]:
    """Per-brand sweep: iterate brand pages until `limit` listings collected.

    Resume from persistent (brand_idx, page_num) cursor. When the current
    page yields 0 hrefs (end of brand), advance to next brand at page 1.
    The /used-cars/<body-type>/ override (--path) skips brand iteration and
    walks pages within that single path.
    """
    out: list[Listing] = []
    seen: set[str] = set()
    skipped = 0
    _ensure_session()
    # Resolve brand list (cache → discovery → fallback). Populates the
    # module-global BRAND_SLUGS used by _read_page_cursor's bounds-check.
    get_brand_slugs()

    # Per-brand iteration mode only kicks in when path is the default
    # /used-cars/ root. Anything else (e.g. --path /used-cars/sedan/) walks
    # pages directly without brand swap.
    brand_mode = path == LIST_PATH
    brand_idx, page_num = _read_page_cursor()
    if not brand_mode:
        brand_idx = 0  # ignored
        # legacy cursor for explicit-path mode
        page_num = max(1, page_num)

    start_brand = brand_idx
    start_page = page_num
    pages_visited = 0

    if brand_mode:
        print(f"[guazi] brand sweep: start at brand[{brand_idx}]="
              f"{BRAND_SLUGS[brand_idx]!r}, page {page_num}", file=sys.stderr)
    else:
        print(f"[guazi] path mode: {path}, start page {page_num}", file=sys.stderr)

    while len(out) < limit and pages_visited < max_pages:
        brand_slug = BRAND_SLUGS[brand_idx] if brand_mode else None
        url = _build_list_url(path, page_num, params, brand_slug=brand_slug)
        label = f"{brand_slug} p{page_num}" if brand_slug else f"p{page_num}"
        print(f"[guazi] list {label}: {url}", file=sys.stderr)
        try:
            hrefs = _fetch_list_page(url, seen)
        except Exception as e:
            print(f"[guazi] fetch error {label}: {e}", file=sys.stderr)
            hrefs = []
        pages_visited += 1

        if not hrefs:
            if brand_mode:
                # End of this brand → advance to next brand at page 1.
                # Wrap at end of BRAND_SLUGS so the sweep restarts.
                brand_idx = (brand_idx + 1) % len(BRAND_SLUGS)
                page_num = 1
                print(f"[guazi]   brand exhausted, advancing to "
                      f"brand[{brand_idx}]={BRAND_SLUGS[brand_idx]!r}",
                      file=sys.stderr)
                # If we've come full circle this batch, stop.
                if brand_idx == start_brand and pages_visited > 1:
                    print(f"[guazi]   full sweep complete, stopping", file=sys.stderr)
                    break
                continue
            else:
                print(f"[guazi]   no new hrefs, stop", file=sys.stderr)
                break

        for h in hrefs:
            seen.add(h)
            slug = Path(urlparse(h).path).stem
            parsed = parse_slug(slug)
            if parsed:
                l = Listing(url=urljoin(BASE, h), slug=slug, **parsed)
            else:
                l = Listing(url=urljoin(BASE, h), slug=slug, listing_id=slug)
            if max_mileage_km is not None and (l.mileage_km or 0) > max_mileage_km:
                skipped += 1; continue
            if min_year is not None and (l.year or 0) < min_year:
                skipped += 1; continue
            if max_year is not None and (l.year or 0) > max_year:
                skipped += 1; continue
            out.append(l)
            if len(out) >= limit:
                break
        page_num += 1

    # Persist cursor for next self-chained run.
    if brand_mode:
        _save_page_cursor(brand_idx, page_num)
        next_brand = BRAND_SLUGS[brand_idx % len(BRAND_SLUGS)]
        print(f"[guazi] total parsed: {len(out)} (skipped {skipped}, "
              f"visited {pages_visited} pages, next={next_brand} p{page_num})",
              file=sys.stderr)
    else:
        _save_page_cursor(0, page_num)
        print(f"[guazi] total parsed: {len(out)} (skipped {skipped}, "
              f"pages {start_page}-{page_num - 1})", file=sys.stderr)
    return out


def _grade_from_labels(labels) -> str:
    """Extract the condition grade letter from a list item's labels[]
    (label type 2 → name like 'Grade C')."""
    for l in labels or []:
        if isinstance(l, dict) and l.get("type") == 2:
            m = re.match(r"Grade\s+(\S+)", l.get("name") or "")
            if m:
                return m.group(1)
    return ""


def _listing_from_api(it: dict) -> Listing:
    """Map one `data.list[]` item into a Listing. Slug parse fills brand/model/
    year/engine/etc.; list-level fields overlay the rest. enrich_detail() still
    runs later for VIN, full spec, inspection report, and the photo gallery."""
    seo = it.get("seoUri") or ""
    slug = seo[:-5] if seo.endswith(".html") else seo
    href = ("/products/" + seo) if seo else ""
    parsed = parse_slug(slug)
    if parsed:
        l = Listing(url=urljoin(BASE, href), slug=slug, **parsed)
    else:
        l = Listing(url=urljoin(BASE, href), slug=slug,
                    listing_id=it.get("productId") or slug)
    if it.get("productId"):
        l.listing_id = it["productId"]
    if it.get("title"):
        l.title = str(it["title"]).strip()
    if it.get("brandName"):
        l.brand = it["brandName"]
    if it.get("fuelTypeName"):
        l.fuel = it["fuelTypeName"]
    price = it.get("price")
    if price:
        l.price_raw = price
        l.price_amount = _to_float(re.sub(r"[^0-9.]", "", price))
    elif it.get("auctionType") is not None:
        l.is_auction = True
    if it.get("mfgDate"):
        l.production_date = it["mfgDate"]
        if l.year is None and re.match(r"\d{4}", str(it["mfgDate"])):
            l.year = int(str(it["mfgDate"])[:4])
    if it.get("mileage") and l.mileage_km is None:
        try:
            l.mileage_km = int(str(it["mileage"]).replace(",", ""))
        except ValueError:
            pass
    if g := _grade_from_labels(it.get("labels")):
        l.grade = g
    if it.get("headImage"):
        l.photos = [it["headImage"]]
    l.raw["api"] = {
        "productLabels": it.get("productLabels"),
        "businessType": it.get("businessType"),
        "brandId": it.get("brandId"),
        "seriesId": it.get("seriesId"),
        "exchangeRate": it.get("exchangeRate"),
    }
    return l


def _filters_from_params(
    params: dict | None,
    min_year: int | None,
    max_year: int | None,
    max_mileage_km: int | None,
    grades: set[str] | None,
) -> dict:
    """Translate the workflow's -f filter params + explicit args into the JS
    sweep's filter dict. Recognised guazi keys: price=MIN[,MAX] (USD),
    licenseYear=YEAR[,..], roadHaul=MIN,MAX (km), detectionLevels=S,A,
    vehicleSourceClassificationCustomers=180003,180002."""
    p = params or {}
    f: dict = {}

    def _lo(v):
        head = str(v).split(",")[0].strip()
        try:
            return float(head) if head else None
        except ValueError:
            return None

    def _hi(v):
        parts = str(v).split(",")
        if len(parts) > 1 and parts[1].strip():
            try:
                return float(parts[1].strip())
            except ValueError:
                return None
        return None

    if "price" in p and (lo := _lo(p["price"])) is not None:
        f["minPrice"] = lo
    if "roadHaul" in p and (hi := _hi(p["roadHaul"])) is not None:
        f["maxMileage"] = hi
    license_year = _lo(p["licenseYear"]) if "licenseYear" in p else None
    min_y = min_year if min_year is not None else (int(license_year) if license_year else None)
    if min_y is not None:
        f["minYear"] = min_y
    if max_year is not None:
        f["maxYear"] = max_year
    if max_mileage_km is not None:
        f["maxMileage"] = max_mileage_km  # explicit arg wins over roadHaul
    gset = set(grades) if grades else set()
    if "detectionLevels" in p:
        gset |= {g.strip().upper() for g in str(p["detectionLevels"]).split(",") if g.strip()}
    if gset:
        f["grades"] = sorted(gset)
    if "vehicleSourceClassificationCustomers" in p:
        f["sources"] = [s.strip() for s in str(p["vehicleSourceClassificationCustomers"]).split(",") if s.strip()]
    return f


def fetch_list_api(
    limit: int = 10,
    params: dict | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    max_mileage_km: int | None = None,
    grades: set[str] | None = None,
) -> list[Listing]:
    """Fresh-mode sweep over the JSON facade API (replaces the SSR-HTML per-page
    scrape for the default /used-cars/ root). Pages the newest-first listing
    (sort=created_at desc), value-filters in-page, returns mapped Listings up to
    `limit`. Cursorless — always starts at the newest; the (source, source_id)
    upsert dedups across runs."""
    _ensure_session()
    filters = _filters_from_params(params, min_year, max_year, max_mileage_km, grades)
    print(f"[guazi] fresh sweep (sort=created_at desc): limit {limit}, "
          f"filters {filters}", file=sys.stderr)

    def _job(page):
        guid, did = _parse_guid_did(page.evaluate("() => document.cookie"))
        if not guid or not did:
            raise RuntimeError("no guid/global_did cookie after homepage load")
        return page.evaluate(_JS_FRESH, {
            "limit": limit, "filters": filters, "guid": guid, "did": did,
            "PS": LIST_PAGE_SIZE, "CAP": OFFSET_CAP,
        })

    res = _run_in_page(_job) or {}
    raw = res.get("items") or []
    print(f"[guazi] fresh sweep: {len(raw)} items over {res.get('pages')} pages",
          file=sys.stderr)
    if not raw:
        print("[guazi] WARNING: 0 items — EdgeOne block, API change, or filters "
              "too strict?", file=sys.stderr)
    return [_listing_from_api(it) for it in raw]


JSONLD_OPEN_RE = re.compile(
    r'<script\s+type="application/ld\+json">\s*', re.IGNORECASE
)
META_RE = re.compile(
    r'<meta\s+(?:name|property)="([^"]+)"\s+content="([^"]*)"', re.IGNORECASE
)
NEXT_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
REPORT_LITE_KEY = '"reportDetailLite"'
INQUIRE_PRICE = 9999999.0


def _parse_metas(body: str) -> dict[str, str]:
    return {k.lower(): v for k, v in META_RE.findall(body)}


def _join_next_chunks(body: str) -> str:
    chunks = NEXT_CHUNK_RE.findall(body)
    if not chunks:
        return ""
    return "".join(chunks).encode("utf-8", "replace").decode("unicode_escape", "ignore")


def _extract_object_at(text: str, start: int) -> str | None:
    """Read a balanced JSON object starting at the first '{' after `start`.

    Aware of double-quoted JSON strings and backslash escapes so braces
    inside string values do not throw off the depth counter.
    """
    try:
        open_at = text.index("{", start)
    except ValueError:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(open_at, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    return None


def _parse_jsonld(body: str) -> dict:
    """Find every <script type="application/ld+json"> block, parse it with
    a balanced-brace extractor (handles nested objects and strings with
    braces), and return the first @type=Car node."""
    for m in JSONLD_OPEN_RE.finditer(body):
        obj_str = _extract_object_at(body, m.end())
        if not obj_str:
            continue
        try:
            data = json.loads(obj_str)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Car":
            return data
    return {}


def _parse_report_lite(joined: str) -> dict:
    idx = joined.find(REPORT_LITE_KEY)
    if idx < 0:
        return {}
    obj = _extract_object_at(joined, idx + len(REPORT_LITE_KEY))
    if not obj:
        return {}
    try:
        return json.loads(obj)
    except json.JSONDecodeError:
        return {}


def _parse_spec_list(joined: str) -> dict:
    """Extract the [{key,name,value}, ...] vehicle-spec array.

    Anchor search probes several known keys — older listings may lack
    `regDate` / `vin` but still have `engine` / `bodyType` / `horsepower`,
    so we don't bail just because the first anchor misses.
    """
    SPEC_ANCHORS = (
        '"key":"regDate"', '"key":"vin"', '"key":"engine"',
        '"key":"horsepower"', '"key":"bodyType"', '"key":"driveType"',
        '"key":"mileage"', '"key":"exteriorColor"',
    )
    anchor = -1
    for needle in SPEC_ANCHORS:
        anchor = joined.find(needle)
        if anchor >= 0:
            break
    if anchor < 0:
        return {}
    arr_start = joined.rfind("[", 0, anchor)
    if arr_start < 0:
        return {}
    depth = 0
    end = -1
    for i in range(arr_start, len(joined)):
        c = joined[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {}
    try:
        items = json.loads(joined[arr_start:end])
    except json.JSONDecodeError:
        return {}
    out: dict = {}
    for it in items or []:
        if isinstance(it, dict) and "key" in it:
            out[it["key"]] = it.get("value")
    return out


def _fetch_detail_page(url: str):
    # Keep resources enabled (disable_resources=False): some guazi templates
    # only inject the full <img> gallery once the page is fully laid out, so
    # blocking images/css can leave us with just the og:image cover.
    return StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,
        humanize=True,
        wait=2500,
        disable_resources=False,
        timeout=30000,
        cookies=_cookies_for_stealthy(),
    )


def enrich_detail(l: Listing) -> Listing:
    print(f"[guazi] detail: {l.url}", file=sys.stderr)
    page = _fetch_detail_page(l.url)
    status = getattr(page, "status", None)
    body = page.body.decode("utf-8", "replace") if getattr(page, "body", None) else ""

    # Anti-bot / login wall returns a tiny stub (~2 KB, no gallery, no
    # og:image); the real listing is ~700 KB with its full <img> gallery
    # already in the HTML. The wall is intermittent per-listing, so retry
    # once before settling — this is what turns the common "1 photo only"
    # (cover/og:image salvaged from a walled page) into the full gallery.
    if (not status or status < 400) and ("og:image" not in body or len(body) < 50_000):
        print(f"[guazi] thin/walled detail ({len(body)}B) for {l.url} — retry once",
              file=sys.stderr)
        page = _fetch_detail_page(l.url)
        status = getattr(page, "status", None)
        body = page.body.decode("utf-8", "replace") if getattr(page, "body", None) else ""

    l.raw["detail_status"] = status
    # Skip parsing on HTTP errors — the body is an error page, not the listing.
    if status and isinstance(status, int) and status >= 400:
        print(f"[guazi] detail HTTP {status} for {l.url} — skip enrichment",
              file=sys.stderr)
        return l

    metas = _parse_metas(body)
    ld = _parse_jsonld(body)
    joined = _join_next_chunks(body)

    title = metas.get("og:title") or (ld.get("name") if ld else "")
    if title:
        l.title = title.strip()

    if (brand := (ld.get("brand") or {}).get("name") if ld else None):
        l.brand = brand
    if ld.get("model"):
        l.model = ld["model"]

    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    raw_price = offers.get("price") or metas.get("product:price:amount") or ""
    cur = offers.get("priceCurrency") or metas.get("product:price:currency") or "USD"
    if raw_price:
        price_f = _to_float(str(raw_price).replace(",", ""))
        if price_f == INQUIRE_PRICE:
            l.price_raw = "inquire"
            l.price_amount = None
        else:
            l.price_raw = f"{cur}{raw_price}"
            l.price_amount = price_f
        l.currency = cur

    # detect auction lot — guazi marks auctionType:1 + bidPrice with $$ placeholder
    if re.search(r'"auctionType"\s*:\s*1', joined) or re.search(r'"bidPrice"\s*:\s*"\$+', joined):
        l.is_auction = True

    # Schema.org Car extras (from streamed payload — not in inline JSON-LD)
    if not l.fuel:
        if (fm := re.search(r'"fuelType"\s*:\s*"([^"]+)"', joined)):
            l.fuel = fm.group(1)
    if (pd := re.search(r'"productionDate"\s*:\s*"([^"]+)"', joined)):
        l.production_date = pd.group(1)
    if (md := re.search(r'"vehicleModelDate"\s*:\s*"([^"]+)"', joined)):
        l.model_date = md.group(1)

    # additionalProperty: Grade, Inspection Status
    for prop in re.finditer(
        r'\{"@type":"PropertyValue","name":"([^"]+)","value":"([^"]+)"\}', joined
    ):
        name, value = prop.group(1), prop.group(2)
        if name == "Grade":
            l.grade = value
        elif name == "Inspection Status":
            l.inspection_status = value

    # Spec array (key/name/value list) — has reg date, location, engine code, etc.
    spec = _parse_spec_list(joined)
    if spec:
        l.spec = spec
        if v := spec.get("regDate"):
            l.registration_date = str(v)
        if v := spec.get("mfgDate"):
            l.production_date = str(v) or l.production_date
        if v := spec.get("modelYear"):
            l.model_date = str(v) or l.model_date
        if v := spec.get("engine"):
            l.engine_code = str(v)
        if v := spec.get("horsepower"):
            l.horsepower_ps = _to_float(str(v))
        if v := spec.get("driveType"):
            l.drive_train = str(v)
        # body_type — try spec first, then category fallback names parsed
        # from streamed payload.
        if v := spec.get("bodyType"):
            l.body_type = str(v)
        elif v := spec.get("bodyName"):
            l.body_type = str(v)
        elif v := spec.get("style"):
            l.body_type = str(v)
        if v := spec.get("doors"):
            try:
                l.doors = int(str(v))
            except ValueError:
                pass
        if v := spec.get("exteriorColor"):
            l.color = str(v).lower()
        if v := spec.get("dimension"):
            l.dimension_mm = str(v)
        if v := spec.get("weight"):
            try:
                l.curb_weight_kg = int(str(v).replace(",", ""))
            except ValueError:
                pass
        if v := spec.get("steering"):
            l.steering = str(v)
        if v := spec.get("location"):
            l.location = str(v)
        if v := spec.get("vin"):
            l.vin = str(v)
        if v := spec.get("fuel"):
            l.fuel = str(v)
        if v := spec.get("mileage"):
            try:
                # Spec mileage is the authoritative odometer reading; slug
                # value is rounded for the URL. Always prefer spec.
                l.mileage_km = int(str(v).replace(",", ""))
            except ValueError:
                pass

    # Inspection report block (reportDetailLite)
    report = _parse_report_lite(joined)
    base = report.get("baseInfo") or {}
    if base:
        l.vin_mask = base.get("vinMask", "") or ""
        if not l.grade and base.get("level"):
            l.grade = base["level"]
        l.has_inspection_report = bool(base.get("guaziReport"))
        for s in base.get("threeStateList", []) or []:
            t = s.get("title", "").lower()
            ok = not bool(s.get("state"))  # state=false means "no issues" → free=True
            if "accident" in t:
                l.accident_free = ok
            elif "water" in t:
                l.water_damage_free = ok
            elif "fire" in t:
                l.fire_damage_free = ok
    if report.get("categoryList"):
        l.inspection_categories = [
            {"name": c.get("categoryName"),
             "normal": c.get("normalCount"),
             "abnormal": c.get("abnormalCount")}
            for c in report["categoryList"]
        ]

    # Color: prefer slug match (canonical English colors guazi puts there).
    # Spec's exteriorColor may be the source-language label (e.g. Chinese
    # "白色") — slug overrides it when present.
    color_match = re.search(
        r"-(black|white|red|blue|silver|gray|grey|green|gold|brown|yellow|"
        r"orange|purple|beige|champagne|bronze|copper|pink|navy|ivory|pearl)-",
        l.slug,
    )
    if color_match:
        l.color = color_match.group(1)

    # Filter out promo/brand/tag images; keep only real car photos.
    # Junk patterns: /files/brand/, /files/tag_img/, /ovp/ (overseas marketing).
    JUNK_RE = re.compile(r"/files/|/ovp/")
    photos: list[str] = []
    if og_img := metas.get("og:image"):
        photos.append(og_img)
    for img in ld.get("image", []) or []:
        if isinstance(img, str) and img not in photos:
            photos.append(img)
    for src in re.findall(r'<img[^>]+src="(https://[^"]+\.(?:jpe?g|png|webp))', body):
        if src not in photos:
            photos.append(src)
    photos = [p for p in photos if not JUNK_RE.search(p)]
    if photos:
        l.photos = photos[:30]

    return l


def run(
    limit: int = 10,
    detail: bool = True,
    path: str = LIST_PATH,
    params: dict[str, str] | None = None,
    max_mileage_km: int | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    grades: set[str] | None = None,
) -> list[dict]:
    # The SSR path only learns grade after detail enrichment, so it over-fetches
    # candidates when a grade filter is set. The API path already has grade (and
    # filters on it in-page), so it fetches exactly `limit` — no wasted details.
    api_mode = path == LIST_PATH
    overfetch_mult = 4 if (grades and not api_mode) else 1
    target = limit * overfetch_mult
    if api_mode:
        # Default root sweep → JSON facade API (brand×series, AUT-73).
        listings = fetch_list_api(
            limit=target, params=params, min_year=min_year, max_year=max_year,
            max_mileage_km=max_mileage_km, grades=grades,
        )
    else:
        # Explicit --path override (e.g. /used-cars/sedan/) → legacy SSR walk.
        listings = fetch_list(
            limit=target, path=path, params=params,
            max_mileage_km=max_mileage_km, min_year=min_year, max_year=max_year,
        )
    if grades and len(listings) < target:
        print(
            f"[guazi] WARNING: fetched {len(listings)}/{target} candidates for "
            f"grades={sorted(grades)} — result may be smaller than --limit={limit}",
            file=sys.stderr,
        )
    if detail:
        # Parallel enrich — each worker hits its own detail URL with an
        # isolated StealthyFetcher (Chromium context). enrich_detail
        # mutates the Listing in place; we wrap it so a single failure
        # doesn't take down the pool.
        def _enrich_safe(l: Listing) -> Listing:
            try:
                enrich_detail(l)
            except Exception as e:
                l.raw["detail_error"] = repr(e)
                print(f"[guazi] err {l.url}: {e}", file=sys.stderr)
            return l

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
            # Consume the iterator to materialize mutations — order does
            # not matter because we filter + cap afterwards.
            list(ex.map(_enrich_safe, listings))
        print(f"[guazi] enriched {len(listings)} in {time.time()-t0:.1f}s "
              f"({DETAIL_WORKERS} workers)", file=sys.stderr)

    kept: list[Listing] = []
    for l in listings:
        if grades and (l.grade or "").upper() not in grades:
            print(f"[guazi] skip grade={l.grade!r} ({l.url})", file=sys.stderr)
            continue
        kept.append(l)
        if len(kept) >= limit:
            break
    return [asdict(l) for l in kept]


def _parse_filter_args(items: list[str]) -> dict[str, str]:
    """Default filters: tradeType=buyItNow (skip auctions).
    User can override via -f tradeType=sealedBid or -f tradeType=all."""
    out: dict[str, str] = {"tradeType": "buyItNow"}
    for it in items or []:
        if "=" not in it:
            continue
        k, v = it.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "tradeType" and v.lower() == "all":
            out.pop("tradeType", None)
        else:
            out[k] = v
    return out


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description=(
            "Guazi.com (en.guazi.com) scraper.\n"
            "Path filters: /used-cars/, /used-cars/<brand>/, /used-cars/<brand>/<model>/, "
            "/used-cars/<body>/ (sedan|suv|hatchback|mini-van|pick-up|truck|van|wagon).\n"
            "Query filters via -f: price=MIN,MAX  horsepower=MIN,MAX  tradeType=buyItNow|sealedBid"
        )
    )
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--no-detail", action="store_true")
    p.add_argument("--path", default=LIST_PATH)
    p.add_argument("-f", "--filter", action="append", default=[],
                   help="Repeatable. key=value, e.g. -f price=5000,15000 -f horsepower=0,160")
    p.add_argument("--max-mileage-km", type=int, default=None,
                   help="Client-side: drop listings with higher mileage")
    p.add_argument("--min-year", type=int, default=None)
    p.add_argument("--max-year", type=int, default=None)
    p.add_argument("--grades", default="",
                   help="Comma-separated Guazi grades to keep (e.g. 'S,A'). Empty = all.")
    p.add_argument("--out", default="out/guazi.json")
    args = p.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    params = _parse_filter_args(args.filter)
    grades = {g.strip().upper() for g in args.grades.split(",") if g.strip()} or None
    data = run(
        limit=args.limit, detail=not args.no_detail, path=args.path, params=params,
        max_mileage_km=args.max_mileage_km, min_year=args.min_year, max_year=args.max_year,
        grades=grades,
    )
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(data)} listings -> {args.out}")
