"""Keep requests to the Paradime API clear of its firewall's false positives.

Paradime's API firewall blocks a request whose body links to a local or
private address, as protection against server-side request forgery: a message
with "http://localhost:8765/?thread=abc" gets HTTP 403 and an HTML page
("Bot or Not? Firewall's Robot Riddle"). The same address without its scheme,
"localhost:8765/?thread=abc", passes, and so do public links and SQL. Tested
on 2026-10-07 against the dev API.

Readers can type such links, and the app builds one for each conversation,
so the backend defangs every message before it sends it.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_URL = re.compile(r"\bhttps?://[^\s<>\"'`)\]]+", re.IGNORECASE)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal")
_HTML = re.compile(r"<(?:!doctype|html|head|body|div)\b", re.IGNORECASE)


def is_local_url(url: str) -> bool:
    """True for a link that only works on this computer or its network."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified


def defang_local_urls(text: str) -> str:
    """Drop the scheme of each local link, which is what the firewall matches."""
    return _URL.sub(lambda m: m.group(0).split("://", 1)[1] if is_local_url(m.group(0)) else m.group(0), text)


def is_firewall_page(error: str) -> bool:
    """True when an API error carries an HTML page (the firewall's) instead of a GraphQL error."""
    return bool(_HTML.search(error))
