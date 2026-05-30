"""Generate SQL for upserting scraper output into auto_exp Supabase.

Reads normalized scraper JSON (out/<site>.json with brand/model_family/model/complectation/kolesa_*_slug
+ raw scraper fields), produces SQL that can be executed via Supabase MCP execute_sql.

Tables touched:
  brands  (UNIQUE (source, slug))
  models  (UNIQUE (brand_id, slug, source))
  cars    (UNIQUE (source, source_id))
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SOURCE_TAG = "guazi-scraper"  # brands/models source for new entries from this scraper


def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


def sql_str(v) -> str:
    if v is None or v == "":
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def fuel_map(raw: str) -> str:
    """Map scraper fuel -> cars.engine_type (gasoline/diesel/electric/hybrid/phev/lpg)."""
    if not raw:
        return ""
    r = raw.lower()
    if any(k in r for k in ("bev", "electric", "ev")):
        return "electric"
    if "phev" in r or "plug-in" in r:
        return "phev"
    if "hev" in r or "hybrid" in r or "reev" in r:
        return "hybrid"
    if "diesel" in r:
        return "diesel"
    if "gasoline" in r or "petrol" in r:
        return "gasoline"
    if "lpg" in r:
        return "lpg"
    return r


def make_upsert(records: list[dict], source: str) -> str:
    """Build a single SQL DO block that upserts everything in a tx."""
    brands_seen: dict[str, dict] = {}
    models_seen: dict[tuple, dict] = {}

    for r in records:
        brand_canon = r.get("mark") or r.get("brand_canonical") or r.get("brand") or ""
        if not brand_canon:
            continue
        kolesa_brand = r.get("kolesa_brand_slug") or None
        brand_slug = slugify(brand_canon)
        brands_seen.setdefault(brand_slug, {
            "slug": brand_slug,
            "name": brand_canon,
            "kolesa_slug": kolesa_brand,
        })
        if kolesa_brand and not brands_seen[brand_slug]["kolesa_slug"]:
            brands_seen[brand_slug]["kolesa_slug"] = kolesa_brand

        model_canon = r.get("model_family") or r.get("model_canonical") or r.get("model") or ""
        if not model_canon:
            continue
        model_slug = slugify(model_canon)
        kolesa_model = r.get("kolesa_model_slug") or None
        key = (brand_slug, model_slug)
        models_seen.setdefault(key, {
            "brand_slug": brand_slug,
            "slug": model_slug,
            "name": model_canon,
            "kolesa_slug": kolesa_model,
            "kolesa_brand_slug": kolesa_brand,
            "body_type": r.get("body_type") or None,
        })

    # 1. Upsert brands
    brand_values = ",\n  ".join(
        f"({sql_str(b['slug'])}, {sql_str(b['name'])}, {sql_str(SOURCE_TAG)}, {sql_str(b['kolesa_slug'])}, 'China')"
        for b in brands_seen.values()
    )
    sql_parts = [f"""
-- 1) Upsert brands
INSERT INTO brands (slug, name, source, kolesa_slug, country) VALUES
  {brand_values}
ON CONFLICT (source, slug) DO UPDATE SET
  kolesa_slug = COALESCE(EXCLUDED.kolesa_slug, brands.kolesa_slug),
  name        = EXCLUDED.name,
  updated_at  = now();

-- Backfill kolesa_slug for brands that existed already under a different source
UPDATE brands b SET kolesa_slug = v.kolesa_slug
FROM (VALUES
  {','.join(f"({sql_str(b['slug'])}, {sql_str(b['kolesa_slug'])})" for b in brands_seen.values() if b['kolesa_slug'])}
) AS v(slug, kolesa_slug)
WHERE b.slug = v.slug AND (b.kolesa_slug IS NULL OR b.kolesa_slug = '');
"""]

    # 2. Upsert models (need brand_id lookup; do it in SQL via subselect)
    model_values = ",\n  ".join(
        f"({sql_str(m['brand_slug'])}, {sql_str(m['slug'])}, {sql_str(m['name'])}, "
        f"{sql_str(m['kolesa_slug'])}, {sql_str(m['kolesa_brand_slug'])}, {sql_str(m['body_type'])})"
        for m in models_seen.values()
    )
    sql_parts.append(f"""
-- 2) Upsert models — use latest brand id (prefer scraper source, else any)
WITH brand_pick AS (
  SELECT DISTINCT ON (slug) slug, id
  FROM brands
  ORDER BY slug,
    CASE WHEN source = {sql_str(SOURCE_TAG)} THEN 0 ELSE 1 END,
    id
),
model_input(brand_slug, slug, name, kolesa_slug, kolesa_brand_slug, body_type) AS (
  VALUES {model_values}
)
INSERT INTO models (brand_id, slug, name, source, kolesa_slug, kolesa_brand_slug, body_type)
SELECT bp.id, mi.slug, mi.name, {sql_str(SOURCE_TAG)},
       mi.kolesa_slug, mi.kolesa_brand_slug, mi.body_type
FROM model_input mi
JOIN brand_pick bp ON bp.slug = mi.brand_slug
ON CONFLICT (brand_id, slug, source) DO UPDATE SET
  kolesa_slug       = COALESCE(EXCLUDED.kolesa_slug, models.kolesa_slug),
  kolesa_brand_slug = COALESCE(EXCLUDED.kolesa_brand_slug, models.kolesa_brand_slug),
  body_type         = COALESCE(EXCLUDED.body_type, models.body_type),
  name              = EXCLUDED.name,
  updated_at        = now();

-- Backfill kolesa_slug on existing models from other sources
UPDATE models m SET kolesa_slug = v.kolesa_slug,
                    kolesa_brand_slug = COALESCE(v.kolesa_brand_slug, m.kolesa_brand_slug)
FROM (VALUES
  {','.join(f"({sql_str(m['brand_slug'])}, {sql_str(m['slug'])}, {sql_str(m['kolesa_slug'])}, {sql_str(m['kolesa_brand_slug'])})" for m in models_seen.values() if m['kolesa_slug'])}
) AS v(brand_slug, slug, kolesa_slug, kolesa_brand_slug),
     brands b
WHERE b.id = m.brand_id AND b.slug = v.brand_slug AND m.slug = v.slug
  AND (m.kolesa_slug IS NULL OR m.kolesa_slug = '');
""")

    # 3. Upsert cars
    car_rows = []
    for r in records:
        brand_canon = r.get("mark") or r.get("brand_canonical") or r.get("brand") or ""
        model_canon = r.get("model_family") or r.get("model_canonical") or r.get("model") or ""
        if not (brand_canon and model_canon):
            continue
        bslug = slugify(brand_canon)
        mslug = slugify(model_canon)
        cols = {
            "source": source,
            "source_id": r.get("listing_id") or r.get("source_id") or r.get("id"),
            "url": r.get("url"),
            "title": r.get("title"),
            "mark_original": r.get("brand"),
            "mark": brand_canon,
            "series_original": None,
            "model_family": model_canon,
            "model": r.get("model_normalized") or r.get("model") or model_canon,
            "complectation": r.get("complectation") or r.get("trim") or r.get("badge"),
            "year": r.get("year"),
            "price_original": r.get("price_amount"),
            "price_currency": r.get("currency"),
            "km_age_unit": "km",
            "km_age": r.get("mileage_km"),
            "color_original": r.get("color"),
            "color": r.get("color"),
            "body_type": r.get("body_type"),
            "engine_type": fuel_map(r.get("fuel") or ""),
            "fuel_original": r.get("fuel"),
            "transmission_original": r.get("gearbox") or r.get("transmission"),
            "transmission_type": r.get("gearbox") or r.get("transmission"),
            "drive_original": r.get("drive") or r.get("drive_train"),
            "drive_type": r.get("drive"),
            "displacement": r.get("engine_l"),
            "horse_power": int(r.get("horsepower_ps")) if r.get("horsepower_ps") else None,
            "city_original": r.get("location") or r.get("city"),
            "city": (r.get("location") or r.get("city") or "").split(",")[0].strip() or None,
            "country": (r.get("location") or "").split(",")[-1].strip() if (r.get("location") and "," in (r.get("location") or "")) else None,
            "reg_date": (r.get("registration_date") or "").replace(".", "-") + "-01"
                         if r.get("registration_date") and len(r.get("registration_date")) == 7 else None,
            "images": r.get("photos"),
            "image_count": len(r.get("photos") or []) or None,
            "vin": r.get("vin") or r.get("vin_mask"),
            "inspection_grade": r.get("grade"),
            "accident_free": r.get("accident_free"),
            "no_water_damage": r.get("water_damage_free"),
            "no_fire_damage": r.get("fire_damage_free"),
            "inspection_data": r.get("inspection_categories"),
            "steering": r.get("steering"),
            "seats": r.get("seats"),
            "source_data": {k: v for k, v in r.items()
                            if k not in ("photos", "inspection_categories", "spec", "raw")},
            "first_seen": "now()",  # marker — handled separately
        }
        car_rows.append(cols)

    if not car_rows:
        sql_parts.append("-- no car rows to insert\n")
        return "BEGIN;\n" + "\n".join(sql_parts) + "\nCOMMIT;\n"

    car_cols = [c for c in car_rows[0].keys() if c != "first_seen"]
    values_sql_chunks = []
    for c in car_rows:
        vals = []
        for k in car_cols:
            v = c[k]
            vals.append(sql_str(v))
        values_sql_chunks.append("  (" + ", ".join(vals) + ")")
    values_block = ",\n".join(values_sql_chunks)
    update_set = ", ".join(
        f"{k} = EXCLUDED.{k}" for k in car_cols
        if k not in ("source", "source_id")
    )
    sql_parts.append(f"""
-- 3) Upsert cars (compute brand_id/model_id via slug-canonical names)
WITH brand_pick AS (
  SELECT DISTINCT ON (slug) slug, id
  FROM brands
  ORDER BY slug,
    CASE WHEN source = {sql_str(SOURCE_TAG)} THEN 0 ELSE 1 END,
    id
),
model_pick AS (
  SELECT DISTINCT ON (b.slug, m.slug) b.slug AS brand_slug, m.slug, m.id
  FROM models m JOIN brands b ON b.id = m.brand_id
  ORDER BY b.slug, m.slug,
    CASE WHEN m.source = {sql_str(SOURCE_TAG)} THEN 0 ELSE 1 END,
    m.id
),
car_input ({', '.join(car_cols)}, brand_slug, model_slug) AS (
  VALUES
{','.join(
    '  (' + ', '.join(sql_str(c[k]) for k in car_cols) + ', ' +
        sql_str(slugify(c['mark'])) + ', ' + sql_str(slugify(c['model_family'])) + ')'
    for c in car_rows
)}
)
INSERT INTO cars ({', '.join(car_cols)}, brand_id, model_id, llm_normalized_at, last_seen, first_seen)
SELECT
  {', '.join('ci.'+k for k in car_cols)},
  bp.id AS brand_id,
  mp.id AS model_id,
  now() AS llm_normalized_at,
  now() AS last_seen,
  now() AS first_seen
FROM car_input ci
JOIN brand_pick bp ON bp.slug = ci.brand_slug
LEFT JOIN model_pick mp ON mp.brand_slug = ci.brand_slug AND mp.slug = ci.model_slug
ON CONFLICT (source, source_id) DO UPDATE SET
  {update_set},
  last_seen = now(),
  llm_normalized_at = now();
""")

    return "BEGIN;\n" + "\n".join(sql_parts) + "\nCOMMIT;\n"


def split_chunks(records: list[dict], source: str, batch_size: int = 10) -> list[str]:
    """Return list of SQL chunks safe to execute one-by-one."""
    chunks = []
    # part 1: brands + models — small, all in one
    head = make_upsert(records[:0], source)  # empty body
    brands_models = make_upsert(records, source)
    cars_start = brands_models.find("-- 3) Upsert cars")
    if cars_start > 0:
        chunks.append("BEGIN;\n" + brands_models[brands_models.find("-- 1)"):cars_start] + "\nCOMMIT;\n")
        # then cars in batches
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            sql_batch = make_upsert(batch, source)
            cars_only_start = sql_batch.find("-- 3) Upsert cars")
            if cars_only_start > 0:
                cars_only_end = sql_batch.find("COMMIT;")
                chunks.append("BEGIN;\n" + sql_batch[cars_only_start:cars_only_end] + "\nCOMMIT;\n")
    else:
        chunks.append(brands_models)
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="normalized scraper JSON")
    ap.add_argument("--source", default=None, help="cars.source value (default = first record's site)")
    ap.add_argument("--out-dir", default=None, help="write chunked SQL files into dir")
    ap.add_argument("--batch", type=int, default=10)
    args = ap.parse_args()

    records = json.loads(Path(args.input).read_text())
    if not isinstance(records, list) or not records:
        raise SystemExit("input is not a non-empty JSON list")
    source = args.source or records[0].get("site") or "unknown"

    chunks = split_chunks(records, source, batch_size=args.batch)
    if args.out_dir:
        d = Path(args.out_dir)
        d.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(chunks):
            (d / f"chunk-{i:03d}.sql").write_text(c)
        print(f"wrote {len(chunks)} chunks -> {d}", file=sys.stderr)
    else:
        for i, c in enumerate(chunks):
            print(f"-- chunk {i} ({len(c)} bytes) --")
            sys.stdout.write(c)


if __name__ == "__main__":
    main()
