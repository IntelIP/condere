"""LangGraph persistence uses its own database and credential, not telemetry storage."""

from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import conninfo_to_dict


@asynccontextmanager
async def checkpoint_store(checkpoint_dsn: str, *, application_dsn: str):
    application = conninfo_to_dict(application_dsn)
    checkpoint = conninfo_to_dict(checkpoint_dsn)
    if application.get("user") == checkpoint.get("user") or (
        application.get("host"),
        application.get("port"),
        application.get("dbname"),
    ) == (checkpoint.get("host"), checkpoint.get("port"), checkpoint.get("dbname")):
        raise ValueError("Checkpoints need a separate database and credential.")
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as saver:
        await saver.setup()
        yield saver
