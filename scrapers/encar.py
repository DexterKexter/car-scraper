"""Encar.com (Korea) scraper. Direct JSON API, no browser needed.

List API: api.encar.com/search/car/list/premium
Detail:   api.encar.com/v1/readside/vehicle/{id}
Photos:   https://ci.encar.com<Photo path>
Prices in 만원 (10,000 KRW).
"""
from __future__ import annotations

import json
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
    price_amount: float | None = None
    currency: str = "KRW"
    vin: str = ""
    city: str = ""
    options_standard: list[str] = field(default_factory=list)
    options_extra: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _photo_urls(photos: list[dict]) -> list[str]:
    out: list[str] = []
    for p in photos or []:
        loc = p.get("location") if isinstance(p, dict) else None
        if loc:
            out.append(PHOTO_HOST + loc)
    return out


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
    r = client.get(LIST_URL, params=params, headers=DEFAULT_HEADERS, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    print(f"[encar] total={data.get('Count')} got={len(data.get('SearchResults', []))}",
          file=sys.stderr)
    return data.get("SearchResults", [])


def fetch_detail(client: httpx.Client, listing_id: str) -> dict:
    url = DETAIL_URL.format(id=listing_id)
    r = client.get(url, headers=DEFAULT_HEADERS, timeout=30.0)
    if r.status_code != 200:
        return {"_err": f"http {r.status_code}"}
    return r.json()


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
        photos=_photo_urls(card.get("Photos", [])),
    )
    l.raw["list_keys"] = sorted(card.keys())

    if detail and "_err" not in detail:
        l.vin = detail.get("vin", "") or ""
        cat = detail.get("category") or {}
        if cat.get("colorName"):
            l.color = cat["colorName"]
        opts = detail.get("options") or {}
        if isinstance(opts, dict):
            l.options_standard = list(opts.get("standard") or [])
            l.options_extra = list(opts.get("etc") or opts.get("choice") or [])
        det_photos = _photo_urls(detail.get("photos") or [])
        if det_photos:
            l.photos = det_photos
        l.raw["detail_status"] = "ok"
    elif detail:
        l.raw["detail_status"] = detail.get("_err", "err")

    return l


def run(limit: int = 10, query: str = "(And.Hidden.N._.CarType.Y.)",
        offset: int = 0, detail: bool = True) -> list[dict]:
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
    p.add_argument("--query", default="(And.Hidden.N._.CarType.Y.)",
                   help="Encar filter expression, e.g. (And.Hidden.N._.(C.CarType.Y._.Manufacturer.기아.))")
    p.add_argument("--no-detail", action="store_true")
    p.add_argument("--out", default="out/encar.json")
    args = p.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    data = run(limit=args.limit, offset=args.offset, query=args.query,
               detail=not args.no_detail)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(data)} listings -> {args.out}")
