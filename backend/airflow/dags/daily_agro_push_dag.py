from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago
import logging
import sys
import os

# Ajout du chemin backend au PYTHONPATH pour que Airflow trouve les modules
current_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(os.path.dirname(current_dir)) # Remonte airflow -> backend
src_dir = os.path.join(backend_dir, "src")
if src_dir not in sys.path:
    sys.path.append(src_dir)

# Import du service (Fail-safe import)
try:
    from agriconnect.services.notification_service import notification_service
except ImportError as e:
    logging.error(f"Impossible d'importer notification_service: {e}")
    notification_service = None

default_args = {
    'owner': 'agriconnect',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

def trigger_notifications(**kwargs):
    if not notification_service:
        raise ImportError("Service Notification non chargé.")
    
    logging.info("Démarrage de la tâche de notification...")
    notification_service.send_daily_agro_report(dry_run=False)
    logging.info("Tâche de notification terminée.")

with DAG(
    'daily_agro_push',
    default_args=default_args,
    description='Envoi quotidien de conseils agro-météo aux abonnés',
    schedule_interval='0 6 * * *',  # 6h00 matin
    start_date=days_ago(1),
    tags=['agriconnect', 'notification', 'farming'],
    catchup=False,
) as dag:

    notify_task = PythonOperator(
        task_id='send_daily_notifications',
        python_callable=trigger_notifications,
    )

    notify_task
