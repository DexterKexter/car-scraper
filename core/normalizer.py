"""AI normalizer via OpenRouter.

Reads scraper output JSON, sends batches to an LLM (default Claude Haiku 4.5
via OpenRouter), returns canonical brand/model/trim per listing.
Local sqlite cache dedupes calls across runs.

Env:
  OPENROUTER_API_KEY    required to call the model
  OPENROUTER_MODEL      optional, default anthropic/claude-haiku-4.5

Usage:
  python -m core.normalizer raw.json norm.json [--model X] [--batch 20]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
CACHE_PATH = Path(".cache/normalizer.sqlite")
BATCH_SIZE = 20
TIMEOUT = 60.0
APP_NAME = "car-scraper"
APP_URL = "https://github.com/DexterKexter/car-scraper"

SYSTEM_PROMPT = """You normalize used-car listing data scraped from various sites (Guazi, Encar, Kolesa, Autocango).
For each input record, infer the canonical English brand and model name and extract trim/edition text.

RULES:
- brand_canonical: standard English brand name. Examples: "Geely Auto" -> "Geely"; "land" -> "Land Rover"; "기아" -> "Kia"; "제네시스" -> "Genesis"; "KG모빌리티(쌍용)" -> "SsangYong".
- model_canonical: standard English model name with proper capitalization. Examples: "rover range rover evoque" -> "Range Rover Evoque"; "auto preface" -> "Preface"; "카니발 4세대" -> "Carnival 4th Gen"; "ct5" -> "CT5".
- trim: the edition/grade/package text. Strip the year, engine spec, and the words "Used", "Model", "Version", "Edition" if redundant. Examples: "Used Cadillac CT5 2021 28T Platinum Sport Model" -> "28T Platinum Sport"; "Used Geometry C 2022 400KM Commuter Version" -> "400KM Commuter"; "디젤 3.0 4WD 6인승" -> "3.0 Diesel 4WD 6-seat".
- If a field cannot be reliably inferred, return an empty string.
- Output via the provided JSON tool only.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "brand_canonical": {"type": "string"},
                    "model_canonical": {"type": "string"},
                    "trim": {"type": "string"},
                },
                "required": ["id", "brand_canonical", "model_canonical", "trim"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _cache_open() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(CACHE_PATH))
    c.execute(
        "CREATE TABLE IF NOT EXISTS norm("
        "k TEXT PRIMARY KEY, v TEXT NOT NULL, model TEXT, ts INTEGER)"
    )
    return c


def _key(rec: dict) -> str:
    parts = [
        str(rec.get("site", "")).lower(),
        str(rec.get("brand", "")).lower(),
        str(rec.get("model", "")).lower(),
        str(rec.get("title", "")).lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _cache_get(c: sqlite3.Connection, k: str) -> dict | None:
    r = c.execute("SELECT v FROM norm WHERE k = ?", (k,)).fetchone()
    return json.loads(r[0]) if r else None


def _cache_put(c: sqlite3.Connection, k: str, v: dict, model: str) -> None:
    c.execute(
        "INSERT OR REPLACE INTO norm(k, v, model, ts) VALUES(?,?,?,?)",
        (k, json.dumps(v, ensure_ascii=False), model, int(time.time())),
    )
    c.commit()


def _build_prompt(items: list[dict]) -> str:
    payload = [
        {
            "id": str(it["__id"]),
            "site": it.get("site", ""),
            "brand": it.get("brand", ""),
            "model": it.get("model", ""),
            "title": it.get("title", ""),
        }
        for it in items
    ]
    return (
        "Normalize the following used-car records. Return ONE tool call.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _call_openrouter(
    client: httpx.Client, api_key: str, model: str, items: list[dict]
) -> dict[str, dict]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": APP_URL,
        "X-Title": APP_NAME,
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(items)},
        ],
        "tool_choice": {"type": "function", "function": {"name": "submit_normalized"}},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "submit_normalized",
                    "description": "Submit normalized records.",
                    "parameters": OUTPUT_SCHEMA,
                },
            }
        ],
        "temperature": 0,
    }
    r = client.post(OPENROUTER_URL, headers=headers, json=body, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:300]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    tc = (msg.get("tool_calls") or [{}])[0].get("function", {})
    args = tc.get("arguments")
    if not args:
        # fallback: model returned plain content JSON
        args = msg.get("content") or "{}"
    parsed = json.loads(args) if isinstance(args, str) else args
    out: dict[str, dict] = {}
    for r_ in parsed.get("results", []):
        if "id" in r_:
            out[str(r_["id"])] = {
                "brand_canonical": r_.get("brand_canonical", ""),
                "model_canonical": r_.get("model_canonical", ""),
                "trim": r_.get("trim", ""),
            }
    return out


def normalize(
    records: list[dict],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    batch_size: int = BATCH_SIZE,
) -> list[dict]:
    if not records:
        return records
    api_key = api_key or os.getenv("OPENROUTER_API_KEY")
    cache = _cache_open()

    pending: list[dict] = []
    for idx, rec in enumerate(records):
        k = _key(rec)
        hit = _cache_get(cache, k)
        if hit:
            rec.update(hit)
            rec.setdefault("_normalized_by", "cache")
        else:
            rec["__id"] = str(idx)
            rec["__key"] = k
            pending.append(rec)

    if not pending:
        print(f"[normalizer] all {len(records)} from cache", file=sys.stderr)
        cache.close()
        return [{k: v for k, v in r.items() if not k.startswith("__")} for r in records]

    if not api_key:
        print(
            f"[normalizer] {len(pending)} pending, no OPENROUTER_API_KEY — skip LLM",
            file=sys.stderr,
        )
        cache.close()
        return [{k: v for k, v in r.items() if not k.startswith("__")} for r in records]

    print(
        f"[normalizer] {len(pending)} pending (of {len(records)}), model={model}, batch={batch_size}",
        file=sys.stderr,
    )
    with httpx.Client() as client:
        for i in range(0, len(pending), batch_size):
            chunk = pending[i : i + batch_size]
            try:
                result = _call_openrouter(client, api_key, model, chunk)
            except Exception as e:
                print(f"[normalizer] batch {i//batch_size} err: {e}", file=sys.stderr)
                continue
            for rec in chunk:
                norm = result.get(rec["__id"])
                if norm:
                    rec.update(norm)
                    rec["_normalized_by"] = model
                    _cache_put(cache, rec["__key"], norm, model)

    cache.close()
    return [{k: v for k, v in r.items() if not k.startswith("__")} for r in records]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="raw JSON list (from scraper)")
    ap.add_argument("output", help="normalized JSON list")
    ap.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL))
    ap.add_argument("--batch", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    records = json.loads(Path(args.input).read_text())
    if not isinstance(records, list):
        raise SystemExit("input must be a JSON list")
    out = normalize(records, model=args.model, batch_size=args.batch)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"wrote {len(out)} -> {args.output}")


if __name__ == "__main__":
    main()
