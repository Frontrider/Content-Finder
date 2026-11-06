from api import public_domain_torrents
from conftest import FakeResponse
import rqbit_client


class FakeRqbitClient:
    def __init__(self):
        self.added = []
        self.output_folders = []

    def add_torrent(self, torrent_data, *, output_folder=None):
        self.added.append(torrent_data)
        self.output_folders.append(str(output_folder))
        return {"info_hash": "test-info-hash", "torrent_id": len(self.added) - 1}

    def list_torrents(self):
        return [{
            "info_hash": "test-info-hash",
            "stats": {
                "state": "live",
                "finished": False,
                "progress_bytes": 100,
                "total_bytes": 1000,
                "error": None,
            },
        }]


def search(client, **params):
    return client.get("/api/search", query_string={"source": "catalog", **params})


def test_search_keeps_site_order_and_drops_ignored_categories(client, pdt_catalog):
    response = search(client)
    assert response.json["total_results"] == 2
    assert response.json["categories"] == ["Horror", "SciFi"]
    assert [movie["title"] for movie in response.json["results"]] == ["Nosferatu", "Metropolis"]
    assert [torrent["label"] for torrent in response.json["results"][1]["torrents"]] == ["AVI", "MP4"]


def test_category_resets_when_not_in_query_matches(client, pdt_catalog):
    response = search(client, q="metro", category="Horror")
    assert response.json["selected_category"] == ""
    assert response.json["categories"] == ["SciFi"]
    assert search(client, category="Nope").status_code == 400


def test_category_filter(client, pdt_catalog):
    response = search(client, category="Horror")
    assert [movie["identifier"] for movie in response.json["results"]] == ["5"]


def test_catalog_page(client, pdt_catalog):
    assert client.get("/catalog/5").status_code == 200
    assert client.get("/catalog/99").status_code == 404
    assert client.get("/catalog/abc").status_code == 404


def test_torrent_download_sends_to_rqbit_once_and_checks_status(client, pdt_catalog, monkeypatch):
    rqbit = FakeRqbitClient()
    monkeypatch.setattr(rqbit_client, "get_rqbit_client", lambda: rqbit)
    monkeypatch.setattr(public_domain_torrents, "get_rqbit_client", lambda: rqbit)
    monkeypatch.setattr(public_domain_torrents, "urlopen", lambda *args, **kwargs: FakeResponse(b"d4:infoe"))
    assert client.post("/catalog/6/download/1").status_code == 302
    assert rqbit.added == [b"d4:infoe"]
    assert rqbit.output_folders == [client.application.config["VIDEOS_DOWNLOAD_DIR"]]
    status = client.get(
        "/api/rqbit/status",
        query_string={"source": "catalog", "identifier": "6", "key": "1"},
    )
    assert status.json["state"] == "live"
    assert status.json["progress_bytes"] == 100
    assert client.post("/catalog/6/download/1").status_code == 409
    assert client.post("/catalog/6/download/2").status_code == 404


def test_torrent_download_rejects_untrusted_redirect(client, pdt_catalog, monkeypatch):
    monkeypatch.setattr(
        public_domain_torrents, "urlopen",
        lambda *args, **kwargs: FakeResponse(b"d4:infoe", "https://example.com/x"),
    )
    assert client.post("/catalog/5/download/0").status_code == 502
