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

    def _query(self, body: dict, retries: int = 2) -> str | None:
        """Submit a WSA job, return results[0].content or None."""
        if not self.enabled or not self._client:
            return None
        for i in range(retries + 1):
            try:
                r = self._client.post(WSA_URL, json=body)
                if r.status_code == 200:
                    data = r.json()
                    results = data.get("results") or []
                    if results:
                        return results[0].get("content")
                    print(f"[oxylabs] empty results for {body.get('url')}",
                          file=sys.stderr)
                    return None
                if r.status_code in (429, 503):
                    wait = 2 ** i
                    print(f"[oxylabs] {r.status_code} backoff {wait}s",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"[oxylabs] {body.get('url')} -> {r.status_code} "
                      f"{r.text[:200]}", file=sys.stderr)
                return None
            except Exception as e:
                print(f"[oxylabs] attempt {i+1}: {e}", file=sys.stderr)
                time.sleep(1.5 * (i + 1))
        return None

    def get(
        self,
        url: str,
        render: bool = False,
        source: str = "universal",
        geo: str | None = None,
        user_agent_type: str = "desktop",
        retries: int = 2,
    ) -> str | None:
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
        return self._query(body, retries=retries)

    def post_json(
        self,
        url: str,
        json_body: dict,
        headers: dict | None = None,
        geo: str | None = None,
        user_agent_type: str = "desktop",
        retries: int = 2,
    ) -> str | None:
        """POST a JSON body to `url` via WSA. Returns the response body string
        (typically the JSON the target API returned). Use for endpoints behind
        bot-walls (Tencent EdgeOne, Cloudflare etc.) where direct httpx 403s.

        Oxylabs API uses `request.body` for the POST payload — `http_method`
        is ignored on the public universal source. We pass body as a dict (the
        API serializes it server-side) and an explicit Content-Type header so
        the upstream sees `application/json`.
        """
        import json as _json
        body: dict[str, Any] = {
            "source": "universal",
            "url": url,
            "user_agent_type": user_agent_type,
            # Oxylabs convention for non-GET targets — `request.body` triggers
            # POST + serializes the dict. Some accounts also need the legacy
            # `body` at top level, so we send both.
            "request": {"method": "POST", "body": json_body},
            "body": _json.dumps(json_body),
        }
        merged_headers = {"content-type": "application/json"}
        if headers:
            merged_headers.update(headers)
        body["headers"] = [{"key": k, "value": v}
                            for k, v in merged_headers.items()]
        g = geo or self.geo
        if g:
            body["geo_location"] = g
        return self._query(body, retries=retries)
