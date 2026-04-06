$env:SCRAPER_DISABLED_SOURCES='agri_sonagess_prices,insd_microdata_resources,lefaso_actualites,meteo_agromet_decadaire,meteo_previsions_saisonnieres,meteo_bulletin_mensuel,meteo_etat_annuel_climat,meteo_bulletin_hebdomadaire,meteo_agromet_mensuel,meteo_bulletin_quotidien,meteo_bulletin_eau_energie,meteo_bulletin_climatique_mensuel,meteo_agrometeo_medias,meteo_climat_burkina,meteo_bulletin_climat_sante,meteo_previsions_saisonnieres,fao_technical_resources,sidwaya_actualites,meteo_anam_bulletins'
$env:DATABASE_URL='postgresql://ladiniadmin:kingradene@localhost:5433/postgres?sslmode=require'
$env:REDIS_URL='rediss://localhost:6380'
& '.\.venv\Scripts\python.exe' backend\src\agriconnect\domain\ingestion\worker.py --once --max-files 20 > worker_selector_validate.txt 2>&1
if (Test-Path worker_selector_validate.txt) { Get-Content worker_selector_validate.txt -Raw }
else { Write-Output 'No output file produced' }
