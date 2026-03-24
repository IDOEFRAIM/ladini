from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Cloud-ready settings with env vars first, then Airflow/Secrets helpers."""

    APP_ENV: str = "dev"
    AWS_REGION: str = "us-east-1"

    # Storage
    S3_BUCKET: str = ""
    S3_KEY_PREFIX: str = ""
    RAW_DATA_PREFIX: str = "raw_data/"

    # Database
    DATABASE_URL: str = ""
    AIRFLOW_POSTGRES_CONN_ID: str = "agriconnect_postgres"
    POSTGRES_SECRET_ID: str = ""

    # AWS creds are optional in cloud runtimes (IAM role preferred)
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_SESSION_TOKEN: str = ""

    # Embeddings (OpenAI default)
    OPENAI_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_DIM: int = 1536

    # Airflow / secret integration
    AIRFLOW_AWS_CONN_ID: str = "aws_default"
    AWS_SECRET_PREFIX: str = ""

    _pkg_env = Path(__file__).resolve().parents[1] / ".env"
    _backend_env = Path(__file__).resolve().parents[3] / ".env"
    # Prefer backend/.env to keep one source of truth for runtime configuration.
    _env_path = _backend_env if _backend_env.exists() else _pkg_env

    model_config = SettingsConfigDict(
        env_file=str(_env_path),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def boto3_session_kwargs(self) -> dict:
        kwargs = {"region_name": self.AWS_REGION}
        if self.AWS_ACCESS_KEY_ID and self.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = self.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = self.AWS_SECRET_ACCESS_KEY
            if self.AWS_SESSION_TOKEN:
                kwargs["aws_session_token"] = self.AWS_SESSION_TOKEN
        return kwargs

    def load_secret_value(self, secret_id: str) -> Optional[str]:
        """Best-effort read from AWS Secrets Manager (returns None on failure)."""
        if not secret_id:
            return None
        try:
            import boto3
            from botocore.config import Config

            session = boto3.session.Session(**self.boto3_session_kwargs())
            client = session.client(
                "secretsmanager",
                config=Config(retries={"max_attempts": 8, "mode": "adaptive"}),
            )
            resp = client.get_secret_value(SecretId=secret_id)
            return resp.get("SecretString")
        except Exception:
            return None

    def get_airflow_connection_uri(self, conn_id: str) -> Optional[str]:
        """Best-effort pull URI from Airflow connection metadata."""
        if not conn_id:
            return None
        try:
            import importlib
            base_mod = importlib.import_module("airflow.hooks.base")
            BaseHook = getattr(base_mod, "BaseHook")
            conn = BaseHook.get_connection(conn_id)
            if conn:
                return conn.get_uri()
        except Exception:
            return None
        return None

    def resolve_database_url(self) -> str:
        """Resolution order: env DATABASE_URL -> Airflow connection -> Secrets Manager."""
        if self.DATABASE_URL:
            return self.DATABASE_URL

        airflow_uri = self.get_airflow_connection_uri(self.AIRFLOW_POSTGRES_CONN_ID)
        if airflow_uri:
            return airflow_uri

        secret_id = self.POSTGRES_SECRET_ID
        if not secret_id and self.AWS_SECRET_PREFIX:
            secret_id = f"{self.AWS_SECRET_PREFIX.rstrip('/')}/postgres_url"
        secret_val = self.load_secret_value(secret_id)
        if secret_val:
            return secret_val

        # final fallback for interactive shells
        return os.getenv("DATABASE_URL", "")


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
