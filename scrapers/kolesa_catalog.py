"""Kolesa.kz brand+model catalog scraper.

Builds a master taxonomy of brands and models from kolesa.kz used-car listings.
Outputs JSON: { "brand_slug": { name, count, url, models: [{slug, name, count}, ...] }, ... }
Used as ground truth for the AI normalizer.

Run:
  python -m scrapers.kolesa_catalog --out out/kolesa_catalog.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from core.oxylabs import OxylabsWSA

BASE = "https://kolesa.kz"
BRAND_LIST_URL = f"{BASE}/cars/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.7",
}
# matches <li class="...cross-links__item..."><a href="/cars/<slug>">Name</a><span>count</span>
ITEM_RE = re.compile(
    r'<li[^>]*cross-links__item[^>]*>\s*<a\s+href="(/cars/[^"]+)"\s*>([^<]+)</a>'
    r'\s*<span[^>]*cross-links__count[^>]*>([\d\s]+)</span>',
    re.DOTALL,
)


def _get(
    client: httpx.Client | None,
    url: str,
    retries: int = 4,
    delay: float = 0.6,
    wsa: OxylabsWSA | None = None,
) -> str | None:
    if wsa and wsa.enabled:
        return wsa.get(url)
    time.sleep(delay)
    for i in range(retries):
        try:
            r = client.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 503):
                wait = (2 ** i) * 2.0
                print(f"[kolesa] {url} {r.status_code} backoff {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"[kolesa] {url} -> {r.status_code}", file=sys.stderr)
            return None
        except Exception as e:
            wait = (2 ** i) * 1.5
            print(f"[kolesa] {url} attempt {i+1}: {type(e).__name__} backoff {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    return None


def _is_brand_path(href: str) -> bool:
    # brand URLs are /cars/<slug>  (no trailing slash)
    # city  URLs are /cars/<slug>/ (with trailing slash)
    if "?" in href or href.endswith("/"):
        return False
    parts = href.strip("/").split("/")
    return len(parts) == 2 and parts[0] == "cars"


def _is_model_path(href: str, brand_slug: str) -> bool:
    if "?" in href:
        return False
    parts = href.strip("/").split("/")
    return len(parts) == 3 and parts[0] == "cars" and parts[1] == brand_slug


def parse_brands(html: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for href, name, count in ITEM_RE.findall(html):
        if not _is_brand_path(href):
            continue
        slug = href.strip("/").split("/")[1]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({
            "slug": slug,
            "name": name.strip(),
            "count": int(count.replace(" ", "")) if count else 0,
            "url": BASE + href,
        })
    return out


def parse_models(html: str, brand_slug: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for href, name, count in ITEM_RE.findall(html):
        if not _is_model_path(href, brand_slug):
            continue
        slug = href.strip("/").split("/")[2]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({
            "slug": slug,
            "name": name.strip(),
            "count": int(count.replace(" ", "")) if count else 0,
        })
    # fallback for brand pages with simpler markup
    if not out:
        for m in re.finditer(
            rf'<a\s+href="(/cars/{re.escape(brand_slug)}/[a-z0-9_-]+)/?"', html
        ):
            slug = m.group(1).strip("/").split("/")[2]
            if slug not in seen:
                seen.add(slug)
                out.append({"slug": slug, "name": slug.replace("-", " ").title(), "count": 0})
    return out


def fetch_brand_models(client: httpx.Client, brand: dict) -> dict:
    html = _get(client, brand["url"])
    models = parse_models(html, brand["slug"]) if html else []
    return {**brand, "models": models}


def build_catalog(workers: int = 3, brand_limit: int | None = None) -> dict:
    wsa = OxylabsWSA(geo=os.getenv("OXYLABS_COUNTRY") or os.getenv("OXYLABS_GEO") or "Kazakhstan")
    if wsa.enabled:
        print(f"[kolesa] using Oxylabs Web Scraper API (geo={wsa.geo})", file=sys.stderr)
    with httpx.Client() as client:
        html = _get(client, BRAND_LIST_URL, wsa=wsa)
        if not html:
            raise SystemExit("Failed to fetch brand list page")
        brands = parse_brands(html)
        print(f"[kolesa] brands found: {len(brands)}", file=sys.stderr)
        if brand_limit:
            brands = brands[:brand_limit]
        out: dict[str, dict] = {}
        def _fetch(b: dict) -> dict:
            html = _get(client, b["url"], wsa=wsa)
            return {**b, "models": parse_models(html, b["slug"]) if html else []}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch, b): b for b in brands}
            for i, fut in enumerate(as_completed(futs), 1):
                b = fut.result()
                out[b["slug"]] = b
                print(f"[kolesa] {i:3}/{len(brands)} {b['slug']:20} models={len(b['models']):3} count={b['count']}",
                      file=sys.stderr)
        return {
            "source": "kolesa.kz",
            "brand_count": len(out),
            "model_count": sum(len(b["models"]) for b in out.values()),
            "brands": out,
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/kolesa_catalog.json")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--brand-limit", type=int, default=None,
                    help="Stop after N brands (for testing)")
    args = ap.parse_args()
    catalog = build_catalog(workers=args.workers, brand_limit=args.brand_limit)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(catalog, ensure_ascii=False, indent=2))
    print(f"\nBrands: {catalog['brand_count']}  Models: {catalog['model_count']}  -> {args.out}")


if __name__ == "__main__":
    main()
