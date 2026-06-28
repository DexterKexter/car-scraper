"""core/mirror_images.py — rehost blocked CDN images into Supabase Storage.

Why:
  Guazi's export CDN (guazistatic-global.com) returns 403 for bursts of
  concurrent image loads from non-CN IPs, so the catalog grid (14+ photos
  at once) shows broken images for KZ/RU users. che168 (autoimg.cn) is
  ORB-blocked by Chrome; autocango (i1.autocango.com) is geo-restricted.
  We download each blocked URL server-side (with the right Referer) and
  re-host it in the public `car-images` bucket, then rewrite cars.images.

Idempotent: already-mirrored URLs point at our own Supabase host and are
skipped by _is_blocked(), so re-runs only pick up new photos.

Storage writes require the service_role key — the `car-images` bucket
policy is "public read, service_role write". Set SUPABASE_SERVICE_KEY in
the workflow env (the anon key used elsewhere will get a 403 on upload).

Run:
  python -m core.mirror_images                  # all eligible rows
  python -m core.mirror_images --limit 20       # smoke-test
  python -m core.mirror_images --source guazi   # restrict by source
Env: SUPABASE_SERVICE_KEY (service_role; falls back to SUPABASE_KEY)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import httpx

from core.db_rest import SUPABASE_URL

sys.stdout.reconfigure(line_buffering=True)

BUCKET = "car-images"
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or ""

# Hosts whose images we mirror because they 403/ORB-block for non-CN end
# users. Mirrored URLs live on our own SUPABASE_URL host, so they never
# match here — that's what makes re-runs idempotent.
BLOCKED_HOST_SUBSTRINGS = (
    "guazistatic-global.com",        # guazi EN export CDN (image-oversea + global-image-pub)
    "image-public.guazistatic.com",  # guazi CN
    "image-pub.guazistatic.com",
    "autoimg.cn",                    # che168 (Chrome ORB block)
    "i1.autocango.com",              # autocango (geo-restricted to mainland)
    "ci.encar.com",                  # encar — fine globally but adds 250-400ms
                                     # trans-Pacific RTT to LCP for KZ users;
                                     # mirroring beats Korea round-trip.
)
# Per-host Referer override — these CDNs check Referer against their own site.
REFERER_BY_HOST = {
    "guazistatic-global.com":        "https://en.guazi.com/",
    "image-public.guazistatic.com":  "https://www.guazi.com/",
    "image-pub.guazistatic.com":     "https://www.guazi.com/",
    "autoimg.cn":                    "https://www.autohome.com.cn/",
    "i1.autocango.com":              "https://www.autocango.com/",
    "ci.encar.com":                  "https://www.encar.com/",
}
PUBLIC_URL = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}"
BASE_DL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
}
EXT_BY_MIME = {
    "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/png": "png", "image/webp": "webp", "image/avif": "avif",
}


def _rest_headers() -> dict:
    return {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}


def _dl_headers(url: str) -> dict:
    h = dict(BASE_DL_HEADERS)
    for sub, ref in REFERER_BY_HOST.items():
        if sub in url:
            h["Referer"] = ref
            break
    return h


def _is_blocked(url: str) -> bool:
    return any(s in (url or "") for s in BLOCKED_HOST_SUBSTRINGS)


def _storage_url(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"


def mirror_one(client: httpx.Client, orig: str, dst: str) -> str | None:
    """Download orig and PUT into Storage. Return the public URL or None."""
    try:
        r = client.get(orig, headers=_dl_headers(orig), timeout=30,
                       follow_redirects=True)
    except Exception as e:
        print(f"  ! GET fail {orig}: {e}")
        return None
    if r.status_code != 200:
        print(f"  ! GET {r.status_code} {orig}")
        return None
    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        print(f"  ! not image ({ct}) {orig}")
        return None
    ext = EXT_BY_MIME.get(ct, "jpg")
    final = dst.rsplit(".", 1)[0] + "." + ext
    put = client.put(
        _storage_url(final), content=r.content,
        headers={**_rest_headers(), "Content-Type": ct, "x-upsert": "true",
                 "Cache-Control": "public, max-age=31536000, immutable"},
        timeout=60,
    )
    if put.status_code not in (200, 201):
        print(f"  ! PUT {put.status_code} {final}: {put.text[:200]}")
        return None
    return f"{PUBLIC_URL}/{final}"


def mirror_car(client: httpx.Client, car: dict) -> tuple[int, int]:
    """Mirror every blocked URL in a car's `images`, then rewrite the row.
    Returns (succeeded, total_blocked)."""
    source, sid = car["source"], car["source_id"]
    images: list[str] = car.get("images") or []
    new_images = list(images)
    ok = blocked = 0
    for idx, url in enumerate(images):
        if not url or not _is_blocked(url):
            continue
        blocked += 1
        ext = os.path.splitext(urlparse(url).path)[1].lstrip(".").lower() or "jpg"
        if ext not in EXT_BY_MIME.values():
            ext = "jpg"
        mirrored = mirror_one(client, url, f"cars/{source}/{sid}/{idx}.{ext}")
        if mirrored:
            new_images[idx] = mirrored
            ok += 1
    if ok:
        r = client.patch(
            f"{SUPABASE_URL}/rest/v1/cars",
            params={"source": f"eq.{source}", "source_id": f"eq.{sid}"},
            headers={**_rest_headers(), "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json={"images": new_images}, timeout=60,
        )
        if r.status_code not in (200, 204):
            print(f"  ! DB update {source}/{sid}: {r.status_code} {r.text[:200]}")
    return ok, blocked


def fetch_cars(client: httpx.Client, limit: int | None,
               source: str | None) -> list[dict]:
    """Page through candidate cars; filter client-side (images is jsonb so
    PostgREST can't ilike into it)."""
    params = {"select": "source,source_id,images"}
    params["source"] = (f"eq.{source}" if source
                        else "in.(guazi,guazi_en,encar,che168,autocango)")
    out: list[dict] = []
    offset, page = 0, 1000
    while True:
        r = client.get(f"{SUPABASE_URL}/rest/v1/cars",
                       params={**params, "limit": page, "offset": offset},
                       headers=_rest_headers(), timeout=60)
        if r.status_code != 200:
            sys.exit(f"DB read fail: {r.status_code} {r.text[:200]}")
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    rows = [c for c in out
            if any(_is_blocked(u or "") for u in (c.get("images") or []))]
    return rows[:limit] if limit else rows


def mirror_rows(rows: list[dict], workers: int = 4) -> int:
    """Mirror blocked images for an in-memory batch of just-upserted car
    rows (each needs source/source_id/images). Called by the pipeline right
    after the DB upsert so freshly-scraped raw CDN URLs are rehosted in the
    same run — otherwise a self-chaining scrape overwrites already-mirrored
    URLs with raw ones and the catalog flickers broken until the next cron.
    Returns the number of images rehosted."""
    if not SERVICE_KEY:
        print("[mirror] no service key — skip inline mirror", flush=True)
        return 0
    todo = [r for r in rows
            if r and any(_is_blocked(u or "") for u in (r.get("images") or []))]
    if not todo:
        return 0
    ok = 0
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(mirror_car, client, r) for r in todo]
            for f in as_completed(futs):
                try:
                    ok += f.result()[0]
                except Exception as e:
                    print(f"  ! inline mirror: {e}", flush=True)
    print(f"[mirror] inline: {ok} images rehosted across {len(todo)} cars",
          flush=True)
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--source", default=None,
                    help="restrict to a single source (e.g. guazi)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if not SERVICE_KEY:
        sys.exit("SUPABASE_SERVICE_KEY (service_role) required for storage writes")

    with httpx.Client() as client:
        cars = fetch_cars(client, args.limit, args.source)
        print(f"Cars needing mirror: {len(cars)}")
        if not cars:
            return

        start = time.time()
        done = ok_imgs = bad_imgs = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(mirror_car, client, c): c for c in cars}
            for fut in as_completed(futures):
                c = futures[fut]
                try:
                    ok, total = fut.result()
                except Exception as e:
                    print(f"  ! {c['source']}/{c['source_id']} crashed: {e}")
                    ok, total = 0, 0
                done += 1
                ok_imgs += ok
                bad_imgs += (total - ok)
                if done % 10 == 0 or done == len(cars):
                    rate = done / max(time.time() - start, 1e-3)
                    eta = (len(cars) - done) / max(rate, 1e-3)
                    print(f"  [{done}/{len(cars)}] imgs ok={ok_imgs} "
                          f"fail={bad_imgs}, {rate:.1f} cars/s, "
                          f"ETA {eta/60:.1f} min")

        print(f"\nDone in {(time.time()-start)/60:.1f} min. "
              f"Cars: {done}, images ok: {ok_imgs}, fail: {bad_imgs}")


if __name__ == "__main__":
    main()
