<!-- Fill this in once per project and paste into Planning → Settings → Project context.
     Delete sections that do not apply. Keep it under ~3,000 characters: facts only, no workflow rules. -->

**What it is:** <one or two sentences: product/purpose and who uses it>

**Repository:** <GitHub URL> — default branch: `<main>`
**Issues:** GitHub Issues in `<owner/repo>` (labels: <list, if used>)
**Live/deploy:** <URLs, environments, how it is deployed — or "not deployed">

**Stack:** <languages, frameworks, database, key services>

**Layout:**
- `<dir>/` — <what lives there>
- `<dir>/` — <what lives there>

**Commands** (run from the repo root):
- Install: `<command>`
- Run locally: `<command>`
- Tests: `<command>`
- Lint/format: `<command>`
- Type check/build: `<command>`

**Invariants — never break these:**
- <e.g. all DB mutations go through services.py inside a transaction>
- <e.g. regenerate the API client after any API change: scripts/update_api_client.sh>
- <e.g. never log raw API keys>

**Conventions:** <code style, naming, test style, commit style, UI library, i18n…>

**Gotchas:** <known traps, flaky tests, environment quirks>
