"""backfill_encar_photos.py — repair photo galleries for existing encar rows.

Rows captured before the _photo_urls fix only kept the ~4 search-list
thumbnails. This re-fetches the encar detail API (full ~26-photo set) for
every encar row at/under a photo threshold and rewrites cars.images.

Deterministic: targets exactly the stale rows, unlike the list scraper
which only re-covers the newest-modified window. Idempotent — re-runs skip
rows that already have more photos than the detail API returns.

Run:
  python -m core.backfill_encar_photos                       # all encar rows <=4 imgs
  python -m core.backfill_encar_photos --max-imgs 4 --workers 6 --limit 200
Env: SUPABASE_KEY (service_role / RLS-off); api.encar.com reachable.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from core.db_rest import SUPABASE_URL
from scrapers.encar import fetch_detail, _photo_urls

sys.stdout.reconfigure(line_buffering=True)

KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or ""


def _h() -> dict:
    return {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def fetch_stale(client: httpx.Client, max_imgs: int) -> list[dict]:
    """All encar rows whose image array is <= max_imgs (jsonb length can't be
    filtered server-side in PostgREST, so page everything and filter here)."""
    rows: list[dict] = []
    offset = 0
    while True:
        r = client.get(
            f"{SUPABASE_URL}/rest/v1/cars",
            params={"select": "source_id,images", "source": "eq.encar",
                    "limit": 1000, "offset": offset},
            headers=_h(), timeout=60,
        )
        if r.status_code != 200:
            sys.exit(f"DB read fail {r.status_code}: {r.text[:200]}")
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return [x for x in rows if len(x.get("images") or []) <= max_imgs]


def backfill_one(client: httpx.Client, row: dict) -> tuple[str, str, object]:
    sid = row["source_id"]
    cur = len(row.get("images") or [])
    det = fetch_detail(client, sid)
    if det.get("_err"):
        return ("err", sid, det["_err"])
    photos = _photo_urls(det.get("photos") or [])[:30]
    if len(photos) <= cur:
        return ("nochange", sid, len(photos))
    r = client.patch(
        f"{SUPABASE_URL}/rest/v1/cars",
        params={"source": "eq.encar", "source_id": f"eq.{sid}"},
        headers={**_h(), "Content-Type": "application/json",
                 "Prefer": "return=minimal"},
        json={"images": photos, "image_count": len(photos)}, timeout=60,
    )
    if r.status_code not in (200, 204):
        return ("patchfail", sid, r.status_code)
    return ("ok", sid, len(photos))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-imgs", type=int, default=4,
                    help="re-fetch rows with at most this many photos")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    if not KEY:
        sys.exit("SUPABASE_KEY (service_role) required")

    with httpx.Client(follow_redirects=True) as client:
        stale = fetch_stale(client, args.max_imgs)
        if args.limit:
            stale = stale[:args.limit]
        print(f"encar rows to backfill (<= {args.max_imgs} imgs): {len(stale)}")
        if not stale:
            return

        ok = nochange = err = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(backfill_one, client, row) for row in stale]
            done = 0
            for f in as_completed(futures):
                kind, _sid, _info = f.result()
                done += 1
                if kind == "ok":
                    ok += 1
                elif kind == "nochange":
                    nochange += 1
                else:
                    err += 1
                if done % 50 == 0 or done == len(stale):
                    rate = done / max(time.time() - t0, 1e-3)
                    print(f"  [{done}/{len(stale)}] updated={ok} "
                          f"nochange={nochange} err={err} {rate:.1f}/s")

        print(f"Done in {(time.time()-t0)/60:.1f}m. "
              f"updated={ok} nochange={nochange} err={err}")


if __name__ == "__main__":
    main()
