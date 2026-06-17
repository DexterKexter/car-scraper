"""AI normalizer via OpenRouter.

Reads scraper output JSON, sends batches to an LLM (default Owl Alpha
via OpenRouter), returns canonical brand/model/trim per listing.
Local sqlite cache dedupes calls across runs.

Env:
  OPENROUTER_API_KEY    required to call the model
  OPENROUTER_MODEL      optional, default openrouter/owl-alpha

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

from core.kolesa_index import KolesaIndex

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/owl-alpha"
CACHE_PATH = Path(".cache/normalizer.sqlite")
BATCH_SIZE = 20
TIMEOUT = 60.0
APP_NAME = "car-scraper"
APP_URL = "https://github.com/DexterKexter/car-scraper"

SYSTEM_PROMPT = """You normalize used-car listing data from sites Guazi, Encar, Kolesa, Autocango.
Output exactly these 4 canonical fields (+ kolesa slugs):

  mark           - canonical English brand name (DB: cars.mark, brands.name)
  model_family   - aggregate class/family name used for grouping on the catalog.
                   ALWAYS filled. Examples below per brand.
  model          - concrete model with engine badge. ALWAYS filled.
                   This is the MOST important field - concrete model > class.
                   For class-having brands it's the badge inside the class.
                   For all other brands it's the bare model name.
  complectation  - trim / package / edition text that REMAINS after model is extracted.
                   May be empty.

CRITICAL RULE: `model` is ALWAYS the concrete variant. NEVER put the class name
(like "3 Series", "C-Class", "ES") into `model`. The class goes only to
`model_family`. If the source only gives the class with no badge in the title,
set model = model_family (do NOT leave model empty).

BMW examples (class -> model_family, badge -> model):
   title                                              | model_family | model    | complectation
   "BMW 3 Series 2023 320Li M Sport Package"          | 3 Series     | 320Li    | M Sport Package
   "BMW 5 Series 2024 530Li Luxury Line"              | 5 Series     | 530Li    | Luxury Line
   "BMW 5 Series 2019 Restyled 530Li Luxury Edition M Sport Package"
                                                      | 5 Series     | 530Li    | Restyled Luxury Edition M Sport Package
   "BMW 320i F30 M Sport"                             | 3 Series     | 320i     | M Sport (F30 dropped)
   "BMW M340i xDrive Touring"                         | 3 Series     | M340i    | xDrive Touring
   "BMW X5 xDrive40i G05"                             | X5           | X5       | xDrive40i (no class for X-cars)
   "BMW M3 Competition"                               | M3           | M3       | Competition
   "BMW i7 xDrive60 M Sport"                          | i7           | i7       | xDrive60 M Sport
   "BMW Z4 sDrive30i"                                 | Z4           | Z4       | sDrive30i

   Class-having BMW: 1/2/3/4/5/6/7/8 Series ONLY.
   NOT classes: X1-X7, XM, M2-M8 (standalone), i3/i4/i5/i7/iX, Z3/Z4 - for these
                model_family = model = the badge itself.

Mercedes-Benz examples:
   "Mercedes-Benz CLS 2018 CLS 300 Dynamic"           | CLS-Class    | CLS 300  | Dynamic
   "Mercedes-Benz C-Class 2022 C 260 L AMG Line"      | C-Class      | C 260 L  | AMG Line
   "Mercedes-Benz GLE 450 4MATIC"                     | GLE-Class    | GLE 450  | 4MATIC
   "Mercedes-Benz AMG GT 63 S 4-Door"                 | AMG GT       | GT 63 S  | 4-Door
   "Mercedes-Benz EQE 500 4MATIC"                     | EQE          | EQE 500  | 4MATIC
   "Mercedes-Benz Sprinter 316 CDI"                   | Sprinter     | Sprinter | 316 CDI (commercial; family=model)

   Mercedes class names (model_family values): A-Class, B-Class, C-Class, E-Class, S-Class,
     CLA-Class, CLS-Class, CL-Class, CLK-Class,
     G-Class, GL-Class, GLA-Class, GLB-Class, GLC-Class, GLE-Class, GLS-Class,
     M-Class, R-Class, SL-Class, SLK-Class, V-Class,
     AMG GT, Maybach S-Class, Maybach GLS,
     EQA, EQB, EQC, EQE, EQS, EQE SUV, EQS SUV.
   Commercials (model_family = model = badge): Sprinter, Vito, Viano, Metris.

Lexus examples (class = family letters, model = letters + engine number):
   "Lexus ES 300h F Sport"                            | ES           | ES 300h  | F Sport
   "Lexus RX 350 Premium Plus"                        | RX           | RX 350   | Premium Plus
   "Lexus LX 600 Ultra Luxury"                        | LX           | LX 600   | Ultra Luxury

   Lexus families (model_family): CT, ES, GS, GX, HS, IS, LC, LM, LS, LX, NX, RC, RZ, SC, TX, UX.

Audi: model = the bare A/Q/RS/S/TT/R8/e-tron badge. NO class.
   "Audi A4 45 TFSI quattro S line"                   | A4           | A4       | 45 TFSI quattro S line
   "Audi RS6 Avant Performance"                       | RS6          | RS6      | Avant Performance
   "Audi Q5 50 TFSI e Black Optic"                    | Q5           | Q5       | 50 TFSI e Black Optic

All OTHER brands (Toyota/Honda/Hyundai/Kia/Genesis/Cadillac/Volvo/Porsche/
Land Rover/Jaguar/Bentley/Rolls-Royce/Aston Martin/Ferrari/Lamborghini/McLaren/
Maserati/Alfa Romeo/Nissan/Mazda/Mitsubishi/Subaru/Suzuki/SsangYong/Daewoo/
Ford/Chevrolet/Buick/Jeep/Ram/Dodge/Chrysler/GMC/Tesla/Rivian/Lucid/VW/Skoda/
SEAT/Renault/Peugeot/Citroen/Fiat/Opel/Mini/Smart/Geely/BYD/Chery/Haval/
Great Wall/Changan/GAC/Hongqi/FAW/Dongfeng/NIO/Xpeng/Li/Zeekr/BAIC/JAC/MG/
Wuling/Voyah/Avatr/Aito/Luxeed/Denza/ONVO/Roewe/Maxus/Tank/Lynk & Co/Jetour/
OMODA/EXEED/Jaecoo/Geometry/Mansory/LEVC/etc.):
  model_family = model = the bare model name. No class layer.
  Examples:
   "Toyota Camry 2.5 Hybrid XSE"                      | Camry        | Camry    | 2.5 Hybrid XSE
   "Honda CR-V Touring AWD"                           | CR-V         | CR-V     | Touring AWD
   "Hyundai Sonata DN8 Inspiration"                   | Sonata       | Sonata   | DN8 Inspiration (chassis -> trim)
   "Genesis GV70 2.5T AWD"                            | GV70         | GV70     | 2.5T AWD
   "Cadillac CT5 28T Platinum Sport"                  | CT5          | CT5      | 28T Platinum Sport
   "Porsche 911 Carrera 4S"                           | 911          | 911      | Carrera 4S
   "Tesla Model 3 Long Range"                         | Model 3      | Model 3  | Long Range

NORMALIZATION CLEANUP (applies to all brands):
  - Strip generation/chassis codes from model into trim: F30, G20, B9, W205, V167, E46, etc.
  - Strip "Used", "Year", "Restyled", "Facelifted", "Second Facelift" -> drop or trim.
  - Strip body variants ("Coupe","Cabriolet","Avant","Touring","Sportback") into trim.
  - Strip drivetrain suffixes (xDrive, 4MATIC, quattro, AWD) into trim WHEN class-having brand.
    For non-class brands those suffixes stay in trim too (the model is already the bare name).
  - Korean facelift markers ("4세대","All New","The New","Premium New") -> trim.

CHINESE SUB-BRAND MAP (when raw brand is "<parent> <sub>", split correctly):
  "Dongfeng Aeolus"    -> mark="Dongfeng"   (Aeolus as a model series)
  "Dongfeng Fengxing"  -> mark="Dongfeng"
  "Dongfeng Voyah"     -> mark="Voyah"      (standalone on kolesa)
  "Dongfeng M-Hero"    -> mark="M-Hero"
  "GAC Trumpchi"       -> mark="GAC"        (Trumpchi as series)
  "GAC Aion"           -> mark="Aion"       (standalone EV brand)
  "GAC Hyptec"         -> mark="Hyptec"
  "SAIC Roewe"         -> mark="Roewe"
  "SAIC Maxus"         -> mark="Maxus"
  "SAIC-GM Wuling"     -> mark="Wuling"
  "FAW Hongqi"         -> mark="Hongqi"
  "FAW Bestune"        -> mark="Bestune"
  "Geely Galaxy"       -> mark="Geely"      (Galaxy as series)
  "Geely Geometry"     -> mark="Geometry"
  "Geely Zeekr"        -> mark="Zeekr"
  "Geely Lynk & Co"    -> mark="Lynk & Co"
  "Chery Jetour"       -> mark="Jetour"
  "Chery Exeed"        -> mark="EXEED"
  "Chery iCar"         -> mark="iCar"
  "Chery Omoda"        -> mark="OMODA"
  "Chery Jaecoo"       -> mark="Jaecoo"
  "Great Wall Haval"   -> mark="Haval"
  "Great Wall Tank"    -> mark="Tank"
  "Great Wall Wey"     -> mark="Wey"
  "Great Wall Ora"     -> mark="Ora"
  "Changan Deepal"     -> mark="Deepal"
  "Changan Avatr"      -> mark="Avatr"
  "BYD Denza"          -> mark="Denza"
  "BYD Yangwang"       -> mark="Yangwang"
  "BYD Fang Cheng Bao" -> mark="Fang Cheng Bao"
  "NIO ONVO"           -> mark="ONVO"
  "NIO Firefly"        -> mark="Firefly"
  "Huawei Luxeed"      -> mark="Luxeed"
  "Huawei AITO"        -> mark="AITO"
  "Huawei Stelato"     -> mark="Stelato"
  "Huawei Maextro"     -> mark="Maextro"

MARK NORMALIZATION:
  "Geely Auto" -> "Geely", "land" -> "Land Rover", "기아" -> "Kia",
  "제네시스" -> "Genesis", "KG모빌리티(쌍용)" -> "SsangYong",
  "르노코리아(삼성)" -> "Renault Korea", "쉐보레(GM대우)" -> "Chevrolet",
  "벤츠" -> "Mercedes-Benz", "奔驰" -> "Mercedes-Benz", "宝马" -> "BMW", "奥迪" -> "Audi".

JDM / CHINA-ONLY MODEL ALIAS MAP (used ONLY for setting kolesa_model_slug -
the model_family/model fields keep the source name):
  Honda:    "Vezel"->"HR-V", "Fit"->"Jazz", "Inspire"->"Accord", "Avancier"->"Passport",
            "Envix"->"Civic", "Crider"->"Civic", "Breeze"->"CR-V".
  Toyota:   "Wildlander"->"RAV4", "Frontlander"->"Corolla Cross", "Levin"->"Corolla",
            "Allion"->"Corolla", "Vios"->"Yaris", "Crown Kluger"->"Highlander".
  Nissan:   "Sylphy"->"Sentra", "Teana"->"Altima", "X-Trail"->"Rogue".
  Mazda:    "Atenza"->"Mazda 6", "Axela"->"Mazda 3", "Demio"->"Mazda 2".
  Mitsubishi: "ASX"->"RVR" or "Outlander Sport", "Triton"->"L200",
              "Pajero Sport"->"Montero Sport".
  Haval:    "H Dog"->"Dargo", "Big Dog"->"Dargo", "Cool Dog"->"Jolion".
  Hyundai:  "Avante"->"Elantra", "Staria"->"Staria" (NOT Starex - Staria
            is the 2021+ successor, Starex was discontinued. Never collapse
            them.), "Grandeur"->"Grandeur", "Sonata"->"Sonata".
  Kia:      "스타리아"->Hyundai brand, never Kia. Korean katakana model names
            translate as-is (카니발=Carnival, K7=K7, 그랜저=Grandeur).
  Volkswagen: "Santana"->"Santana" (China rebadge of Polo).
  BYD:      "Atto 3"->"Yuan Plus", "Seagull"->"Dolphin Mini" (export).
  Geely:    "Coolray"->"Coolray", "Atlas"->"Atlas", "Tugella"->"Tugella", "Emgrand"->"Emgrand".

  Rule: when raw model has a JDM/China-only name, set kolesa_model_slug to the global
  slug from the candidate.models list. If no kolesa entry matches even after alias,
  leave kolesa_model_slug empty.

KOLESA MAPPING (set kolesa_brand_slug, kolesa_model_slug, in_kolesa):
The user message gives you, for each record, a `kolesa_candidate` object with the best
fuzzy-matched kolesa brand + that brand's full model list. Rules:
  - If raw brand clearly matches the candidate, set kolesa_brand_slug = candidate.brand_slug.
  - Match the CONCRETE model (your `model` field, not `model_family`) against candidate.models.
    Use the JDM alias map above when raw model is China-only.
  - in_kolesa = true ONLY if BOTH brand AND model are matched in kolesa.
  - If raw brand is a sub-brand (e.g. Aeolus, Trumpchi, Voyah, Fang Cheng Bao),
    point kolesa_brand_slug at the PARENT brand on kolesa (kolesa lists them under
    parent: dong-feng, gac, voyah, byd).
  - If no candidate fits, leave kolesa_*_slug empty and in_kolesa=false.

Output a single JSON object {"results": [...]} matching the schema. Empty
string is only allowed for complectation, kolesa_brand_slug, and
kolesa_model_slug. mark, model_family, and model MUST be filled.
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
                    "mark": {"type": "string"},
                    "model_family": {"type": "string"},
                    "model": {"type": "string"},
                    "complectation": {"type": "string"},
                    "kolesa_brand_slug": {"type": "string"},
                    "kolesa_model_slug": {"type": "string"},
                    "in_kolesa": {"type": "boolean"},
                },
                "required": [
                    "id", "mark", "model_family", "model", "complectation",
                    "kolesa_brand_slug", "kolesa_model_slug", "in_kolesa",
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


def _build_prompt(items: list[dict], idx: KolesaIndex | None = None) -> str:
    payload = []
    for it in items:
        rec = {
            "id": str(it["__id"]),
            "site": it.get("site", ""),
            "brand": it.get("brand", ""),
            "model": it.get("model", ""),
            "title": it.get("title", ""),
        }
        if idx and idx.loaded:
            slug = idx.match_brand(it.get("brand", "")) or ""
            if slug:
                b = idx.brand(slug)
                rec["kolesa_candidate"] = {
                    "brand_slug": slug,
                    "brand_name": b.get("name", ""),
                    "models": [
                        {"slug": m["slug"], "name": m["name"]}
                        for m in b.get("models", [])
                    ][:80],
                }
        payload.append(rec)
    return (
        "Normalize the following used-car records. Return ONE tool call.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _call_openrouter(
    client: httpx.Client, api_key: str, model: str, items: list[dict],
    idx: KolesaIndex | None = None,
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
            {"role": "user", "content": _build_prompt(items, idx)},
        ],
        # Force JSON via response_format (NOT a forced tool_choice): Owl
        # Alpha's OpenRouter routing 404s on tool_choice but supports
        # structured_outputs / response_format. The response arrives in
        # message.content and is parsed by the content fallback below.
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "normalized",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            },
        },
        "temperature": 0,
        # Cap output: a batch of ~20 normalized records is a few thousand
        # tokens. Without this, OpenRouter reserves the model's default
        # (some models default to 64k+), which can 402 on a tight credit
        # balance and fail the batch → raw CJK brand/model values leak.
        "max_tokens": 8192,
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
                "mark": r_.get("mark", ""),
                "model_family": r_.get("model_family", ""),
                "model": r_.get("model", ""),
                "complectation": r_.get("complectation", ""),
                "kolesa_brand_slug": r_.get("kolesa_brand_slug", ""),
                "kolesa_model_slug": r_.get("kolesa_model_slug", ""),
                "in_kolesa": bool(r_.get("in_kolesa", False)),
            }
    return out


def _dedup_consecutive(s: str) -> str:
    """Collapse consecutive duplicate words: 'Dana V1 V1' -> 'Dana V1'."""
    out: list[str] = []
    for w in s.split():
        if not out or out[-1].casefold() != w.casefold():
            out.append(w)
    return " ".join(out)


def _smart_title(s: str) -> str:
    """Title-case plain lowercase words while preserving alphanumeric model
    codes (H6, ix35, eπ007), all-caps acronyms (NAT, GS), and hyphenated
    codes (CR-V, EM-i). 'sienna' -> 'Sienna', 'nammi 01' -> 'Nammi 01'."""
    def fix(w: str) -> str:
        if any(ch.isdigit() for ch in w):  # H6, ix35, eπ007, 530
            return w
        if w.isupper():                     # NAT, GS, EV, CR-V
            return w
        if "-" in w:                        # already-cased hyphenated tokens
            return w
        return w[:1].upper() + w[1:]
    return " ".join(fix(w) for w in s.split())


def _clean_names(rec: dict) -> None:
    """Deterministic post-process for the LLM's name fields (also fixes cache
    hits). Conservative: dedup consecutive words + smart Title-case. No
    brand-strip (would corrupt sub-brands like 'Dongfeng NAMMI')."""
    for fld in ("mark", "model_family", "model"):
        v = rec.get(fld)
        if isinstance(v, str) and v:
            rec[fld] = _smart_title(_dedup_consecutive(v))


def _finalize(records: list[dict]) -> list[dict]:
    """Strip internal __ keys and apply name hygiene to every record."""
    out: list[dict] = []
    for r in records:
        rec = {k: v for k, v in r.items() if not k.startswith("__")}
        _clean_names(rec)
        out.append(rec)
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
    kolesa = KolesaIndex()
    if kolesa.loaded:
        print(f"[normalizer] kolesa catalog: {len(kolesa.catalog)} brands", file=sys.stderr)
    else:
        print("[normalizer] no kolesa catalog (out/kolesa_catalog.json missing)", file=sys.stderr)

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
        return _finalize(records)

    if not api_key:
        print(
            f"[normalizer] {len(pending)} pending, no OPENROUTER_API_KEY — skip LLM",
            file=sys.stderr,
        )
        cache.close()
        return _finalize(records)

    print(
        f"[normalizer] {len(pending)} pending (of {len(records)}), model={model}, batch={batch_size}",
        file=sys.stderr,
    )
    with httpx.Client() as client:
        for i in range(0, len(pending), batch_size):
            chunk = pending[i : i + batch_size]
            try:
                result = _call_openrouter(client, api_key, model, chunk, kolesa)
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
    return _finalize(records)


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
