# Temporarily runs the validation worker with robots.txt checks bypassed
$env:SCRAPER_IGNORE_ROBOTS = '1'
Write-Host "SCRAPER_IGNORE_ROBOTS=$env:SCRAPER_IGNORE_ROBOTS"
.\scripts\run_worker_selector_validate.ps1
Select-String -Path worker_selector_validate.txt -Pattern 'Skipping disabled source|Scraper source failed|Scraper-stream ingestion finished|source_id=fao_technical_resources|source_id=sidwaya_actualites|source_id=meteo_anam_bulletins' | ForEach-Object { $_.Line }
