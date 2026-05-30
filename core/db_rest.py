"""Upsert scraper output into Supabase via PostgREST.

Uses anon key (RLS disabled). Resolves brand_id/model_id via REST GET, then bulk POSTs cars.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path
import httpx

SUPABASE_URL = "https://pdmbdclhqiqyoomeswxs.supabase.co"
DEFAULT_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBkbWJkY2xocWlxeW9vbWVzd3hzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg2OTIyMDksImV4cCI6MjA5NDI2ODIwOX0.mgWZwf0EKutCllKwdmB6yB3NJSdFOCdfVmPBL55g89M"
SOURCE_TAG = "manual"  # tag for brands/models we have to create (when not in existing catalog)


def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


def fuel_map(raw: str) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    if any(k in r for k in ("bev", "electric")):
        return "electric"
    if "phev" in r or "plug-in" in r:
        return "phev"
    if "hev" in r or "hybrid" in r or "reev" in r:
        return "hybrid"
    if "diesel" in r:
        return "diesel"
    if "gasoline" in r or "petrol" in r:
        return "gasoline"
    return r


def headers(key: str, prefer: str = "") -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def fetch_brand_map(client, key) -> dict[str, int]:
    """slug -> id, preferring guazi-scraper source."""
    r = client.get(f"{SUPABASE_URL}/rest/v1/brands",
                   params={"select": "id,slug,source"}, headers=headers(key))
    r.raise_for_status()
    data = r.json()
    out: dict[str, int] = {}
    # prefer scraper rows, else any
    for row in sorted(data, key=lambda b: (b["source"] != SOURCE_TAG, b["id"])):
        out.setdefault(row["slug"], row["id"])
    return out


def fetch_model_map(client, key, brand_map) -> dict[tuple, int]:
    """(brand_slug, model_slug) -> model_id."""
    r = client.get(f"{SUPABASE_URL}/rest/v1/models",
                   params={"select": "id,slug,brand_id,source"}, headers=headers(key))
    r.raise_for_status()
    data = r.json()
    # Need brand_id -> brand_slug reverse map
    bid_to_slug = {bid: slug for slug, bid in brand_map.items()}
    out: dict[tuple, int] = {}
    for row in sorted(data, key=lambda m: (m["source"] != SOURCE_TAG, m["id"])):
        bslug = bid_to_slug.get(row["brand_id"])
        if not bslug:
            continue
        key_ = (bslug, row["slug"])
        out.setdefault(key_, row["id"])
    return out


def build_car_row(r: dict, source: str, brand_map: dict, model_map: dict) -> dict | None:
    mark = r.get("mark") or r.get("brand_canonical") or r.get("brand") or ""
    mf = r.get("model_family") or r.get("model") or ""
    if not (mark and mf):
        return None
    bslug = slugify(mark)
    mslug = slugify(mf)
    brand_id = brand_map.get(bslug)
    if not brand_id:
        print(f"[db] no brand_id for {bslug!r} ({mark}), skipping", file=sys.stderr)
        return None
    model_id = model_map.get((bslug, mslug))
    reg = r.get("registration_date") or ""
    reg_date = reg.replace(".", "-") + "-01" if reg and len(reg) == 7 else None
    loc = r.get("location") or ""
    city = loc.split(",")[0].strip() if loc else None
    country = loc.split(",")[-1].strip() if "," in loc else None
    return {
        "source": source,
        "source_id": r.get("listing_id") or r.get("source_id") or r.get("id"),
        "url": r.get("url"),
        "title": r.get("title"),
        "mark_original": r.get("brand"),
        "mark": mark,
        "model_family": mf,
        "model": r.get("model") or mf,
        "complectation": r.get("complectation") or r.get("trim") or r.get("badge"),
        "year": r.get("year"),
        "price_original": r.get("price_amount"),
        "price_currency": r.get("currency"),
        "km_age_unit": "km",
        "km_age": r.get("mileage_km"),
        "color": r.get("color"),
        "body_type": r.get("body_type"),
        "engine_type": fuel_map(r.get("fuel") or ""),
        "fuel_original": r.get("fuel"),
        "transmission_type": r.get("gearbox") or r.get("transmission"),
        "drive_type": r.get("drive"),
        "displacement": r.get("engine_l"),
        "horse_power": int(r.get("horsepower_ps")) if r.get("horsepower_ps") else None,
        "city": city,
        "country": country,
        "reg_date": reg_date,
        "vin": r.get("vin") or r.get("vin_mask"),
        "inspection_grade": r.get("grade"),
        "accident_free": r.get("accident_free"),
        "no_water_damage": r.get("water_damage_free"),
        "no_fire_damage": r.get("fire_damage_free"),
        "steering": r.get("steering"),
        "seats": r.get("seats"),
        "images": r.get("photos"),
        "image_count": len(r.get("photos") or []) or None,
        "inspection_data": r.get("inspection_categories"),
        "source_data": {k: v for k, v in r.items()
                        if k not in ("photos", "inspection_categories", "spec", "raw")},
        "brand_id": brand_id,
        "model_id": model_id,
    }


def upsert_cars(client, key: str, rows: list[dict], batch: int = 25) -> int:
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        r = client.post(
            f"{SUPABASE_URL}/rest/v1/cars",
            params={"on_conflict": "source,source_id"},
            headers=headers(key, "resolution=merge-duplicates,return=representation"),
            json=chunk,
        )
        if r.status_code not in (200, 201):
            print(f"[db] batch {i//batch} failed: {r.status_code} {r.text[:500]}",
                  file=sys.stderr)
            r.raise_for_status()
        n += len(chunk)
        print(f"[db] upserted {n}/{len(rows)}", file=sys.stderr)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--source", default=None)
    ap.add_argument("--key", default=os.getenv("SUPABASE_KEY", DEFAULT_KEY))
    args = ap.parse_args()

    records = json.loads(Path(args.input).read_text())
    source = args.source or records[0].get("site") or "unknown"

    with httpx.Client(timeout=60.0) as client:
        bmap = fetch_brand_map(client, args.key)
        mmap = fetch_model_map(client, args.key, bmap)
        print(f"[db] brand_map: {len(bmap)} entries", file=sys.stderr)
        print(f"[db] model_map: {len(mmap)} entries", file=sys.stderr)
        rows = [build_car_row(r, source, bmap, mmap) for r in records]
        rows = [r for r in rows if r]
        print(f"[db] {len(rows)} cars to upsert", file=sys.stderr)
        n = upsert_cars(client, args.key, rows)
        print(f"[db] done: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
