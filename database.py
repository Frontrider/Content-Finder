"""Database connection shared by the web app and the exporters.

Uses a local SQLite file by default; set DATABASE_URL to use another database,
for example ``postgresql://user:password@host/dbname``.
"""

import os
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import URL

DEFAULT_DATABASE_URL = f"sqlite:///{Path(__file__).resolve().parent / 'data' / 'contentfind.db'}"
metadata = MetaData()


def database_setting(name, default=None):
    value = os.environ.get(name)
    if value:
        try:
            path = Path(value)
            if not path.is_file():
                return value
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"Unable to read the file specified by {name}.") from error
        if not value:
            raise ValueError(f"The file specified by {name} must contain a non-empty value.")
    return value or default


def database_url():
    url = database_setting("DATABASE_URL")
    if url:
        return url
    host = database_setting("DB_HOST")
    if host:
        try:
            port = int(database_setting("DB_PORT", "5432"))
        except ValueError as error:
            raise ValueError("DB_PORT must be an integer between 1 and 65535.") from error
        if not 1 <= port <= 65535:
            raise ValueError("DB_PORT must be an integer between 1 and 65535.")
        parameters = {
            setting: database_setting(setting) for setting in ("DB_NAME", "DB_USER")
        }
        for setting, value in parameters.items():
            if not value:
                raise ValueError(f"{setting} is required when DB_HOST is configured.")
        query = {}
        sslmode = database_setting("DB_SSLMODE","prefer")
        if sslmode:
            query["sslmode"] = sslmode
        return URL.create(
            "postgresql+psycopg",
            username=parameters["DB_USER"],
            password=database_setting("DB_PASSWORD"),
            host=host,
            port=port,
            database=parameters["DB_NAME"],
            query=query,
        ).render_as_string(hide_password=False)
    sqlite_path = os.environ.get("SQLITE_DATABASE_PATH")
    if sqlite_path:
        return URL.create(
            "sqlite", database=sqlite_path,
        ).render_as_string()
    return DEFAULT_DATABASE_URL


def create_database_engine(url=None):
    url = url or database_url()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        if engine.url.database and engine.url.database != ":memory:":
            Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)

        # WAL lets searches keep reading while an index update is being written.
        @event.listens_for(engine, "connect")
        def enable_wal(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    metadata.create_all(engine)
    return engine
