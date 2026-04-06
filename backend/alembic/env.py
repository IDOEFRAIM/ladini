from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool
import os
import io


# Load .env from repository root (two levels up) so Alembic can pick up
# DATABASE_URL when alembic.ini contains a placeholder. This avoids
# requiring an extra dependency during migration runs.
def _load_dotenv(dotenv_path: str) -> None:
    if not os.path.exists(dotenv_path):
        return
    try:
        with io.open(dotenv_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


# repository root .env (backend/alembic -> ../../.env)
_load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env")))

# Try to import project models to provide target_metadata for autogenerate
try:
    # ensure project src is on sys.path when Alembic runs from backend/
    import sys
    proj_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    if proj_src not in sys.path:
        sys.path.insert(0, proj_src)
    from agriconnect.services import models_v3

    target_metadata = models_v3.Base.metadata
except Exception:
    target_metadata = None

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
# target_metadata is set above from models_v3 if import succeeded

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    # If alembic.ini contains a placeholder, try to read DATABASE_URL from settings
    if (not url) or url.startswith("driver://"):
        try:
            from agriconnect.core.settings import settings

            url = getattr(settings, "DATABASE_URL", url)
        except Exception:
            pass
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Build engine config; if sqlalchemy.url is a placeholder, attempt to get DB URL from settings
    cfg = config.get_section(config.config_ini_section, {})
    if cfg.get("sqlalchemy.url", "").startswith("driver://"):
        try:
            from agriconnect.core.settings import settings

            cfg["sqlalchemy.url"] = getattr(settings, "DATABASE_URL", cfg.get("sqlalchemy.url"))
        except Exception:
            pass

    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
