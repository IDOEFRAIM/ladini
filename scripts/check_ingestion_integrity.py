#!/usr/bin/env python3
"""Mirror validation between ingestion-written Redis keys and RAG RedisSearch reads.

This script checks:
1) direct hash read on a key written by ingestion
2) FT.SEARCH/FT.INFO behavior on the same Redis endpoint

If direct read works but FT.* fails, the issue is RediSearch module/index, not connection.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any

import redis

from agriconnect.rag.providers.redis_search_provider import RedisSearchProvider


def _to_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def main() -> None:
    redis_url = os.getenv("AGRICONNECT_REDIS_URL") or os.getenv("REDIS_URL") or "rediss://127.0.0.1:6380/0"
    index_name = (os.getenv("AGRICONNECT_REDIS_INDEX_NAME", "rag:index") or "rag:index").strip()

    # Mirror ingestion store key patterns first, then legacy.
    key_patterns = [
        "rag:{docs}:doc:*",
        "rag:doc:*",
        "doc:*",
    ]

    report: dict[str, Any] = {
        "redis_url": redis_url,
        "index_name": index_name,
        "picked_key": None,
        "direct_read_ok": False,
        "direct_fields": [],
        "direct_content_preview": "",
        "rag_provider_ready": None,
        "rag_provider_available": None,
        "provider_key_read_ok": False,
        "ft_info_ok": False,
        "ft_search_ok": False,
        "ft_error": None,
        "diagnosis": "",
    }

    raw_client = redis.from_url(redis_url, decode_responses=False, socket_timeout=5)
    raw_client.ping()

    candidates: list[str] = []
    for pat in key_patterns:
        for key in raw_client.scan_iter(match=pat, count=1000):
            candidates.append(_to_text(key))
            if len(candidates) >= 200:
                break
        if candidates:
            break

    if not candidates:
        report["diagnosis"] = "No ingestion-like keys found in Redis."
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    picked_key = random.choice(candidates)
    report["picked_key"] = picked_key

    raw_data = raw_client.hgetall(picked_key)
    if raw_data:
        report["direct_read_ok"] = True
        report["direct_fields"] = sorted(_to_text(k) for k in raw_data.keys())
        content = raw_data.get(b"content") or raw_data.get(b"text") or b""
        report["direct_content_preview"] = _to_text(content)[:160]

    provider = RedisSearchProvider(
        url=redis_url,
        index_name=index_name,
        ensure_index=False,
        socket_timeout=5,
        retry_on_timeout=True,
        decode_responses=False,
    )
    report["rag_provider_ready"] = bool(getattr(provider, "ready", False))
    report["rag_provider_available"] = bool(getattr(provider, "available", False))

    try:
        provider_raw = provider.client.hgetall(picked_key)
        report["provider_key_read_ok"] = bool(provider_raw)
    except Exception as exc:
        report["provider_key_read_ok"] = False
        report["provider_key_read_error"] = str(exc)

    try:
        provider.client.execute_command("FT.INFO", index_name)
        report["ft_info_ok"] = True
    except Exception as exc:
        report["ft_error"] = f"FT.INFO error: {exc}"

    try:
        provider.client.execute_command("FT.SEARCH", index_name, "*", "LIMIT", "0", "1")
        report["ft_search_ok"] = True
    except Exception as exc:
        msg = f"FT.SEARCH error: {exc}"
        report["ft_error"] = f"{report['ft_error']} | {msg}" if report.get("ft_error") else msg

    if report["direct_read_ok"] and not (report["ft_info_ok"] and report["ft_search_ok"]):
        report["diagnosis"] = "Direct key read works but FT.* fails: connection/path is OK, RediSearch module/index path is the issue."
    elif report["direct_read_ok"] and report["ft_info_ok"] and report["ft_search_ok"]:
        report["diagnosis"] = "Direct and FT.* reads both work: ingestion/retrieval pipe looks coherent."
    else:
        report["diagnosis"] = "Direct key read failed: check Redis URL/key prefix and ingestion write path."

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
