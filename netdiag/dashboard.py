"""Loopback dataset views and an explicitly confirmed history-clear action."""

import ipaddress
import json
import math
import hmac
import secrets
import threading
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .storage import ClearBusy, HistoryUpgradeRequired, OutcomeHistoryUnavailable, Reader, clear_database


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True


def create_server(db_path, host="127.0.0.1", port=8765, clear_callback=None):
    """Create a server; caller owns serve_forever/shutdown/server_close."""
    if host != "127.0.0.1":
        raise ValueError("Dashboard must bind to 127.0.0.1")
    reader = Reader(db_path)
    clear_lock = threading.Lock()
    clear_token = secrets.token_urlsafe(32)

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

        def local_request(self,require_origin=False):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return (ipaddress.ip_address(self.client_address[0]).is_loopback
                    and self.headers.get("Host") in allowed
                    and len(self.headers.get_all("Host", [])) == 1
                    and self.headers.get("Sec-Fetch-Site") != "cross-site"
                    and len(self.headers.get_all('Origin',[]))<=1
                    and ((not require_origin and self.headers.get('Origin') is None)
                         or self.headers.get('Origin')=='http://'+self.headers.get('Host','')))

        def do_POST(self):
            nonlocal clear_token
            if not self.local_request(require_origin=True):
                self.respond(403,{'error':'Local same-origin requests only'})
                return
            if self.path!='/api/clear':
                self.respond(404,{'error':'Not found'})
                return
            if (len(self.headers.get_all('X-CSRF-Token',[]))!=1
                    or not self.headers.get('X-CSRF-Token','').isascii()
                    or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),clear_token)):
                self.respond(403,{'error':'Clear confirmation expired; refresh the dashboard and confirm again.'})
                return
            try:
                if (len(self.headers.get_all('Content-Type',[]))!=1
                        or self.headers['Content-Type'].lower().split(';')[0].strip()!='application/json'
                        or len(self.headers.get_all('Content-Length',[]))!=1
                        or self.headers.get('Transfer-Encoding') is not None):
                    raise ValueError('Invalid body headers')
                if not self.headers['Content-Length'].isascii() or not self.headers['Content-Length'].isdigit():
                    raise ValueError('Invalid body size')
                length = int(self.headers['Content-Length'])
                if not 1<=length<=512:
                    raise ValueError('Invalid body size')
                body = self.rfile.read(length)
                if len(body)!=length or json.loads(body,object_pairs_hook=lambda pairs:pairs)!=[('confirm','CLEAR')]:
                    raise ValueError('Explicit confirmation required')
            except (ValueError,UnicodeError,OSError):
                self.close_connection = True
                self.respond(400,{'error':'Send an explicit CLEAR confirmation as a small JSON request.'})
                return
            if not clear_lock.acquire(blocking=False):
                self.respond(409,{'error':'A history clear is already in progress.'})
                return
            try:
                # Recheck after acquiring the lock: another successful clear may rotate it.
                if not hmac.compare_digest(self.headers['X-CSRF-Token'],clear_token):
                    self.respond(403,{'error':'Clear confirmation expired; refresh and confirm again.'})
                    return
                if clear_callback is None:
                    clear_database(db_path)
                else:
                    clear_callback()
                clear_token = secrets.token_urlsafe(32)
            except ClearBusy:
                self.respond(409,{'error':'Collection is stopping or another collector owns this dataset. Use its dashboard or stop it first.'})
            except Exception:
                self.respond(503,{'error':'History could not be cleared. Refresh to inspect the dataset before trying again.'})
            else:
                self.respond(200,{'cleared':True,'collection':'running' if clear_callback is not None else 'stopped'})
            finally:
                clear_lock.release()

        def do_GET(self):
            if not self.local_request():
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
            if url.path not in {"/api/metadata", "/api/series", "/api/samples", "/api/outages", "/api/outcomes"}:
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
                elif url.path == "/api/series":
                    keys |= {"buckets"}
                if set(query) - keys or any(len(values) != 1 for values in query.values()):
                    raise ValueError("Unknown or repeated parameter")
                if url.path == "/api/metadata":
                    result = reader.metadata()
                    result['clear'] = {'token':clear_token,'dataset':str(reader.path),'mode':'continue' if clear_callback is not None else 'stopped','busy':clear_lock.locked()}
                else:
                    start, end = float(query["start"][0]), float(query["end"][0])
                    if not all(math.isfinite(t) and 0 <= t <= 32503680000 for t in (start, end)) or start >= end:
                        raise ValueError("Invalid time range")
                    targets = None
                    if "targets" in query:
                        targets = [int(value) for value in query["targets"][0].split(",")]
                        if not 1 <= len(targets) <= 100 or min(targets) < 1:
                            raise ValueError("Invalid targets")
                    if url.path == "/api/outcomes":
                        result = reader.outcomes(start,end,target_ids=targets)
                    elif url.path == "/api/series":
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
            except HistoryUpgradeRequired as error:
                self.respond(503,{'error':str(error)})
            except OutcomeHistoryUnavailable:
                self.respond(503,{"error":"Outcome history exceeds the exact read limit; start collection once to build summaries, or choose a shorter interval/fewer targets."})
            except (ValueError, KeyError, OverflowError):
                self.respond(400, {"error": "Invalid query parameters"})
            except (sqlite3.Error, OSError):
                self.respond(503, {"error": "Dataset unavailable. Check the collector and retry."})

    return DashboardServer((host, port), Handler)
