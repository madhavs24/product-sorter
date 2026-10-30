"""Local page: choose a photo folder and write product subfolders inside it.

The page only listens on this computer. Photos are read in place. Results
are written to a sorted folder inside the folder you chose.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import subprocess
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import config
from src import describe_cmd, export_cmd, group_cmd, ingest_cmd
from src.utils import LOG, is_inside

PAGE = Path(__file__).with_name("ui_page.html")

_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()
_busy = False
_reveal: Path | None = None


class Job:
    def __init__(self, source: Path) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.source = source
        self.sorted_dir = source / "sorted"
        self.status = "collecting"
        self.logs: list[str] = []
        self.error: str | None = None
        self.result: dict | None = None
        self._lock = threading.Lock()

    def log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)
            del self.logs[:-300]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "logs": list(self.logs),
                "error": self.error,
                "result": self.result,
            }


class _JobLog(logging.Handler):
    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.job.log(self.format(record))


def _media_url(folder: str, filename: str) -> str:
    return "media/" + quote(folder) + "/" + quote(filename)


def _previews() -> list[dict]:
    rows = []
    for folder in describe_cmd.product_dirs():
        files = describe_cmd.images_in(folder)
        if not files:
            continue
        rows.append(
            {
                "id": folder.name,
                "count": len(files),
                "images": [_media_url(folder.name, path.name) for path in files[:4]],
            }
        )
    return rows


def _products() -> list[dict]:
    manifest_path = config.MANIFEST_PATH
    rows = []
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = []
        if isinstance(payload, list):
            rows = [row for row in payload if isinstance(row, dict)]
    sorted_root = config.OUTPUT_DIR / "sorted"
    sorted_dirs = sorted(path for path in sorted_root.iterdir() if path.is_dir()) if sorted_root.is_dir() else []
    products = []
    for index, row in enumerate(rows):
        folder = sorted_dirs[index] if index < len(sorted_dirs) else None
        images = []
        if folder is not None:
            files = describe_cmd.images_in(folder)
            images = [_media_url("sorted/" + folder.name, path.name) for path in files[:4]]
        products.append(
            {
                "title": row.get("title") or "",
                "description": row.get("description") or "",
                "category": row.get("category") or "",
                "condition": row.get("condition") or "",
                "folder": folder.name if folder else row.get("product_id") or "",
                "images": images,
            }
        )
    return products


def _photo_folder_error(path: Path) -> str | None:
    if not path.is_dir():
        return "That folder does not exist."
    if path.resolve() == config.ROOT or is_inside(path, config.ROOT):
        return "Choose the folder that holds the photos, not the sorter project."
    return None


def choose_photo_folder() -> Path | None:
    """Show the Mac folder dialog and return the chosen folder."""
    script = (
        'POSIX path of (choose folder with prompt '
        '"Choose the photo folder. A sorted folder will be created inside it.")'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def _run_group(job: Job) -> None:
    global _busy
    handler = _JobLog(job)
    LOG.addHandler(handler)
    try:
        job.status = "grouping"
        job.log(f"Reading photos in {job.source}")
        job.log(f"Sorted folders will be written to {job.sorted_dir}")
        code = ingest_cmd.run(argparse.Namespace(dump_folder=job.source, force=True))
        if code:
            job.status = "error"
            job.error = "Could not read those photos."
            return
        code = group_cmd.run(argparse.Namespace(threshold=None, dry_run=False))
        if code:
            job.status = "error"
            job.error = "Could not group the photos."
            return
        job.result = {"previews": _previews(), "plan": describe_cmd.plan()}
        job.status = "grouped"
        job.log(f"Grouped into {job.result['plan']['products']} products.")
    except Exception as exc:
        LOG.exception("Grouping failed")
        job.status = "error"
        job.error = str(exc)
    finally:
        LOG.removeHandler(handler)
        _busy = False


def _run_finish(job: Job, titles: bool) -> None:
    global _busy, _reveal
    handler = _JobLog(job)
    LOG.addHandler(handler)
    try:
        if titles:
            job.status = "describing"
            code = describe_cmd.run(argparse.Namespace(limit=None, force=False, yes=True))
            if code:
                job.log("Titles were not drafted. The folders will still be built.")
        else:
            job.log("Building folders without drafted titles.")
        job.status = "exporting"
        job.sorted_dir.mkdir(parents=True, exist_ok=True)
        code = export_cmd.run(argparse.Namespace(publish_into=job.sorted_dir))
        if code:
            job.status = "error"
            job.error = "Could not write the product folders."
            return
        _reveal = job.sorted_dir
        job.result = {"products": _products(), "sorted_path": str(job.sorted_dir)}
        job.status = "done"
        job.log(f"Done. {len(job.result['products'])} product folders are in {job.sorted_dir}")
    except Exception as exc:
        LOG.exception("Export failed")
        job.status = "error"
        job.error = str(exc)
    finally:
        LOG.removeHandler(handler)
        _busy = False


def _output_file(url_path: str) -> Path | None:
    rel = unquote(url_path).lstrip("/")
    root = config.OUTPUT_DIR.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[3] if len(path.split("/")) >= 4 else ""
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                self._json(404, {"error": "Unknown job"})
                return
            self._json(200, job.snapshot())
            return
        if path.startswith("/media/"):
            target = _output_file(path[len("/media/") :])
            if target is None:
                self.send_error(404)
                return
            data = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        global _busy
        path = urlparse(self.path).path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if parts == ["api", "choose"]:
            self._choose()
            return
        if parts == ["api", "reveal"]:
            folder = _reveal if _reveal is not None else config.OUTPUT_DIR / "sorted"
            if not folder.is_dir():
                self._json(404, {"error": "No sorted folder yet."})
                return
            subprocess.run(["open", str(folder)], check=False)
            self._json(200, {"ok": True, "path": str(folder)})
            return
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "files":
            self._save_file(parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "group":
            self._start_group(parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "finish":
            self._start_finish(parts[2])
            return
        self._json(404, {"error": "Not found"})

    def _job(self, job_id: str) -> Job | None:
        with _jobs_lock:
            return _jobs.get(job_id)

    def _save_file(self, job_id: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 0:
            self.rfile.read(length)
        self._json(400, {"error": "Choose the photo folder so the sorted folders can be saved inside it."})

    def _choose(self) -> None:
        folder = choose_photo_folder()
        if folder is None:
            self._json(200, {"cancelled": True})
            return
        problem = _photo_folder_error(folder)
        if problem:
            self._json(400, {"error": problem})
            return
        job = Job(folder)
        with _jobs_lock:
            _jobs[job.id] = job
        self._json(200, {"id": job.id, "path": str(folder), "sorted_path": str(job.sorted_dir)})

    def _start_group(self, job_id: str) -> None:
        global _busy
        job = self._job(job_id)
        if job is None:
            self._json(404, {"error": "Unknown job"})
            return
        if not job.source.is_dir():
            self._json(400, {"error": "Choose a photo folder first."})
            return
        with _jobs_lock:
            if _busy:
                self._json(409, {"error": "A folder is already being sorted."})
                return
            _busy = True
        threading.Thread(target=_run_group, args=(job,), daemon=True).start()
        self._json(200, {"ok": True})

    def _start_finish(self, job_id: str) -> None:
        global _busy
        job = self._job(job_id)
        if job is None:
            self._json(404, {"error": "Unknown job"})
            return
        body = self._read_json()
        titles = bool(body.get("titles"))
        with _jobs_lock:
            if _busy:
                self._json(409, {"error": "Still working on the photos."})
                return
            _busy = True
        threading.Thread(target=_run_finish, args=(job, titles), daemon=True).start()
        self._json(200, {"ok": True})


def run(args) -> int:
    port = args.port
    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        LOG.error("Could not start the page: %s", last_error)
        return 1
    url = f"http://127.0.0.1:{port}/"
    LOG.info("Choose a photo folder at %s", url)
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Stopped.")
    finally:
        server.server_close()
    return 0
