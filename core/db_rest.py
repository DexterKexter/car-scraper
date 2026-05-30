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
    """Normalize source fuel string to one of: electric, phev, hybrid,
    diesel, gasoline, lpg, cng. Returns None for empty input.

    Handles English (guazi_en), Chinese (guazi raw), and Korean (encar).
    Korean fuel labels from encar:
      가솔린         = gasoline
      디젤           = diesel
      전기           = electric
      가솔린+전기    = gasoline+electric → hybrid
      가솔린(하이브리드) = hybrid
      LPG / LPG(일반인 구입) = lpg
      CNG            = cng
      수소           = hydrogen
    """
    if not raw:
        return None
    r = raw.lower()
    # Order matters: PHEV/hybrid checks before bare gasoline/electric.
    if "phev" in r or "plug-in" in r or "플러그인" in r:
        return "phev"
    if (
        "hev" in r or "hybrid" in r or "reev" in r
        or "하이브리드" in r
        or "+전기" in r or "전기+" in r  # 가솔린+전기 / 디젤+전기
    ):
        return "hybrid"
    if "bev" in r or "electric" in r or "전기" in r:
        return "electric"
    if "diesel" in r or "디젤" in r:
        return "diesel"
    if "gasoline" in r or "petrol" in r or "가솔린" in r:
        return "gasoline"
    if "lpg" in r:
        return "lpg"
    if "cng" in r:
        return "cng"
    if "수소" in r or "hydrogen" in r:
        return "hydrogen"
    return r


def transmission_map(raw: str | None) -> str | None:
    """Normalize raw transmission to one of: automatic, manual, cvt, dct, amt.

    Sources:
      guazi (slug):    AT / MT / CVT / DCT / AMT
      encar (Korean):  오토 / 자동 → automatic;  수동 → manual;  CVT / DCT
      che168 (CN):     自动 / 手动 / 双离合 (DCT) / 无级 (CVT) / AMT
    """
    if not raw:
        return None
    r = raw.lower().strip()
    if not r:
        return None
    # DCT / dual-clutch first — 'dsg' is VW's brand for DCT.
    if "dct" in r or "dsg" in r or "双离合" in r or "듀얼클러치" in r:
        return "dct"
    if "cvt" in r or "무단" in r or "무 단" in r or "无级" in r:
        return "cvt"
    if "amt" in raw.upper():
        return "amt"
    if (
        r in ("mt", "m/t")
        or "manual" in r or "수동" in r or "手动" in r or "механ" in r
    ):
        return "manual"
    if (
        r in ("at", "a/t")
        or "auto" in r or "오토" in r or "자동" in r or "自动" in r or "автомат" in r
    ):
        return "automatic"
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


def ensure_brand(client, key, slug: str, name: str, kolesa_slug: str | None) -> int:
    """Find or create brand. Returns brand_id."""
    payload = [{"slug": slug, "name": name, "source": SOURCE_TAG,
                "kolesa_slug": kolesa_slug, "country": "China"}]
    r = client.post(f"{SUPABASE_URL}/rest/v1/brands",
                    params={"on_conflict": "source,slug"},
                    headers=headers(key, "resolution=merge-duplicates,return=representation"),
                    json=payload)
    if r.status_code in (200, 201):
        return r.json()[0]["id"]
    raise RuntimeError(f"ensure_brand failed {r.status_code}: {r.text[:200]}")


def ensure_model(client, key, brand_id: int, slug: str, name: str,
                 kolesa_slug: str | None, kolesa_brand_slug: str | None,
                 body_type: str | None) -> int:
    # First: check existence by (brand_id, lower(name)) — there's a unique constraint on it
    r0 = client.get(
        f"{SUPABASE_URL}/rest/v1/models",
        params={"select": "id,name,slug,kolesa_slug", "brand_id": f"eq.{brand_id}",
                "name": f"ilike.{name}"},
        headers=headers(key),
    )
    if r0.status_code == 200 and r0.json():
        existing = r0.json()[0]
        # backfill kolesa_slug if missing
        if kolesa_slug and not existing.get("kolesa_slug"):
            client.patch(
                f"{SUPABASE_URL}/rest/v1/models",
                params={"id": f"eq.{existing['id']}"},
                headers=headers(key, "return=minimal"),
                json={"kolesa_slug": kolesa_slug, "kolesa_brand_slug": kolesa_brand_slug},
            )
        return existing["id"]

    payload = [{"brand_id": brand_id, "slug": slug, "name": name, "source": SOURCE_TAG,
                "kolesa_slug": kolesa_slug, "kolesa_brand_slug": kolesa_brand_slug,
                "body_type": body_type}]
    r = client.post(f"{SUPABASE_URL}/rest/v1/models",
                    params={"on_conflict": "brand_id,slug,source"},
                    headers=headers(key, "resolution=merge-duplicates,return=representation"),
                    json=payload)
    if r.status_code in (200, 201):
        return r.json()[0]["id"]
    raise RuntimeError(f"ensure_model failed {r.status_code}: {r.text[:200]}")


def _displacement(raw):
    """Guazi's slug parser returns 0.0 for unknown engines (e.g. EVs with '00l').
    Drop those to NULL so the UI shows '-' instead of '0.0 l'."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def build_car_row(r: dict, source: str, brand_map: dict, model_map: dict,
                  client=None, key: str | None = None) -> dict | None:
    """Map one normalized scraper record to a cars-table row.

    Field contract (per AGENTS.md + normalizer.py):
      mark            = brand canonical
      model_family    = class label for grouping ("3 Series", "C-Class")
      model           = concrete model badge ("320Li", "C 260") — wins over class
      models.name FK  = the concrete model, NOT the class.
    """
    mark = r.get("mark") or r.get("brand_canonical") or r.get("brand") or ""
    mf = r.get("model_family") or r.get("model") or ""
    if not (mark and mf):
        return None
    # Concrete model wins. Empty -> fall back to family so the row still inserts.
    concrete = (r.get("model") or "").strip() or mf
    bslug = slugify(mark)
    mslug = slugify(concrete)  # FK keyed on concrete, not class
    brand_id = brand_map.get(bslug)
    if not brand_id and client:
        brand_id = ensure_brand(client, key, bslug, mark, r.get("kolesa_brand_slug"))
        brand_map[bslug] = brand_id
        print(f"[db] +brand {bslug} -> id={brand_id}", file=sys.stderr)
    if not brand_id:
        print(f"[db] no brand_id for {bslug!r} ({mark}), skipping", file=sys.stderr)
        return None
    model_id = model_map.get((bslug, mslug))
    if not model_id and client:
        model_id = ensure_model(
            client, key, brand_id, mslug, concrete,
            r.get("kolesa_model_slug"), r.get("kolesa_brand_slug"),
            r.get("body_type"),
        )
        model_map[(bslug, mslug)] = model_id
        print(f"[db] +model {bslug}/{mslug} -> id={model_id} ({concrete})", file=sys.stderr)
    # reg_date: guazi gives "2021.04" (YYYY.MM); encar gives year_month "2021-04"
    reg = r.get("registration_date") or r.get("year_month") or ""
    reg_norm = reg.replace(".", "-")
    reg_date = (reg_norm + "-01") if reg_norm and len(reg_norm) == 7 else None
    # location formats:
    #  guazi: "Shijiazhuang, China"  -> city + country
    #  encar: city only ("인천", "부산", "경기") via r["city"]
    loc = r.get("location") or ""
    if loc:
        city = loc.split(",")[0].strip() or None
        country = loc.split(",")[-1].strip() if "," in loc else None
    else:
        city = r.get("city") or None
        country = r.get("country") or None

    # Title for SEO + admin display. Source-language titles (Hangul / Hanzi
    # from encar / guazi) are useless to users — rebuild from the normalized
    # fields so og:title and admin tables are Latin. Raw scraper title lives
    # in source_data for forensics.
    complectation = r.get("complectation") or r.get("trim") or r.get("badge")
    title_parts = [
        mark,
        mf if mf and mf != concrete else None,
        concrete,
        complectation,
        str(r.get("year")) if r.get("year") else None,
    ]
    rebuilt_title = " ".join(p for p in title_parts if p).strip() or None

    return {
        "source": source,
        "source_id": r.get("listing_id") or r.get("source_id") or r.get("id"),
        "url": r.get("url"),
        "title": rebuilt_title,
        "mark": mark,
        "model_family": mf,
        "model": concrete,
        "complectation": complectation,
        "year": r.get("year"),
        "price_original": r.get("price_amount"),
        "price_currency": r.get("currency"),
        "km_age": r.get("mileage_km"),
        "color": r.get("color") or None,
        "body_type": r.get("body_type") or None,
        "engine_type": fuel_map(r.get("fuel") or ""),
        "transmission_type": transmission_map(r.get("gearbox") or r.get("transmission")),
        "drive_type": r.get("drive") or None,
        "displacement": _displacement(r.get("engine_l") or r.get("displacement")),
        "horse_power": int(r.get("horsepower_ps")) if r.get("horsepower_ps") else None,
        "seats": r.get("seats"),
        "steering": r.get("steering") or None,
        "accident_free": r.get("accident_free"),
        "no_water_damage": r.get("water_damage_free"),
        "no_fire_damage": r.get("fire_damage_free"),
        "published_at": r.get("published_at") or None,
        "city": city,
        "country": country,
        "reg_date": reg_date,
        "vin": r.get("vin") or r.get("vin_mask"),
        "inspection_grade": r.get("grade"),
        "images": r.get("photos"),
        "image_count": len(r.get("photos") or []) or None,
        "inspection_data": r.get("inspection_categories"),
        # Forensics: full raw scrape (incl. dropped *_original / new_price fields)
        # lives in source_data so re-normalization is possible without re-scraping.
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
        rows = [build_car_row(r, source, bmap, mmap, client, args.key) for r in records]
        rows = [r for r in rows if r]
        print(f"[db] {len(rows)} cars to upsert", file=sys.stderr)
        n = upsert_cars(client, args.key, rows)
        print(f"[db] done: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
