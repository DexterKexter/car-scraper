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

SYSTEM_PROMPT = """You normalize used-car listing data from sites Guazi, Encar, Kolesa, Autocango.
For each input record output the canonical fields in this hierarchy:
brand -> vehicle_class -> model -> generation -> trim.

FIELDS:

brand_canonical: standard English brand name.
  Examples: "Geely Auto"->"Geely", "land"->"Land Rover", "기아"->"Kia",
  "제네시스"->"Genesis", "KG모빌리티(쌍용)"->"SsangYong", "르노코리아(삼성)"->"Renault Korea",
  "쉐보레(GM대우)"->"Chevrolet", "벤츠"->"Mercedes-Benz", "奔驰"->"Mercedes-Benz",
  "宝马"->"BMW", "奥迪"->"Audi".

vehicle_class: family/lineup name within the brand. Empty for brands without class structure.
  Mercedes-Benz:  "A-Class","B-Class","C-Class","E-Class","S-Class","CLA-Class","CLS-Class",
                  "GLA-Class","GLB-Class","GLC-Class","GLE-Class","GLS-Class","G-Class",
                  "EQ" (EQS/EQE/EQA/EQB/EQC), "AMG" (AMG GT lineup), "Maybach".
  BMW:            "1 Series","2 Series","3 Series","4 Series","5 Series","6 Series",
                  "7 Series","8 Series","X Series" (X1-X7), "Z Series" (Z4),
                  "M Series" (standalone M2/M3/M4/M5/M8), "i Series" (i3/i4/i5/i7/iX).
  Audi:           "A-Series" (A1-A8), "Q-Series" (Q2-Q8), "RS-Series", "S-Series",
                  "TT","R8","e-tron".
  Lexus:          "ES","IS","LS","LC","RC","NX","RX","GX","LX","UX".
  Genesis:        "G" (G70/G80/G90), "GV" (GV60/GV70/GV80).
  Cadillac:       "CT-Series" (CT4/CT5/CT6), "XT-Series" (XT4/XT5/XT6),
                  "Escalade","ATS","CTS".
  Infiniti:       "Q-Series","QX-Series".
  Volvo:          "S-Series" (S60/S90), "V-Series" (V60/V90), "XC-Series" (XC40/XC60/XC90).
  Porsche:        "911","718","Cayenne","Macan","Panamera","Taycan".
  Kia:            "K-Series" only if model is K3/K5/K7/K8/K9. Otherwise empty
                  (Sportage, Sorento, Carnival, Seltos, Morning, EV6 etc -> empty).
  Hyundai:        empty (no class lineup; Sonata/Elantra/Tucson/Santa Fe directly).
  Brands without class structure (most Asian/Chinese mass-market): leave empty.

model_canonical: bare model badge (the short identifier within the class).
  Strip generation/chassis codes and facelift markers from model. Strip body variants
  like "Coupe","Cabriolet","Avant","Touring" into trim. Strip engine/powertrain suffixes
  (xDrive40i, 45 TFSI, 4MATIC, quattro) into trim.
  Examples:
    "BMW 320i F30 M Sport"             -> "320i"
    "BMW 330i xDrive G20"              -> "330i"      (xDrive -> trim)
    "BMW X5 xDrive40i G05"             -> "X5"        (xDrive40i -> trim)
    "BMW M3 Competition"               -> "M3"        (Competition -> trim)
    "Mercedes-Benz C200 4MATIC W205"   -> "C200"      (4MATIC -> trim)
    "Mercedes-Benz GLE350 V167"        -> "GLE350"
    "Mercedes-Benz AMG GT 4-Door"      -> "GT 4-Door"
    "Audi A4 45 TFSI B9 quattro"       -> "A4"        (45 TFSI, quattro -> trim)
    "Audi RS6 Avant"                   -> "RS6"       (Avant -> trim)
    "Sportage 5th Gen"                 -> "Sportage"
    "Carnival 4th Gen"                 -> "Carnival"
    "Sonata LF"                        -> "Sonata"
    "All New Carnival"                 -> "Carnival"
    "The New Morning"                  -> "Morning"
    "GV80 Coupe"                       -> "GV80"      (Coupe -> trim)
    "rover range rover evoque"         -> "Range Rover Evoque"
    "auto preface"                     -> "Preface"
    "ct5"                              -> "CT5"
    "셀토스"                            -> "Seltos"
    "Lexus ES300h"                     -> "ES300h"
    "Genesis GV70 2.5T AWD"            -> "GV70"      (2.5T AWD -> trim)

generation: chassis code, generation number, or facelift marker.
  BMW chassis:    E30/E36/E46/E90/F30/G20 (3 Series); E39/E60/F10/G30 (5 Series);
                  E70/F15/G05 (X5); F40 (1 Series); etc.
  Mercedes:       W201/W202/W203/W204/W205/W206 (C-Class);
                  W210/W211/W212/W213 (E-Class); W221/W222/W223 (S-Class);
                  V167 (GLE).
  Audi:           B5/B6/B7/B8/B9 (A4); C5/C6/C7/C8 (A6).
  Porsche:        996/997/991/992 (911).
  Korean facelifts: "4세대","5세대","All New","The New","Premium New".
  Hyundai/Kia chassis: LF (Sonata), IG (Grandeur), AD/CN7 (Elantra), QM (Sportage NQ5).
  Empty if no generation marker present.

trim: edition, grade, package, body variant, drivetrain suffix.
  Strip "Used","Year","Model","Version","Edition" if redundant.
  Examples:
    "Used Cadillac CT5 2021 28T Platinum Sport Model" -> "28T Platinum Sport"
    "Used Geometry C 2022 400KM Commuter Version"     -> "400KM Commuter"
    "디젤 3.0 4WD 6인승"                              -> "3.0 Diesel 4WD 6-seat"
    "9인승 노블레스"                                  -> "9-seat Noblesse"
    "BMW X5 xDrive40i M Sport"                       -> "xDrive40i M Sport"
    "Audi A4 45 TFSI quattro S line"                 -> "45 TFSI quattro S line"

Empty string when a field cannot be inferred. Output via the JSON tool only.
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
                    "vehicle_class": {"type": "string"},
                    "model_canonical": {"type": "string"},
                    "generation": {"type": "string"},
                    "trim": {"type": "string"},
                },
                "required": [
                    "id", "brand_canonical", "vehicle_class",
                    "model_canonical", "generation", "trim",
                ],
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
                "vehicle_class": r_.get("vehicle_class", ""),
                "model_canonical": r_.get("model_canonical", ""),
                "generation": r_.get("generation", ""),
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
