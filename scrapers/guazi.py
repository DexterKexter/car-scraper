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
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import StealthyFetcher

BASE = "https://en.guazi.com"
LIST_PATH = "/used-cars/"
DETAIL_HREF_RE = re.compile(r'href="(/products/[^"?]+?\.html)')
SLUG_RE = re.compile(
    r"^(?P<brand>[a-z\-]+?)-(?P<model>[a-z0-9\-]+?)-(?P<year>(?:19|20)\d{2})-"
    r"(?P<engine>[\d.]+l)-(?:[a-z]+-)?(?P<mileage>\d+)km-"
    r"(?P<gear>at|mt|cvt|amt|dct)(?:-(?P<drive>2wd|4wd|awd))?-(?P<seats>\d+)-seats-"
    r"(?P<id>[a-z0-9]+)$"
)

MULTI_WORD_BRANDS = {
    "land-rover", "mercedes-benz", "geely-auto", "alfa-romeo", "aston-martin",
    "rolls-royce", "great-wall", "wuling-hongguang", "saic-roewe", "dongfeng-aeolus",
    "saic-maxus", "gac-trumpchi", "faw-bestune", "chery-jetour", "beijing-auto",
    "lynk-co", "smart-brabus",
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
    mileage_km: int | None = None
    gearbox: str = ""
    drive: str = ""
    seats: int | None = None
    color: str = ""
    fuel: str = ""
    production_date: str = ""
    model_date: str = ""
    grade: str = ""
    vin_mask: str = ""
    accident_free: bool | None = None
    water_damage_free: bool | None = None
    fire_damage_free: bool | None = None
    has_inspection_report: bool | None = None
    inspection_categories: list[dict] = field(default_factory=list)
    inspection_status: str = ""
    price_raw: str = ""
    price_wan_yuan: float | None = None
    price_usd: float | None = None
    location: str = ""
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
    while len(out) < limit and page_num <= max_pages:
        url = _build_list_url(path, page_num, params)
        print(f"[guazi] list p{page_num}: {url}", file=sys.stderr)
        page = StealthyFetcher.fetch(
            url, headless=True, network_idle=True, humanize=True, wait=2500
        )
        body = page.body.decode("utf-8", "replace")
        hrefs = [h for h in DETAIL_HREF_RE.findall(body) if h not in seen]
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


JSONLD_RE = re.compile(
    r'<script\s+type="application/ld\+json">\s*(\{.*?\})\s*</script>', re.DOTALL
)
META_RE = re.compile(
    r'<meta\s+(?:name|property)="([^"]+)"\s+content="([^"]*)"', re.IGNORECASE
)
NEXT_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
REPORT_LITE_KEY = '"reportDetailLite"'
INQUIRE_PRICE = 9999999.0


def _parse_jsonld(body: str) -> dict:
    for m in JSONLD_RE.finditer(body):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Car":
            return data
    return {}


def _parse_metas(body: str) -> dict[str, str]:
    return {k.lower(): v for k, v in META_RE.findall(body)}


def _join_next_chunks(body: str) -> str:
    chunks = NEXT_CHUNK_RE.findall(body)
    if not chunks:
        return ""
    return "".join(chunks).encode("utf-8", "replace").decode("unicode_escape", "ignore")


def _extract_object_at(text: str, start: int) -> str | None:
    """Read a balanced JSON object starting at the first '{' after `start`."""
    try:
        open_at = text.index("{", start)
    except ValueError:
        return None
    depth = 0
    for i in range(open_at, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    return None


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


def enrich_detail(l: Listing) -> Listing:
    print(f"[guazi] detail: {l.url}", file=sys.stderr)
    page = StealthyFetcher.fetch(
        l.url, headless=True, network_idle=True, humanize=True, wait=2000
    )
    l.raw["detail_status"] = page.status
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
    currency = offers.get("priceCurrency") or metas.get("product:price:currency") or ""
    if raw_price:
        price_f = _to_float(str(raw_price).replace(",", ""))
        if price_f == INQUIRE_PRICE:
            l.price_raw = "inquire"
        else:
            l.price_raw = f"{currency or '$'}{raw_price}"
            if currency == "USD":
                l.price_usd = price_f
            elif currency in {"CNY", "RMB"}:
                if price_f is not None:
                    l.price_wan_yuan = price_f / 10000

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

    # Color: prefer slug match
    color_match = re.search(
        r"-(black|white|red|blue|silver|gray|grey|green|gold|brown|yellow|orange|purple)-",
        l.slug,
    )
    if color_match:
        l.color = color_match.group(1)

    photos: list[str] = []
    if og_img := metas.get("og:image"):
        photos.append(og_img)
    for img in ld.get("image", []) or []:
        if isinstance(img, str) and img not in photos:
            photos.append(img)
    for src in re.findall(r'<img[^>]+src="(https://[^"]+\.(?:jpe?g|png|webp))', body):
        if src not in photos:
            photos.append(src)
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
) -> list[dict]:
    listings = fetch_list(
        limit=limit, path=path, params=params,
        max_mileage_km=max_mileage_km, min_year=min_year, max_year=max_year,
    )
    if detail:
        for l in listings:
            try:
                enrich_detail(l)
                time.sleep(0.6)
            except Exception as e:
                l.raw["detail_error"] = repr(e)
                print(f"[guazi] err {l.url}: {e}", file=sys.stderr)
    return [asdict(l) for l in listings]


def _parse_filter_args(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            continue
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
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
    p.add_argument("--out", default="out/guazi.json")
    args = p.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    params = _parse_filter_args(args.filter)
    data = run(
        limit=args.limit, detail=not args.no_detail, path=args.path, params=params,
        max_mileage_km=args.max_mileage_km, min_year=args.min_year, max_year=args.max_year,
    )
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(data)} listings -> {args.out}")
