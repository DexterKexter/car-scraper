"""Load kolesa_catalog.json and build lookup indexes for the normalizer.

Used to ground LLM normalization in real kolesa.kz brand/model taxonomy.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

CATALOG_PATH = Path("out/kolesa_catalog.json")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.lower()
    s = re.sub(r"[^a-z0-9а-яё]+", " ", s).strip()
    return s


class KolesaIndex:
    def __init__(self, catalog_path: Path | str = CATALOG_PATH):
        self.catalog: dict = {}
        self.brand_lookup: dict[str, str] = {}   # normalized name -> slug
        self.model_lookup: dict[str, dict[str, str]] = {}  # brand_slug -> {norm_name: model_slug}
        p = Path(catalog_path)
        if not p.exists():
            return
        data = json.loads(p.read_text())
        self.catalog = data.get("brands", {})
        for slug, b in self.catalog.items():
            for key in {_norm(b["name"]), _norm(slug)}:
                if key:
                    self.brand_lookup[key] = slug
            mmap: dict[str, str] = {}
            for m in b.get("models", []):
                # store under several normalized forms
                full = _norm(m["name"])                              # "audi a4"
                bare = _norm(re.sub(rf"^{re.escape(b['name'])}\s*", "", m["name"], flags=re.I))  # "a4"
                for k in {full, bare, _norm(m["slug"])}:
                    if k:
                        mmap[k] = m["slug"]
            self.model_lookup[slug] = mmap

    @property
    def loaded(self) -> bool:
        return bool(self.catalog)

    def match_brand(self, brand_raw: str) -> str | None:
        n = _norm(brand_raw)
        if not n:
            return None
        if n in self.brand_lookup:
            return self.brand_lookup[n]
        # try contains
        for key, slug in self.brand_lookup.items():
            if key in n or n in key:
                return slug
        return None

    def brand(self, slug: str) -> dict:
        return self.catalog.get(slug, {})

    def models_of(self, brand_slug: str) -> list[dict]:
        return self.brand(brand_slug).get("models", [])

    def match_model(self, brand_slug: str, model_raw: str, title: str = "") -> str | None:
        mmap = self.model_lookup.get(brand_slug) or {}
        if not mmap:
            return None
        for src in (model_raw, title):
            n = _norm(src)
            if not n:
                continue
            if n in mmap:
                return mmap[n]
            # contains: pick longest match in src
            hits = sorted(
                ((k, s) for k, s in mmap.items() if k and k in n),
                key=lambda kv: -len(kv[0]),
            )
            if hits:
                return hits[0][1]
        return None
