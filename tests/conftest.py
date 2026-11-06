import io

import pytest

from api import get_engine
from app import app as flask_app
from export_gutenberg import DatabaseIndexWriter, write_index
from export_public_domain_torrents import save_catalog


class FakeResponse(io.BytesIO):
    def __init__(self, data, url="https://www.publicdomaintorrents.com/bt/btdownload.php"):
        super().__init__(data)
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def geturl(self):
        return self.url


def gutenberg_book(book_id, title, language, *, summary="", authors=(), subjects=(), bookshelves=()):
    return {
        "id": book_id,
        "title": title,
        "alternative": "",
        "issued": "1851-01-01",
        "language": language,
        "type": "Text",
        "rights": "",
        "downloads": "",
        "summary": summary,
        "creators": [
            {"agent_id": "", "role": "aut", "name": name, "birthdate": "", "deathdate": ""}
            for name in authors
        ],
        "subjects": [{"scheme": scheme, "subject": subject} for scheme, subject in subjects],
        "bookshelves": list(bookshelves),
        "files": [],
    }


def pdt_movie(movie_id, title, categories, torrents):
    return {
        "id": movie_id,
        "title": title,
        "movie_url": f"https://www.publicdomaintorrents.info/nshowmovie.html?movieid={movie_id}",
        "categories": categories,
        "torrents": [
            {"label": label, "url": f"https://www.publicdomaintorrents.com/bt/btdownload.php?type=torrent&file={name}"}
            for label, name in torrents
        ],
    }


@pytest.fixture
def app(tmp_path):
    flask_app.config.update(
        TESTING=True,
        DATABASE_URL=f"sqlite:///{tmp_path / 'test.db'}",
        DOWNLOAD_DIR=str(tmp_path / "downloads"),
        BOOKS_DOWNLOAD_DIR=str(tmp_path / "books"),
        VIDEOS_DOWNLOAD_DIR=str(tmp_path / "videos"),
    )
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def engine(app):
    with app.app_context():
        return get_engine()


@pytest.fixture
def gutenberg_index(engine):
    write_index([
        gutenberg_book(
            "1", "Moby Dick", "en",
            summary="<b>whale</b><script>alert(1)</script>",
            authors=["Melville"],
            subjects=[("LCSH", "Whaling"), ("LCC", "PS")],
            bookshelves=["Adventure"],
        ),
        gutenberg_book("2", "Faust", "de", subjects=[("LCSH", "Drama")]),
        gutenberg_book("3", "100% Proof", "en", subjects=[("LCSH", "Drama")]),
    ], DatabaseIndexWriter(engine))


@pytest.fixture
def pdt_catalog(engine):
    save_catalog(engine, [
        pdt_movie("5", "Nosferatu", ["Horror", "ALL"], [("MP4", "a")]),
        pdt_movie("6", "Metropolis", ["SciFi"], [("AVI", "b"), ("MP4", "c")]),
    ])
