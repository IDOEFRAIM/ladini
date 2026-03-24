# Strategie No Data Loss pour mise a jour des chunks

## Objectif
Garantir qu'aucune information RAG n'est perdue pendant les re-ingestions (documents, meteo, marche) et pendant la migration de rag_db vers PostgreSQL + PGVector.

## Principes
1. Idempotence stricte par fichier: hash MD5 stocke en base dans ingestion.document_state.
2. Ecriture atomique: les insertions/updates de chunks sont faites en transaction SQL.
3. Versioning de chunk: increment de chunk_version au lieu d'ecraser sans trace.
4. Soft deactivation: on marque is_active=false avant purge physique.
5. Fenetre de retention: suppression physique uniquement apres verification des nouveaux chunks.

## Workflow recommande
1. Etape Collect:
   - Airflow collecte les donnees brutes (meteo/marche/docs).
   - Chaque payload source recoit un md5 de contenu.
2. Etape Stage:
   - Les nouveaux chunks sont ecrits avec chunk_version = version_precedente + 1.
   - Les anciens chunks restent actifs jusqu'a la validation.
3. Etape Validate:
   - Controle du nombre de chunks attendus vs inseres.
   - Controle de non-regression des metadonnees critiques: zone, forecast_date, valid_until.
4. Etape Switch:
   - Activation de la nouvelle version (is_active=true).
   - Desactivation de la version precedente (is_active=false).
5. Etape Garbage Collection differée:
   - Purge physique apres delai de retention (ex: 7 jours) et verification applicative.

## Protection contre pertes sur crash
1. Les DAGs sont idempotents: relance possible sans dupliquer.
2. Les contraintes UNIQUE evictent les doublons silencieux.
3. Les jobs de notification utilisent une outbox (status PENDING/SENT) pour reprise.

## Migration rag_db JSON vers PGVector
1. Export controlle des fichiers JSON historiques.
2. Import par lots en transaction (batch size fixe).
3. Verification par checksum de lot et comptage total.
4. Double lecture temporaire (JSON + PGVector) jusqu'a validation complete.
5. Coupure finale du legacy JSON apres periode de stabilisation.

## Checklist d'exploitation
1. Verifier ingestion.document_state mis a jour sur chaque run Airflow.
2. Verifier qu'aucun chunk actif n'a valid_until depasse sans remplacement.
3. Verifier le ratio chunks inactifs/chunks actifs avant purge.
4. Sauvegarder le schema et les tables vectorielles avant migration majeure.
