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


def fetch_list(limit: int = 10, path: str = LIST_PATH) -> list[Listing]:
    url = urljoin(BASE, path)
    print(f"[guazi] list: {url}", file=sys.stderr)
    page = StealthyFetcher.fetch(
        url, headless=True, network_idle=True, humanize=True, wait=2500
    )
    print(f"[guazi] list status={page.status} bytes={len(page.body)}", file=sys.stderr)
    body = page.body.decode("utf-8", "replace")
    hrefs = []
    seen = set()
    for href in DETAIL_HREF_RE.findall(body):
        if href in seen:
            continue
        seen.add(href)
        hrefs.append(href)
        if len(hrefs) >= limit:
            break
    print(f"[guazi] detail hrefs: {len(hrefs)}", file=sys.stderr)
    out: list[Listing] = []
    for h in hrefs:
        slug = Path(urlparse(h).path).stem
        parsed = parse_slug(slug)
        l = Listing(url=urljoin(BASE, h), slug=slug, **parsed) if parsed else Listing(
            url=urljoin(BASE, h), slug=slug, listing_id=slug
        )
        out.append(l)
    return out


JSONLD_RE = re.compile(
    r'<script\s+type="application/ld\+json">\s*(\{.*?\})\s*</script>', re.DOTALL
)
META_RE = re.compile(
    r'<meta\s+(?:name|property)="([^"]+)"\s+content="([^"]*)"', re.IGNORECASE
)
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


def enrich_detail(l: Listing) -> Listing:
    print(f"[guazi] detail: {l.url}", file=sys.stderr)
    page = StealthyFetcher.fetch(
        l.url, headless=True, network_idle=True, humanize=True, wait=2000
    )
    l.raw["detail_status"] = page.status
    body = page.body.decode("utf-8", "replace")

    metas = _parse_metas(body)
    ld = _parse_jsonld(body)

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


def run(limit: int = 10, detail: bool = True, path: str = LIST_PATH) -> list[dict]:
    listings = fetch_list(limit=limit, path=path)
    print(f"[guazi] parsed: {len(listings)}", file=sys.stderr)
    if detail:
        for l in listings:
            try:
                enrich_detail(l)
                time.sleep(0.6)
            except Exception as e:
                l.raw["detail_error"] = repr(e)
                print(f"[guazi] err {l.url}: {e}", file=sys.stderr)
    return [asdict(l) for l in listings]


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--no-detail", action="store_true")
    p.add_argument("--path", default=LIST_PATH, help="e.g. /used-cars/ or /used-cars/toyota/")
    p.add_argument("--out", default="out/guazi.json")
    args = p.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    data = run(limit=args.limit, detail=not args.no_detail, path=args.path)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(data)} listings -> {args.out}")
