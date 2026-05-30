"""End-to-end pipeline: scrape -> normalize -> upsert to Supabase.

Single command runs all 3 steps. Skip normalize/db via flags.

Examples:
  # guazi with user's filter set
  python -m core.pipeline guazi --limit 50 \\
    -f vehicleSourceClassificationCustomers=180003,180002 \\
    -f price=8000, -f carYear=2020, -f detectionLevels=S,A -f roadHaul=0,100000

  # encar 10 newest
  python -m core.pipeline encar --limit 10

Env required (else step is skipped):
  OPENROUTER_API_KEY  for AI normalize
  SUPABASE_KEY        for DB upsert (defaults to anon key embedded in db_rest)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from core import db_rest, normalizer


def _parse_kv(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def run_scrape(site: str, args) -> list[dict]:
    if site == "guazi":
        from scrapers import guazi as scr
        params = scr._parse_filter_args(args.filter)
        grades = {g.strip().upper() for g in (args.grades or "").split(",") if g.strip()} or None
        return scr.run(
            limit=args.limit,
            detail=not args.no_detail,
            path=args.path,
            params=params,
            max_mileage_km=args.max_mileage_km,
            min_year=args.min_year,
            max_year=args.max_year,
            grades=grades,
        )
    if site == "encar":
        from scrapers import encar as scr
        return scr.run(
            limit=args.limit, offset=args.offset,
            query=args.query, detail=not args.no_detail,
            min_year=args.min_year, max_year=args.max_year,
            min_price_man=args.min_price_man, max_price_man=args.max_price_man,
            max_mileage_km=args.max_mileage_km, min_mileage_km=args.min_mileage_km,
            fuel=args.fuel, transmission=args.transmission,
            manufacturer=args.manufacturer, only_inspection=args.only_inspection,
        )
    raise SystemExit(f"unknown site: {site}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=["guazi", "encar"])
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("-f", "--filter", action="append", default=[])
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--no-normalize", action="store_true", help="Skip AI normalizer")
    ap.add_argument("--no-db", action="store_true", help="Skip Supabase upsert")
    ap.add_argument("--out", default=None)
    # guazi-specific
    ap.add_argument("--path", default="/used-cars/")
    ap.add_argument("--max-mileage-km", type=int)
    ap.add_argument("--min-year", type=int)
    ap.add_argument("--max-year", type=int)
    ap.add_argument("--grades", default="")
    # encar-specific
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--query", default=None)
    ap.add_argument("--min-price-man", type=int, help="encar: min price in 만원")
    ap.add_argument("--max-price-man", type=int)
    ap.add_argument("--min-mileage-km", type=int)
    ap.add_argument("--fuel", default=None)
    ap.add_argument("--transmission", default=None)
    ap.add_argument("--manufacturer", default=None)
    ap.add_argument("--only-inspection", action="store_true")
    args = ap.parse_args()

    site = args.site
    out_path = Path(args.out or f"out/{site}-{int(time.time())}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) SCRAPE
    print(f"[pipeline] === SCRAPE ({site}) ===", file=sys.stderr)
    t0 = time.time()
    records = run_scrape(site, args)
    print(f"[pipeline] scraped {len(records)} in {time.time()-t0:.1f}s", file=sys.stderr)
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"[pipeline] wrote {out_path}", file=sys.stderr)

    if not records:
        print("[pipeline] no records, stop", file=sys.stderr)
        return

    # 2) NORMALIZE
    if args.no_normalize:
        print("[pipeline] skip normalize (--no-normalize)", file=sys.stderr)
    elif not os.getenv("OPENROUTER_API_KEY"):
        print("[pipeline] skip normalize (OPENROUTER_API_KEY not set)", file=sys.stderr)
    else:
        print(f"[pipeline] === NORMALIZE ===", file=sys.stderr)
        t1 = time.time()
        records = normalizer.normalize(records)
        out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2))
        print(f"[pipeline] normalized in {time.time()-t1:.1f}s", file=sys.stderr)

    # 3) UPSERT to Supabase
    if args.no_db:
        print("[pipeline] skip db (--no-db)", file=sys.stderr)
        return
    print(f"[pipeline] === DB UPSERT ===", file=sys.stderr)
    t2 = time.time()
    key = os.getenv("SUPABASE_KEY") or db_rest.DEFAULT_KEY
    with httpx.Client(timeout=60.0) as client:
        bmap = db_rest.fetch_brand_map(client, key)
        mmap = db_rest.fetch_model_map(client, key, bmap)
        rows = [db_rest.build_car_row(r, site, bmap, mmap, client, key) for r in records]
        rows = [r for r in rows if r]
        print(f"[pipeline] {len(rows)} car rows ready to upsert", file=sys.stderr)
        n = db_rest.upsert_cars(client, key, rows)
    print(f"[pipeline] upserted {n} in {time.time()-t2:.1f}s", file=sys.stderr)
    print(f"[pipeline] DONE in {time.time()-t0:.1f}s total", file=sys.stderr)


if __name__ == "__main__":
    main()
