import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from api import LANGUAGE_CODES, LANGUAGES, archive, gutenberg, public_domain_torrents
from database import database_url
from rqbit_client import RqbitError, torrent_status

app = Flask(__name__)
app.config["DATABASE_URL"] = database_url()
app.config["RQBIT_URL"] = os.environ.get("RQBIT_URL", "http://rqbit:3030").rstrip("/")
app.config["BOOKS_DOWNLOAD_DIR"] = os.environ.get("BOOKS_DOWNLOAD_DIR", "/data/books")
app.config["VIDEOS_DOWNLOAD_DIR"] = os.environ.get("VIDEOS_DOWNLOAD_DIR", "/data/videos")
app.config["RQBIT_HTTP_BASIC_AUTH_USERPASS"] = os.environ.get(
    "RQBIT_HTTP_BASIC_AUTH_USERPASS",
)
app.config["DOWNLOAD_DIR"] = os.environ.get(
    "DOWNLOAD_DIR",
    str(Path(app.root_path) / "downloads"),
)
app.register_blueprint(archive.bp)
app.register_blueprint(public_domain_torrents.bp)
app.register_blueprint(gutenberg.bp)


@app.route("/api/rqbit/status")
def rqbit_torrent_status():
    source = request.args.get("source", "")
    identifier = request.args.get("identifier", "")
    torrent_key = request.args.get("key", "")
    if source == "archive":
        if not archive.valid_identifier(identifier) or torrent_key != "archive":
            return jsonify(error="Select a valid torrent."), 400
    elif source == "catalog":
        if not identifier.isdigit() or not torrent_key.isdigit():
            return jsonify(error="Select a valid torrent."), 400
    elif source == "archive_file":
        if (
            not archive.valid_identifier(identifier)
            or not torrent_key.startswith("file:")
            or len(torrent_key) > 500
        ):
            return jsonify(error="Select a valid torrent."), 400
    else:
        return jsonify(error="Select a valid torrent."), 400

    try:
        return jsonify(torrent_status(source, identifier, torrent_key))
    except RqbitError as error:
        app.logger.warning("Could not read torrent status from rqbit: %s", error)
        return jsonify(error="Unable to check torrent status in rqbit."), 502


@app.route("/")
def home():
    return render_template("page.html", languages=LANGUAGES)


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if len(query) > 300:
        return jsonify(error="Search terms must be 300 characters or fewer."), 400

    source = request.args.get("source", "archive")
    if source not in {"archive", "catalog", "gutenberg"}:
        return jsonify(error="Select a valid search source."), 400

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return jsonify(error="Page must be a positive number."), 400
    if page < 1:
        return jsonify(error="Page must be a positive number."), 400

    if source == "catalog":
        return public_domain_torrents.search_catalog(query, request.args.get("category", ""), page)
    language = request.args.get("language", "")
    if language and language not in LANGUAGE_CODES:
        return jsonify(error="Select a valid language."), 400
    if source == "gutenberg":
        return gutenberg.search_gutenberg(
            query, language, page,
            request.args.getlist("tag"), request.args.getlist("exclude_tag"),
        )
    return archive.search_archive(query, language, page)


if __name__ == "__main__":
    with app.app_context():
        public_domain_torrents.ensure_catalog()
    app.run(debug=True)
