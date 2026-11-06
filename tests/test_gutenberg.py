import time
from pathlib import Path

from api import gutenberg


def search(client, **params):
    return client.get("/api/search", query_string={"source": "gutenberg", **params})


def test_unavailable_before_import(client):
    assert client.get("/api/gutenberg/status").json["available"] is False
    assert client.get("/api/gutenberg/tags").status_code == 503
    assert search(client).status_code == 503


def test_tags_are_counted_and_skip_lcc(client, gutenberg_index):
    assert client.get("/api/gutenberg/tags").json["tags"] == [
        {"name": "Drama", "count": 2},
        {"name": "Adventure", "count": 1},
        {"name": "Whaling", "count": 1},
    ]


def test_search_result_shape(client, gutenberg_index):
    response = search(client, q="moby melville")
    assert response.status_code == 200
    assert response.json["total_results"] == 1
    assert response.json["results"][0] == {
        "identifier": "1",
        "title": "Moby Dick",
        "description": "<b>whale</b>alert(1)",
        "mediatype": "Text",
        "year": "1851",
        "languages": ["en"],
        "authors": ["Melville"],
        "topics": ["Whaling", "Adventure"],
        "source": "gutenberg",
    }


def test_search_escapes_like_wildcards(client, gutenberg_index):
    assert [book["identifier"] for book in search(client, q="%").json["results"]] == ["3"]


def test_language_filter_matches_alpha2_codes(client, gutenberg_index):
    assert [book["identifier"] for book in search(client, language="eng").json["results"]] == ["1", "3"]
    assert [book["identifier"] for book in search(client, language="deu").json["results"]] == ["2"]


def test_tag_include_and_exclude(client, gutenberg_index):
    assert [book["identifier"] for book in search(client, tag="Drama").json["results"]] == ["2", "3"]
    assert [book["identifier"] for book in search(client, exclude_tag="Drama").json["results"]] == ["1"]
    assert search(client, tag=[f"t{i}" for i in range(51)]).status_code == 400


def test_update_job_writes_to_database(client, monkeypatch):
    paths = []

    def export(path, writer, progress):
        paths.append(path)
        return 7

    monkeypatch.setattr(gutenberg, "export_gutenberg", export)
    assert client.post("/api/gutenberg/update").status_code == 202
    for _ in range(50):
        status = client.get("/api/gutenberg/status").json
        if status["state"] != "running":
            break
        time.sleep(0.01)
    assert status["state"] == "done"
    assert status["books"] == 7
    assert paths == [
        Path(client.application.config["BOOKS_DOWNLOAD_DIR"]) / "rdf-files.tar.bz2",
    ]
