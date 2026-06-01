from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List
from urllib.parse import unquote_plus

import boto3

from agriconnect.domain.ingestion.processors.factory import get_processor
from agriconnect.domain.ingestion.worker import IngestionWorker
from agriconnect.rag.config import RAW_DATA_DIR


logger = logging.getLogger("s3_event_consumer")
logging.basicConfig(level=logging.INFO)


@dataclass
class S3WorkItem:
    bucket_name: str
    file_key: str
    etag: str | None = None
    event_time: str | None = None


def _extract_items_from_s3_event(payload: Dict[str, Any]) -> List[S3WorkItem]:
    items: List[S3WorkItem] = []
    for record in payload.get("Records", []):
        s3_obj = record.get("s3") or {}
        bucket = ((s3_obj.get("bucket") or {}).get("name") or "").strip()
        obj = s3_obj.get("object") or {}
        key = unquote_plus(str(obj.get("key") or "").strip())
        if not bucket or not key:
            continue
        items.append(
            S3WorkItem(
                bucket_name=bucket,
                file_key=key,
                etag=(obj.get("eTag") or obj.get("etag") or ""),
                event_time=(record.get("eventTime") or ""),
            )
        )
    return items


def _extract_items_from_message(body: str) -> List[S3WorkItem]:
    payload = json.loads(body)

    # Native S3 Event Notification envelope.
    if isinstance(payload, dict) and isinstance(payload.get("Records"), list):
        return _extract_items_from_s3_event(payload)

    # Fallback custom contract.
    bucket_name = str(payload.get("bucket_name") or payload.get("bucket") or "").strip()
    file_key = str(payload.get("file_key") or payload.get("key") or "").strip()
    if bucket_name and file_key:
        return [S3WorkItem(bucket_name=bucket_name, file_key=unquote_plus(file_key))]

    return []


def _resolve_source_type_from_key(file_key: str) -> str:
    lower = (file_key or "").lower()
    if lower.endswith(".pdf"):
        return "institutional_pdf"
    if "news" in lower:
        return "news_article"
    if "fews" in lower:
        return "fews_report"
    if "weather" in lower or "bulletin" in lower:
        return "weather_bulletin"
    return "technical_resource"


def _iter_sqs_messages(client: Any, queue_url: str, wait_time: int, visibility_timeout: int) -> Iterable[Dict[str, Any]]:
    while True:
        response = client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=wait_time,
            VisibilityTimeout=visibility_timeout,
        )
        messages = response.get("Messages", [])
        if not messages:
            continue
        for msg in messages:
            yield msg


def run_consumer() -> None:
    queue_url = os.environ["INGESTION_SQS_URL"]
    region = os.getenv("AWS_REGION", "eu-central-1")
    wait_time = int(os.getenv("INGESTION_SQS_WAIT_TIME", "20"))
    visibility_timeout = int(os.getenv("INGESTION_SQS_VISIBILITY_TIMEOUT", "120"))
    poll_sleep = float(os.getenv("INGESTION_POLL_SLEEP_SECONDS", "0.25"))

    sqs_client = boto3.client("sqs", region_name=region)
    worker = IngestionWorker(raw_data_dir=str(RAW_DATA_DIR))

    logger.info("Ingestion S3-event consumer started for queue=%s", queue_url)

    for message in _iter_sqs_messages(
        client=sqs_client,
        queue_url=queue_url,
        wait_time=wait_time,
        visibility_timeout=visibility_timeout,
    ):
        receipt_handle = message.get("ReceiptHandle")
        body = message.get("Body") or "{}"
        try:
            work_items = _extract_items_from_message(body)
            if not work_items:
                raise ValueError("No S3 work item detected in message")

            for item in work_items:
                source_type = _resolve_source_type_from_key(item.file_key)
                # Processor factory check keeps the bridge aligned with domain contracts.
                _ = get_processor(source_type)
                worker.process_s3_event_message(
                    bucket_name=item.bucket_name,
                    file_key=item.file_key,
                    etag=item.etag,
                    last_modified=item.event_time,
                )

            if receipt_handle:
                sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed processing SQS message: %s", exc)
            # Keep the message in queue for retry / DLQ policy.
        finally:
            if poll_sleep > 0:
                time.sleep(poll_sleep)


if __name__ == "__main__":
    run_consumer()
