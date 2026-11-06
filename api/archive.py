import json
import math
import os
import re
import tempfile
from pathlib import Path
from threading import Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen

import pycountry
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from sqlalchemy import Column, Integer, Table, Text, UniqueConstraint, insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.utils import secure_filename

from api import (
    RESULTS_PER_PAGE,
    SEARCH_TIMEOUT_SECONDS,
    TORRENT_MAX_BYTES,
    download_directory,
    get_engine,
    media_download_directory,
    sanitize_description,
)
from database import metadata
from rqbit_client import RqbitError, get_rqbit_client, link_torrent, torrent_status

bp = Blueprint("archive", __name__)

saved_filters_table = Table(
    "archive_saved_filters", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("value", Text, nullable=False),
    UniqueConstraint("kind", "value"),
)
topics_lock = Lock()
file_jobs_lock = Lock()
file_jobs = {}
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
SEARCH_URL = "https://archive.org/advancedsearch.php"
CONTENT_TYPES = {"audio", "collection", "data", "image", "movies", "software", "texts", "web"}
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def read_archive_topics(kind="topics", connection=None):
    query = select(saved_filters_table.c.value).where(saved_filters_table.c.kind == kind)
    if connection is None:
        with get_engine().connect() as connection:
            values = connection.execute(query).scalars().all()
    else:
        values = connection.execute(query).scalars().all()
    return sorted(values, key=str.casefold)


def subject_filter(topic):
    return archive_field_filter("subject", topic)


def archive_field_filter(field, value):
    phrase = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}:"{phrase}"'


@bp.route("/api/topics", methods=["GET", "POST"])
@bp.route("/api/collections", methods=["GET", "POST"], defaults={"kind": "collections"})
def archive_topics(kind="topics"):
    noun = "collection" if kind == "collections" else "topic"
    try:
        if request.method == "GET":
            return jsonify({kind: read_archive_topics(kind)})

        data = request.get_json()
        topic = data.get(noun) if isinstance(data, dict) else None
        if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 100:
            return jsonify(error=f"Enter a {noun} between 1 and 100 characters."), 400
        topic = topic.strip()
        parameters = urlencode({
            "q": archive_field_filter("collection" if kind == "collections" else "subject", topic),
            "rows": 0, "output": "json",
        })
        with urlopen(f"{SEARCH_URL}?{parameters}", timeout=SEARCH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
        total = payload["response"]["numFound"]
        if type(total) is not int or total < 0:
            raise ValueError("Invalid topic search response")
        if total == 0:
            return jsonify(error=f"No Internet Archive items match this {noun}."), 404

        with topics_lock:
            try:
                with get_engine().begin() as connection:
                    if topic.casefold() not in {
                        value.casefold() for value in read_archive_topics(kind, connection)
                    }:
                        connection.execute(insert(saved_filters_table), {"kind": kind, "value": topic})
            except IntegrityError:
                pass
            topics = read_archive_topics(kind)
        return jsonify({kind: topics, "total_results": total})
    except (HTTPError, URLError, TimeoutError) as error:
        current_app.logger.warning("Internet Archive %s check failed: %s", noun, error)
        return jsonify(error=f"Internet Archive {noun} lookup is unavailable."), 502
    except (SQLAlchemyError, OSError, ValueError, KeyError, TypeError) as error:
        current_app.logger.exception("Could not check or save Archive %s", kind)
        return jsonify(error=f"Unable to load, verify, or save {kind}."), 503


def valid_identifier(identifier):
    return bool(IDENTIFIER_PATTERN.fullmatch(identifier))


def get_item_metadata(identifier):
    url = f"https://archive.org/metadata/{quote(identifier, safe='')}"
    with urlopen(url, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise ValueError("Invalid item metadata")
    return payload


def get_item_description(metadata):
    description = metadata.get("description", "")
    if isinstance(description, list):
        description = "\n\n".join(value for value in description if isinstance(value, str))
    return description if isinstance(description, str) else ""


def metadata_values(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        entry.strip() for entry in value if isinstance(entry, str) and entry.strip()
    ))


def language_names(value):
    names = []
    for language in metadata_values(value):
        try:
            name = pycountry.languages.lookup(language).name
        except LookupError:
            name = language
        if name not in names:
            names.append(name)
    return names


def find_torrent_file(payload):
    files = payload.get("files", [])
    if not isinstance(files, list):
        return None
    for file in files:
        if not isinstance(file, dict):
            continue
        name = file.get("name")
        if (
            isinstance(name, str)
            and name.endswith(".torrent")
            and "/" not in name
            and "\\" not in name
            and file.get("private") not in (True, "true", "1", 1)
        ):
            return name
    return None


def file_destination(identifier, name, mediatype=None):
    return download_directory(
        "internetarchive", identifier, mediatype=mediatype,
    ) / (secure_filename(name) or "file")


def download_files(identifier, payload):
    files = payload.get("files", [])
    if not isinstance(files, list):
        return []
    downloads = []
    for file in files:
        if not isinstance(file, dict) or file.get("private") in (True, "true", "1", 1):
            continue
        name = file.get("name")
        if not isinstance(name, str) or not name:
            continue
        size = file.get("size")
        downloads.append({
            "name": name,
            "format": file.get("format") if isinstance(file.get("format"), str) else "",
            "size": int(size) if isinstance(size, str) and size.isdigit() else None,
            "is_torrent": name.endswith(".torrent"),
            "url": f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name)}",
            "saved": not name.endswith(".torrent") and file_destination(
                identifier, name, payload["metadata"].get("mediatype"),
            ).is_file(),
        })
    downloads.sort(key=lambda download: not download["is_torrent"])
    return downloads


def run_file_download(app, identifier, download, destination):
    key = (identifier, download["name"])
    temporary_path = None
    try:
        with urlopen(download["url"], timeout=SEARCH_TIMEOUT_SECONDS) as response:
            host = urlsplit(response.geturl()).hostname or ""
            if host != "archive.org" and not host.endswith(".archive.org"):
                raise ValueError("The download was redirected to an untrusted host.")
            total = int(response.headers.get("Content-Length") or 0) or download["size"] or 0
            with file_jobs_lock:
                file_jobs[key]["total"] = total
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".download-", delete=False,
            ) as file:
                temporary_path = Path(file.name)
                while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                    file.write(chunk)
                    with file_jobs_lock:
                        file_jobs[key]["done"] += len(chunk)
        os.replace(temporary_path, destination)
        temporary_path = None
        with file_jobs_lock:
            file_jobs[key]["state"] = "done"
    except Exception as error:
        app.logger.warning("Internet Archive file download failed: %s", error)
        with file_jobs_lock:
            file_jobs[key].update(state="error", error=str(error) or type(error).__name__)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@bp.route("/item/<identifier>/files")
def file_status(identifier):
    if not valid_identifier(identifier):
        abort(404)
    with file_jobs_lock:
        jobs = {name: dict(job) for (item, name), job in file_jobs.items() if item == identifier}
    return jsonify(files=jobs)


@bp.route("/item/<identifier>/files", methods=["POST"])
def save_file(identifier):
    if not valid_identifier(identifier):
        abort(404)
    data = request.get_json(silent=True)
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        return jsonify(error="Select a file to save."), 400
    try:
        payload = get_item_metadata(identifier)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        current_app.logger.warning("Internet Archive item lookup failed: %s", error)
        return jsonify(error="Internet Archive is temporarily unavailable."), 502
    download = next(
        (download for download in download_files(identifier, payload) if download["name"] == name),
        None,
    )
    if download is None:
        return jsonify(error="This file is not available for download."), 404
    if download["saved"]:
        return jsonify(error="This file is already saved on the server."), 409
    if download["is_torrent"]:
        torrent_key = f"file:{name}"
        try:
            status = torrent_status("archive_file", identifier, torrent_key)
            if status["state"] not in {"not_added", "not_found"}:
                return jsonify(error="This torrent is already managed by rqbit."), 409
            with urlopen(download["url"], timeout=SEARCH_TIMEOUT_SECONDS) as response:
                host = urlsplit(response.geturl()).hostname or ""
                if host != "archive.org" and not host.endswith(".archive.org"):
                    return jsonify(error="Torrent download redirected to an untrusted host."), 502
                torrent_data = response.read(TORRENT_MAX_BYTES + 1)
            if len(torrent_data) > TORRENT_MAX_BYTES:
                return jsonify(error="The torrent file exceeds the download size limit."), 413
            torrent = get_rqbit_client().add_torrent(
                torrent_data,
                output_folder=media_download_directory(payload["metadata"].get("mediatype")),
            )
            link_torrent("archive_file", identifier, torrent_key, torrent)
        except RqbitError as error:
            current_app.logger.warning("Could not send Internet Archive torrent to rqbit: %s", error)
            return jsonify(error="Unable to send the torrent to rqbit."), 502
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            current_app.logger.warning("Could not retrieve Internet Archive torrent: %s", error)
            return jsonify(error="Unable to retrieve the torrent from Internet Archive."), 502
        return jsonify({"state": "done", "done": len(torrent_data), "total": len(torrent_data)}), 202
    key = (identifier, name)
    with file_jobs_lock:
        if file_jobs.get(key, {}).get("state") == "running":
            return jsonify(error="This file is already being saved."), 409
        file_jobs[key] = {"state": "running", "done": 0, "total": download["size"] or 0, "error": ""}
        status = dict(file_jobs[key])
    Thread(
        target=run_file_download,
        args=(
            current_app._get_current_object(), identifier, download,
            file_destination(identifier, name, payload["metadata"].get("mediatype")),
        ),
        daemon=True,
    ).start()
    return jsonify(status), 202


@bp.route("/item/<identifier>")
def item_details(identifier):
    if not valid_identifier(identifier):
        abort(404)
    return_query = request.args.get("q", "").strip()
    return_type = request.args.get("type", "all")
    try:
        return_page = int(request.args.get("page", "1"))
    except ValueError:
        return_page = 1
    if len(return_query) > 300:
        return_query = ""
    if return_type != "all" and return_type not in CONTENT_TYPES:
        return_type = "all"
    if return_page < 1:
        return_page = 1

    try:
        payload = get_item_metadata(identifier)
    except HTTPError as error:
        if error.code == 404:
            abort(404)
        current_app.logger.warning("Internet Archive item lookup failed: %s", error)
        abort(502, description="Internet Archive is temporarily unavailable.")
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        current_app.logger.warning("Internet Archive item lookup failed: %s", error)
        abort(502, description="Internet Archive is temporarily unavailable.")

    metadata = payload["metadata"]
    title = metadata.get("title")
    if not isinstance(title, str) or not title:
        title = identifier
    description = get_item_description(metadata)
    return render_template(
        "item.html",
        identifier=identifier,
        title=title,
        description=description,
        description_html=sanitize_description(description),
        mediatype=metadata.get("mediatype", ""),
        year=metadata.get("year", ""),
        image_url=f"https://archive.org/services/img/{identifier}",
        categories=metadata_values(metadata.get("collection")),
        topics=metadata_values(metadata.get("subject")),
        languages=language_names(metadata.get("language")),
        torrents=[{
            "label": "Send torrent to rqbit",
            "key": "archive",
            "status_url": url_for(
                "rqbit_torrent_status",
                source="archive",
                identifier=identifier,
                key="archive",
            ),
            "action": url_for(
                "archive.download_torrent", identifier=identifier, q=return_query,
                type=return_type, page=return_page, topic=request.args.getlist("topic"),
                exclude_topic=request.args.getlist("exclude_topic"),
                collection=request.args.getlist("collection"),
                exclude_collection=request.args.getlist("exclude_collection"),
                language=request.args.get("language", ""),
            ),
        }] if find_torrent_file(payload) is not None else [],
        downloads=download_files(identifier, payload),
        downloaded=request.args.get("downloaded") == "1",
        search_url=url_for(
            "home",
            q=return_query,
            type=return_type,
            page=return_page,
            topic=request.args.getlist("topic"),
            exclude_topic=request.args.getlist("exclude_topic"),
            collection=request.args.getlist("collection"),
            exclude_collection=request.args.getlist("exclude_collection"),
            language=request.args.get("language", ""),
        ),
    )


@bp.route("/item/<identifier>/download-torrent", methods=["POST"])
def download_torrent(identifier):
    if not valid_identifier(identifier):
        abort(404)
    try:
        status = torrent_status("archive", identifier, "archive")
        if status["state"] not in {"not_added", "not_found"}:
            abort(409, description="This torrent is already managed by rqbit.")
    except RqbitError as error:
        current_app.logger.warning("Could not check Internet Archive torrent in rqbit: %s", error)
        abort(502, description="Unable to check rqbit before sending the torrent.")

    try:
        payload = get_item_metadata(identifier)
    except HTTPError as error:
        if error.code == 404:
            abort(404)
        current_app.logger.warning("Internet Archive torrent lookup failed: %s", error)
        abort(502, description="Unable to retrieve the torrent from Internet Archive.")
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        current_app.logger.warning("Internet Archive torrent lookup failed: %s", error)
        abort(502, description="Unable to retrieve the torrent from Internet Archive.")

    torrent_name = find_torrent_file(payload)
    if torrent_name is None:
        abort(404, description="This item does not have a public torrent file.")

    torrent_url = (
        f"https://archive.org/download/{quote(identifier, safe='')}/"
        f"{quote(torrent_name, safe='')}"
    )
    try:
        with urlopen(torrent_url, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            torrent_data = response.read(TORRENT_MAX_BYTES + 1)
        if len(torrent_data) > TORRENT_MAX_BYTES:
            abort(413, description="The torrent file exceeds the download size limit.")

        torrent = get_rqbit_client().add_torrent(
            torrent_data,
            output_folder=media_download_directory(payload["metadata"].get("mediatype")),
        )
        link_torrent("archive", identifier, "archive", torrent)
    except HTTPError as error:
        current_app.logger.warning("Internet Archive torrent download failed: %s", error)
        abort(502, description="Unable to retrieve the torrent from Internet Archive.")
    except RqbitError as error:
        current_app.logger.warning("Could not send Internet Archive torrent to rqbit: %s", error)
        abort(502, description="Unable to send the torrent to rqbit.")
    except (URLError, TimeoutError, OSError) as error:
        current_app.logger.exception("Could not retrieve or send Internet Archive torrent")
        abort(502, description="Unable to retrieve or send the torrent.")

    return_query = request.args.get("q", "").strip()
    return_type = request.args.get("type", "all")
    try:
        return_page = int(request.args.get("page", "1"))
    except ValueError:
        return_page = 1
    if len(return_query) > 300:
        return_query = ""
    if return_type != "all" and return_type not in CONTENT_TYPES:
        return_type = "all"
    if return_page < 1:
        return_page = 1
    return redirect(
        url_for(
            "archive.item_details",
            identifier=identifier,
            q=return_query,
            type=return_type,
            page=return_page,
            topic=request.args.getlist("topic"),
            exclude_topic=request.args.getlist("exclude_topic"),
            collection=request.args.getlist("collection"),
            exclude_collection=request.args.getlist("exclude_collection"),
            language=request.args.get("language", ""),
            downloaded="1",
        )
    )


def search_archive(query, language, page):
    topics = request.args.getlist("topic")
    excluded_topics = request.args.getlist("exclude_topic")
    all_topics = topics + excluded_topics
    if len(all_topics) > 50 or any(not 1 <= len(topic.strip()) <= 100 for topic in all_topics):
        return jsonify(error="Select at most 50 topics, each between 1 and 100 characters."), 400
    collections = request.args.getlist("collection")
    excluded_collections = request.args.getlist("exclude_collection")
    all_collections = collections + excluded_collections
    if len(all_collections) > 50 or any(
        not 1 <= len(value.strip()) <= 100 for value in all_collections
    ):
        return jsonify(error="Select at most 50 collections, each between 1 and 100 characters."), 400
    if not query and not all_topics and not all_collections and not language:
        return jsonify(error="Enter a search term."), 400

    content_type = request.args.get("type", "all")
    if content_type != "all" and content_type not in CONTENT_TYPES:
        return jsonify(error="Select a valid content type."), 400

    search_query = query or "*:*"
    for topic in topics:
        search_query = f"({search_query}) AND {subject_filter(topic.strip())}"
    for topic in excluded_topics:
        search_query = f"({search_query}) AND NOT {subject_filter(topic.strip())}"
    for collection in collections:
        search_query = f"({search_query}) AND {archive_field_filter('collection', collection.strip())}"
    for collection in excluded_collections:
        search_query = f"({search_query}) AND NOT {archive_field_filter('collection', collection.strip())}"
    if content_type != "all":
        search_query = f"({search_query}) AND mediatype:{content_type}"
    if language:
        entry = pycountry.languages.get(alpha_3=language)
        codes = [language]
        for attribute in ("alpha_2", "bibliographic", "name"):
            value = getattr(entry, attribute, None)
            if value and value not in codes:
                codes.append(value)
        language_filter = " OR ".join(
            f'language:"{code}"' for code in codes
        )
        search_query = f"({search_query}) AND ({language_filter})"

    parameters = [
        ("q", search_query),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "description"),
        ("fl[]", "mediatype"),
        ("fl[]", "year"),
        ("fl[]", "subject"),
        ("rows", str(RESULTS_PER_PAGE)),
        ("page", str(page)),
        ("output", "json"),
    ]
    url = f"{SEARCH_URL}?{urlencode(parameters)}"

    try:
        with urlopen(url, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        current_app.logger.warning("Internet Archive search failed: %s", error)
        return jsonify(error="Internet Archive search is temporarily unavailable."), 502

    try:
        search_response = payload["response"]
        documents = search_response["docs"]
        total_results = search_response["numFound"]
        if not isinstance(documents, list) or type(total_results) is not int:
            raise ValueError("Unexpected search response shape")
    except (KeyError, TypeError, ValueError) as error:
        current_app.logger.error("Invalid response from Internet Archive search: %s", error)
        return jsonify(error="Internet Archive returned an invalid search response."), 502

    results = []
    for document in documents:
        identifier = document.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            continue

        description = document.get("description", "")
        if isinstance(description, list):
            description = description[0] if description else ""
        if not isinstance(description, str):
            description = ""

        subjects = document.get("subject", [])
        if isinstance(subjects, str):
            subjects = [subjects]
        if not isinstance(subjects, list):
            subjects = []
        topics = [
            subject for subject in subjects
            if isinstance(subject, str) and subject.strip()
        ]

        results.append(
            {
                "identifier": identifier,
                "title": document.get("title") or identifier,
                "description": sanitize_description(description),
                "mediatype": document.get("mediatype", ""),
                "year": document.get("year", ""),
                "topics": topics,
            }
        )

    return jsonify(
        results=results,
        page=page,
        page_size=RESULTS_PER_PAGE,
        total_pages=math.ceil(total_results / RESULTS_PER_PAGE),
        total_results=total_results,
    )
