import pytest
from sqlalchemy.engine import make_url

from database import DEFAULT_DATABASE_URL, database_setting, database_url


@pytest.fixture(autouse=True)
def clear_database_environment(monkeypatch):
    for name in (
        "DATABASE_URL", "SQLITE_DATABASE_PATH", "DB_HOST", "DB_PORT",
        "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_sqlite_default():
    assert database_url() == DEFAULT_DATABASE_URL


def test_sqlite_path(monkeypatch):
    monkeypatch.setenv("SQLITE_DATABASE_PATH", "/data/contentfind.db")
    assert database_url() == "sqlite:////data/contentfind.db"


def test_postgresql_parameters_override_sqlite_path(monkeypatch):
    monkeypatch.setenv("SQLITE_DATABASE_PATH", "/data/contentfind.db")
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "contentfind")
    monkeypatch.setenv("DB_USER", "reader")
    monkeypatch.setenv("DB_PASSWORD", "test@password:/?#%")
    monkeypatch.setenv("DB_SSLMODE", "require")
    url = make_url(database_url())
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "postgres"
    assert url.port == 5432
    assert url.database == "contentfind"
    assert url.username == "reader"
    assert url.password == "test@password:/?#%"
    assert url.query == {"sslmode": "require"}


def test_database_url_has_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("DB_HOST", "postgres")
    assert database_url() == "sqlite:///:memory:"


@pytest.mark.parametrize("port", ["invalid", "0", "65536"])
def test_invalid_postgresql_port(monkeypatch, port):
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_PORT", port)
    with pytest.raises(ValueError, match="DB_PORT"):
        database_url()


@pytest.mark.parametrize("missing", ["DB_NAME", "DB_USER"])
def test_missing_postgresql_parameter(monkeypatch, missing):
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "contentfind")
    monkeypatch.setenv("DB_USER", "reader")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        database_url()


def test_custom_postgresql_port(monkeypatch):
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "contentfind")
    monkeypatch.setenv("DB_USER", "reader")
    url = make_url(database_url())
    assert url.port == 5433
    assert url.password is None
    assert url.query == {"sslmode": "prefer"}


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL", "DB_HOST", "DB_PORT",
        "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE",
    ],
)
def test_settings_can_come_from_files(monkeypatch, tmp_path, name):
    path = tmp_path / "value"
    path.write_text("test-value\n", encoding="utf-8")
    monkeypatch.setenv(name, str(path))
    assert database_setting(name) == "test-value"


def test_postgresql_parameters_from_files(monkeypatch, tmp_path):
    values = {
        "DB_HOST": "postgres",
        "DB_PORT": "5433",
        "DB_NAME": "contentfind",
        "DB_USER": "reader",
        "DB_PASSWORD": " test@password ",
        "DB_SSLMODE": "require",
    }
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value + "\n", encoding="utf-8")
        monkeypatch.setenv(name, str(path))
    url = make_url(database_url())
    assert url.host == values["DB_HOST"]
    assert url.port == 5433
    assert url.database == values["DB_NAME"]
    assert url.username == values["DB_USER"]
    assert url.password == values["DB_PASSWORD"]
    assert url.query == {"sslmode": "require"}


def test_database_url_from_file(monkeypatch, tmp_path):
    path = tmp_path / "url"
    path.write_text("sqlite:///:memory:\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", str(path))
    assert database_url() == "sqlite:///:memory:"


def test_unreadable_setting_file(monkeypatch, tmp_path):
    path = tmp_path / "invalid"
    path.write_bytes(b"\xff")
    monkeypatch.setenv("DB_PASSWORD", str(path))
    with pytest.raises(ValueError, match="Unable to read"):
        database_setting("DB_PASSWORD")


def test_empty_setting_file(monkeypatch, tmp_path):
    path = tmp_path / "empty"
    path.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("DB_PASSWORD", str(path))
    with pytest.raises(ValueError, match="non-empty"):
        database_setting("DB_PASSWORD")


def test_nonexistent_file_path_is_literal(monkeypatch, tmp_path):
    value = str(tmp_path / "missing")
    monkeypatch.setenv("DB_PASSWORD", value)
    assert database_setting("DB_PASSWORD") == value


def test_existing_sqlite_database_path_is_not_read(monkeypatch, tmp_path):
    path = tmp_path / "database.db"
    path.write_bytes(b"\xffSQLite database")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    assert make_url(database_url()).database == str(path)
