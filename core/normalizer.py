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

from core.kolesa_index import KolesaIndex

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
CACHE_PATH = Path(".cache/normalizer.sqlite")
BATCH_SIZE = 20
TIMEOUT = 60.0
APP_NAME = "car-scraper"
APP_URL = "https://github.com/DexterKexter/car-scraper"

SYSTEM_PROMPT = """You normalize used-car listing data from sites Guazi, Encar, Kolesa, Autocango.
Output 4 canonical fields matching the target DB schema (cars table):
  mark           — canonical English brand name (DB: cars.mark)
  model_family   — kolesa-level aggregate model/class name. ALWAYS filled.
                   For class-having brands: the class name ("C-Class", "5 Series", "ES").
                   For all other brands: the bare model name ("Q3", "X5", "Camry", "HR-V").
  model          — engine-spec variant when applicable; else equals model_family.
                   Mercedes-Benz: "C 260 L", "E 250", "GLC 300", "AMG GT 63 S".
                   BMW Series:   "530Li", "325Li", "740Li", "M340i".
                   BMW X/M/i/Z:  same as model_family ("X5", "M3", "i7", "Z4").
                   Lexus:        "ES 300h", "RX 350", "NX 250", "LX 600".
                   Audi/Genesis/Cadillac/Volvo/Porsche/etc.: same as model_family.
                   Toyota/Honda/Kia/Hyundai/mass-market: same as model_family.
  complectation  — full trim/package/edition text remaining after model is extracted.
                   "4MATIC AMG Line", "M Sport Package", "xDrive40i M Night",
                   "35 TFSI Fashion Dynamic", "2.5 Hybrid XSE", "F Sport".

Also output the matching kolesa.kz brand/model slug when given a candidate.

CHINESE SUB-BRAND MAP (parent / sub-brand split):
  "Dongfeng Aeolus"    -> brand="Dongfeng",   vehicle_class="Aeolus"     (Fengshen/风神 lineup)
  "Dongfeng Fengxing"  -> brand="Dongfeng",   vehicle_class="Fengxing"
  "Dongfeng Voyah"     -> brand="Dongfeng",   vehicle_class="Voyah"      (or Voyah as separate brand if site treats so)
  "Dongfeng M-Hero"    -> brand="Dongfeng",   vehicle_class="M-Hero"
  "GAC Trumpchi"       -> brand="GAC",        vehicle_class="Trumpchi"
  "GAC Aion"           -> brand="GAC",        vehicle_class="Aion"
  "GAC Hyptec"         -> brand="GAC",        vehicle_class="Hyptec"
  "SAIC Roewe"         -> brand="Roewe"       (Roewe is recognized as standalone)
  "SAIC Maxus"         -> brand="Maxus"
  "SAIC-GM Wuling"     -> brand="Wuling"
  "FAW Hongqi"         -> brand="Hongqi"
  "FAW Bestune"        -> brand="Bestune"
  "Geely Galaxy"       -> brand="Geely",      vehicle_class="Galaxy"
  "Geely Geometry"     -> brand="Geometry"    (Geometry is now standalone EV brand)
  "Geely Zeekr"        -> brand="Zeekr"       (standalone)
  "Geely Lynk & Co"    -> brand="Lynk & Co"
  "Chery Jetour"       -> brand="Jetour"
  "Chery Exeed"        -> brand="EXEED"
  "Chery iCar"         -> brand="iCar"
  "Chery Omoda"        -> brand="OMODA"
  "Chery Jaecoo"       -> brand="Jaecoo"
  "Great Wall Haval"   -> brand="Haval"
  "Great Wall Tank"    -> brand="Tank"
  "Great Wall Wey"     -> brand="Wey"
  "Great Wall Ora"     -> brand="Ora"
  "Changan Deepal"     -> brand="Deepal"
  "Changan Avatr"      -> brand="Avatr"
  "BYD Denza"          -> brand="Denza"
  "BYD Yangwang"       -> brand="Yangwang"
  "BYD Fang Cheng Bao" -> brand="Fang Cheng Bao"
  "NIO ONVO"           -> brand="ONVO"        (NIO sub-brand)
  "NIO Firefly"        -> brand="Firefly"     (NIO sub-brand)
  "Huawei Luxeed"      -> brand="Luxeed"      (Huawei+Chery)
  "Huawei AITO"        -> brand="AITO"        (Huawei+Seres)
  "Huawei Stelato"     -> brand="Stelato"     (Huawei+BAIC)
  "Huawei Maextro"     -> brand="Maextro"     (Huawei+JAC)
  Rule: if a sub-brand has its own dealer network + own model line, treat it as standalone brand.
        If it's just a series under parent, use vehicle_class.

JDM / CHINA-ONLY MODEL ALIAS MAP (map to the global/kolesa name when listing kolesa_model_slug):
  Honda:    "Vezel"->"HR-V", "Fit"->"Jazz", "Inspire"->"Accord", "Avancier"->"Passport",
            "Envix"->"Civic", "Crider"->"Civic", "Breeze"->"CR-V".
  Toyota:   "Wildlander"->"RAV4", "Frontlander"->"Corolla Cross", "Levin"->"Corolla",
            "Allion"->"Corolla", "Vios"->"Yaris" (sedan), "Crown Kluger"->"Highlander",
            "Vellfire"->"Alphard" (premium twin, keep as Vellfire if kolesa has it).
  Nissan:   "Sylphy"->"Sentra", "Teana"->"Altima", "X-Trail"->"Rogue" (US name),
            "Lannia"->no-match.
  Mazda:    "Atenza"->"Mazda 6", "Axela"->"Mazda 3", "Demio"->"Mazda 2",
            "CX-4"->no-match (China-only).
  Mitsubishi: "ASX"->"RVR" or "Outlander Sport", "Triton"->"L200",
              "Pajero Sport"->"Montero Sport".
  Haval:    "H Dog"->"Dargo", "Big Dog"->"Dargo", "Cool Dog"->"Jolion",
            "Xiaolong"->"Xiaolong Max" (or no-match), "Chitu"->"Chitu".
  Hyundai:  "Avante"->"Elantra", "Mufasa"->no-match (China-only),
            "Lafesta"->no-match, "Custin"->no-match, "Bayon"->"Bayon".
  Kia:      "K3"->"K3", "K5"->"K5" or "Optima" (older), "K7"->"K7" or "Cadenza",
            "K8"->"K8" (newer), "K9"->"K900".
  Volkswagen: "Lavida"->no-match (China), "Lamando"->no-match, "Santana"->"Santana"
              (modern China rebadged Polo-based).
  Chevrolet: "Cavalier"->no-match (modern China sedan), "Monza"->no-match (China),
             "Tracker"->"Tracker", "Captiva"->"Captiva" (different gen).
  BYD:      "Atto 3"->"Yuan Plus" (China name), "Seal"->"Seal",
            "Seagull"->"Dolphin Mini" (export), "Dolphin"->"Dolphin", "Han"->"Han".
  Geely:    "Coolray"->"Coolray", "Atlas"->"Atlas", "Azkarra"->"Azkarra",
            "Tugella"->"Tugella", "Preface"->no-match (newer), "Emgrand"->"Emgrand".
  Subaru:   "Forester"->"Forester", "Outback"->"Outback", "Tribeca"->"Tribeca".
  Rule: when raw model has JDM/China-only name, set kolesa_model_slug to the global slug
        from the candidate.models list. If no kolesa entry matches even after alias, leave empty.

FIELDS:

CLASS-HAVING BRANDS (model_family stores the class name, model stores the engine badge):
  Mercedes-Benz classes: A-Class, B-Class, C-Class, E-Class, S-Class,
    CLA-Class, CLS-Class, CL-Class, CLK-Class,
    G-Class, GL-Class, GLA-Class, GLB-Class, GLC-Class, GLE-Class, GLS-Class,
    M-Class (ML-Class), R-Class, SL-Class, SLK-Class, V-Class,
    AMG GT, Maybach S-Class, Maybach GLS,
    EQA, EQB, EQC, EQE, EQS, EQE SUV, EQS SUV,
    Sprinter, Vito, Viano (commercial - model_family = the badge).
  BMW series only:
    1 Series, 2 Series, 2 Series Active Tourer, 2 Series Gran Coupe,
    3 Series, 4 Series, 5 Series, 6 Series, 7 Series, 8 Series.
    (BMW X1-X7, M2-M8, i3-iX, Z3, Z4, XM are NOT classes — model_family = the badge.)
  Lexus families:
    CT, ES, GS, GX, HS, IS, LC, LM, LS, LX, NX, RC, RX, RZ, SC, TX, UX.

For ALL OTHER BRANDS (Audi, Genesis, Cadillac, Infiniti, Volvo, Porsche, Lincoln,
Acura, Land Rover, Jaguar, Bentley, Rolls-Royce, Aston Martin, Ferrari, Lamborghini,
McLaren, Maserati, Alfa Romeo, Toyota, Honda, Nissan, Mazda, Mitsubishi, Subaru,
Suzuki, Hyundai, Kia, SsangYong, Daewoo, Ford, Chevrolet, Buick, Jeep, Ram, Dodge,
Chrysler, GMC, Tesla, Rivian, Lucid, VW, Skoda, SEAT, Renault, Peugeot, Citroen,
Fiat, Opel, Mini, Smart, Geely, BYD, Chery, Haval, Great Wall, Changan, GAC, Hongqi,
FAW, Dongfeng, NIO, Xpeng, Li, Zeekr, BAIC, JAC, MG, Wuling, Voyah, Avatr, Aito,
Luxeed, Denza, ONVO, Roewe, Maxus, Tank, Lynk & Co, Jetour, OMODA, EXEED, Jaecoo,
Geometry, Mansory, LEVC, ВАЗ, ГАЗ, УАЗ, Москвич, etc.):
  model_family = model = the bare model name (Q3, X5, Camry, HR-V, Sportage, Phantom, 488, 911).

FIELDS:

mark: standard English brand name.
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

KOLESA MAPPING (set kolesa_brand_slug, kolesa_model_slug, in_kolesa):
The user message gives you, for each record, a `kolesa_candidate` object with the best
fuzzy-matched kolesa brand + that brand's full model list. Rules:
  - If raw brand clearly matches the candidate, set kolesa_brand_slug = candidate.brand_slug.
  - If raw model matches one of candidate.models (case-insensitive, ignore brand prefix),
    set kolesa_model_slug to that model's slug.
  - in_kolesa = true only if BOTH brand AND model are matched in kolesa.
  - If raw brand is a sub-brand (e.g. Aeolus, Trumpchi, Voyah), map kolesa_brand_slug
    to the PARENT brand on kolesa (kolesa lists them under parent: dong-feng, gac, voyah).
  - If no candidate fits, leave kolesa_*_slug empty, in_kolesa=false.
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
                "mark": r_.get("mark", ""),
                "model_family": r_.get("model_family", ""),
                "model": r_.get("model", ""),
                "complectation": r_.get("complectation", ""),
                "kolesa_brand_slug": r_.get("kolesa_brand_slug", ""),
                "kolesa_model_slug": r_.get("kolesa_model_slug", ""),
                "in_kolesa": bool(r_.get("in_kolesa", False)),
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
