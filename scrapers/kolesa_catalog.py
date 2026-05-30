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


def _get(client: httpx.Client, url: str, retries: int = 4, delay: float = 0.6) -> str | None:
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
    if "?" in href:
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


def _proxy_url() -> str | None:
    if env := os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"):
        return env
    user = os.getenv("OXYLABS_USERNAME")
    pwd = os.getenv("OXYLABS_PASSWORD")
    if not (user and pwd):
        return None
    host = os.getenv("OXYLABS_HOST", "pr.oxylabs.io")
    port = os.getenv("OXYLABS_PORT", "7777")
    country = os.getenv("OXYLABS_COUNTRY", "").strip()  # e.g. "kz", "us"
    u = f"customer-{user}-cc-{country}" if country else f"customer-{user}"
    return f"http://{u}:{pwd}@{host}:{port}"


def build_catalog(workers: int = 3, brand_limit: int | None = None) -> dict:
    proxy = _proxy_url()
    if proxy:
        safe = re.sub(r"://[^@]+@", "://***@", proxy)
        print(f"[kolesa] using proxy {safe}", file=sys.stderr)
    client_kw: dict = {"verify": True}
    if proxy:
        client_kw["proxy"] = proxy
    with httpx.Client(**client_kw) as client:
        html = _get(client, BRAND_LIST_URL)
        if not html:
            raise SystemExit("Failed to fetch brand list page")
        brands = parse_brands(html)
        print(f"[kolesa] brands found: {len(brands)}", file=sys.stderr)
        if brand_limit:
            brands = brands[:brand_limit]
        out: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_brand_models, client, b): b for b in brands}
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
