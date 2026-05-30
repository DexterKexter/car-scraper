"""Upsert scraper output into Supabase via PostgREST.

Reads SUPABASE_URL and SUPABASE_KEY from env. Resolves brand_id/model_id via REST GET,
then bulk POSTs cars.

Field contract (per AGENTS.md + normalizer.py):
  cars.mark           = brands.name  = "BMW"
  cars.model_family   = grouping class label = "3 Series"
  cars.model          = concrete model badge  = "320Li"
  cars.complectation  = trim/package text     = "M Sport Package"
  models.name = the CONCRETE model ("320Li"), NOT the class.
  models is keyed (brand_id, LOWER(name)) - one row per concrete variant.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path
import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SOURCE_TAG = "car-scraper"  # brands/models inserted by this pipeline are tagged here

# Site -> country mapping. The raw `location` field is the city, not the country,
# so derive country from the upstream platform identity instead of from text.
SITE_COUNTRY = {
    "guazi": "CN",
    "encar": "KR",
    "autocango": "CN",
    "kolesa": "KZ",
}


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


def displacement(raw) -> float | None:
    """Guazi's slug parser returns 0.0 for unknown engines (e.g. EVs with '00l').
    Drop those to NULL so the UI shows '-' instead of '0.0 l'."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def headers(key: str, prefer: str = "") -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def fetch_brand_map(client, key) -> dict[str, int]:
    """slug -> id, preferring car-scraper source."""
    r = client.get(f"{SUPABASE_URL}/rest/v1/brands",
                   params={"select": "id,slug,source"}, headers=headers(key))
    r.raise_for_status()
    data = r.json()
    out: dict[str, int] = {}
    for row in sorted(data, key=lambda b: (b["source"] != SOURCE_TAG, b["id"])):
        out.setdefault(row["slug"], row["id"])
    return out


def fetch_model_map(client, key, brand_map) -> dict[tuple, int]:
    """(brand_slug, model_slug) -> model_id."""
    r = client.get(f"{SUPABASE_URL}/rest/v1/models",
                   params={"select": "id,slug,brand_id,source"}, headers=headers(key))
    r.raise_for_status()
    data = r.json()
    bid_to_slug = {bid: slug for slug, bid in brand_map.items()}
    out: dict[tuple, int] = {}
    for row in sorted(data, key=lambda m: (m["source"] != SOURCE_TAG, m["id"])):
        bslug = bid_to_slug.get(row["brand_id"])
        if not bslug:
            continue
        key_ = (bslug, row["slug"])
        out.setdefault(key_, row["id"])
    return out


def ensure_brand(client, key, slug: str, name: str, kolesa_slug: str | None,
                 country: str | None) -> int:
    """Find by LOWER(name) first (cross-source dedupe), else create. Returns brand_id."""
    r0 = client.get(
        f"{SUPABASE_URL}/rest/v1/brands",
        params={"select": "id,name,kolesa_slug", "name": f"ilike.{name}"},
        headers=headers(key),
    )
    if r0.status_code == 200 and r0.json():
        existing = r0.json()[0]
        if kolesa_slug and not existing.get("kolesa_slug"):
            client.patch(
                f"{SUPABASE_URL}/rest/v1/brands",
                params={"id": f"eq.{existing['id']}"},
                headers=headers(key, "return=minimal"),
                json={"kolesa_slug": kolesa_slug},
            )
        return existing["id"]

    payload = [{"slug": slug, "name": name, "source": SOURCE_TAG,
                "kolesa_slug": kolesa_slug, "country": country}]
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
    r0 = client.get(
        f"{SUPABASE_URL}/rest/v1/models",
        params={"select": "id,name,slug,kolesa_slug,kolesa_brand_slug",
                "brand_id": f"eq.{brand_id}", "name": f"ilike.{name}"},
        headers=headers(key),
    )
    if r0.status_code == 200 and r0.json():
        existing = r0.json()[0]
        patch = {}
        if kolesa_slug and not existing.get("kolesa_slug"):
            patch["kolesa_slug"] = kolesa_slug
        if kolesa_brand_slug and not existing.get("kolesa_brand_slug"):
            patch["kolesa_brand_slug"] = kolesa_brand_slug
        if patch:
            client.patch(
                f"{SUPABASE_URL}/rest/v1/models",
                params={"id": f"eq.{existing['id']}"},
                headers=headers(key, "return=minimal"),
                json=patch,
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


def build_car_row(r: dict, source: str, brand_map: dict, model_map: dict,
                  client=None, key: str | None = None) -> dict | None:
    """Map one normalized scraper record to a cars-table row.

    Field policy:
      mark = brand_canonical from LLM (fallback to raw brand only if LLM failed)
      model_family = class label from LLM
      model = concrete model from LLM (NEVER the class)
      models.name = the concrete model (FK target)
    """
    mark = (r.get("mark") or r.get("brand_canonical") or r.get("brand") or "").strip()
    mf = (r.get("model_family") or r.get("model") or "").strip()
    if not (mark and mf):
        return None

    # Concrete model wins. If LLM left model empty, fall back to family so the
    # row still inserts (should be rare with the new prompt).
    concrete = (r.get("model") or "").strip() or mf

    bslug = slugify(mark)
    mslug = slugify(concrete)
    country = SITE_COUNTRY.get(source)

    brand_id = brand_map.get(bslug)
    if not brand_id and client:
        brand_id = ensure_brand(client, key, bslug, mark,
                                r.get("kolesa_brand_slug"), country)
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

    reg = r.get("registration_date") or ""
    reg_date = reg.replace(".", "-") + "-01" if reg and len(reg) == 7 else None
    # location formats:
    #  guazi: "Shijiazhuang, China"  -> city + country
    #  encar: city only ("인천", "부산", "경기") via r["city"]
    loc = r.get("location") or ""
    if loc:
        city = loc.split(",")[0].strip() or None
        # location may include a country segment (guazi: "Shijiazhuang, China").
        # If absent, fall back to the per-source country from SITE_COUNTRY.
        country_from_loc = loc.split(",")[-1].strip() if "," in loc else None
        country = country_from_loc or country
    else:
        city = r.get("city") or None
        # country stays as the SITE_COUNTRY default set above.

    return {
        "source": source,
        "source_id": r.get("listing_id") or r.get("source_id") or r.get("id"),
        "url": r.get("url"),
        "title": r.get("title"),
        "mark_original": r.get("brand"),
        "mark": mark,
        "model_family": mf,
        "model": concrete,
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
        "displacement": displacement(r.get("engine_l")),
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


def _require_env():
    if not SUPABASE_URL:
        raise SystemExit("SUPABASE_URL env required")
    key = os.getenv("SUPABASE_KEY")
    if not key:
        raise SystemExit("SUPABASE_KEY env required (anon or service-role key)")
    return key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--source", default=None)
    args = ap.parse_args()
    key = _require_env()

    records = json.loads(Path(args.input).read_text())
    source = args.source or records[0].get("site") or "unknown"

    with httpx.Client(timeout=60.0) as client:
        bmap = fetch_brand_map(client, key)
        mmap = fetch_model_map(client, key, bmap)
        print(f"[db] brand_map: {len(bmap)} entries", file=sys.stderr)
        print(f"[db] model_map: {len(mmap)} entries", file=sys.stderr)
        rows = [build_car_row(r, source, bmap, mmap, client, key) for r in records]
        rows = [r for r in rows if r]
        print(f"[db] {len(rows)} cars to upsert", file=sys.stderr)
        n = upsert_cars(client, key, rows)
        print(f"[db] done: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
