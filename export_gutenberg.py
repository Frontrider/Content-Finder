"""Download the Project Gutenberg RDF catalog and export it to a database or CSV files."""

import argparse
import csv
import os
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Iterator, Protocol
from urllib.request import Request, urlopen

from sqlalchemy import Column, Integer, Table, Text, delete, insert

from database import create_database_engine, database_url, metadata

INDEX_URL = "https://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2"
REQUEST_TIMEOUT_SECONDS = 60
CHUNK_SIZE = 1024 * 1024
DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_ARCHIVE_PATH = DATA_DIR / "gutenberg" / "rdf-files.tar.bz2"
DEFAULT_OUTPUT_DIR = DATA_DIR / "gutenberg"

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "dcam": "http://purl.org/dc/dcam/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
}
RDF_ABOUT = f"{{{NS['rdf']}}}about"
RDF_RESOURCE = f"{{{NS['rdf']}}}resource"
MARC_ROLES = {"creator": "aut"}

CSV_COLUMNS = {
    "books": ["id", "title", "alternative", "issued", "language", "type", "rights", "downloads", "summary"],
    "creators": ["book_id", "agent_id", "role", "name", "birthdate", "deathdate"],
    "subjects": ["book_id", "scheme", "subject"],
    "bookshelves": ["book_id", "bookshelf"],
    "files": ["book_id", "url", "format", "extent", "modified"],
}


def _book_table(name, *columns):
    return Table(
        name, metadata,
        Column("id", Integer, primary_key=True),
        Column("book_id", Integer, nullable=False, index=True),
        *columns,
    )


books_table = Table(
    "gutenberg_books", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    *(Column(name, Text, nullable=False) for name in CSV_COLUMNS["books"][1:]),
    Column("search_text", Text, nullable=False),
)
creators_table = _book_table(
    "gutenberg_creators",
    *(Column(name, Text, nullable=False) for name in CSV_COLUMNS["creators"][1:]),
)
subjects_table = _book_table("gutenberg_subjects", Column("scheme", Text), Column("subject", Text))
bookshelves_table = _book_table("gutenberg_bookshelves", Column("bookshelf", Text))
files_table = _book_table(
    "gutenberg_files",
    *(Column(name, Text, nullable=False) for name in CSV_COLUMNS["files"][1:]),
)
# Searchable tags: non-LCC subjects plus bookshelves, de-duplicated per book.
tags_table = _book_table("gutenberg_tags", Column("tag", Text, nullable=False, index=True))
GUTENBERG_TABLES = (books_table, creators_table, subjects_table, bookshelves_table, files_table, tags_table)


class IndexWriter(Protocol):
    """Sink for parsed books. Implement this to write somewhere else."""

    def write(self, book: dict) -> None: ...

    def close(self) -> None: ...

    def abort(self) -> None: ...


def print_progress(phase, done, total):
    if total:
        fraction = min(done / total, 1)
        filled = int(30 * fraction)
        bar = "#" * filled + "-" * (30 - filled)
        message = f"\r{phase:<8} [{bar}] {fraction:6.1%}"
    else:
        message = f"\r{phase:<8} {done / (1024 * 1024):8.1f} MiB"
    print(message, end="", file=sys.stderr, flush=True)


def download_index(destination=DEFAULT_ARCHIVE_PATH, url=INDEX_URL, progress=print_progress):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "contentFind/1.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        total = int(response.headers.get("Content-Length") or 0)
        file_descriptor, temp_name = tempfile.mkstemp(dir=destination.parent, suffix=".part")
        try:
            with os.fdopen(file_descriptor, "wb") as output:
                downloaded = 0
                while chunk := response.read(CHUNK_SIZE):
                    output.write(chunk)
                    downloaded += len(chunk)
                    progress("download", downloaded, total)
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    return destination


def _text(element, path):
    found = element.find(path, NS)
    return found.text.strip() if found is not None and found.text else ""


def _texts(element, path):
    return [found.text.strip() for found in element.findall(path, NS) if found.text]


def parse_rdf(source) -> Iterator[dict]:
    """Parse one Gutenberg RDF document and yield its ebook records."""
    root = ET.parse(source).getroot()
    for ebook in root.findall("pgterms:ebook", NS):
        book_id = ebook.get(RDF_ABOUT, "").rsplit("/", 1)[-1]
        if not book_id:
            continue

        creators = []
        for child in ebook:
            tag = child.tag.split("}", 1)[-1]
            namespace = child.tag[1:].split("}", 1)[0]
            if namespace == NS["dcterms"] and tag == "creator":
                role = MARC_ROLES["creator"]
            elif namespace == "http://id.loc.gov/vocabulary/relators/":
                role = tag
            else:
                continue
            agent = child.find("pgterms:agent", NS)
            if agent is None:
                continue
            creators.append({
                "agent_id": agent.get(RDF_ABOUT, "").rsplit("/", 1)[-1],
                "role": role,
                "name": _text(agent, "pgterms:name"),
                "birthdate": _text(agent, "pgterms:birthdate"),
                "deathdate": _text(agent, "pgterms:deathdate"),
            })

        subjects = []
        for description in ebook.findall("dcterms:subject/rdf:Description", NS):
            scheme = description.find("dcam:memberOf", NS)
            subjects.append({
                "scheme": (scheme.get(RDF_RESOURCE, "") if scheme is not None else "").rsplit("/", 1)[-1],
                "subject": _text(description, "rdf:value"),
            })

        files = []
        for file in ebook.findall("dcterms:hasFormat/pgterms:file", NS):
            files.append({
                "url": file.get(RDF_ABOUT, ""),
                "format": "; ".join(_texts(file, "dcterms:format/rdf:Description/rdf:value")),
                "extent": _text(file, "dcterms:extent"),
                "modified": _text(file, "dcterms:modified"),
            })

        yield {
            "id": book_id,
            "title": _text(ebook, "dcterms:title"),
            "alternative": "; ".join(_texts(ebook, "dcterms:alternative")),
            "issued": _text(ebook, "dcterms:issued"),
            "language": "; ".join(_texts(ebook, "dcterms:language/rdf:Description/rdf:value")),
            "type": _text(ebook, "dcterms:type/rdf:Description/rdf:value"),
            "rights": _text(ebook, "dcterms:rights"),
            "downloads": _text(ebook, "pgterms:downloads"),
            "summary": _text(ebook, "pgterms:marc520"),
            "creators": creators,
            "subjects": subjects,
            "bookshelves": _texts(ebook, "pgterms:bookshelf/rdf:Description/rdf:value"),
            "files": files,
        }


def iter_index(archive_path=DEFAULT_ARCHIVE_PATH, progress=print_progress) -> Iterator[dict]:
    """Stream books out of the downloaded tar.bz2 archive without extracting it."""
    total = Path(archive_path).stat().st_size
    with open(archive_path, "rb") as raw, tarfile.open(fileobj=raw, mode="r|bz2") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".rdf"):
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            try:
                yield from parse_rdf(source)
            except ET.ParseError as error:
                print(f"\nSkipping {member.name}: {error}", file=sys.stderr)
            progress("parse", raw.tell(), total)


class CsvIndexWriter:
    """Writes each book to a set of CSV files, one row at a time."""

    def __init__(self, output_dir=DEFAULT_OUTPUT_DIR):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._files = {}
        self._writers = {}
        for name, columns in CSV_COLUMNS.items():
            handle = open(self.output_dir / f"{name}.csv", "w", newline="", encoding="utf-8")
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            self._files[name] = handle
            self._writers[name] = writer

    def write(self, book):
        book_id = book["id"]
        self._writers["books"].writerow(book)
        for creator in book["creators"]:
            self._writers["creators"].writerow({"book_id": book_id, **creator})
        for subject in book["subjects"]:
            self._writers["subjects"].writerow({"book_id": book_id, **subject})
        for bookshelf in book["bookshelves"]:
            self._writers["bookshelves"].writerow({"book_id": book_id, "bookshelf": bookshelf})
        for file in book["files"]:
            self._writers["files"].writerow({"book_id": book_id, **file})

    def close(self):
        for handle in self._files.values():
            handle.close()

    def abort(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class DatabaseIndexWriter:
    """Replaces the Gutenberg tables in one transaction, so readers see the old index until close()."""

    BATCH_SIZE = 500

    def __init__(self, engine):
        self._connection = engine.connect()
        self._transaction = self._connection.begin()
        for table in GUTENBERG_TABLES:
            self._connection.execute(delete(table))
        self._rows = {table: [] for table in GUTENBERG_TABLES}
        self._seen = set()

    def write(self, book):
        if not book["id"].isdigit() or int(book["id"]) in self._seen:
            return
        book_id = int(book["id"])
        self._seen.add(book_id)
        tags = list(dict.fromkeys(
            [subject["subject"] for subject in book["subjects"]
             if subject["subject"] and subject["scheme"] != "LCC"]
            + [bookshelf for bookshelf in book["bookshelves"] if bookshelf]
        ))
        authors = [creator["name"] for creator in book["creators"] if creator["name"]]
        self._rows[books_table].append({
            **{name: book[name] for name in CSV_COLUMNS["books"]},
            "id": book_id,
            "search_text": " ".join([book["title"], *authors, *tags]).casefold(),
        })
        self._rows[creators_table].extend({"book_id": book_id, **creator} for creator in book["creators"])
        self._rows[subjects_table].extend({"book_id": book_id, **subject} for subject in book["subjects"])
        self._rows[bookshelves_table].extend(
            {"book_id": book_id, "bookshelf": bookshelf} for bookshelf in book["bookshelves"]
        )
        self._rows[files_table].extend({"book_id": book_id, **file} for file in book["files"])
        self._rows[tags_table].extend({"book_id": book_id, "tag": tag} for tag in tags)
        if len(self._rows[books_table]) >= self.BATCH_SIZE:
            self._flush()

    def _flush(self):
        for table, rows in self._rows.items():
            if rows:
                self._connection.execute(insert(table), rows)
                rows.clear()

    def close(self):
        try:
            self._flush()
            self._transaction.commit()
        finally:
            self._connection.close()

    def abort(self):
        try:
            self._transaction.rollback()
        finally:
            self._connection.close()


def write_index(books: Iterable[dict], writer: IndexWriter):
    count = 0
    try:
        for book in books:
            writer.write(book)
            count += 1
    except BaseException:
        writer.abort()
        raise
    writer.close()
    return count


def export_gutenberg(
    archive_path=DEFAULT_ARCHIVE_PATH,
    writer: IndexWriter | None = None,
    download=True,
    progress=print_progress,
):
    """Download the Gutenberg index (if requested) and send every book to ``writer``."""
    if download or not Path(archive_path).exists():
        download_index(archive_path, progress=progress)
    return write_index(
        iter_index(archive_path, progress),
        writer or DatabaseIndexWriter(create_database_engine()),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE_PATH)
    parser.add_argument("--database-url", default=database_url())
    parser.add_argument("--csv-dir", type=Path, help="write CSV files here instead of the database")
    parser.add_argument("--skip-download", action="store_true", help="reuse an existing archive")
    args = parser.parse_args()
    if args.csv_dir:
        writer, target = CsvIndexWriter(args.csv_dir), args.csv_dir
    else:
        engine = create_database_engine(args.database_url)
        writer, target = DatabaseIndexWriter(engine), engine.url.render_as_string(hide_password=True)
    count = export_gutenberg(args.archive, writer, download=not args.skip_download)
    print(file=sys.stderr)
    print(f"Exported {count} books to {target}")


if __name__ == "__main__":
    main()
