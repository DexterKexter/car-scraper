# car-scraper

Multi-site car-listing scraper. Targets: encar.com, guazi.com, kolesa.kz, autocango.com.

Stack: [Scrapling](https://github.com/D4Vinci/Scrapling) + Playwright stealth. Output: Supabase Postgres (planned). AI enrichment for brand/model/trim parsing (planned).

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/scrapling install
.venv/bin/python -m scrapers.guazi --city bj --limit 10 --out out/guazi.json
```

## Sites

| Site | Approach | Status |
|------|----------|--------|
| guazi.com | StealthyFetcher (Playwright) | wip |
| encar.com | direct JSON API | todo |
| kolesa.kz | Fetcher (httpx) + stealth fallback | todo |
| autocango.com | Fetcher + stealth fallback | todo |

## GitHub Actions

`.github/workflows/scrape-guazi.yml` runs the guazi scraper on demand (`workflow_dispatch`) and uploads `out/guazi.json` as an artifact.
