"""Encar.com (Korea) scraper. Direct JSON API, no browser needed.

List API: api.encar.com/search/car/list/premium
Detail:   api.encar.com/v1/readside/vehicle/{id}
Photos:   https://ci.encar.com<Photo path>
Prices in 만원 (10,000 KRW).
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

LIST_URL = "https://api.encar.com/search/car/list/premium"
DETAIL_URL = "http://api.encar.com/v1/readside/vehicle/{id}"
PHOTO_HOST = "https://ci.encar.com"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko,en-US;q=0.7,en;q=0.3",
    "Origin": "http://www.encar.com",
    "Referer": "http://www.encar.com/",
}


@dataclass
class Listing:
    site: str = "encar"
    listing_id: str = ""
    url: str = ""
    title: str = ""
    brand: str = ""
    model: str = ""
    badge: str = ""
    year: int | None = None
    year_month: str = ""
    mileage_km: int | None = None
    fuel: str = ""
    transmission: str = ""
    color: str = ""
    seats: int | None = None
    displacement_cc: int | None = None
    engine_l: float | None = None
    body_type: str = ""
    price_amount: float | None = None
    currency: str = "KRW"
    new_price_krw: int | None = None
    vin: str = ""
    city: str = ""
    country: str = "Korea"
    steering: str = "Left"
    accident_free: bool | None = None
    no_water_damage: bool | None = None
    owners_count: int | None = None
    has_inspection_report: bool | None = None
    published_at: str = ""
    options_standard: list[str] = field(default_factory=list)
    options_extra: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _photo_urls(photos: list[dict]) -> list[str]:
    out: list[str] = []
    for p in photos or []:
        loc = p.get("location") if isinstance(p, dict) else None
        if not loc:
            continue
        # Guard against absolute URLs (rare but possible) — only prefix
        # PHOTO_HOST when the path starts with `/`.
        if loc.startswith("http://") or loc.startswith("https://"):
            out.append(loc)
        else:
            out.append(PHOTO_HOST + (loc if loc.startswith("/") else "/" + loc))
    return out


def _iso_published(raw: str) -> str | None:
    """Encar ModifiedDate looks like '2026-05-31 06:24:02.000 +09'.
    Convert to ISO-8601 so Postgres timestamptz parses it cleanly.
    Returns None for empty/unparseable input.
    """
    if not raw:
        return None
    s = raw.strip()
    # Drop milliseconds: '06:24:02.000 +09' -> '06:24:02 +09'
    s = re.sub(r"\.\d+\b", "", s)
    # Replace space-before-tz with nothing, then normalize tz: '+09' -> '+09:00'
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*([+\-]\d{2}):?(\d{2})?$",
        s,
    )
    if not m:
        return s if "T" in s else None  # already ISO? best effort.
    date_part, time_part, tz_hour, tz_min = m.groups()
    return f"{date_part}T{time_part}{tz_hour}:{tz_min or '00'}"


def _get_with_retry(client: httpx.Client, url: str, *, params=None,
                    headers=None, timeout: float = 30.0,
                    max_attempts: int = 3) -> httpx.Response:
    """GET with exponential backoff on 429/503 / network errors."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            r = client.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code in (429, 503):
                wait = (2 ** attempt) * 1.0
                print(f"[encar] {r.status_code} on {url} — backoff {wait}s "
                      f"(attempt {attempt+1}/{max_attempts})", file=sys.stderr)
                time.sleep(wait)
                continue
            return r
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exc = e
            wait = (2 ** attempt) * 1.5
            print(f"[encar] {type(e).__name__} on {url} — backoff {wait}s "
                  f"(attempt {attempt+1}/{max_attempts})", file=sys.stderr)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    # All attempts hit 429/503 — return last response so caller decides.
    return r


def fetch_list(
    client: httpx.Client,
    query: str = "(And.Hidden.N._.CarType.Y.)",
    sort: str = "ModifiedDate",
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    sr = f"|{sort}|{offset}|{limit}"
    params = {"count": "true", "q": query, "sr": sr}
    print(f"[encar] list: {LIST_URL} q={query} sr={sr}", file=sys.stderr)
    r = _get_with_retry(client, LIST_URL, params=params, headers=DEFAULT_HEADERS)
    r.raise_for_status()
    data = r.json()
    print(f"[encar] total={data.get('Count')} got={len(data.get('SearchResults', []))}",
          file=sys.stderr)
    return data.get("SearchResults", [])


def fetch_detail(client: httpx.Client, listing_id: str) -> dict:
    url = DETAIL_URL.format(id=listing_id)
    try:
        r = _get_with_retry(client, url, headers=DEFAULT_HEADERS)
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"_err": f"http {r.status_code}"}
    try:
        return r.json()
    except Exception as e:
        return {"_err": f"json: {e}"}


def build_listing(card: dict, detail: dict | None = None) -> Listing:
    cid = str(card.get("Id", ""))
    year_raw = card.get("Year")
    year = None
    year_month = ""
    if isinstance(year_raw, (int, float)):
        yr_int = int(year_raw)
        year = yr_int // 100
        month = yr_int % 100
        year_month = f"{year}-{month:02d}" if 1 <= month <= 12 else str(year)
    elif card.get("FormYear"):
        try:
            year = int(card["FormYear"])
        except ValueError:
            pass

    price_man = card.get("Price")
    title_parts = [card.get("Manufacturer", ""), card.get("Model", ""), card.get("Badge", "")]
    title = " ".join(p for p in title_parts if p).strip()

    l = Listing(
        listing_id=cid,
        url=f"http://www.encar.com/dc/dc_cardetailview.do?carid={cid}",
        title=title,
        brand=card.get("Manufacturer", ""),
        model=card.get("Model", ""),
        badge=card.get("Badge", ""),
        year=year,
        year_month=year_month,
        mileage_km=int(card["Mileage"]) if card.get("Mileage") else None,
        fuel=card.get("FuelType", ""),
        transmission=card.get("Transmission", ""),
        price_amount=int(float(price_man) * 10000) if price_man is not None else None,
        city=card.get("OfficeCityState", ""),
        published_at=_iso_published(card.get("ModifiedDate", "")) or "",
        photos=_photo_urls(card.get("Photos", []))[:30],
    )
    l.raw["list_keys"] = sorted(card.keys())

    if detail and "_err" not in detail:
        l.vin = detail.get("vin", "") or ""
        spec = detail.get("spec") or {}
        cat = detail.get("category") or {}
        cond = detail.get("condition") or {}

        if spec.get("colorName"):
            l.color = spec["colorName"]
        if spec.get("seatCount"):
            try:
                l.seats = int(spec["seatCount"])
            except (TypeError, ValueError):
                pass
        if spec.get("displacement"):
            try:
                cc = int(spec["displacement"])
                l.displacement_cc = cc
                l.engine_l = round(cc / 1000, 1) if cc else None
            except (TypeError, ValueError):
                pass
        # Encar's spec.bodyName mixes real body shapes (SUV/RV/Van) with sedan
        # size classes (Full-size / Mid-size / Light / Compact). Map size
        # classes to "Sedan" so downstream gets a canonical body.
        raw_body = spec.get("bodyName") or ""
        ENCAR_BODY = {
            "SUV": "SUV",
            "RV": "Minivan",        # Recreational Vehicle = MPV in Korea
            "Van": "Minivan",
            "Cargo": "Minivan",
            "Box": "Minivan",
            "Full-size": "Sedan",
            "Mid-size": "Sedan",
            "Light": "Sedan",       # 경차 = "Light Car" — kei-class sedan
            "Compact": "Sedan",
            "Sub-compact": "Hatchback",
            "Small": "Hatchback",
            "Wagon": "Wagon",
            "Sports": "Coupe",
            "Coupe": "Coupe",
            "Truck": "Truck",       # pickup (Bongo / Porter)
            "Pickup": "Truck",
        }
        if raw_body:
            l.body_type = ENCAR_BODY.get(raw_body, raw_body)
        if cat.get("originPrice"):
            try:
                l.new_price_krw = int(float(cat["originPrice"]) * 10000)
            except (TypeError, ValueError):
                pass

        # accident report: condition.accident = {"count": N, "items": [...]}
        acc = cond.get("accident") or {}
        if isinstance(acc, dict):
            cnt = acc.get("count")
            if cnt is not None:
                try:
                    l.accident_free = (int(cnt) == 0)
                except (TypeError, ValueError):
                    pass
            oc = acc.get("ownerChanged")
            if oc is not None:
                try:
                    l.owners_count = int(oc)
                except (TypeError, ValueError):
                    pass
        insp = cond.get("inspection") or {}
        l.has_inspection_report = bool(insp)

        # Steering: prefer condition/spec field over the LHD default.
        steer = (cond.get("steering") or spec.get("handle") or "").lower()
        if "right" in steer or "rhd" in steer or "우" in steer:
            l.steering = "Right"
        elif "left" in steer or "lhd" in steer or "좌" in steer:
            l.steering = "Left"

        opts = detail.get("options") or {}
        if isinstance(opts, dict):
            l.options_standard = list(opts.get("standard") or [])
            l.options_extra = list(opts.get("etc") or opts.get("choice") or [])
        det_photos = _photo_urls(detail.get("photos") or [])
        if det_photos:
            l.photos = det_photos[:30]  # cap at 30 like guazi
        l.raw["detail_status"] = "ok"
    elif detail:
        l.raw["detail_status"] = detail.get("_err", "err")

    return l


def build_query(
    min_year: int | None = None,
    max_year: int | None = None,
    min_price_man: int | None = None,
    max_price_man: int | None = None,
    max_mileage_km: int | None = None,
    min_mileage_km: int | None = None,
    fuel: str | None = None,            # 가솔린|디젤|LPG|하이브리드|전기
    transmission: str | None = None,    # 오토|수동
    manufacturer: str | None = None,    # 기아|현대|제네시스|...
    only_inspection: bool = False,
) -> str:
    """Compose encar `q=` expression (dot-tree syntax)."""
    clauses = ["Hidden.N.", "CarType.Y."]

    if min_year is not None or max_year is not None:
        lo = f"{min_year}01" if min_year else ""
        hi = f"{max_year}12" if max_year else ""
        clauses.append(f"Year.range({lo}..{hi}).")
    if min_price_man is not None or max_price_man is not None:
        lo = str(min_price_man) if min_price_man is not None else ""
        hi = str(max_price_man) if max_price_man is not None else ""
        clauses.append(f"Price.range({lo}..{hi}).")
    if max_mileage_km is not None or min_mileage_km is not None:
        lo = str(min_mileage_km) if min_mileage_km is not None else "0"
        hi = str(max_mileage_km) if max_mileage_km is not None else ""
        clauses.append(f"Mileage.range({lo}..{hi}).")
    if fuel:
        clauses.append(f"FuelType.{fuel}.")
    if transmission:
        clauses.append(f"Transmission.{transmission}.")
    if manufacturer:
        clauses.append(f"Manufacturer.{manufacturer}.")
    if only_inspection:
        clauses.append("Trust.Inspection.")

    return "(And." + "_.".join(clauses) + ")"


def run(limit: int = 10, query: str | None = None,
        offset: int = 0, detail: bool = True, **filter_kw) -> list[dict]:
    if query is None:
        query = build_query(**filter_kw)
    with httpx.Client(follow_redirects=True) as client:
        cards = fetch_list(client, query=query, limit=limit, offset=offset)
        out: list[Listing] = []
        for card in cards:
            cid = str(card.get("Id", ""))
            det = fetch_detail(client, cid) if detail and cid else None
            out.append(build_listing(card, det))
            if detail:
                time.sleep(0.4)
    return [asdict(l) for l in out]


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--query", default=None,
                   help="Raw encar filter expression. If set, overrides all --min-*/--max-* flags.")
    p.add_argument("--min-year", type=int)
    p.add_argument("--max-year", type=int)
    p.add_argument("--min-price-man", type=int,
                   help="Min price in 만원 (1만원=10000 KRW). $8000 ≈ 1100")
    p.add_argument("--max-price-man", type=int)
    p.add_argument("--max-mileage-km", type=int)
    p.add_argument("--min-mileage-km", type=int)
    p.add_argument("--fuel", default=None,
                   help="가솔린|디젤|LPG|하이브리드|전기")
    p.add_argument("--transmission", default=None, help="오토|수동")
    p.add_argument("--manufacturer", default=None,
                   help="기아|현대|제네시스|쉐보레|쌍용|르노삼성 ...")
    p.add_argument("--only-inspection", action="store_true",
                   help="Only listings with Trust.Inspection")
    p.add_argument("--no-detail", action="store_true")
    p.add_argument("--out", default="out/encar.json")
    args = p.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    data = run(limit=args.limit, offset=args.offset, query=args.query,
               detail=not args.no_detail,
               min_year=args.min_year, max_year=args.max_year,
               min_price_man=args.min_price_man, max_price_man=args.max_price_man,
               max_mileage_km=args.max_mileage_km, min_mileage_km=args.min_mileage_km,
               fuel=args.fuel, transmission=args.transmission,
               manufacturer=args.manufacturer, only_inspection=args.only_inspection)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(data)} listings -> {args.out}")
