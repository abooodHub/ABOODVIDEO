"""Protect the local Studio server from browser requests issued by other sites."""
import os
from urllib.parse import urlsplit


def local_request_error(headers, *, scheme: str, mutating: bool) -> str | None:
    host = headers.get("host", "")
    try:
        target = urlsplit(f"{scheme}://{host}")
        allowed = {"127.0.0.1", "localhost", "::1"}
        if os.environ.get("ENVIRONMENT", "").casefold() == "test":
            allowed.add("testserver")
        if target.hostname not in allowed or target.username or target.password or target.path or target.query or target.fragment:
            return "invalid_host"
        target_port = target.port or (443 if scheme in {"https", "wss"} else 80)
        origin = headers.get("origin")
        if origin is not None:
            source = urlsplit(origin)
            source_port = source.port or (443 if source.scheme == "https" else 80)
            expected_scheme = "https" if scheme in {"https", "wss"} else "http"
            if (source.scheme != expected_scheme or source.hostname != target.hostname or
                    source_port != target_port or source.username or source.password or
                    source.path not in {"", "/"} or source.query or source.fragment):
                return "invalid_origin"
        if mutating and headers.get("sec-fetch-site", "").casefold() == "cross-site":
            return "invalid_origin"
    except ValueError:
        return "invalid_host"
    return None
