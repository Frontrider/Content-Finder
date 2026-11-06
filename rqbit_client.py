import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import current_app
from sqlalchemy import Column, Integer, String, Table, insert, select, update

from database import metadata

rqbit_torrents_table = Table(
    "rqbit_torrents",
    metadata,
    Column("source", String, primary_key=True),
    Column("identifier", String, primary_key=True),
    Column("torrent_key", String, primary_key=True),
    Column("info_hash", String, nullable=False),
    Column("torrent_id", Integer),
)


class RqbitError(Exception):
    pass


class RqbitClient:
    def __init__(self, base_url, userpass=None, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.userpass = userpass
        self.timeout = timeout

    def _request(self, path, *, data=None, content_type=None):
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.userpass:
            token = base64.b64encode(self.userpass.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = Request(self.base_url + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            detail = getattr(error, "reason", None) or str(error)
            raise RqbitError(f"rqbit request failed: {detail}") from error
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RqbitError("rqbit returned an invalid JSON response.") from error

    def add_torrent(self, torrent_data, *, output_folder=None):
        path = "/torrents"
        if output_folder is not None:
            path += "?" + urlencode({"output_folder": str(output_folder)})
        result = self._request(
            path,
            data=torrent_data,
            content_type="application/octet-stream",
        )
        try:
            info_hash = result["details"]["info_hash"]
        except (TypeError, KeyError) as error:
            raise RqbitError("rqbit returned no torrent info hash.") from error
        if not isinstance(info_hash, str) or not info_hash:
            raise RqbitError("rqbit returned an invalid torrent info hash.")
        torrent_id = result.get("id")
        if torrent_id is not None and type(torrent_id) is not int:
            raise RqbitError("rqbit returned an invalid torrent ID.")
        return {"info_hash": info_hash, "torrent_id": torrent_id}

    def list_torrents(self):
        result = self._request("/torrents?" + urlencode({"with_stats": "true"}))
        torrents = result.get("torrents") if isinstance(result, dict) else None
        if not isinstance(torrents, list):
            raise RqbitError("rqbit returned an invalid torrent list.")
        return torrents


def get_rqbit_client():
    return RqbitClient(
        current_app.config["RQBIT_URL"],
        current_app.config.get("RQBIT_HTTP_BASIC_AUTH_USERPASS"),
    )


def link_torrent(source, identifier, torrent_key, torrent):
    values = {
        "source": source,
        "identifier": str(identifier),
        "torrent_key": str(torrent_key),
        "info_hash": torrent["info_hash"],
        "torrent_id": torrent["torrent_id"],
    }
    table = rqbit_torrents_table
    from api import get_engine

    with get_engine().begin() as connection:
        existing = connection.execute(
            select(table.c.source).where(
                table.c.source == values["source"],
                table.c.identifier == values["identifier"],
                table.c.torrent_key == values["torrent_key"],
            )
        ).first()
        if existing:
            connection.execute(
                update(table)
                .where(
                    table.c.source == values["source"],
                    table.c.identifier == values["identifier"],
                    table.c.torrent_key == values["torrent_key"],
                )
                .values(info_hash=values["info_hash"], torrent_id=values["torrent_id"])
            )
        else:
            connection.execute(insert(table).values(**values))


def torrent_status(source, identifier, torrent_key):
    from api import get_engine

    with get_engine().connect() as connection:
        info_hash = connection.execute(
            select(rqbit_torrents_table.c.info_hash).where(
                rqbit_torrents_table.c.source == source,
                rqbit_torrents_table.c.identifier == str(identifier),
                rqbit_torrents_table.c.torrent_key == str(torrent_key),
            )
        ).scalar_one_or_none()

    if info_hash is None:
        return {"state": "not_added"}

    torrent = next(
        (
            item for item in get_rqbit_client().list_torrents()
            if item.get("info_hash") == info_hash
        ),
        None,
    )
    if torrent is None:
        return {"state": "not_found", "info_hash": info_hash}
    stats = torrent.get("stats")
    if not isinstance(stats, dict):
        return {"state": "unknown", "info_hash": info_hash}
    return {
        "state": stats.get("state", "unknown"),
        "finished": stats.get("finished", False),
        "progress_bytes": stats.get("progress_bytes", 0),
        "total_bytes": stats.get("total_bytes", 0),
        "error": stats.get("error"),
        "info_hash": info_hash,
    }
