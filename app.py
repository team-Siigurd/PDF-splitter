#!/usr/bin/env python3
"""Small, dependency-free HTTP API for splitting large PDFs on Render.

The service receives a short-lived OneDrive download URL, downloads the PDF
directly to temporary storage, verifies the download, and uses qpdf to split
the document into parts below a configured byte limit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_VERSION = "1.1.0"
BASE_DIR = Path(os.getenv("JOB_DIR", "/tmp/siigurd-pdf-splitter-jobs")).resolve()
API_KEY = os.getenv("SPLITTER_API_KEY", "")
MAX_SOURCE_BYTES = int(os.getenv("MAX_SOURCE_BYTES", "500000000"))
DEFAULT_MAX_PART_BYTES = int(os.getenv("DEFAULT_MAX_PART_BYTES", "45000000"))
ABSOLUTE_MAX_PART_BYTES = 45_000_000
FILE_TTL_SECONDS = int(os.getenv("FILE_TTL_SECONDS", "3600"))
SIGNED_URL_TTL_SECONDS = min(
    FILE_TTL_SECONDS,
    max(60, int(os.getenv("SIGNED_URL_TTL_SECONDS", "3600"))),
)
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
PORT = int(os.getenv("PORT", "10000"))

_default_hosts = (
    ".sharepoint.com,.sharepointonline.com,.1drv.com,.onedrive.com,"
    ".microsoft.com,.storage.live.com"
)
ALLOWED_HOST_SUFFIXES = tuple(
    value.strip().lower()
    for value in os.getenv("ALLOWED_DOWNLOAD_HOST_SUFFIXES", _default_hosts).split(",")
    if value.strip()
)

_public_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
if not _public_base and os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    _public_base = "https://" + os.environ["RENDER_EXTERNAL_HOSTNAME"].strip().strip("/")
PUBLIC_BASE_URL = _public_base

JOB_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
ACTIVE_JOBS: set[str] = set()
ACTIVE_JOBS_LOCK = threading.Lock()


class JobError(RuntimeError):
    """An error message that is safe to return to the API client."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_status(job_id: str) -> dict[str, Any] | None:
    status_path = BASE_DIR / job_id / "status.json"
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def update_status(job_id: str, **changes: Any) -> dict[str, Any]:
    status = read_status(job_id) or {"job_id": job_id}
    status.update(changes)
    status["updated_at"] = utc_now()
    status["updated_epoch"] = time.time()
    atomic_write_json(BASE_DIR / job_id / "status.json", status)
    return status


def is_valid_job_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", value))


def file_signature(job_id: str, filename: str, expires: int) -> str:
    """Create a signature scoped to one job, filename, and expiration time."""
    message = f"{job_id}\n{filename}\n{expires}".encode("utf-8")
    return hmac.new(API_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed_file_path(job_id: str, filename: str) -> tuple[str, int]:
    expires = int(time.time()) + SIGNED_URL_TTL_SECONDS
    path = f"/files/{job_id}/{urllib.parse.quote(filename)}"
    query = urllib.parse.urlencode(
        {
            "expires": expires,
            "signature": file_signature(job_id, filename, expires),
        }
    )
    return f"{path}?{query}", expires


def host_is_allowed(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    for suffix in ALLOWED_HOST_SUFFIXES:
        normalized = suffix.lstrip(".")
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise JobError("Download URL must use HTTPS.")
    if parsed.username or parsed.password:
        raise JobError("Download URL must not contain embedded credentials.")
    if not host_is_allowed(parsed.hostname):
        raise JobError(f"Download host is not allowed: {parsed.hostname or 'unknown'}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalized_sha1(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value).lower()


def download_pdf(
    url: str,
    destination: Path,
    expected_size: int | None,
    expected_sha1: str | None,
) -> dict[str, Any]:
    validate_download_url(url)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"SiigurdPDFSplitter/{APP_VERSION}"},
        method="GET",
    )

    sha1 = hashlib.sha1()  # noqa: S324 - used only for integrity comparison with OneDrive
    sha256 = hashlib.sha256()
    downloaded = 0

    try:
        with opener.open(request, timeout=90) as response, destination.open("wb") as output:
            final_url = response.geturl()
            validate_download_url(final_url)

            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_SOURCE_BYTES:
                raise JobError("Source file is larger than MAX_SOURCE_BYTES.")

            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_SOURCE_BYTES:
                    raise JobError("Source file exceeded MAX_SOURCE_BYTES during download.")
                output.write(chunk)
                sha1.update(chunk)
                sha256.update(chunk)
    except JobError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise JobError(f"OneDrive download failed ({type(exc).__name__}).") from None

    if expected_size is not None and downloaded != expected_size:
        raise JobError(
            f"Incomplete download: expected {expected_size} bytes, received {downloaded} bytes."
        )

    actual_sha1 = sha1.hexdigest()
    if expected_sha1:
        expected_normalized = normalized_sha1(expected_sha1)
        if len(expected_normalized) != 40:
            raise JobError("expected_sha1 is not a valid hexadecimal SHA-1 value.")
        if not hmac.compare_digest(actual_sha1, expected_normalized):
            raise JobError("Downloaded file SHA-1 does not match OneDrive.")

    with destination.open("rb") as pdf_file:
        if pdf_file.read(5) != b"%PDF-":
            raise JobError("Downloaded file is not a valid PDF file.")

    return {
        "size": downloaded,
        "sha1": actual_sha1,
        "sha256": sha256.hexdigest(),
    }


def run_qpdf(arguments: list[str], timeout: int = 900, allow_warnings: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["qpdf", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise JobError("qpdf is not installed in the container.") from None
    except subprocess.TimeoutExpired:
        raise JobError("qpdf timed out while processing the PDF.") from None

    accepted_codes = {0, 3} if allow_warnings else {0}
    if completed.returncode not in accepted_codes:
        detail = (completed.stderr or completed.stdout or "unknown qpdf error").strip()
        detail = detail[-500:]
        raise JobError(f"qpdf failed: {detail}")
    return completed.stdout.strip()


def get_page_count(input_pdf: Path) -> int:
    output = run_qpdf(["--show-npages", str(input_pdf)])
    try:
        page_count = int(output)
    except ValueError:
        raise JobError("qpdf returned an invalid page count.") from None
    if page_count < 1:
        raise JobError("The PDF contains no pages.")
    return page_count


def qpdf_page_range(input_pdf: Path, output_pdf: Path, start: int, end: int) -> None:
    page_range = str(start) if start == end else f"{start}-{end}"
    run_qpdf(
        [
            "--empty",
            "--pages",
            str(input_pdf),
            page_range,
            "--",
            str(output_pdf),
        ]
    )


def validate_pdf(path: Path) -> None:
    run_qpdf(["--check", str(path)], allow_warnings=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def split_range_recursively(
    input_pdf: Path,
    parts_dir: Path,
    start: int,
    end: int,
    max_part_bytes: int,
    accepted: list[dict[str, Any]],
) -> None:
    candidate = parts_dir / f"candidate_{start}_{end}_{uuid.uuid4().hex}.pdf"
    qpdf_page_range(input_pdf, candidate, start, end)
    size = candidate.stat().st_size

    if size <= max_part_bytes:
        validate_pdf(candidate)
        accepted.append({"start_page": start, "end_page": end, "path": candidate})
        return

    candidate.unlink(missing_ok=True)
    if start == end:
        raise JobError(
            f"Page {start} alone is {size} bytes and cannot be reduced below "
            f"the configured {max_part_bytes}-byte limit by splitting."
        )

    middle = (start + end) // 2
    split_range_recursively(
        input_pdf, parts_dir, start, middle, max_part_bytes, accepted
    )
    split_range_recursively(
        input_pdf, parts_dir, middle + 1, end, max_part_bytes, accepted
    )


def split_pdf(
    input_pdf: Path,
    parts_dir: Path,
    source_size: int,
    max_part_bytes: int,
) -> tuple[int, list[dict[str, Any]]]:
    page_count = get_page_count(input_pdf)
    soft_target = max(1_000_000, int(max_part_bytes * 0.88))
    initial_part_count = max(1, math.ceil(source_size / soft_target))
    initial_part_count = min(initial_part_count, page_count)
    pages_per_range = math.ceil(page_count / initial_part_count)

    accepted: list[dict[str, Any]] = []
    start = 1
    while start <= page_count:
        end = min(page_count, start + pages_per_range - 1)
        split_range_recursively(
            input_pdf, parts_dir, start, end, max_part_bytes, accepted
        )
        start = end + 1

    accepted.sort(key=lambda part: part["start_page"])
    expected_page = 1
    final_parts: list[dict[str, Any]] = []

    for index, part in enumerate(accepted, start=1):
        if part["start_page"] != expected_page:
            raise JobError("Internal page coverage check failed.")
        expected_page = part["end_page"] + 1

        filename = (
            f"part_{index:03d}_pages_"
            f"{part['start_page']}-{part['end_page']}.pdf"
        )
        final_path = parts_dir / filename
        os.replace(part["path"], final_path)
        size = final_path.stat().st_size
        if size > max_part_bytes:
            raise JobError("Internal part-size check failed.")

        final_parts.append(
            {
                "part": index,
                "filename": filename,
                "start_page": part["start_page"],
                "end_page": part["end_page"],
                "size": size,
                "sha256": file_sha256(final_path),
            }
        )

    if expected_page != page_count + 1:
        raise JobError("Internal final-page coverage check failed.")

    return page_count, final_parts


def process_job(job_id: str, payload: dict[str, Any]) -> None:
    job_dir = BASE_DIR / job_id
    input_pdf = job_dir / "source.pdf"
    parts_dir = job_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    try:
        update_status(job_id, status="processing", stage="downloading")
        source = download_pdf(
            payload["download_url"],
            input_pdf,
            payload.get("expected_size"),
            payload.get("expected_sha1"),
        )

        update_status(
            job_id,
            status="processing",
            stage="splitting",
            source={
                "filename": payload["filename"],
                **source,
            },
        )
        page_count, parts = split_pdf(
            input_pdf,
            parts_dir,
            source["size"],
            payload["max_part_size"],
        )
        input_pdf.unlink(missing_ok=True)

        update_status(
            job_id,
            status="complete",
            stage="complete",
            page_count=page_count,
            part_count=len(parts),
            parts=parts,
            error=None,
        )
    except JobError as exc:
        input_pdf.unlink(missing_ok=True)
        shutil.rmtree(parts_dir, ignore_errors=True)
        update_status(job_id, status="error", stage="error", error=str(exc), parts=[])
    except Exception as exc:  # Defensive: do not leak sensitive URLs or internals.
        input_pdf.unlink(missing_ok=True)
        shutil.rmtree(parts_dir, ignore_errors=True)
        update_status(
            job_id,
            status="error",
            stage="error",
            error=f"Unexpected processing error ({type(exc).__name__}).",
            parts=[],
        )
    finally:
        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS.discard(job_id)
        JOB_SEMAPHORE.release()


def cleanup_expired_jobs() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - FILE_TTL_SECONDS
    with ACTIVE_JOBS_LOCK:
        active = set(ACTIVE_JOBS)

    for job_dir in BASE_DIR.iterdir():
        if not job_dir.is_dir() or job_dir.name in active:
            continue
        status = read_status(job_dir.name)
        updated = status.get("updated_epoch", 0) if status else 0
        if updated and updated < cutoff:
            shutil.rmtree(job_dir, ignore_errors=True)


def public_status(status: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in status.items()
        if key not in {"updated_epoch"}
    }
    if result.get("status") == "complete":
        parts = []
        for part in result.get("parts", []):
            item = dict(part)
            path, expires = signed_file_path(result["job_id"], part["filename"])
            item["download_path"] = path
            item["download_url"] = PUBLIC_BASE_URL + path if PUBLIC_BASE_URL else None
            item["download_url_expires_epoch"] = expires
            parts.append(item)
        result["parts"] = parts
    return result


class APIHandler(BaseHTTPRequestHandler):
    server_version = "SiigurdPDFSplitter/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        # Do not log request bodies or OneDrive URLs.
        message = format_string % args
        message = re.sub(r"([?&]signature=)[^&\s]+", r"\1[redacted]", message)
        print(f"{self.address_string()} - {message}", flush=True)

    def send_json(
        self,
        status_code: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        provided = self.headers.get("X-API-Key", "")
        if not API_KEY:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "SPLITTER_API_KEY is not configured on the server."},
            )
            return False
        if not hmac.compare_digest(provided, API_KEY):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized."})
            return False
        return True

    def authorized_file_download(
        self,
        parsed: urllib.parse.ParseResult,
        job_id: str,
        filename: str,
    ) -> bool:
        """Allow either the API-key header or a short-lived signed file URL."""
        if not API_KEY:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "SPLITTER_API_KEY is not configured on the server."},
            )
            return False

        provided = self.headers.get("X-API-Key", "")
        if provided and hmac.compare_digest(provided, API_KEY):
            return True

        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        expires_values = query.get("expires", [])
        signature_values = query.get("signature", [])
        if len(expires_values) != 1 or len(signature_values) != 1:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized."})
            return False
        try:
            expires = int(expires_values[0])
        except ValueError:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized."})
            return False
        if expires < int(time.time()):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Download URL has expired."})
            return False

        expected = file_signature(job_id, filename, expires)
        if not hmac.compare_digest(signature_values[0], expected):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized."})
            return False
        return True

    def read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length."})
            return None
        if length <= 0 or length > 65_536:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "JSON body must be between 1 and 65536 bytes."},
            )
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
            return None
        if not isinstance(value, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object."})
            return None
        return value

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]

        if parsed.path == "/health":
            try:
                qpdf_version = run_qpdf(["--version"], timeout=15).splitlines()[0]
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "version": APP_VERSION,
                        "qpdf": qpdf_version,
                    },
                )
            except JobError as exc:
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "error", "error": str(exc)},
                )
            return

        if len(path_parts) == 3 and path_parts[0] == "files":
            job_id = path_parts[1]
            filename = urllib.parse.unquote(path_parts[2])
            if not self.authorized_file_download(parsed, job_id, filename):
                return
            if not is_valid_job_id(job_id):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not found."})
                return
            status = read_status(job_id)
            if not status or status.get("status") != "complete":
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not found or expired."})
                return
            allowed_names = {part["filename"] for part in status.get("parts", [])}
            if filename not in allowed_names:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not found."})
                return
            file_path = BASE_DIR / job_id / "parts" / filename
            if not file_path.is_file():
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not found or expired."})
                return

            size = file_path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                with file_path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if not self.authorized():
            return

        if len(path_parts) == 2 and path_parts[0] == "jobs":
            job_id = path_parts[1]
            if not is_valid_job_id(job_id):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found."})
                return
            status = read_status(job_id)
            if not status:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found or expired."})
                return
            self.send_json(HTTPStatus.OK, public_status(status))
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/jobs":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        if not self.authorized():
            return

        payload = self.read_json_body()
        if payload is None:
            return

        download_url = payload.get("download_url")
        filename = payload.get("filename")
        if not isinstance(download_url, str) or not download_url:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "download_url is required."})
            return
        if not isinstance(filename, str) or not filename.lower().endswith(".pdf"):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "filename must end with .pdf."})
            return
        try:
            validate_download_url(download_url)
        except JobError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        expected_size = payload.get("expected_size")
        if expected_size in (None, ""):
            expected_size = None
        else:
            try:
                expected_size = int(expected_size)
            except (TypeError, ValueError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "expected_size must be an integer."})
                return
            if expected_size < 1 or expected_size > MAX_SOURCE_BYTES:
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "expected_size is outside the permitted range."},
                )
                return

        requested_limit = payload.get("max_part_size", DEFAULT_MAX_PART_BYTES)
        try:
            requested_limit = int(requested_limit)
        except (TypeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "max_part_size must be an integer."})
            return
        if not 1_000_000 <= requested_limit <= ABSOLUTE_MAX_PART_BYTES:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": (
                        "max_part_size must be between 1000000 and "
                        f"{ABSOLUTE_MAX_PART_BYTES}."
                    )
                },
            )
            return

        expected_sha1 = payload.get("expected_sha1")
        if expected_sha1 in (None, ""):
            expected_sha1 = None
        elif not isinstance(expected_sha1, str):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "expected_sha1 must be text."})
            return

        cleanup_expired_jobs()
        if not JOB_SEMAPHORE.acquire(blocking=False):
            self.send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "The splitter is busy. Retry later."},
                {"Retry-After": "30"},
            )
            return

        job_id = uuid.uuid4().hex
        job_dir = BASE_DIR / job_id
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            with ACTIVE_JOBS_LOCK:
                ACTIVE_JOBS.add(job_id)

            safe_payload = {
                "download_url": download_url,
                "filename": filename,
                "expected_size": expected_size,
                "expected_sha1": expected_sha1,
                "max_part_size": requested_limit,
            }
            update_status(
                job_id,
                status="processing",
                stage="starting",
                created_at=utc_now(),
                source={"filename": filename},
                max_part_size=requested_limit,
                parts=[],
                error=None,
            )
            worker = threading.Thread(
                target=process_job,
                args=(job_id, safe_payload),
                daemon=True,
                name=f"pdf-job-{job_id[:8]}",
            )
            worker.start()
        except Exception:
            with ACTIVE_JOBS_LOCK:
                ACTIVE_JOBS.discard(job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            JOB_SEMAPHORE.release()
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Could not start the PDF job."},
            )
            return

        self.send_json(
            HTTPStatus.ACCEPTED,
            {
                "job_id": job_id,
                "status": "processing",
                "status_path": f"/jobs/{job_id}",
                "status_url": (
                    f"{PUBLIC_BASE_URL}/jobs/{job_id}" if PUBLIC_BASE_URL else None
                ),
            },
        )

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]
        if not self.authorized():
            return
        if len(path_parts) != 2 or path_parts[0] != "jobs" or not is_valid_job_id(path_parts[1]):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found."})
            return
        job_id = path_parts[1]
        with ACTIVE_JOBS_LOCK:
            if job_id in ACTIVE_JOBS:
                self.send_json(HTTPStatus.CONFLICT, {"error": "Job is still processing."})
                return
        job_dir = BASE_DIR / job_id
        if not job_dir.exists():
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found or expired."})
            return
        shutil.rmtree(job_dir, ignore_errors=True)
        self.send_json(HTTPStatus.OK, {"job_id": job_id, "status": "deleted"})


def main() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not API_KEY:
        print("WARNING: SPLITTER_API_KEY is not configured; protected endpoints will reject requests.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), APIHandler)
    print(f"Siigurd PDF Splitter {APP_VERSION} listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
