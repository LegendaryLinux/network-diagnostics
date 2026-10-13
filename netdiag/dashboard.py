"""Read-only, loopback-only HTTP access to the diagnostics dataset."""

import ipaddress
import json
import math
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .storage import Reader


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True


def create_server(db_path, host="127.0.0.1", port=8765):
    """Create a server; caller owns serve_forever/shutdown/server_close."""
    if host != "127.0.0.1":
        raise ValueError("Dashboard must bind to 127.0.0.1")
    reader = Reader(db_path)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if (not ipaddress.ip_address(self.client_address[0]).is_loopback
                    or self.headers.get("Host") not in allowed
                    or len(self.headers.get_all("Host", [])) != 1
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or (self.headers.get("Origin") is not None
                        and self.headers["Origin"] != "http://" + self.headers["Host"])):
                self.respond(403, {"error": "Local same-origin requests only"})
                return
            url = urlsplit(self.path)
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if url.path in assets:
                filename, content_type = assets[url.path]
                self.respond(200, (Path(__file__).parent / "static" / filename).read_bytes(), content_type)
                return
            if url.path not in {"/api/metadata", "/api/series", "/api/samples", "/api/outages"}:
                self.respond(404, {"error": "Not found"})
                return
            try:
                query = parse_qs(url.query, keep_blank_values=True, max_num_fields=8)
                keys = {"start", "end", "targets"}
                if url.path == "/api/metadata":
                    keys = set()
                elif url.path in {"/api/samples", "/api/outages"}:
                    keys |= {"limit"}
                    if url.path == "/api/samples":
                        keys.add("before_id")
                else:
                    keys |= {"buckets"}
                if set(query) - keys or any(len(values) != 1 for values in query.values()):
                    raise ValueError("Unknown or repeated parameter")
                if url.path == "/api/metadata":
                    result = reader.metadata()
                else:
                    start, end = float(query["start"][0]), float(query["end"][0])
                    if not all(math.isfinite(t) and 0 <= t <= 32503680000 for t in (start, end)) or start >= end:
                        raise ValueError("Invalid time range")
                    targets = None
                    if "targets" in query:
                        targets = [int(value) for value in query["targets"][0].split(",")]
                        if not 1 <= len(targets) <= 100 or min(targets) < 1:
                            raise ValueError("Invalid targets")
                    if url.path == "/api/series":
                        buckets = int(query.get("buckets", ["300"])[0])
                        if not 10 <= buckets <= 600:
                            raise ValueError("buckets must be 10–600")
                        result = reader.series(start, end, target_ids=targets, buckets=buckets)
                    else:
                        limit = int(query.get("limit", ["100"])[0])
                        before_id = int(query["before_id"][0]) if "before_id" in query else None
                        if not 1 <= limit <= 200 or (before_id is not None and before_id < 1):
                            raise ValueError("Invalid pagination")
                        if url.path == "/api/outages":
                            result = reader.outages(start, end, target_ids=targets, limit=limit)
                        else:
                            result = reader.samples(start, end, target_ids=targets, limit=limit, before_id=before_id)
                self.respond(200, result)
            except (ValueError, KeyError, OverflowError):
                self.respond(400, {"error": "Invalid query parameters"})
            except (sqlite3.Error, OSError):
                self.respond(503, {"error": "Dataset unavailable. Check the collector and retry."})

    return DashboardServer((host, port), Handler)
