"""Local dashboard for ApplyPilot results: pipeline status + shortlisted jobs.

Reads the shortlist folder that `applypilot cycle` / `export` produce (status.json,
shortlist.csv, jobs/<Company - Role>/{Resume.pdf, job.txt}) either from a local
folder or straight from the Cloud Storage bucket, and serves a single page at
http://127.0.0.1:8765. Your per-job colour labels are saved locally and, when
reading from a bucket, also to decisions.json in the bucket.

Standard library only, plus google-cloud-storage when using --gcs.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

STATIC = Path(__file__).with_name("static")
DECISIONS = ("apply", "retry", "no", "lowpay")  # green, yellow, red, pink
UI_HOME = Path(os.environ.get("APPLYPILOT_UI_DIR", Path.home() / ".applypilot-ui"))


# -- optional password (for hosting, e.g. Cloud Run) --------------------------------
COOKIE = "ap_session"
SESSION_DAYS = 30


def _password() -> str:
    return os.environ.get("APPLYPILOT_UI_PASSWORD", "")


def _sign(expires: int) -> str:
    key = hashlib.sha256(("applypilot-ui:" + _password()).encode("utf-8")).digest()
    mac = hmac.new(key, str(expires).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def _valid_session(cookie_header: str | None) -> bool:
    if not _password():
        return True
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and "." in value:
            exp = value.split(".", 1)[0]
            if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(value, _sign(int(exp))):
                return True
    return False


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ApplyPilot login</title>
<style>:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2330;--line:#e3e6ec;--accent:#2f6fed;--err:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--panel:#1a1f27;--text:#e6e9ef;--line:#2a313c;--accent:#6d9cff}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(340px,90vw)}
h1{font-size:18px;margin:0 0 14px}input,button{width:100%;padding:10px;border-radius:8px;font:inherit;box-sizing:border-box}
input{border:1px solid var(--line);background:var(--panel);color:var(--text);margin-bottom:10px}
button{border:0;background:var(--accent);color:#fff;cursor:pointer}.e{color:var(--err);margin:0 0 10px}</style></head>
<body><form method="post" action="/login"><h1>ApplyPilot</h1>__ERROR__
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Log in</button></form></body></html>"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_rel(rel: str) -> str:
    """Only allow plain relative paths inside the shortlist folder."""
    p = PurePosixPath(unquote(rel).lstrip("/"))
    if not p.parts or ".." in p.parts:
        raise ValueError("bad path")
    return p.as_posix()


# ---------------------------------------------------------------------------
# Where the shortlist lives
# ---------------------------------------------------------------------------

class LocalSource:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory).expanduser().resolve()
        self.label = str(self.dir)

    def read(self, rel: str, fresh: bool = False) -> bytes | None:
        path = (self.dir / _safe_rel(rel)).resolve()
        if self.dir not in path.parents and path != self.dir:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def write(self, rel: str, data: bytes) -> None:
        path = self.dir / _safe_rel(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class GCSSource:
    """Reads gs://bucket/prefix/<rel>, caching job files on disk."""

    def __init__(self, uri: str) -> None:
        if not uri.startswith("gs://"):
            raise SystemExit("--gcs must look like gs://BUCKET/PREFIX")
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("Install the bucket library first: pip install google-cloud-storage") from exc
        self.bucket_name, _, prefix = uri[5:].partition("/")
        self.prefix = prefix.strip("/")
        self.label = uri.rstrip("/")
        self.bucket = storage.Client().bucket(self.bucket_name)
        self.cache = UI_HOME / "cache" / self.bucket_name / (self.prefix or "_root")

    def _name(self, rel: str) -> str:
        rel = _safe_rel(rel)
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def read(self, rel: str, fresh: bool = False) -> bytes | None:
        cached = self.cache / _safe_rel(rel)
        if not fresh and cached.exists():
            return cached.read_bytes()
        from google.api_core import exceptions as gexc
        try:
            data = self.bucket.blob(self._name(rel)).download_as_bytes()
        except gexc.NotFound:
            return None
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data

    def write(self, rel: str, data: bytes) -> None:
        blob = self.bucket.blob(self._name(rel))
        blob.cache_control = "no-cache"
        blob.upload_from_string(data, content_type="application/json")


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class App:
    def __init__(self, source) -> None:
        self.source = source
        self.lock = threading.Lock()
        self.decisions_path = UI_HOME / "decisions.json"
        self.decisions: dict = self._load_local_decisions()
        self.refresh()

    # -- decisions ------------------------------------------------------------
    def _load_local_decisions(self) -> dict:
        try:
            return json.loads(self.decisions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _merge(self, other: dict) -> None:
        for url, d in (other or {}).items():
            mine = self.decisions.get(url)
            if not mine or (d.get("updated") or "") > (mine.get("updated") or ""):
                self.decisions[url] = d

    def _save_decisions(self) -> None:
        UI_HOME.mkdir(parents=True, exist_ok=True)
        tmp = self.decisions_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.decisions, indent=2), encoding="utf-8")
        os.replace(tmp, self.decisions_path)
        data = json.dumps(self.decisions, indent=2).encode("utf-8")

        def push():
            try:
                self.source.write("decisions.json", data)
            except Exception as exc:  # keep working offline
                print(f"(could not save labels to {self.source.label}: {exc})")
        threading.Thread(target=push, daemon=True).start()

    def set_decision(self, url: str, decision: str | None, note: str | None) -> dict:
        with self.lock:
            entry = dict(self.decisions.get(url) or {})
            if decision is not None:
                if decision and decision not in DECISIONS:
                    raise ValueError("unknown decision")
                entry["decision"] = decision or None
            if note is not None:
                entry["note"] = note[:2000]
            entry["updated"] = _now()
            self.decisions[url] = entry
            self._save_decisions()
            return entry

    # -- data -------------------------------------------------------------------
    def refresh(self) -> None:
        with self.lock:
            cache = getattr(self.source, "cache", None)
            if cache is not None:
                import shutil
                shutil.rmtree(cache / "jobs", ignore_errors=True)
            raw = self.source.read("status.json", fresh=True)
            try:
                self.status = json.loads(raw) if raw else {}
            except ValueError:
                self.status = {}
            raw = self.source.read("shortlist.csv", fresh=True)
            self.jobs = list(csv.DictReader(io.StringIO(raw.decode("utf-8")))) if raw else []
            raw = self.source.read("decisions.json", fresh=True)
            if raw:
                try:
                    self._merge(json.loads(raw))
                except ValueError:
                    pass
            self.loaded_at = _now()

    def jobs_payload(self) -> list[dict]:
        out = []
        for i, j in enumerate(self.jobs, 1):
            d = self.decisions.get(j.get("job_url", ""), {})
            out.append({**j, "index": i, "decision": d.get("decision"), "note": d.get("note", "")})
        return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _secure(self) -> bool:
            return self.headers.get("X-Forwarded-Proto", "") == "https"

        def _authed(self) -> bool:
            return _valid_session(self.headers.get("Cookie"))

        def _login_page(self, error: str = "", code: int = 200) -> None:
            html = LOGIN_PAGE.replace("__ERROR__", f'<p class="e">{error}</p>' if error else "")
            self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if url.path == "/login":
                return self._login_page()
            if url.path == "/logout":
                return self._send(303, b"", "text/plain", {
                    "Location": "/login", "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
            if not self._authed():
                if url.path.startswith("/api/") or url.path.startswith("/files/"):
                    return self._json({"error": "login required"}, 401)
                return self._send(303, b"", "text/plain", {"Location": "/login"})
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/status":
                return self._json({"status": app.status, "source": app.source.label,
                                   "loaded_at": app.loaded_at, "job_count": len(app.jobs),
                                   "protected": bool(_password())})
            if url.path == "/api/jobs":
                return self._json(app.jobs_payload())
            if url.path.startswith("/files/"):
                try:
                    rel = _safe_rel(url.path[len("/files/"):])
                except ValueError:
                    return self._send(400, b"bad path", "text/plain")
                if not rel.startswith("jobs/"):
                    return self._send(403, b"forbidden", "text/plain")
                data = app.source.read(rel)
                if data is None:
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
                if ctype.startswith("text/"):
                    ctype += "; charset=utf-8"
                disp = "attachment" if parse_qs(url.query).get("download") else "inline"
                name = PurePosixPath(rel).name
                return self._send(200, data, ctype, {"Content-Disposition": f'{disp}; filename="{name}"'})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            if url.path == "/login":
                form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                given = (form.get("password") or [""])[0]
                if _password() and hmac.compare_digest(given.encode("utf-8"), _password().encode("utf-8")):
                    exp = int(time.time()) + SESSION_DAYS * 86400
                    flags = "HttpOnly; SameSite=Lax; Path=/; Max-Age=%d" % (SESSION_DAYS * 86400)
                    if self._secure():
                        flags += "; Secure"
                    return self._send(303, b"", "text/plain", {
                        "Location": "/", "Set-Cookie": f"{COOKIE}={_sign(exp)}; {flags}"})
                time.sleep(1.0)  # slow down guessing
                return self._login_page("Wrong password.", 401)
            if not self._authed():
                return self._json({"error": "login required"}, 401)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            if url.path == "/api/refresh":
                try:
                    app.refresh()
                except Exception as exc:
                    return self._json({"error": str(exc)}, 502)
                return self._json({"ok": True, "loaded_at": app.loaded_at})
            if url.path == "/api/decision":
                try:
                    entry = app.set_decision(body["url"], body.get("decision"), body.get("note"))
                except (KeyError, ValueError) as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json(entry)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(source, port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    if host not in ("127.0.0.1", "localhost") and not _password():
        raise SystemExit("Refusing to listen on a public address without APPLYPILOT_UI_PASSWORD set.")
    app = App(source)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://127.0.0.1:{port}/" if host in ("127.0.0.1", "0.0.0.0") else f"http://{host}:{port}/"
    print(f"ApplyPilot dashboard: {url}  (data: {source.label})")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="applypilot-ui", description="Local ApplyPilot dashboard")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--gcs", help="Read from gs://BUCKET/PREFIX (e.g. gs://applypilot-jeevan-shortlist/applypilot)")
    g.add_argument("--dir", help="Read from a local shortlist folder (default ~/.applypilot/shortlist)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    p.add_argument("--host", default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
                   help="Address to listen on (Cloud Run sets PORT, which switches this to 0.0.0.0)")
    p.add_argument("--no-open", action="store_true", help="Don't open the browser")
    a = p.parse_args(argv)

    gcs = a.gcs or (os.environ.get("APPLYPILOT_GCS_URI", "").strip() if not a.dir else "")
    source = GCSSource(gcs) if gcs else LocalSource(Path(a.dir or Path.home() / ".applypilot" / "shortlist"))
    serve(source, port=a.port, open_browser=not a.no_open and a.host == "127.0.0.1", host=a.host)
