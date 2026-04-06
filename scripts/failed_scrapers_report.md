# Failed scrapers report (run 2026-04-06)

Summary: the latest scraper-stream run (via SSH-tunnelled DB/Redis) reported 19 sources executed with 1 success and 18 failures. Below are the failing `source_id`s and a short error summary extracted from the worker logs.

- **agri_sonagess_prices**: PermissionError - robots.txt forbids scraping: https://sonagess.bf/?page_id=239
- **fao_technical_resources**: SelectorSyntaxError - soupsieve "Expected a selector at position 14" (problematic preferred selector: `article, main,`)
- **insd_microdata_resources**: PermissionError - robots.txt forbids scraping: https://microdata.insd.bf/index.php/catalog
- **lefaso_actualites**: PermissionError - robots.txt forbids scraping: https://lefaso.net/
- **meteo_agromet_decadaire**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/
- **meteo_agromet_mensuel**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-agrometeorologique-mensuel/
- **meteo_agrometeo_medias**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-agrometeo-pour-les-medias/
- **meteo_anam_bulletins**: scrape_failed (trafilatura discarded payload / no usable content)
- **meteo_bulletin_climat_sante**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-climat-sante/
- **meteo_bulletin_climatique_mensuel**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-climatique-mensuel/
- **meteo_bulletin_eau_energie**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-eau-energie/
- **meteo_bulletin_hebdomadaire**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-hebdomadaire/
- **meteo_bulletin_mensuel**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-mensuel/
- **meteo_bulletin_quotidien**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-quotidien/
- **meteo_climat_burkina**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/climat-du-burkina-faso/
- **meteo_etat_annuel_climat**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/etat-annuel-du-climat/
- **meteo_previsions_saisonnieres**: PermissionError - robots.txt forbids scraping: https://meteoburkina.bf/produits/bulletin-de-previsions-saisonnieres/
- **sidwaya_actualites**: SelectorSyntaxError - soupsieve "Expected a selector at position 14" (preferred selector `article, main,`)

Run summary:

- total sources executed: 19
- success: 1
- error: 18

Recommended next steps:

1. Keep these sources disabled via `SCRAPER_DISABLED_SOURCES` while we triage individual problems.
2. For `robots.txt`-blocked sources: either obtain permission / allow-listing from site owners, or keep them disabled.
3. For `SelectorSyntaxError` issues (e.g., `article, main,`): update `preferred_selectors` in `backend/sources/sources.yaml` to valid CSS selector strings (e.g., `article, main` without trailing comma or provide a list).
4. I can start triaging the `SelectorSyntaxError` sources first (fix `preferred_selectors` and re-run) if you want — say "yes, triage selectors" and I'll begin.

Focused correction update (same day):

- Disabled-source filtering is confirmed working in runtime logs (`Skipping disabled source: ...`).
- Selector parser regression is fixed in scraper base code:
	- Added robust handling for list selectors and stringified-list selectors.
	- Removed trailing-comma selector breakage.
	- Fixed missing logger definition in fallback path.
- Source configs now use explicit YAML lists for `preferred_selectors` on:
	- `lefaso_actualites`
	- `sidwaya_actualites`
	- `fao_technical_resources`
	- `insd_microdata_resources`

Focused validation result (selector triage run):

- `total`: 4
- `success`: 1
- `error`: 3
- still failing among enabled sources:
	- `fao_technical_resources` (`scrape_failed`)
	- `sidwaya_actualites` (`scrape_failed`)
	- `meteo_anam_bulletins` (`scrape_failed`)

Action taken (2026-04-06):

- The three remaining failing sources above have been added to the `SCRAPER_DISABLED_SOURCES` list used by the local validation run to avoid blocking the ingestion while we triage content-extraction issues.
- Updated run script: [scripts/run_worker_selector_validate.ps1](scripts/run_worker_selector_validate.ps1#L1) now includes `fao_technical_resources`, `sidwaya_actualites`, and `meteo_anam_bulletins` in the disabled set.
- A focused validation run was executed; full worker logs are saved to [worker_selector_validate.txt](worker_selector_validate.txt#L1) and the per-source failure lines are in [scripts/per_source_failures.txt](scripts/per_source_failures.txt#L1).

Next steps recommended:

1. Triager(s) should investigate `scrape_failed` sources offline by fetching sample pages and running local scraper logic to identify extraction heuristics to tune.
2. For long-term operation, decide whether to request robots.txt allow-listing from site owners or keep those sources disabled in production.
3. When ready, re-enable sources one-by-one and run a focused validation to confirm fixes.

Interpretation:

- The prior `SelectorSyntaxError` is no longer the blocker.
- Remaining failures are content-extraction quality failures (`scrape_failed`), not CSS syntax parse failures.
