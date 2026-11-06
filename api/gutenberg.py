import math
from collections import defaultdict
from threading import Lock, Thread

import pycountry
from flask import Blueprint, current_app, jsonify
from sqlalchemy import func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError

from api import RESULTS_PER_PAGE, get_engine, media_download_directory, sanitize_description
from export_gutenberg import (
    DatabaseIndexWriter,
    books_table,
    creators_table,
    export_gutenberg,
    tags_table,
)

bp = Blueprint("gutenberg", __name__)

UNAVAILABLE = "The Gutenberg index is unavailable. Download it from the sidebar."
gutenberg_lock = Lock()
gutenberg_job = {"state": "idle", "phase": "", "done": 0, "total": 0, "books": 0, "error": ""}
tags_cache = {"url": None, "tags": None}


def run_gutenberg_update(app):
    def progress(phase, done, total):
        with gutenberg_lock:
            gutenberg_job.update(phase=phase, done=done, total=total)

    try:
        with app.app_context():
            engine = get_engine()
            archive_path = media_download_directory("texts") / "rdf-files.tar.bz2"
        count = export_gutenberg(
            archive_path,
            DatabaseIndexWriter(engine),
            progress=progress,
        )
        with gutenberg_lock:
            tags_cache.update(url=None, tags=None)
            gutenberg_job.update(state="done", phase="", books=count)
    except Exception as error:
        app.logger.exception("Gutenberg index update failed")
        with gutenberg_lock:
            gutenberg_job.update(state="error", error=str(error) or type(error).__name__)


def index_available(connection):
    return connection.execute(select(books_table.c.id).limit(1)).first() is not None


@bp.route("/api/gutenberg/status")
def gutenberg_status():
    with gutenberg_lock:
        status = dict(gutenberg_job)
    try:
        with get_engine().connect() as connection:
            status["available"] = index_available(connection)
    except SQLAlchemyError as error:
        current_app.logger.warning("Could not read Gutenberg index: %s", error)
        status["available"] = False
    return jsonify(status)


@bp.route("/api/gutenberg/update", methods=["POST"])
def gutenberg_update():
    with gutenberg_lock:
        if gutenberg_job["state"] == "running":
            return jsonify(error="The Gutenberg index is already updating."), 409
        gutenberg_job.update(state="running", phase="download", done=0, total=0, books=0, error="")
    Thread(
        target=run_gutenberg_update,
        args=(current_app._get_current_object(),),
        daemon=True,
    ).start()
    return gutenberg_status(), 202


@bp.route("/api/gutenberg/tags")
def gutenberg_tags():
    url = current_app.config["DATABASE_URL"]
    with gutenberg_lock:
        if tags_cache["url"] == url:
            return jsonify(tags=tags_cache["tags"])
    try:
        with get_engine().connect() as connection:
            if not index_available(connection):
                return jsonify(error=UNAVAILABLE), 503
            count = func.count().label("count")
            rows = connection.execute(select(tags_table.c.tag, count).group_by(tags_table.c.tag)).all()
    except SQLAlchemyError as error:
        current_app.logger.warning("Could not read Gutenberg index: %s", error)
        return jsonify(error=UNAVAILABLE), 503
    tags = [
        {"name": tag, "count": total}
        for tag, total in sorted(rows, key=lambda row: (-row[1], row[0].casefold()))
    ]
    with gutenberg_lock:
        tags_cache.update(url=url, tags=tags)
    return jsonify(tags=tags)


def search_gutenberg(query, language, page, tags=(), excluded_tags=()):
    if len(tags) + len(excluded_tags) > 50 or any(
        not 1 <= len(tag) <= 300 for tag in [*tags, *excluded_tags]
    ):
        return jsonify(error="Select at most 50 tags."), 400

    conditions = [
        books_table.c.search_text.contains(term, autoescape=True)
        for term in query.casefold().split()
    ]
    if language:
        entry = pycountry.languages.get(alpha_3=language)
        codes = {code for code in (language, getattr(entry, "alpha_2", None)) if code}
        languages = literal("; ") + books_table.c.language + literal("; ")
        conditions.append(or_(*(languages.contains(f"; {code}; ", autoescape=True) for code in codes)))
    for tag in tags:
        conditions.append(books_table.c.id.in_(select(tags_table.c.book_id).where(tags_table.c.tag == tag)))
    for tag in excluded_tags:
        conditions.append(books_table.c.id.not_in(select(tags_table.c.book_id).where(tags_table.c.tag == tag)))

    try:
        with get_engine().connect() as connection:
            if not index_available(connection):
                return jsonify(error=UNAVAILABLE), 503
            total_results = connection.execute(
                select(func.count()).select_from(books_table).where(*conditions)
            ).scalar_one()
            books = connection.execute(
                select(books_table).where(*conditions).order_by(books_table.c.id)
                .offset((page - 1) * RESULTS_PER_PAGE).limit(RESULTS_PER_PAGE)
            ).mappings().all()
            book_ids = [book["id"] for book in books]
            authors = defaultdict(list)
            for book_id, name in connection.execute(
                select(creators_table.c.book_id, creators_table.c.name)
                .where(creators_table.c.book_id.in_(book_ids), creators_table.c.name != "")
                .order_by(creators_table.c.id)
            ):
                authors[book_id].append(name)
            topics = defaultdict(list)
            for book_id, tag in connection.execute(
                select(tags_table.c.book_id, tags_table.c.tag)
                .where(tags_table.c.book_id.in_(book_ids))
                .order_by(tags_table.c.id)
            ):
                topics[book_id].append(tag)
    except SQLAlchemyError as error:
        current_app.logger.warning("Could not read Gutenberg index: %s", error)
        return jsonify(error=UNAVAILABLE), 503

    return jsonify(
        results=[
            {
                "identifier": str(book["id"]),
                "title": book["title"] or f"Book {book['id']}",
                "description": sanitize_description(book["summary"]),
                "mediatype": book["type"],
                "year": book["issued"][:4],
                "languages": [value for value in book["language"].split("; ") if value],
                "authors": authors[book["id"]],
                "topics": topics[book["id"]],
                "source": "gutenberg",
            }
            for book in books
        ],
        page=page,
        page_size=RESULTS_PER_PAGE,
        total_pages=math.ceil(total_results / RESULTS_PER_PAGE),
        total_results=total_results,
    )
