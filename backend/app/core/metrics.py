"""Small in-process, low-cardinality request metrics for operational scraping."""
from collections import defaultdict
from threading import Lock
from typing import Dict, Tuple


_lock = Lock()
_requests: Dict[Tuple[str, str, str], int] = defaultdict(int)
_duration_seconds: Dict[Tuple[str, str], float] = defaultdict(float)
_duration_count: Dict[Tuple[str, str], int] = defaultdict(int)


def observe_request(method: str, route: str, status_code: int, duration_seconds: float) -> None:
    """Record bounded labels; callers must pass a route template, never a raw URL."""
    method = method if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "OTHER"
    route = route if route.startswith("/") and len(route) <= 160 else "unmatched"
    status_class = f"{int(status_code) // 100}xx"
    key = (method, route, status_class)
    duration_key = (method, route)
    with _lock:
        _requests[key] += 1
        _duration_seconds[duration_key] += max(0.0, float(duration_seconds))
        _duration_count[duration_key] += 1


def render_prometheus() -> str:
    """Return a Prometheus text exposition with no user- or media-derived labels."""
    with _lock:
        requests = sorted(_requests.items())
        durations = sorted(_duration_seconds.items())
        counts = dict(_duration_count)
    lines = [
        "# HELP aboodvideo_http_requests_total Completed HTTP requests.",
        "# TYPE aboodvideo_http_requests_total counter",
    ]
    for (method, route, status_class), value in requests:
        lines.append(f'aboodvideo_http_requests_total{{method="{method}",route="{_escape(route)}",status_class="{status_class}"}} {value}')
    lines.extend([
        "# HELP aboodvideo_http_request_duration_seconds_sum Total observed request duration.",
        "# TYPE aboodvideo_http_request_duration_seconds_sum counter",
    ])
    for (method, route), value in durations:
        labels = f'method="{method}",route="{_escape(route)}"'
        lines.append(f"aboodvideo_http_request_duration_seconds_sum{{{labels}}} {value:.9f}")
    lines.extend([
        "# HELP aboodvideo_http_request_duration_seconds_count Number of observed request durations.",
        "# TYPE aboodvideo_http_request_duration_seconds_count counter",
    ])
    for (method, route), value in sorted(counts.items()):
        lines.append(f'aboodvideo_http_request_duration_seconds_count{{method="{method}",route="{_escape(route)}"}} {value}')
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
