from pathlib import Path
from threading import Lock

import bleach
import pycountry
from flask import current_app
from werkzeug.utils import secure_filename

from database import create_database_engine

RESULTS_PER_PAGE = 20
SEARCH_TIMEOUT_SECONDS = 15
TORRENT_MAX_BYTES = 50 * 1024 * 1024
LANGUAGES = sorted(
    [(language.alpha_3, language.name) for language in pycountry.languages],
    key=lambda language: language[1].casefold(),
)
LANGUAGE_CODES = {code for code, _ in LANGUAGES}
engines = {}
engines_lock = Lock()


def get_engine():
    url = current_app.config["DATABASE_URL"]
    with engines_lock:
        if url not in engines:
            engines[url] = create_database_engine(url)
        return engines[url]


def sanitize_description(description):
    return bleach.clean(
        description,
        tags={"p", "br", "div", "span", "strong", "b", "em", "i", "u",
              "ul", "ol", "li", "blockquote", "a", "h2", "h3", "h4",
              "pre", "code", "hr", "sub", "sup"},
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )


def media_download_directory(mediatype):
    setting = {
        "texts": "BOOKS_DOWNLOAD_DIR",
        "movies": "VIDEOS_DOWNLOAD_DIR",
    }.get(mediatype, "DOWNLOAD_DIR")
    download_dir = Path(current_app.config[setting]).expanduser()
    if not download_dir.is_absolute():
        download_dir = Path(current_app.root_path) / download_dir
    return download_dir


def download_directory(source, identifier, variant=None, *, mediatype=None):
    download_dir = media_download_directory(mediatype)
    variant_suffix = f"-{variant}" if variant is not None else ""
    return download_dir / f"{source}-{identifier}{variant_suffix}"


def torrent_destination(source, identifier, title, variant=None):
    readable_name = secure_filename(title if isinstance(title, str) else identifier)[:80]
    readable_name = readable_name or "untitled"
    return download_directory(source, identifier, variant) / f"{readable_name}.torrent"
