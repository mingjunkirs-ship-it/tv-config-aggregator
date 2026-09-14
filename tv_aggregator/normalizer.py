"""Parsing and de-duplication for common TVBox style configurations."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SITE_KEYS = ("sites", "sources", "providers", "apis", "source")
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _strip_json_comments(text: str) -> str:
    """Remove JSON5 comments while leaving URL strings untouched."""

    output: list[str] = []
    index = 0
    in_string = False
    quote = ""
    escaped = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            index += 1
            continue
        if char in ('"', "'"):
            in_string = True
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            newline = text.find("\n", index + 2)
            if newline == -1:
                break
            output.append("\n")
            index = newline + 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            if end == -1:
                break
            index = end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def parse_config(text: str) -> Any:
    """Parse strict JSON and the small JSON5 subset used by public configs."""

    cleaned = text.lstrip("\ufeff").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first_error:
        cleaned = _strip_json_comments(cleaned)
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        # A number of older hand-written configs leave object keys unquoted.
        cleaned = re.sub(r"([{,]\s*)([A-Za-z_$][\w$-]*)\s*:", r'\1"\2":', cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            raise ValueError(f"配置不是有效的 JSON/JSON5: {first_error.msg}") from first_error


def canonical_api(value: Any) -> str:
    """Return a stable representation used to identify the same provider API."""

    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.rstrip("/").lower()
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/").lower()
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/"
    try:
        query_items = sorted(parse_qsl(parsed.query, keep_blank_values=True))
        query = urlencode(query_items)
    except ValueError:
        query = parsed.query
    return urlunsplit((scheme, netloc, path, query, ""))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    lowered = str(value).strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    return default


def _site_candidates(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # Some feeds use {"douban": {...}} instead of an array.
        if any(key in value for key in ("api", "url", "baseUrl", "key", "name", "title")):
            return [value]
        candidates: list[Any] = []
        for key, item in value.items():
            if isinstance(item, dict):
                copied = dict(item)
                copied.setdefault("key", key)
                candidates.append(copied)
            elif isinstance(item, str):
                candidates.append({"key": key, "name": key, "api": item})
        return candidates
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def extract_sites(payload: Any) -> list[dict[str, Any]]:
    """Extract site definitions from TVBox, 影视仓, and simple list formats."""

    if isinstance(payload, str):
        payload = parse_config(payload)
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = []
        for key in _SITE_KEYS:
            if key in payload:
                candidates.extend(_site_candidates(payload[key]))
        if not candidates:
            for key in ("data", "config", "result", "body"):
                nested = payload.get(key)
                if isinstance(nested, (dict, list)):
                    candidates.extend(extract_sites(nested))
        if not candidates and any(key in payload for key in ("api", "url", "baseUrl", "key")):
            candidates = [payload]
    else:
        candidates = []

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            candidate = {"name": candidate, "api": candidate}
        if not isinstance(candidate, dict):
            continue
        api = candidate.get("api") or candidate.get("url") or candidate.get("baseUrl") or candidate.get("base_url")
        key = candidate.get("key") or candidate.get("id") or candidate.get("code")
        name = candidate.get("name") or candidate.get("title") or candidate.get("label") or key or api
        if not api and not key:
            continue
        result.append(
            {
                "key": str(key).strip() if key is not None else "",
                "name": str(name).strip() if name is not None else "未命名源",
                "api": str(api).strip() if api is not None else "",
                "type": candidate.get("type", 1),
                "searchable": _as_bool(candidate.get("searchable", candidate.get("search")), True),
                "quickSearch": _as_bool(candidate.get("quickSearch", candidate.get("quick_search")), True),
                "filterable": _as_bool(candidate.get("filterable", candidate.get("filter")), False),
                "group": str(candidate.get("group") or candidate.get("classify") or candidate.get("category") or "影视").strip(),
                "ext": candidate.get("ext") if "ext" in candidate else candidate.get("jar"),
                "raw": candidate,
            }
        )
    return result


def normalize_site(site: dict[str, Any], source_id: str = "", source_name: str = "", source_url: str = "") -> dict[str, Any]:
    """Normalize one site and attach provenance for later merged output."""

    api = str(site.get("api") or "").strip()
    key = str(site.get("key") or "").strip()
    identity = canonical_api(api) or key.lower() or str(site.get("name") or "未命名源").strip().lower()
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return {
        "fingerprint": fingerprint,
        "key": key,
        "name": str(site.get("name") or key or api or "未命名源").strip(),
        "api": api,
        "canonical_api": canonical_api(api),
        "type": site.get("type", 1),
        "searchable": _as_bool(site.get("searchable"), True),
        "quickSearch": _as_bool(site.get("quickSearch"), True),
        "filterable": _as_bool(site.get("filterable"), False),
        "group": str(site.get("group") or "影视").strip() or "影视",
        "ext": site.get("ext"),
        "raw": site.get("raw", site),
        "source": {
            "id": source_id,
            "name": source_name,
            "url": source_url,
            "siteKey": key,
        },
    }


def merge_sites(sites: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge identical provider APIs while retaining all source provenance."""

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for site in sites:
        fingerprint = site.get("fingerprint")
        if not fingerprint:
            provenance = site.get("source") or {}
            site = normalize_site(
                site,
                str(provenance.get("id", "")),
                str(provenance.get("name", "")),
                str(provenance.get("url", "")),
            )
            fingerprint = site["fingerprint"]
        groups[str(fingerprint)].append(site)

    merged: list[dict[str, Any]] = []
    for fingerprint, entries in groups.items():
        names = Counter(entry.get("name") or entry.get("key") or "未命名源" for entry in entries)
        canonical = next((entry.get("canonical_api") for entry in entries if entry.get("canonical_api")), "")
        api = next((entry.get("api") for entry in entries if entry.get("api")), canonical)
        key = next((entry.get("key") for entry in entries if entry.get("key")), f"merged_{fingerprint[:10]}")
        groups_seen = sorted({entry.get("group") or "影视" for entry in entries})
        aliases = sorted({str(entry.get("name")) for entry in entries if entry.get("name")})
        ext_values: list[Any] = []
        for entry in entries:
            ext = entry.get("ext")
            if ext not in (None, "", {}):
                try:
                    if ext not in ext_values:
                        ext_values.append(ext)
                except TypeError:
                    ext_values.append(str(ext))
        item: dict[str, Any] = {
            "id": f"provider_{fingerprint[:16]}",
            "key": key,
            "name": names.most_common(1)[0][0],
            "api": api,
            "type": next((entry.get("type") for entry in entries if entry.get("type") is not None), 1),
            "searchable": any(bool(entry.get("searchable")) for entry in entries),
            "quickSearch": any(bool(entry.get("quickSearch")) for entry in entries),
            "filterable": any(bool(entry.get("filterable")) for entry in entries),
            "group": groups_seen[0] if len(groups_seen) == 1 else "多源",
            "groups": groups_seen,
            "aliases": aliases,
            "sourceCount": len({entry.get("source", {}).get("id") for entry in entries}),
            "sources": [entry.get("source", {}) for entry in entries],
        }
        if ext_values:
            item["ext"] = ext_values[0]
            if len(ext_values) > 1:
                item["extVariants"] = ext_values
        merged.append(item)
    return sorted(merged, key=lambda item: (str(item.get("group")), str(item.get("name")).lower(), str(item.get("id"))))
