"""Compact JSONB-array upsert into cars."""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path


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


def build_car_payload(records: list[dict], site_source: str) -> list[dict]:
    out = []
    for r in records:
        mark = r.get("mark") or r.get("brand_canonical") or r.get("brand") or ""
        mf = r.get("model_family") or r.get("model") or ""
        if not (mark and mf):
            continue
        reg = r.get("registration_date") or ""
        reg_date = None
        if reg and len(reg) == 7 and reg[4] == ".":
            reg_date = reg.replace(".", "-") + "-01"
        loc = r.get("location") or ""
        city = loc.split(",")[0].strip() if loc else None
        country = loc.split(",")[-1].strip() if "," in loc else None
        out.append({
            "source": site_source,
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
            "image_count": len(r.get("photos") or []) or None,
            # images deliberately omitted from compact payload (too bulky for one SQL);
            # send separately via update_images()
            "inspection_data": r.get("inspection_categories"),
            "brand_slug": slugify(mark),
            "model_slug": slugify(mf),
        })
    return out


def build_sql(records: list[dict], source: str) -> str:
    payload = build_car_payload(records, source)
    js = json.dumps(payload, ensure_ascii=False).replace("'", "''")
    return f"""
WITH brand_pick AS (
  SELECT DISTINCT ON (slug) slug, id FROM brands
  ORDER BY slug, CASE WHEN source = 'guazi-scraper' THEN 0 ELSE 1 END, id
),
model_pick AS (
  SELECT DISTINCT ON (b.slug, m.slug) b.slug AS brand_slug, m.slug, m.id
  FROM models m JOIN brands b ON b.id = m.brand_id
  ORDER BY b.slug, m.slug, CASE WHEN m.source = 'guazi-scraper' THEN 0 ELSE 1 END, m.id
),
input AS (
  SELECT jsonb_array_elements('{js}'::jsonb) AS j
)
INSERT INTO cars (
  source, source_id, url, title, mark_original, mark, model_family, model, complectation,
  year, price_original, price_currency, km_age_unit, km_age,
  color, body_type, engine_type, fuel_original, transmission_type, drive_type,
  displacement, horse_power, city, country, reg_date,
  vin, inspection_grade, accident_free, no_water_damage, no_fire_damage,
  steering, seats, image_count, inspection_data,
  brand_id, model_id, last_seen, first_seen
)
SELECT
  j->>'source', j->>'source_id', j->>'url', j->>'title',
  j->>'mark_original', j->>'mark', j->>'model_family', j->>'model', j->>'complectation',
  (j->>'year')::int,
  NULLIF(j->>'price_original','')::numeric, j->>'price_currency',
  'km', NULLIF(j->>'km_age','')::numeric,
  j->>'color', j->>'body_type', j->>'engine_type', j->>'fuel_original',
  j->>'transmission_type', j->>'drive_type',
  NULLIF(j->>'displacement','')::numeric, NULLIF(j->>'horse_power','')::int,
  j->>'city', j->>'country', NULLIF(j->>'reg_date','')::date,
  j->>'vin', j->>'inspection_grade',
  CASE WHEN j->>'accident_free' = 'true' THEN TRUE WHEN j->>'accident_free' = 'false' THEN FALSE END,
  CASE WHEN j->>'no_water_damage' = 'true' THEN TRUE WHEN j->>'no_water_damage' = 'false' THEN FALSE END,
  CASE WHEN j->>'no_fire_damage' = 'true' THEN TRUE WHEN j->>'no_fire_damage' = 'false' THEN FALSE END,
  j->>'steering', NULLIF(j->>'seats','')::int,
  NULLIF(j->>'image_count','')::int, j->'inspection_data',
  bp.id, mp.id, now(), now()
FROM input
JOIN brand_pick bp ON bp.slug = input.j->>'brand_slug'
LEFT JOIN model_pick mp ON mp.brand_slug = input.j->>'brand_slug' AND mp.slug = input.j->>'model_slug'
ON CONFLICT (source, source_id) DO UPDATE SET
  url = EXCLUDED.url, title = EXCLUDED.title, mark = EXCLUDED.mark,
  model_family = EXCLUDED.model_family, model = EXCLUDED.model, complectation = EXCLUDED.complectation,
  year = EXCLUDED.year, price_original = EXCLUDED.price_original, price_currency = EXCLUDED.price_currency,
  km_age = EXCLUDED.km_age, color = EXCLUDED.color, body_type = EXCLUDED.body_type,
  engine_type = EXCLUDED.engine_type, transmission_type = EXCLUDED.transmission_type, drive_type = EXCLUDED.drive_type,
  displacement = EXCLUDED.displacement, horse_power = EXCLUDED.horse_power,
  city = EXCLUDED.city, country = EXCLUDED.country, reg_date = EXCLUDED.reg_date,
  vin = EXCLUDED.vin, inspection_grade = EXCLUDED.inspection_grade,
  accident_free = EXCLUDED.accident_free, no_water_damage = EXCLUDED.no_water_damage, no_fire_damage = EXCLUDED.no_fire_damage,
  steering = EXCLUDED.steering, seats = EXCLUDED.seats,
  image_count = EXCLUDED.image_count, inspection_data = EXCLUDED.inspection_data,
  brand_id = EXCLUDED.brand_id, model_id = EXCLUDED.model_id,
  last_seen = now();
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--source", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    records = json.loads(Path(args.input).read_text())
    source = args.source or records[0].get("site") or "unknown"
    sql = build_sql(records, source)
    if args.out:
        Path(args.out).write_text(sql)
        print(f"wrote {len(sql)} bytes -> {args.out}", file=sys.stderr)
    else:
        print(sql)
