"""Guazi.com (English export site) scraper.

Lists at https://en.guazi.com/used-cars/, detail at /products/<slug>.html.
Geo-redirect: www.guazi.com/<city>/buy/ -> en.guazi.com for non-CN IPs.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import StealthyFetcher

from core.oxylabs import OxylabsWSA

# Oxylabs Web Scraper API endpoint for guazi's filter/list JSON. When the
# Oxylabs creds are present in env, we POST list queries through WSA to
# bypass Tencent EdgeOne's CAPTCHA wall — that gets us instant JSON in
# ~1-2s vs ~9s through StealthyFetcher's Chromium boot.
GUAZI_LIST_API = "https://en.guazi.com/os/facade/search/product/list?language=en"

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


def _build_list_url(
    path: str = LIST_PATH,
    page_num: int = 1,
    params: dict[str, str] | None = None,
) -> str:
    from urllib.parse import urlencode
    q = dict(params or {})
    if page_num and page_num != 1:
        q["page"] = str(page_num)
    base = urljoin(BASE, path)
    return base + (("?" + urlencode(q, safe=",")) if q else "")


def _params_to_api_body(params: dict[str, str] | None, page_num: int,
                         page_size: int = 30) -> dict:
    """Convert UI-style query params (price=8000,15000) to guazi's list-API
    JSON shape. Best-effort — keys we don't recognize get dropped."""
    body: dict = {
        "language": "en",
        "pageSize": page_size,
        "pageNum": page_num,
        "clientScene": "cars",
        "sourceFrom": "wap",
        "businessType": 5,
        "countryCode": "",
        # did/guid required by API. Random UUIDs accepted — no session binding.
        "guid": "11111111-2222-3333-4444-555555555555",
        "did": "DID177957158162000007",
    }
    for k, v in (params or {}).items():
        if k == "price" and "," in v:
            lo, hi = v.split(",", 1)
            if lo: body["priceStart"] = int(lo)
            if hi: body["priceEnd"] = int(hi)
        elif k == "licenseYear" and "," in v:
            lo, hi = v.split(",", 1)
            if lo: body["licenseYearStart"] = int(lo)
            if hi: body["licenseYearEnd"] = int(hi)
        elif k == "roadHaul" and "," in v:
            lo, hi = v.split(",", 1)
            if lo: body["roadHaulStart"] = int(lo)
            if hi: body["roadHaulEnd"] = int(hi)
        elif k == "detectionLevels":
            body["detectionLevels"] = v.split(",")
        elif k == "vehicleSourceClassificationCustomers":
            body["vehicleSourceClassificationCustomers"] = v.split(",")
    return body


def _hrefs_from_api(json_body: str | None) -> list[str]:
    """Extract /products/<slug>.html paths from guazi list-API JSON.
    Returns empty list if input isn't valid JSON with the expected shape."""
    if not json_body:
        return []
    try:
        data = json.loads(json_body)
    except json.JSONDecodeError:
        return []
    rows = ((data.get("data") or {}).get("list") or
            (data.get("data") or {}).get("records") or
            data.get("list") or [])
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Try several known shapes for slug / url field.
        slug = row.get("slug") or row.get("productSlug") or row.get("seoSlug")
        url = row.get("url") or row.get("productUrl")
        clue = row.get("clueId") or row.get("clueid") or row.get("productId")
        if slug:
            out.append(f"/products/{slug}.html")
        elif url and "/products/" in url:
            out.append(url[url.index("/products/"):])
        elif clue:
            out.append(f"/products/{clue}.html")
    return out


def fetch_list(
    limit: int = 10,
    path: str = LIST_PATH,
    params: dict[str, str] | None = None,
    max_pages: int = 50,
    max_mileage_km: int | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
) -> list[Listing]:
    out: list[Listing] = []
    seen: set[str] = set()
    skipped = 0
    page_num = 1
    wsa = OxylabsWSA()
    use_api = wsa.enabled
    if use_api:
        print(f"[guazi] list source: Oxylabs WSA -> JSON API (geo={wsa.geo!r})",
              file=sys.stderr)
    else:
        print(f"[guazi] list source: StealthyFetcher (no Oxylabs creds)",
              file=sys.stderr)
    while len(out) < limit and page_num <= max_pages:
        hrefs: list[str] = []
        if use_api:
            api_body = _params_to_api_body(params, page_num, page_size=max(20, limit))
            t0 = time.time()
            resp = wsa.post_json(
                GUAZI_LIST_API,
                api_body,
                headers={"content-type": "application/json",
                         "origin": "https://en.guazi.com",
                         "referer": "https://en.guazi.com/used-cars/"},
            )
            print(f"[guazi] list p{page_num} API: {time.time()-t0:.1f}s "
                  f"({len(resp) if resp else 0} bytes)", file=sys.stderr)
            if resp:
                # Debug: first 300 chars of response so we can see what guazi
                # said when API returns 0 hrefs.
                preview = resp.replace("\n", " ").replace("\r", " ")[:300]
                print(f"[guazi]   response preview: {preview}", file=sys.stderr)
            hrefs = [h for h in _hrefs_from_api(resp) if h not in seen]
            if not hrefs:
                # API returned nothing usable (captcha / shape change) —
                # fall back to HTML scrape for this page.
                print(f"[guazi] API gave 0 hrefs on p{page_num}, falling back to Stealthy",
                      file=sys.stderr)
                use_api = False

        if not hrefs:
            url = _build_list_url(path, page_num, params)
            print(f"[guazi] list p{page_num}: {url}", file=sys.stderr)
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,       # Phase 1 disabled this — but the
                humanize=True,           # listings JSON is streamed in late,
                wait=2500,               # so without network-idle we get an
                disable_resources=True,  # empty body. Keep disable_resources.
                timeout=30000,
            )
            body = page.body.decode("utf-8", "replace")
            hrefs = [h for h in DETAIL_HREF_RE.findall(body) if h not in seen]
            print(f"[guazi] HTML body {len(body)} bytes, {len(hrefs)} hrefs",
                  file=sys.stderr)
        if not hrefs:
            print(f"[guazi] no new hrefs on p{page_num}, stop", file=sys.stderr)
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
    print(f"[guazi] total parsed: {len(out)} (skipped {skipped} by client filters)",
          file=sys.stderr)
    return out


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


def enrich_detail(l: Listing) -> Listing:
    print(f"[guazi] detail: {l.url}", file=sys.stderr)
    page = StealthyFetcher.fetch(
        l.url,
        headless=True,
        network_idle=True,
        humanize=True,
        wait=2000,
        disable_resources=True,
        timeout=30000,
    )
    status = getattr(page, "status", None)
    l.raw["detail_status"] = status
    # Skip parsing on HTTP errors — the body is an error page, not the listing.
    if status and isinstance(status, int) and status >= 400:
        print(f"[guazi] detail HTTP {status} for {l.url} — skip enrichment",
              file=sys.stderr)
        return l
    body = page.body.decode("utf-8", "replace")

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
    # if grade filter is requested, we must over-fetch candidates because
    # grade is known only after detail enrichment
    overfetch_mult = 4 if grades else 1
    target = limit * overfetch_mult
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
