"""Export PublicDomainTorrents.info catalog entries and torrent links to the database."""

import argparse
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import Column, Integer, Table, Text, delete, insert
from sqlalchemy.exc import SQLAlchemyError

from database import create_database_engine, database_url, metadata


SITE_URL = "https://www.publicdomaintorrents.info/"
ALL_MOVIES_URL = urljoin(SITE_URL, "nshowcat.html?category=ALL")
REQUEST_DELAY_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 30
PDT_TORRENT_HOSTS = {"publicdomaintorrents.com", "www.publicdomaintorrents.com"}
IGNORED_CATEGORIES = {"ALL", "STREAMS"}

movies_table = Table(
    "pdt_movies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("position", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("movie_url", Text, nullable=False),
    Column("search_text", Text, nullable=False),
)
categories_table = Table(
    "pdt_categories", metadata,
    Column("id", Integer, primary_key=True),
    Column("movie_id", Integer, nullable=False, index=True),
    Column("category", Text, nullable=False, index=True),
)
torrents_table = Table(
    "pdt_torrents", metadata,
    Column("id", Integer, primary_key=True),
    Column("movie_id", Integer, nullable=False, index=True),
    Column("label", Text, nullable=False),
    Column("url", Text, nullable=False),
)


class CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.movies = {}
        self._movie_id = None
        self._movie_text = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        parsed = urlsplit(href)
        if parsed.path.endswith("nshowmovie.html"):
            movie_id = parse_qs(parsed.query).get("movieid", [""])[0]
            if movie_id.isdigit():
                self._movie_id = movie_id
                self._movie_text = []

    def handle_data(self, data):
        if self._movie_id is not None:
            self._movie_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._movie_id is not None:
            title = " ".join(" ".join(self._movie_text).split())
            if title:
                self.movies[self._movie_id] = title
            self._movie_id = None
            self._movie_text = []


class ItemParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.categories = []
        self.torrents = []
        self._capture_title = False
        self._capture_torrent = False
        self._in_movie_details = False
        self._capture_movie_heading = False
        self._torrent_url = ""
        self._torrent_label = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._capture_title = True
        elif tag == "h3":
            self._capture_movie_heading = True
        if tag != "a":
            return

        href = attrs.get("href", "")
        parsed = urlsplit(href)
        if self._in_movie_details and parsed.path.endswith("nshowcat.html"):
            categories = parse_qs(parsed.query).get("category", [])
            if categories:
                self.categories.extend(categories)
        elif parsed.path.endswith("btdownload.php"):
            parameters = parse_qs(parsed.query)
            if parameters.get("type") == ["torrent"] and parameters.get("file"):
                self._capture_torrent = True
                self._torrent_url = urljoin(SITE_URL, href)
                self._torrent_label = []

    def handle_data(self, data):
        if self._capture_title:
            self.title_parts.append(data)
        if self._capture_torrent:
            self._torrent_label.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._capture_title = False
        elif tag == "h3":
            self._capture_movie_heading = False
            self._in_movie_details = True
        if tag == "a" and self._capture_torrent:
            self.torrents.append(
                (self._torrent_url, " ".join(" ".join(self._torrent_label).split()))
            )
            self._capture_torrent = False


def fetch_page(url):
    request = Request(
        url,
        headers={"User-Agent": "contentFind catalog export/1.0"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "iso-8859-1"
    return body.decode(charset, errors="replace")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def print_progress(completed, total, started_at):
    elapsed = time.monotonic() - started_at
    eta = elapsed / completed * (total - completed) if completed else 0
    width = 30
    filled = int(width * completed / total)
    bar = "#" * filled + "-" * (width - filled)
    message = (
        f"\r[{bar}] {completed}/{total} "
        f"({completed / total:.0%}) elapsed {format_duration(elapsed)} "
        f"ETA {format_duration(eta)}"
    )
    print(message, end="", file=sys.stderr, flush=True)


def valid_torrent_url(url):
    parsed_url = urlsplit(url)
    return (
        parsed_url.scheme in {"http", "https"}
        and parsed_url.hostname in PDT_TORRENT_HOSTS
        and parsed_url.path == "/bt/btdownload.php"
        and parse_qs(parsed_url.query).get("type") == ["torrent"]
    )


def iter_catalog():
    """Scrape the site and yield one movie dict at a time."""
    print("Fetching PublicDomainTorrents movie catalog...", file=sys.stderr, flush=True)
    catalog = CatalogParser()
    catalog.feed(fetch_page(ALL_MOVIES_URL))
    if not catalog.movies:
        raise RuntimeError("The all-movies catalog contained no movie entries.")
    total_movies = len(catalog.movies)
    print(f"Found {total_movies} movies; downloading details and torrent listings.", file=sys.stderr)

    started_at = time.monotonic()
    try:
        for index, (movie_id, catalog_title) in enumerate(catalog.movies.items(), start=1):
            if index > 1:
                time.sleep(REQUEST_DELAY_SECONDS)
            movie_url = f"{SITE_URL}nshowmovie.html?{urlencode({'movieid': movie_id})}"
            try:
                item = ItemParser()
                item.feed(fetch_page(movie_url))
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                raise RuntimeError(
                    f"Could not retrieve movie {movie_id} ({catalog_title!r}): {error}"
                ) from error

            yield {
                "id": movie_id,
                "title": " ".join(" ".join(item.title_parts).split()) or catalog_title,
                "movie_url": movie_url,
                "categories": list(dict.fromkeys(item.categories)),
                "torrents": [{"url": url, "label": label} for url, label in item.torrents],
            }
            print_progress(index, total_movies, started_at)
    finally:
        print(file=sys.stderr, flush=True)


def save_catalog(engine, movies):
    """Replace the catalog tables with ``movies`` in one transaction."""
    movie_count = torrent_count = 0
    with engine.begin() as connection:
        for table in (movies_table, categories_table, torrents_table):
            connection.execute(delete(table))
        seen = set()
        for movie in movies:
            if not str(movie["id"]).isdigit() or int(movie["id"]) in seen:
                continue
            movie_id = int(movie["id"])
            categories = list(dict.fromkeys(
                category.strip() for category in movie["categories"]
                if category.strip() and category.strip().upper() not in IGNORED_CATEGORIES
            ))
            torrents = [
                {"movie_id": movie_id, "label": torrent["label"].strip() or "Torrent", "url": torrent["url"].strip()}
                for torrent in movie["torrents"] if valid_torrent_url(torrent["url"].strip())
            ]
            if not torrents:
                continue
            seen.add(movie_id)
            title = movie["title"].strip() or f"Movie {movie_id}"
            connection.execute(insert(movies_table), {
                "id": movie_id,
                "position": movie_count,
                "title": title,
                "movie_url": movie["movie_url"],
                "search_text": " ".join(
                    [title, *categories, *(torrent["label"] for torrent in torrents)]
                ).casefold(),
            })
            if categories:
                connection.execute(
                    insert(categories_table),
                    [{"movie_id": movie_id, "category": category} for category in categories],
                )
            connection.execute(insert(torrents_table), torrents)
            movie_count += 1
            torrent_count += len(torrents)
        if torrent_count == 0:
            raise RuntimeError("No torrent links were found in the movie pages.")
    return movie_count, torrent_count


def export_catalog(engine):
    return save_catalog(engine, iter_catalog())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=database_url())
    args = parser.parse_args()
    try:
        engine = create_database_engine(args.database_url)
        movie_count, torrent_count = export_catalog(engine)
    except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, SQLAlchemyError) as error:
        parser.error(str(error))
    print(
        f"Saved {torrent_count} torrent links from {movie_count} movies to "
        f"{engine.url.render_as_string(hide_password=True)}"
    )


if __name__ == "__main__":
    main()
