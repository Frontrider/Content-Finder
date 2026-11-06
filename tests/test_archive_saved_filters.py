import json

import pytest

from api import archive
from conftest import FakeResponse


@pytest.fixture
def archive_matches(monkeypatch):
    monkeypatch.setattr(
        archive, "urlopen",
        lambda *args, **kwargs: FakeResponse(json.dumps({"response": {"numFound": 3}}).encode()),
    )


def test_saved_topics_start_empty(client):
    assert client.get("/api/topics").json == {"topics": []}
    assert client.get("/api/collections").json == {"collections": []}


def test_saved_topics_are_deduplicated_case_insensitively(client, archive_matches):
    assert client.post("/api/topics", json={"topic": "jazz"}).json["topics"] == ["jazz"]
    assert client.post("/api/topics", json={"topic": "Anime"}).json["topics"] == ["Anime", "jazz"]
    assert client.post("/api/topics", json={"topic": " JAZZ "}).json["topics"] == ["Anime", "jazz"]
    assert client.get("/api/topics").json == {"topics": ["Anime", "jazz"]}


def test_topics_and_collections_are_separate(client, archive_matches):
    client.post("/api/collections", json={"collection": "opensource"})
    assert client.get("/api/collections").json == {"collections": ["opensource"]}
    assert client.get("/api/topics").json == {"topics": []}


def test_topic_without_matches_is_not_saved(client, monkeypatch):
    monkeypatch.setattr(
        archive, "urlopen",
        lambda *args, **kwargs: FakeResponse(json.dumps({"response": {"numFound": 0}}).encode()),
    )
    assert client.post("/api/topics", json={"topic": "nothing"}).status_code == 404
    assert client.get("/api/topics").json == {"topics": []}
