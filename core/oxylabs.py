"""Oxylabs Web Scraper API client.

POSTs to https://realtime.oxylabs.io/v1/queries with HTTP Basic auth.
Returns rendered HTML for any URL — bypasses bans, optionally executes JS.

Env:
  OXYLABS_USERNAME, OXYLABS_PASSWORD   required
  OXYLABS_GEO                          default geo_location, e.g. "Kazakhstan"
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

WSA_URL = "https://realtime.oxylabs.io/v1/queries"
DEFAULT_TIMEOUT = 120.0


class OxylabsWSA:
    def __init__(self, username: str | None = None, password: str | None = None,
                 geo: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.user = username or os.getenv("OXYLABS_USERNAME") or ""
        self.pwd = password or os.getenv("OXYLABS_PASSWORD") or ""
        self.geo = geo or os.getenv("OXYLABS_GEO") or ""
        self.timeout = timeout
        self._client = httpx.Client(
            auth=(self.user, self.pwd), timeout=timeout
        ) if self.enabled else None

    @property
    def enabled(self) -> bool:
        return bool(self.user and self.pwd)

    def close(self) -> None:
        if self._client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def get(
        self,
        url: str,
        render: bool = False,
        source: str = "universal",
        geo: str | None = None,
        user_agent_type: str = "desktop",
        retries: int = 2,
    ) -> str | None:
        if not self.enabled or not self._client:
            return None
        body: dict[str, Any] = {
            "source": source,
            "url": url,
            "user_agent_type": user_agent_type,
        }
        if render:
            body["render"] = "html"
        g = geo or self.geo
        if g:
            body["geo_location"] = g
        for i in range(retries + 1):
            try:
                r = self._client.post(WSA_URL, json=body)
                if r.status_code == 200:
                    data = r.json()
                    results = data.get("results") or []
                    if results:
                        return results[0].get("content")
                    print(f"[oxylabs] empty results for {url}", file=sys.stderr)
                    return None
                if r.status_code in (429, 503):
                    wait = 2 ** i
                    print(f"[oxylabs] {r.status_code} backoff {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"[oxylabs] {url} -> {r.status_code} {r.text[:200]}",
                      file=sys.stderr)
                return None
            except Exception as e:
                print(f"[oxylabs] {url} attempt {i+1}: {e}", file=sys.stderr)
                time.sleep(1.5 * (i + 1))
        return None
