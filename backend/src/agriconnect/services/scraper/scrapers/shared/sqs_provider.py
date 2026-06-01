from __future__ import annotations

import json
import logging
from typing import Any, Dict

import boto3
from botocore.client import BaseClient

from .message_models import ScraperQueueMessage


logger = logging.getLogger(__name__)


class SQSProvider:
    """SQS client wrapper for scraper producers."""

    def __init__(self, queue_url: str, region_name: str, client: BaseClient | None = None) -> None:
        if not queue_url:
            raise ValueError("queue_url is required")
        if not region_name:
            raise ValueError("region_name is required")
        self.queue_url = queue_url
        self.client = client or boto3.client("sqs", region_name=region_name)

    def send_scraper_message(self, message: ScraperQueueMessage) -> Dict[str, Any]:
        body = json.dumps(message.model_dump(mode="json"), ensure_ascii=True)
        response = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=body,
        )
        logger.info(
            "SQS message sent",
            extra={
                "queue_url": self.queue_url,
                "source_id": message.source_id,
                "trace_id": str(message.trace_id),
                "message_id": response.get("MessageId"),
            },
        )
        return response
