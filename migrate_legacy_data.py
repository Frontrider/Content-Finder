"""Import the old CSV/JSON data files into the database.

The Gutenberg index is not handled here; rebuild it with
``python export_gutenberg.py --skip-download``.
"""

import argparse
import csv
import json
from pathlib import Path

from sqlalchemy import insert, select

from api.archive import saved_filters_table
from database import create_database_engine, database_url
from export_public_domain_torrents import save_catalog

DATA_DIR = Path(__file__).resolve().parent / "data"


def read_catalog_csv(path):
    movies = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as csv_file:
        for row in csv.DictReader(csv_file):
            movie = movies.setdefault(row["movie_id"], {
                "id": row["movie_id"],
                "title": row["title"],
                "movie_url": row["movie_url"],
                "categories": [],
                "torrents": [],
            })
            movie["categories"].extend(row["categories"].split(";"))
            movie["torrents"].append({"label": row["torrent_label"], "url": row["torrent_url"]})
    return list(movies.values())


def import_catalog_csv(engine, path):
    return save_catalog(engine, read_catalog_csv(path))


def import_saved_filters(engine, kind, path):
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{path} must contain a list of strings")
    with engine.begin() as connection:
        existing = {
            value.casefold()
            for value in connection.execute(
                select(saved_filters_table.c.value).where(saved_filters_table.c.kind == kind)
            ).scalars()
        }
        rows = []
        for value in values:
            value = value.strip()
            if value and value.casefold() not in existing:
                existing.add(value.casefold())
                rows.append({"kind": kind, "value": value})
        if rows:
            connection.execute(insert(saved_filters_table), rows)
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=database_url())
    parser.add_argument("--catalog-csv", type=Path, default=DATA_DIR / "public_domain_torrents.csv")
    parser.add_argument("--topics-json", type=Path, default=DATA_DIR / "archive_topics.json")
    parser.add_argument("--collections-json", type=Path, default=DATA_DIR / "archive_collections.json")
    args = parser.parse_args()

    engine = create_database_engine(args.database_url)
    if args.catalog_csv.is_file():
        movies, torrents = import_catalog_csv(engine, args.catalog_csv)
        print(f"Imported {torrents} torrents from {movies} movies")
    for kind, path in (("topics", args.topics_json), ("collections", args.collections_json)):
        if path.is_file():
            print(f"Imported {import_saved_filters(engine, kind, path)} new {kind}")


if __name__ == "__main__":
    main()
