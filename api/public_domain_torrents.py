import math
from collections import defaultdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from api import RESULTS_PER_PAGE, SEARCH_TIMEOUT_SECONDS, TORRENT_MAX_BYTES, get_engine, media_download_directory
from export_public_domain_torrents import (
    PDT_TORRENT_HOSTS,
    categories_table,
    export_catalog,
    movies_table,
    torrents_table,
)
from rqbit_client import RqbitError, get_rqbit_client, link_torrent, torrent_status

bp = Blueprint("catalog", __name__)

UNAVAILABLE = "The public-domain torrent catalog is unavailable."


def ensure_catalog():
    try:
        engine = get_engine()
        with engine.connect() as connection:
            if connection.execute(select(movies_table.c.id).limit(1)).first() is not None:
                return True
        current_app.logger.info("Downloading PublicDomainTorrents catalog into the database")
        export_catalog(engine)
    except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, SQLAlchemyError) as error:
        current_app.logger.error("Could not initialize torrent catalog: %s", error)
        return False
    return True


def movie_details(connection, movies):
    movie_ids = [movie["id"] for movie in movies]
    categories = defaultdict(list)
    for movie_id, category in connection.execute(
        select(categories_table.c.movie_id, categories_table.c.category)
        .where(categories_table.c.movie_id.in_(movie_ids))
        .order_by(categories_table.c.id)
    ):
        categories[movie_id].append(category)
    torrents = defaultdict(list)
    for movie_id, label, url in connection.execute(
        select(torrents_table.c.movie_id, torrents_table.c.label, torrents_table.c.url)
        .where(torrents_table.c.movie_id.in_(movie_ids))
        .order_by(torrents_table.c.id)
    ):
        torrents[movie_id].append({"label": label, "url": url})
    return [
        {
            "identifier": str(movie["id"]),
            "title": movie["title"],
            "description": "",
            "mediatype": "Public Domain Movie",
            "year": "",
            "categories": categories[movie["id"]],
            "torrents": torrents[movie["id"]],
            "source": "catalog",
        }
        for movie in movies
    ]


def get_movie(movie_id):
    if not movie_id.isdigit():
        abort(404)
    if not ensure_catalog():
        abort(503, description=UNAVAILABLE)
    try:
        with get_engine().connect() as connection:
            movie = connection.execute(
                select(movies_table.c.id, movies_table.c.title).where(movies_table.c.id == int(movie_id))
            ).mappings().first()
            if movie is None:
                abort(404)
            return movie_details(connection, [movie])[0]
    except SQLAlchemyError as error:
        current_app.logger.error("Could not read public-domain torrent catalog: %s", error)
        abort(503, description=UNAVAILABLE)


def search_catalog(query, category, page):
    if not ensure_catalog():
        return jsonify(error=UNAVAILABLE), 503
    conditions = []
    normalized_query = query.casefold()
    if normalized_query:
        conditions.append(movies_table.c.search_text.contains(normalized_query, autoescape=True))
    try:
        with get_engine().connect() as connection:
            if category and connection.execute(
                select(categories_table.c.id).where(categories_table.c.category == category).limit(1)
            ).first() is None:
                return jsonify(error="Select a valid category."), 400
            matching_ids = select(movies_table.c.id).where(*conditions)
            available_categories = sorted(
                connection.execute(
                    select(categories_table.c.category).distinct()
                    .where(categories_table.c.movie_id.in_(matching_ids))
                ).scalars(),
                key=str.casefold,
            )
            if category not in available_categories:
                category = ""
            if category:
                conditions.append(movies_table.c.id.in_(
                    select(categories_table.c.movie_id).where(categories_table.c.category == category)
                ))
            total_results = connection.execute(
                select(func.count()).select_from(movies_table).where(*conditions)
            ).scalar_one()
            movies = connection.execute(
                select(movies_table.c.id, movies_table.c.title).where(*conditions)
                .order_by(movies_table.c.position)
                .offset((page - 1) * RESULTS_PER_PAGE).limit(RESULTS_PER_PAGE)
            ).mappings().all()
            results = movie_details(connection, movies)
    except SQLAlchemyError as error:
        current_app.logger.error("Could not read public-domain torrent catalog: %s", error)
        return jsonify(error=UNAVAILABLE), 503

    return jsonify(
        results=results,
        page=page,
        page_size=RESULTS_PER_PAGE,
        total_pages=math.ceil(total_results / RESULTS_PER_PAGE),
        total_results=total_results,
        categories=available_categories,
        selected_category=category,
    )


@bp.route("/catalog/<movie_id>")
def catalog_item_details(movie_id):
    movie = get_movie(movie_id)
    for index, torrent in enumerate(movie["torrents"]):
        torrent["key"] = str(index)
        torrent["status_url"] = url_for(
            "rqbit_torrent_status",
            source="catalog",
            identifier=movie_id,
            key=str(index),
        )
    return_query = request.args.get("q", "").strip()
    return_category = request.args.get("category", "")
    try:
        return_page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        return_page = 1
    return render_template(
        "catalog_item.html",
        movie=movie,
        return_query=return_query,
        return_category=return_category,
        return_page=return_page,
        downloaded=request.args.get("downloaded") == "1",
    )


@bp.route("/catalog/<movie_id>/download/<int:torrent_index>", methods=["POST"])
def download_catalog_torrent(movie_id, torrent_index):
    movie = get_movie(movie_id)
    if torrent_index >= len(movie["torrents"]):
        abort(404)

    try:
        status = torrent_status("catalog", movie_id, str(torrent_index))
        if status["state"] not in {"not_added", "not_found"}:
            abort(409, description="This torrent is already managed by rqbit.")
    except RqbitError as error:
        current_app.logger.warning("Could not check public-domain torrent in rqbit: %s", error)
        abort(502, description="Unable to check rqbit before sending the torrent.")

    torrent_url = movie["torrents"][torrent_index]["url"]
    try:
        request_to_source = Request(
            torrent_url,
            headers={"User-Agent": "contentFind torrent downloader/1.0"},
        )
        with urlopen(request_to_source, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            final_url = urlsplit(response.geturl())
            if final_url.hostname not in PDT_TORRENT_HOSTS:
                abort(502, description="Torrent download redirected to an untrusted host.")
            torrent_data = response.read(TORRENT_MAX_BYTES + 1)
        if len(torrent_data) > TORRENT_MAX_BYTES:
            abort(413, description="The torrent file exceeds the download size limit.")

        torrent = get_rqbit_client().add_torrent(
            torrent_data, output_folder=media_download_directory("movies"),
        )
        link_torrent("catalog", movie_id, str(torrent_index), torrent)
    except HTTPError as error:
        current_app.logger.warning("Public-domain torrent download failed: %s", error)
        abort(502, description="Unable to retrieve the torrent from its source.")
    except RqbitError as error:
        current_app.logger.warning("Could not send public-domain torrent to rqbit: %s", error)
        abort(502, description="Unable to send the torrent to rqbit.")
    except (URLError, TimeoutError, OSError) as error:
        current_app.logger.exception("Could not retrieve or send public-domain torrent")
        abort(502, description="Unable to retrieve or send the torrent.")

    return redirect(
        url_for(
            "catalog.catalog_item_details",
            movie_id=movie_id,
            q=request.args.get("q", ""),
            category=request.args.get("category", ""),
            page=request.args.get("page", "1"),
            downloaded="1",
        )
    )
