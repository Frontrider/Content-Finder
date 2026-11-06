"""Find torrents for Internet Archive items labeled public domain."""

import math
from typing import TypedDict

import internetarchive


class TorrentSearchPage(TypedDict):
    results: list[dict[str, str]]
    next_page: int | None


def find_public_domain_torrents(
    search_text: str = "",
    *,
    subject: str | None = None,
    collection: str | None = None,
    limit: int = 20,
    page: int = 1,
    timeout: float = 30,
) -> TorrentSearchPage:
    """Use the official client to find Public Domain Mark or CC0 torrents.

    ``search_text`` is a literal phrase; an empty string browses all matches.
    ``subject`` filters by a topic phrase; ``collection`` filters by a
    collection identifier (not its display title). All supplied filters are
    combined with AND and treated as literal values, not raw query syntax.
    ``limit`` (1-100) and ``page`` select a page of candidate items, so fewer
    results may be returned when an item's torrent is unavailable or private.
    Returns ``results`` and ``next_page`` (a page number, or None at the end).
    The next page is determined from the API's total candidate count, even
    when all results on the current page are filtered out.
    Each result contains identifier, title, license_url, item_url, and
    torrent_url. No files are downloaded beyond search and item metadata.

    Labels are uploader-provided claims, not legal verification of public
    domain status in your jurisdiction. Install with ``pip install -r
    requirements.txt``. HTTP, network, JSON, and API errors
    propagate to the caller rather than being treated as an empty result.

    Example:
        result_page = find_public_domain_torrents(
            "silent film", subject="comedy", collection="feature_films", limit=5
        )
    """
    if not isinstance(search_text, str):
        raise TypeError("search_text must be a string")
    for name, value in (("subject", subject), ("collection", collection)):
        if value is not None:
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if type(page) is not int or page < 1:
        raise ValueError("page must be a positive integer")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    licenses = [
        f"{scheme}://creativecommons.org/publicdomain/{kind}/1.0/"
        for scheme in ("http", "https")
        for kind in ("mark", "zero")
    ]
    license_filter = " OR ".join(f'licenseurl:"{url}"' for url in licenses)
    query = (
        f'({license_filter}) AND format:"Archive BitTorrent" '
        "AND NOT access-restricted-item:true"
    )
    for field, value in (
        ("", search_text),
        ("subject:", subject),
        ("collection:", collection),
    ):
        if value is not None and value.strip():
            phrase = value.strip().replace("\\", "\\\\").replace('"', '\\"')
            query = f'({query}) AND {field}"{phrase}"'
    results = []
    with internetarchive.get_session() as session:
        search = session.search_items(
            query,
            fields=["identifier", "title"],
            params={"rows": limit, "page": page},
            request_kwargs={"timeout": timeout},
        )
        for result in search:
            if result.get("error"):
                raise RuntimeError(f"Internet Archive search failed: {result['error']}")
            identifier = result["identifier"]
            item = session.get_item(identifier, request_kwargs={"timeout": timeout})
            if item.item_metadata.get("error"):
                raise RuntimeError(
                    f"Internet Archive metadata failed: {item.item_metadata['error']}"
                )
            if not item.exists:
                raise RuntimeError(f"Internet Archive item no longer exists: {identifier}")

            item_licenses = item.metadata.get("licenseurl", [])
            if isinstance(item_licenses, str):
                item_licenses = [item_licenses]
            license_url = next((url for url in item_licenses if url in licenses), None)
            if license_url is None:
                continue
            for file in item.get_files(glob_pattern="*.torrent"):
                if file.metadata.get("private") in (True, "true", "1", 1):
                    continue
                results.append(
                    {
                        "identifier": identifier,
                        "title": result.get("title", identifier),
                        "license_url": license_url,
                        "item_url": item.urls.details,
                        "torrent_url": file.url,
                    }
                )
                break
        next_page = page + 1 if page * limit < search.num_found else None
    return {"results": results, "next_page": next_page}
