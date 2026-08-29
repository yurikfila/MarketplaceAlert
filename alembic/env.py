"""Alembic environment script.

Deliberately does NOT read `sqlalchemy.url` from `alembic.ini` - the
database URL comes from the exact same place the running application gets
it from (`DATABASE_URL` via `marketplace_alert.config.settings`, resolved
and normalized by `marketplace_alert.core.persistence.database`), so
Alembic can never target a different database than the app actually uses,
and Render's `postgres://`/`postgresql://` URL forms are handled
identically in both places - one normalization function, not two.

Model imports below register every table on `Base.metadata` -
`target_metadata` is what `alembic revision --autogenerate` diffs against,
so a model module that's never imported here would be silently invisible
to autogenerate.
"""

from logging.config import fileConfig

from alembic import context

from marketplace_alert.config import settings
from marketplace_alert.core.persistence.database import Base, create_db_engine, resolve_database_url

# Import every module that defines a table, purely for the side effect of
# registering it on Base.metadata - autogenerate can only see tables whose
# model module has actually been imported somewhere.
import marketplace_alert.core.auth.models  # noqa: F401
import marketplace_alert.core.persistence.models  # noqa: F401
import marketplace_alert.core.saved_searches.models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The resolved URL may contain a real password (production PostgreSQL) -
# never logged, never printed, never passed to Alembic's own config object
# (which would risk it ending up in `alembic.ini`-style output).
_database_url = resolve_database_url(settings.database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode - emits SQL without a live DB connection."""
    context.configure(
        url=_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode - connects and applies them directly."""
    connectable = create_db_engine(_database_url)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
