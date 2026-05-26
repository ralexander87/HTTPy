from __future__ import annotations

import argparse
import html
import re
import shutil
import socket
import tempfile
import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit

CHUNK_SIZE = 1024 * 1024
SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}
DURATION_UNITS = {
    "": 1,
    "S": 1,
    "M": 60,
    "H": 60 * 60,
    "D": 24 * 60 * 60,
}


def parse_size(value: str | None) -> int | None:
    if value is None:
        return None

    cleaned = value.strip().upper()
    if cleaned in {"", "0", "NONE", "UNLIMITED", "OFF"}:
        return None

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?B?)", cleaned)
    if not match:
        raise argparse.ArgumentTypeError("Use a size like 500MB, 2G, or 1048576.")

    amount, unit = match.groups()
    multiplier = SIZE_UNITS.get(unit)
    if multiplier is None:
        raise argparse.ArgumentTypeError("Use a size unit like B, KB, MB, GB, or TB.")

    return int(float(amount) * multiplier)


def parse_duration(value: str | None) -> int | None:
    if value is None:
        return None

    cleaned = value.strip().upper()
    if cleaned in {"", "0", "NONE", "OFF"}:
        return None

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([SMHD]?)", cleaned)
    if not match:
        raise argparse.ArgumentTypeError("Use a duration like 30m, 2h, or 600.")

    amount, unit = match.groups()
    seconds = int(float(amount) * DURATION_UNITS[unit])
    return seconds or None


def format_size(size: int | None) -> str:
    if size is None:
        return "unlimited"

    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "off"

    for unit_name, unit_seconds in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= unit_seconds and seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{unit_name}"

    return f"{seconds}s"


def resolve_request_path(root_dir: Path, request_path: str) -> Path:
    root = root_dir.resolve()
    parsed_path = unquote(urlsplit(request_path).path)
    relative_path = Path(parsed_path.lstrip("/"))

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("invalid upload path")

    target_path = (root / relative_path).resolve()

    try:
        target_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes shared directory") from exc

    return target_path


def resolve_upload_path(upload_dir: Path, request_path: str) -> Path:
    target_path = resolve_request_path(upload_dir, request_path)
    relative_path = target_path.relative_to(upload_dir.resolve())

    if not relative_path.name:
        raise ValueError("missing filename")

    return target_path


def candidate_upload_paths(target_path: Path):
    yield target_path

    stem = target_path.stem
    suffix = target_path.suffix

    counter = 1
    while True:
        yield target_path.with_name(f"{stem}-{counter}{suffix}")
        counter += 1


def open_upload_target(target_path: Path, overwrite: bool) -> tuple[Path, BinaryIO]:
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if overwrite:
        return target_path, target_path.open("wb")

    for candidate_path in candidate_upload_paths(target_path):
        try:
            return candidate_path, candidate_path.open("xb")
        except FileExistsError:
            continue

    raise RuntimeError("could not find an available upload filename")


def iter_shared_files(root_dir: Path) -> list[Path]:
    root = root_dir.resolve()
    return sorted(path for path in root.rglob("*") if path.is_file())


def path_to_url(path: Path) -> str:
    return "/" + quote(path.as_posix(), safe="/")


def get_local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()

    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass

    return sorted(address for address in addresses if not address.startswith("127."))


def server_urls(host: str, port: int) -> list[str]:
    if host in {"", "0.0.0.0"}:
        urls = [f"http://127.0.0.1:{port}"]
        urls.extend(f"http://{address}:{port}" for address in get_local_ipv4_addresses())
        return urls

    return [f"http://{host}:{port}"]


def new_tree_node() -> dict:
    return {"dirs": {}, "files": []}


def build_file_tree(root_dir: Path, files: list[Path]) -> dict:
    root = root_dir.resolve()
    tree = new_tree_node()

    for path in files:
        relative_path = path.relative_to(root)
        current = tree

        for part in relative_path.parts[:-1]:
            current = current["dirs"].setdefault(part, new_tree_node())

        current["files"].append(path)

    return tree


def tree_file_count(node: dict) -> int:
    return len(node["files"]) + sum(tree_file_count(child) for child in node["dirs"].values())


def render_file_row(root_dir: Path, path: Path) -> str:
    relative_path = path.relative_to(root_dir.resolve())
    stat = path.stat()
    file_url = path_to_url(relative_path)
    display_name = html.escape(relative_path.name)
    modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))

    return (
        "<div class=\"file-row\">"
        f"<a class=\"file-name\" href=\"{file_url}\">{display_name}</a>"
        f"<span class=\"file-meta\">{format_size(stat.st_size)}</span>"
        f"<span class=\"file-meta\">{modified}</span>"
        f"<a class=\"button small\" href=\"{file_url}\" download>Download</a>"
        "</div>"
    )


def render_tree_node(root_dir: Path, node: dict) -> str:
    parts = []

    for dirname, child in sorted(node["dirs"].items(), key=lambda item: item[0].lower()):
        folder_name = html.escape(dirname)
        file_count = tree_file_count(child)
        parts.append(
            "<details class=\"folder\">"
            "<summary>"
            "<span class=\"arrow\">&gt;</span>"
            f"<span class=\"folder-name\">{folder_name}</span>"
            f"<span class=\"folder-count\">{file_count}</span>"
            "</summary>"
            f"<div class=\"children\">{render_tree_node(root_dir, child)}</div>"
            "</details>"
        )

    for path in sorted(node["files"], key=lambda item: item.name.lower()):
        parts.append(render_file_row(root_dir, path))

    return "".join(parts)


def render_file_tree(root_dir: Path, files: list[Path]) -> str:
    if not files:
        return "<div class=\"empty\">No files</div>"

    tree = build_file_tree(root_dir, files)
    return f"<div class=\"file-tree\">{render_tree_node(root_dir, tree)}</div>"


def build_index_html(
    upload_dir: Path,
    max_upload_size: int | None,
    overwrite_uploads: bool,
) -> bytes:
    root = upload_dir.resolve()
    files = iter_shared_files(root)
    total_size = sum(path.stat().st_size for path in files)
    file_tree = render_file_tree(root, files)
    max_size_text = format_size(max_upload_size)
    overwrite_text = "overwrite" if overwrite_uploads else "rename"

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>File Share</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #171717;
      --muted: #64645f;
      --line: #d8d8d2;
      --accent: #126b5d;
      --accent-strong: #0d5147;
      --warn: #9b3d24;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #191a18;
        --panel: #232521;
        --text: #f2f2ed;
        --muted: #aaa99f;
        --line: #3b3d37;
        --accent: #6fc2ad;
        --accent-strong: #9fd9cb;
        --warn: #e78a6e;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(1040px, calc(100% - 32px));
      margin: 32px auto;
    }}
    header.top {{
      display: flex;
      gap: 16px;
      align-items: end;
      justify-content: space-between;
      margin-bottom: 18px;
    }}
    h1, h2 {{
      margin: 0;
      font-weight: 700;
      letter-spacing: 0;
    }}
    h1 {{ font-size: clamp(28px, 4vw, 42px); }}
    h2 {{ font-size: 18px; }}
    .stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      color: var(--muted);
      font-size: 13px;
    }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      background: var(--panel);
      white-space: nowrap;
    }}
    .upload {{
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--panel);
      min-height: 160px;
      display: grid;
      place-items: center;
      margin-bottom: 24px;
      transition: border-color .15s ease, background .15s ease;
    }}
    .upload.dragover {{
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 10%, var(--panel));
    }}
    .upload-inner {{
      display: grid;
      gap: 12px;
      justify-items: center;
      padding: 28px;
      text-align: center;
    }}
    .muted {{ color: var(--muted); }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 0 12px;
      background: var(--accent);
      color: #fff;
      font: inherit;
      font-weight: 650;
      text-decoration: none;
      cursor: pointer;
    }}
    .button:hover {{ background: var(--accent-strong); }}
    .button.secondary {{
      background: transparent;
      color: var(--accent);
    }}
    .button.small {{
      min-height: 30px;
      padding: 0 10px;
      font-size: 13px;
    }}
    .file-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }}
    .file-tree {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .folder {{
      border-bottom: 1px solid var(--line);
    }}
    .folder:last-child {{
      border-bottom: 0;
    }}
    summary {{
      min-height: 44px;
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr) auto;
      align-items: center;
      gap: 8px;
      padding: 0 12px;
      cursor: pointer;
      list-style: none;
      user-select: none;
    }}
    summary::-webkit-details-marker {{
      display: none;
    }}
    .arrow {{
      width: 18px;
      height: 18px;
      display: inline-grid;
      place-items: center;
      color: var(--muted);
      font-size: 13px;
      transition: transform .14s ease;
    }}
    .folder[open] > summary .arrow {{
      transform: rotate(90deg);
    }}
    .folder-name {{
      overflow-wrap: anywhere;
      font-weight: 700;
    }}
    .folder-count {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .children {{
      margin-left: 24px;
      border-left: 1px solid var(--line);
    }}
    .file-row {{
      min-height: 44px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto auto;
      align-items: center;
      gap: 12px;
      padding: 7px 12px;
      border-bottom: 1px solid var(--line);
    }}
    .file-tree > .file-row:last-child,
    .children > .file-row:last-child {{
      border-bottom: 0;
    }}
    .file-name {{
      overflow-wrap: anywhere;
      font-weight: 650;
    }}
    .file-meta {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    a {{ color: var(--accent); }}
    progress {{
      width: min(420px, 100%);
      height: 12px;
    }}
    #status {{
      min-height: 22px;
      margin: 0;
      color: var(--muted);
    }}
    #status.error {{ color: var(--warn); }}
    .empty {{
      color: var(--muted);
      text-align: center;
      padding: 28px 12px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    @media (max-width: 720px) {{
      main {{ width: min(100% - 20px, 1040px); margin-top: 18px; }}
      header.top, .file-head {{ align-items: stretch; flex-direction: column; }}
      .stats {{ justify-content: flex-start; }}
      .file-row {{ grid-template-columns: minmax(0, 1fr); align-items: start; gap: 6px; }}
      .file-meta {{ white-space: normal; }}
      .children {{ margin-left: 14px; }}
    }}
  </style>
</head>
<body data-max-upload-size="{max_upload_size or 0}">
  <main>
    <header class="top">
      <div>
        <h1>File Share</h1>
        <div class="muted">{html.escape(str(root))}</div>
      </div>
      <div class="stats">
        <span class="pill">{len(files)} files</span>
        <span class="pill">{format_size(total_size)}</span>
        <span class="pill">Limit {max_size_text}</span>
        <span class="pill">{overwrite_text}</span>
      </div>
    </header>

    <section id="drop-zone" class="upload">
      <div class="upload-inner">
        <input id="file-picker" type="file" multiple hidden>
        <button id="choose-files" class="button" type="button">Choose Files</button>
        <div class="muted">Drop files here</div>
        <progress id="progress" value="0" max="100" hidden></progress>
        <p id="status"></p>
      </div>
    </section>

    <section>
      <div class="file-head">
        <h2>Files</h2>
        <a class="button secondary" href="/download.zip">Download ZIP</a>
      </div>
      {file_tree}
    </section>
  </main>

  <script>
    const zone = document.getElementById("drop-zone");
    const picker = document.getElementById("file-picker");
    const choose = document.getElementById("choose-files");
    const progress = document.getElementById("progress");
    const status = document.getElementById("status");
    const maxUploadSize = Number(document.body.dataset.maxUploadSize || "0");

    choose.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => uploadFiles(picker.files));

    for (const eventName of ["dragenter", "dragover"]) {{
      zone.addEventListener(eventName, event => {{
        event.preventDefault();
        zone.classList.add("dragover");
      }});
    }}

    for (const eventName of ["dragleave", "drop"]) {{
      zone.addEventListener(eventName, event => {{
        event.preventDefault();
        zone.classList.remove("dragover");
      }});
    }}

    zone.addEventListener("drop", event => uploadFiles(event.dataTransfer.files));

    async function uploadFiles(files) {{
      const queue = Array.from(files || []);
      if (!queue.length) return;

      status.className = "";
      progress.hidden = false;
      progress.value = 0;

      for (let index = 0; index < queue.length; index++) {{
        const file = queue[index];

        if (maxUploadSize && file.size > maxUploadSize) {{
          status.className = "error";
          status.textContent = `${{file.name}} is too large`;
          continue;
        }}

        status.textContent = `Uploading ${{file.name}}`;
        const response = await fetch("/" + encodeURIComponent(file.name), {{
          method: "PUT",
          body: file,
          headers: {{
            "Content-Type": file.type || "application/octet-stream"
          }}
        }});

        const message = await response.text();
        if (!response.ok) {{
          status.className = "error";
          status.textContent = message || `Upload failed for ${{file.name}}`;
          progress.hidden = true;
          return;
        }}

        progress.value = Math.round(((index + 1) / queue.length) * 100);
      }}

      status.textContent = "Upload complete";
      setTimeout(() => window.location.reload(), 500);
    }}
  </script>
</body>
</html>
"""
    return document.encode("utf-8")


class UploadHandler(SimpleHTTPRequestHandler):
    server_version = "UploadHTTP/0.2"
    upload_dir: Path
    max_upload_size: int | None = None
    overwrite_uploads = False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path

        if path in {"/", "/index.html"}:
            self.send_index_page()
            return

        if path == "/download.zip":
            self.send_zip_archive()
            return

        try:
            requested_path = resolve_request_path(self.upload_dir, self.path)
        except ValueError:
            requested_path = None

        super().do_GET()

        if requested_path and requested_path.is_file():
            relative_path = requested_path.relative_to(self.upload_dir.resolve()).as_posix()
            self.log_event(
                f"Downloaded {relative_path} ({format_size(requested_path.stat().st_size)})"
            )

    def do_PUT(self) -> None:
        try:
            requested_path = resolve_upload_path(self.upload_dir, self.path)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return

        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_error(411, "Content-Length header is required")
            return

        try:
            remaining = int(content_length)
        except ValueError:
            self.send_error(400, "Invalid Content-Length header")
            return

        if remaining < 0:
            self.send_error(400, "Invalid Content-Length header")
            return

        if self.max_upload_size is not None and remaining > self.max_upload_size:
            self.send_error(413, f"Upload limit is {format_size(self.max_upload_size)}")
            return

        try:
            target_path, upload_file = open_upload_target(requested_path, self.overwrite_uploads)
        except OSError as exc:
            self.send_error(500, f"Could not open upload target: {exc}")
            return

        with upload_file:
            while remaining:
                chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
                if not chunk:
                    target_path.unlink(missing_ok=True)
                    self.send_error(400, "Upload ended before Content-Length bytes were received")
                    return

                upload_file.write(chunk)
                remaining -= len(chunk)

        relative_path = target_path.relative_to(self.upload_dir.resolve())
        uploaded_url = path_to_url(relative_path)

        self.send_response(201)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Location", uploaded_url)
        self.end_headers()
        self.wfile.write(f"Uploaded {relative_path.as_posix()}\n".encode("utf-8"))
        self.log_event(f"Uploaded {relative_path.as_posix()} ({format_size(int(content_length))})")

    def send_index_page(self) -> None:
        body = build_index_html(
            self.upload_dir,
            self.max_upload_size,
            self.overwrite_uploads,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_zip_archive(self) -> None:
        root = self.upload_dir.resolve()
        files = iter_shared_files(root)
        total_size = 0

        with tempfile.TemporaryFile() as archive:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for path in files:
                    stat = path.stat()
                    total_size += stat.st_size
                    zip_file.write(path, path.relative_to(root).as_posix())

            archive_size = archive.tell()
            archive.seek(0)

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="shared-files.zip"')
            self.send_header("Content-Length", str(archive_size))
            self.end_headers()
            shutil.copyfileobj(archive, self.wfile)

        self.log_event(f"Downloaded ZIP ({len(files)} files, {format_size(total_size)})")

    def log_event(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} {message}")

    def log_message(self, format: str, *args) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} {format % args}")


def make_handler(
    upload_dir: Path,
    max_upload_size: int | None,
    overwrite_uploads: bool,
) -> type[UploadHandler]:
    upload_dir = upload_dir.resolve()

    class ConfiguredUploadHandler(UploadHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(upload_dir), **kwargs)

    ConfiguredUploadHandler.upload_dir = upload_dir
    ConfiguredUploadHandler.max_upload_size = max_upload_size
    ConfiguredUploadHandler.overwrite_uploads = overwrite_uploads
    return ConfiguredUploadHandler


def schedule_auto_stop(server: ThreadingHTTPServer, seconds: int | None) -> threading.Timer | None:
    if seconds is None:
        return None

    def stop_server() -> None:
        print(f"\nAuto-stop reached after {format_duration(seconds)}. Stopping server.")
        server.shutdown()

    timer = threading.Timer(seconds, stop_server)
    timer.daemon = True
    timer.start()
    return timer


def print_useful_options() -> None:
    print("Useful options:")
    print("  --upload-dir PATH   Share/save files in another directory")
    print("  --overwrite         Replace existing files instead of renaming duplicates")
    print("  --max-size 500MB    Reject uploads larger than this size")
    print("  --stop-after 30m    Stop automatically after a short session")
    print("  --port 9000         Use a different port")
    print("  --host 127.0.0.1    Listen only on this computer")
    print("  --help              Show all options")


def run_server(
    host: str,
    port: int,
    upload_dir: Path,
    max_upload_size: int | None,
    overwrite_uploads: bool,
    stop_after: int | None,
) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    handler_class = make_handler(upload_dir, max_upload_size, overwrite_uploads)

    with ThreadingHTTPServer((host, port), handler_class) as server:
        actual_host, actual_port = server.server_address[:2]
        timer = schedule_auto_stop(server, stop_after)

        print(f"Serving directory: {upload_dir.resolve()}")
        print(f"Upload limit: {format_size(max_upload_size)}")
        print(f"Existing files: {'overwrite' if overwrite_uploads else 'rename'}")
        if stop_after is not None:
            print(f"Auto-stop: {format_duration(stop_after)}")
        print("Open:")
        for url in server_urls(host if host else actual_host, actual_port):
            print(f"  {url}")
        print_useful_options()

        try:
            server.serve_forever()
        finally:
            if timer is not None:
                timer.cancel()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a small HTTP upload server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    parser.add_argument(
        "--upload-dir",
        type=Path,
        default=Path("."),
        help="Directory where uploaded files are stored. Defaults to the current directory.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files instead of saving duplicates as name-1.ext.",
    )
    parser.add_argument(
        "--max-size",
        type=parse_size,
        default=None,
        help="Maximum upload size per file, for example 500MB. Defaults to unlimited.",
    )
    parser.add_argument(
        "--stop-after",
        type=parse_duration,
        default=None,
        help="Stop automatically after a duration, for example 30m or 2h.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        run_server(
            args.host,
            args.port,
            args.upload_dir,
            args.max_size,
            args.overwrite,
            args.stop_after,
        )
    except KeyboardInterrupt:
        print("\nServer stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
