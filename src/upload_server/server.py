from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

CHUNK_SIZE = 1024 * 1024


def resolve_upload_path(upload_dir: Path, request_path: str) -> Path:
    upload_root = upload_dir.resolve()
    parsed_path = unquote(urlsplit(request_path).path)
    relative_path = Path(parsed_path.lstrip("/"))

    if not relative_path.name:
        raise ValueError("missing filename")

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("invalid upload path")

    target_path = (upload_root / relative_path).resolve()

    try:
        target_path.relative_to(upload_root)
    except ValueError as exc:
        raise ValueError("upload path escapes upload directory") from exc

    return target_path


class UploadHandler(SimpleHTTPRequestHandler):
    server_version = "UploadHTTP/0.1"
    upload_dir: Path

    def do_PUT(self) -> None:
        try:
            target_path = resolve_upload_path(self.upload_dir, self.path)
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

        target_path.parent.mkdir(parents=True, exist_ok=True)

        with target_path.open("wb") as upload_file:
            while remaining:
                chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
                if not chunk:
                    target_path.unlink(missing_ok=True)
                    self.send_error(400, "Upload ended before Content-Length bytes were received")
                    return

                upload_file.write(chunk)
                remaining -= len(chunk)

        self.send_response(201)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"Uploaded {target_path.name}\n".encode("utf-8"))


def make_handler(upload_dir: Path) -> type[UploadHandler]:
    upload_dir = upload_dir.resolve()

    class ConfiguredUploadHandler(UploadHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(upload_dir), **kwargs)

    ConfiguredUploadHandler.upload_dir = upload_dir
    return ConfiguredUploadHandler


def run_server(host: str, port: int, upload_dir: Path) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    handler_class = make_handler(upload_dir)

    with ThreadingHTTPServer((host, port), handler_class) as server:
        print(f"Serving uploads from {upload_dir.resolve()}")
        print(f"Listening on http://{host}:{port}")
        server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a small HTTP upload server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host/IP to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    parser.add_argument(
        "--upload-dir",
        type=Path,
        default=Path("."),
        help="Directory where uploaded files are stored. Defaults to the current directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        run_server(args.host, args.port, args.upload_dir)
    except KeyboardInterrupt:
        print("\nServer stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
