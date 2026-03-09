"""Small helper to test a Redis connection configured via REDIS_URL.

Usage:
  - Set `REDIS_URL` in your environment or in `backend/.env` (recommended):
      REDIS_URL=rediss://default:YOUR_PASSWORD@redis-xxxxx.nn.region-1-1.ec2.cloud.redislabs.com:18836/0
  - Run: python backend/scripts/redis_cloud_example.py

This script intentionally does not store credentials in the repo. If you
paste a full connection string below for a quick test, remove it afterwards.
"""
from __future__ import annotations

import os
import sys
import logging
import re
from urllib.parse import quote_plus
import ssl

try:
    import redis
except Exception:
    redis = None

from agriconnect.core.settings import settings

logger = logging.getLogger("redis_example")


def get_redis_url() -> str:
    # Prefer explicit environment variable (CI / runtime), then settings
    env_url = os.getenv("REDIS_URL")
    if env_url:
        return env_url

    # Support explicit components (convenient for one-off runs)
    host = os.getenv("REDIS_HOST")
    if host:
        port = os.getenv("REDIS_PORT", "6379")
        user = os.getenv("REDIS_USERNAME") or os.getenv("REDIS_USER") or ""
        pwd = os.getenv("REDIS_PASSWORD") or os.getenv("REDIS_PASS") or ""
        # Auto-enable TLS for popular cloud providers if not explicitly set
        tls_env = os.getenv("REDIS_TLS")
        if tls_env is None:
            tls = ("redislabs" in host.lower()) or ("aws" in host.lower() and ":" in host)
        else:
            tls = tls_env.lower() in ("1", "true", "yes")

        scheme = "rediss" if tls else "redis"

        auth = ""
        if pwd:
            # quote credentials to be URL-safe
            if user:
                auth = f"{quote_plus(user)}:{quote_plus(pwd)}@"
            else:
                auth = f":{quote_plus(pwd)}@"

        return f"{scheme}://{auth}{host}:{port}/0"

    return getattr(settings, "REDIS_URL", "redis://localhost:6379/0")


def main() -> int:
    url = get_redis_url()
    if not url:
        print("REDIS_URL is not set. Export REDIS_URL and retry.")
        return 2

    if redis is None:
        print("Please install the redis package (pip install redis) and retry.")
        return 3

    # Mask password for console output
    try:
        masked = re.sub(r":[^@]+@", ":****@", url)
    except Exception:
        masked = url
    print(f"Connecting to Redis: {masked}")
    try:
        client = redis.from_url(url, decode_responses=True)
        pong = client.ping()
        print("PING ->", pong)

        # Basic smoke test
        client.set("agriconnect:test", "ok")
        val = client.get("agriconnect:test")
        print("SET/GET ->", val)

        # Clean up
        client.delete("agriconnect:test")
        return 0
    except Exception as exc:
        # If we attempted TLS and received an SSL wrong-version error, try non-TLS fallback
        err_text = str(exc)
        logger.debug("Initial Redis connect error: %s", err_text)
        # common pattern: ssl.SSLError with WRONG_VERSION_NUMBER
        if "WRONG_VERSION_NUMBER" in err_text or isinstance(exc, ssl.SSLError) or "ssl" in err_text.lower():
            try:
                fallback = url
                if fallback.startswith("rediss://"):
                    fallback = fallback.replace("rediss://", "redis://", 1)
                print("TLS failed, retrying without TLS...")
                client = redis.from_url(fallback, decode_responses=True)
                pong = client.ping()
                print("PING ->", pong)
                client.set("agriconnect:test", "ok")
                val = client.get("agriconnect:test")
                print("SET/GET ->", val)
                client.delete("agriconnect:test")
                return 0
            except Exception as exc2:
                logger.exception("Fallback non-TLS Redis connection also failed: %s", exc2)
                print("Fallback (non-TLS) Redis connection failed:", exc2)
                return 1

        logger.exception("Redis connection test failed: %s", exc)
        print("Redis connection failed:", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
