# Airflow Data-Driven Orchestration

Ce dossier contient les DAGs Airflow de la refonte acquisition/orchestration.

## DAGs
- DAG_Ingest_Docs: surveillance des documents et re-indexation idempotente.
- DAG_Ingest_Weather: collecte meteo hybride, stockage SQL et generation de chunks de conseil.
- DAG_Market_Prices: collecte des prix marche et signaux Put/Call.
- DAG_User_Notification: matching profils utilisateurs vs signaux meteo/marche et dispatch.
- DAG_Migrate_RAG_JSON_To_PGVector: migration one-shot de rag_db/docstore.json vers agri_vector.document_chunks.
- DAG_GC_Old_Chunk_Versions: desactivation/purge controlee des anciennes versions de chunks.

## Variables utiles
- AGRICONNECT_CHUNK_SIZE
- AGRICONNECT_CHUNK_OVERLAP
- DATABASE_URL

## Installation rapide
1. Installer Airflow dans un environnement dedie.
2. Pointer AIRFLOW__CORE__DAGS_FOLDER vers backend/airflow/dags.
3. Exporter PYTHONPATH=backend/src pour resoudre les imports agriconnect.
4. Activer les DAGs selon les besoins.
