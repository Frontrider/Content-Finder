import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

import rqbit_client
from api import archive
from conftest import FakeResponse as FakeSourceResponse
from rqbit_client import RqbitClient, RqbitError


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def read(self):
        return self.data


def test_add_torrent_posts_binary_data_and_parses_info_hash(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(json.dumps({
            "id": 7,
            "details": {"info_hash": "abc123"},
        }).encode())

    monkeypatch.setattr(rqbit_client, "urlopen", fake_urlopen)
    client = RqbitClient("http://rqbit:3030/")

    result = client.add_torrent(b"d4:infoe")

    request, timeout = requests[0]
    assert request.full_url == "http://rqbit:3030/torrents"
    assert request.data == b"d4:infoe"
    assert request.get_header("Content-type") == "application/octet-stream"
    assert timeout == 15
    assert result == {"info_hash": "abc123", "torrent_id": 7}


def test_list_torrents_requests_stats(monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url == "http://rqbit:3030/torrents?with_stats=true"
        return FakeResponse(b'{"torrents":[{"info_hash":"abc123"}]}')

    monkeypatch.setattr(rqbit_client, "urlopen", fake_urlopen)
    assert RqbitClient("http://rqbit:3030").list_torrents() == [{"info_hash": "abc123"}]


def test_add_torrent_encodes_shared_output_folder(monkeypatch):
    def fake_urlopen(request, timeout):
        assert parse_qs(urlsplit(request.full_url).query) == {
            "output_folder": ["/data/books & comics"],
        }
        return FakeResponse(b'{"id":7,"details":{"info_hash":"abc123"}}')

    monkeypatch.setattr(rqbit_client, "urlopen", fake_urlopen)
    RqbitClient("http://rqbit:3030").add_torrent(
        b"d4:infoe", output_folder="/data/books & comics",
    )


def test_invalid_add_response_raises_rqbit_error(monkeypatch):
    monkeypatch.setattr(rqbit_client, "urlopen", lambda *args, **kwargs: FakeResponse(b"{}"))
    with pytest.raises(RqbitError, match="no torrent info hash"):
        RqbitClient("http://rqbit:3030").add_torrent(b"d4:infoe")


def test_rqbit_url_environment_configures_client():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app import app; from rqbit_client import get_rqbit_client; "
            "ctx = app.app_context(); ctx.push(); print(get_rqbit_client().base_url); "
            "print(app.config['BOOKS_DOWNLOAD_DIR']); print(app.config['VIDEOS_DOWNLOAD_DIR'])",
        ],
        env={
            **os.environ,
            "RQBIT_URL": "http://example.test:3030/proxy/",
            "BOOKS_DOWNLOAD_DIR": "/library/comics",
            "VIDEOS_DOWNLOAD_DIR": "/library/videos",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "http://example.test:3030/proxy", "/library/comics", "/library/videos",
    ]


@pytest.mark.parametrize("mediatype", ["texts", "movies", "audio", "software", None])
@pytest.mark.parametrize("route", ["files", "download-torrent"])
def test_archive_torrent_file_is_sent_to_rqbit(client, monkeypatch, mediatype, route):
    class FakeRqbitClient:
        added = []

        def add_torrent(self, torrent_data, *, output_folder=None):
            self.added.append(torrent_data)
            self.output_folder = output_folder
            return {"info_hash": "file-info-hash", "torrent_id": 8}

        def list_torrents(self):
            return [{
                "info_hash": "file-info-hash",
                "stats": {
                    "state": "live",
                    "finished": False,
                    "progress_bytes": 4,
                    "total_bytes": 20,
                    "error": None,
                },
            }]

    rqbit = FakeRqbitClient()
    payload = {
        "metadata": {"title": "Test", "mediatype": mediatype},
        "files": [{"name": "Test.torrent", "format": "Torrent", "size": "9"}],
    }
    monkeypatch.setattr(archive, "get_item_metadata", lambda _identifier: payload)
    monkeypatch.setattr(
        archive,
        "urlopen",
        lambda *args, **kwargs: FakeSourceResponse(b"d4:infoe", "https://archive.org/download/Test.torrent"),
    )
    monkeypatch.setattr(archive, "get_rqbit_client", lambda: rqbit)
    monkeypatch.setattr(rqbit_client, "get_rqbit_client", lambda: rqbit)

    response = client.post(f"/item/test-item/{route}", json={"name": "Test.torrent"})

    assert response.status_code == (202 if route == "files" else 302)
    assert rqbit.added == [b"d4:infoe"]
    expected = {
        "texts": client.application.config["BOOKS_DOWNLOAD_DIR"],
        "movies": client.application.config["VIDEOS_DOWNLOAD_DIR"],
    }.get(mediatype, client.application.config["DOWNLOAD_DIR"])
    assert (str(rqbit.output_folder) if rqbit.output_folder is not None else None) == expected
    status = client.get(
        "/api/rqbit/status",
        query_string={
            "source": "archive_file" if route == "files" else "archive",
            "identifier": "test-item",
            "key": "file:Test.torrent" if route == "files" else "archive",
        },
    )
    assert status.json["state"] == "live"


@pytest.mark.parametrize(
    "mediatype,setting",
    [
        ("texts", "BOOKS_DOWNLOAD_DIR"),
        ("movies", "VIDEOS_DOWNLOAD_DIR"),
        ("audio", "DOWNLOAD_DIR"),
        ("software", "DOWNLOAD_DIR"),
        (None, "DOWNLOAD_DIR"),
    ],
)
def test_direct_download_uses_media_directory(client, monkeypatch, mediatype, setting):
    from pathlib import Path

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    payload = {
        "metadata": {"title": "Test", "mediatype": mediatype},
        "files": [{"name": "Test.bin", "size": "7"}],
    }

    def source_response(*args, **kwargs):
        response = FakeSourceResponse(b"content", "https://archive.org/download/Test.bin")
        response.headers = {"Content-Length": "7"}
        return response

    monkeypatch.setattr(archive, "get_item_metadata", lambda _identifier: payload)
    monkeypatch.setattr(archive, "urlopen", source_response)
    monkeypatch.setattr(archive, "Thread", ImmediateThread)
    response = client.post("/item/direct-test/files", json={"name": "Test.bin"})
    assert response.status_code == 202
    destination = (
        Path(client.application.config[setting]) / "internetarchive-direct-test" / "Test.bin"
    )
    assert destination.read_bytes() == b"content"
    with client.application.app_context():
        assert archive.download_files("direct-test", payload)[0]["saved"] is True
    assert client.post("/item/direct-test/files", json={"name": "Test.bin"}).status_code == 409
