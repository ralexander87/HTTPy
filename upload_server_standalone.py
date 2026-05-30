from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlsplit


# Tunables and safety defaults used by both the package module and standalone copy.
CHUNK_SIZE = 1024 * 1024
MAX_COMMAND_BODY_SIZE = 64 * 1024
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
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


# Size and duration helpers keep CLI options, live settings, and UI labels consistent.
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


def command_output_to_text(output: str | bytes | None) -> str:
    if output is None:
        return ""

    if isinstance(output, bytes):
        text = output.decode("utf-8", errors="replace")
    else:
        text = str(output)

    text = ANSI_ESCAPE_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def run_shell_command(command: str, cwd: Path, timeout: int | None) -> dict:
    """Run one browser CLI command and return JSON-safe output."""
    command = command.strip()
    if not command:
        raise ValueError("missing command")

    if "\x00" in command:
        raise ValueError("invalid command")

    started_at = time.monotonic()

    try:
        # The shell is intentional here: the browser CLI is an opt-in convenience
        # for short trusted-network sessions, not a hardened remote shell.
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd.resolve()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        returncode = completed.returncode
        stdout = command_output_to_text(completed.stdout)
        stderr = command_output_to_text(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = command_output_to_text(exc.stdout)
        stderr = command_output_to_text(exc.stderr)
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        stderr += f"Command timed out after {format_duration(timeout)}."

    return {
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed": round(time.monotonic() - started_at, 3),
        "cwd": str(cwd.resolve()),
    }


def setting_size_value(size: int | None) -> str:
    if size is None:
        return ""

    return format_size(size).replace(" ", "")


def setting_duration_value(seconds: int | None) -> str:
    if seconds is None:
        return ""

    return format_duration(seconds)


class RuntimeSettings:
    """Thread-safe settings shared by all request-handler instances."""

    def __init__(
        self,
        max_upload_size: int | None,
        overwrite_uploads: bool,
        command_timeout: int | None,
        stop_after: int | None,
        show_hidden: bool,
    ) -> None:
        self.max_upload_size = max_upload_size
        self.overwrite_uploads = overwrite_uploads
        self.command_timeout = command_timeout
        self.stop_after = stop_after
        self.show_hidden = show_hidden
        self.auto_stop_deadline: float | None = None
        self.auto_stop_timer: threading.Timer | None = None
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            remaining = None
            if self.auto_stop_deadline is not None:
                remaining = max(0, int(self.auto_stop_deadline - time.time()))

            return {
                "max_upload_size": self.max_upload_size,
                "overwrite_uploads": self.overwrite_uploads,
                "command_timeout": self.command_timeout,
                "stop_after": self.stop_after,
                "auto_stop_remaining": remaining,
                "show_hidden": self.show_hidden,
            }

    def apply_updates(self, server: ThreadingHTTPServer, updates: dict) -> None:
        with self.lock:
            if "max_upload_size" in updates:
                self.max_upload_size = updates["max_upload_size"]
            if "overwrite_uploads" in updates:
                self.overwrite_uploads = updates["overwrite_uploads"]
            if "command_timeout" in updates:
                self.command_timeout = updates["command_timeout"]
            if "stop_after" in updates:
                self.stop_after = updates["stop_after"]
                self._schedule_auto_stop_locked(server)
            if "show_hidden" in updates:
                self.show_hidden = updates["show_hidden"]

    def start_auto_stop(self, server: ThreadingHTTPServer) -> None:
        with self.lock:
            self._schedule_auto_stop_locked(server)

    def cancel_auto_stop(self) -> None:
        with self.lock:
            self._cancel_auto_stop_locked()

    def _cancel_auto_stop_locked(self) -> None:
        if self.auto_stop_timer is not None:
            self.auto_stop_timer.cancel()
        self.auto_stop_timer = None
        self.auto_stop_deadline = None

    def _schedule_auto_stop_locked(self, server: ThreadingHTTPServer) -> None:
        self._cancel_auto_stop_locked()

        if self.stop_after is None:
            return

        seconds = self.stop_after
        self.auto_stop_deadline = time.time() + seconds

        def stop_server() -> None:
            print(f"\nAuto-stop reached after {format_duration(seconds)}. Stopping server.")
            server.shutdown()

        # server.shutdown() is safe to call from another thread and keeps the
        # main server loop from needing to poll for the auto-stop deadline.
        timer = threading.Timer(seconds, stop_server)
        timer.daemon = True
        timer.start()
        self.auto_stop_timer = timer


def settings_to_json(settings: RuntimeSettings) -> dict:
    snapshot = settings.snapshot()
    max_upload_size = snapshot["max_upload_size"]
    overwrite_uploads = snapshot["overwrite_uploads"]
    command_timeout = snapshot["command_timeout"]
    stop_after = snapshot["stop_after"]
    auto_stop_remaining = snapshot["auto_stop_remaining"]

    return {
        "max_upload_size": max_upload_size,
        "max_size": setting_size_value(max_upload_size),
        "max_size_label": format_size(max_upload_size),
        "overwrite": overwrite_uploads,
        "overwrite_label": "Overwrite" if overwrite_uploads else "Rename",
        "command_timeout_seconds": command_timeout,
        "command_timeout": setting_duration_value(command_timeout),
        "command_timeout_label": format_duration(command_timeout),
        "stop_after_seconds": stop_after,
        "stop_after": setting_duration_value(stop_after),
        "stop_after_label": format_duration(stop_after),
        "auto_stop_remaining": auto_stop_remaining,
        "auto_stop_remaining_label": format_duration(auto_stop_remaining),
        "show_hidden": snapshot["show_hidden"],
        "show_hidden_label": "Visible" if snapshot["show_hidden"] else "Hidden",
    }


def parse_settings_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid settings request")

    updates = {}

    if "max_size" in payload:
        updates["max_upload_size"] = parse_size(str(payload["max_size"]))

    if "overwrite" in payload:
        if not isinstance(payload["overwrite"], bool):
            raise ValueError("overwrite must be true or false")
        updates["overwrite_uploads"] = payload["overwrite"]

    if "command_timeout" in payload:
        updates["command_timeout"] = parse_duration(str(payload["command_timeout"]))

    if "stop_after" in payload:
        updates["stop_after"] = parse_duration(str(payload["stop_after"]))

    if "show_hidden" in payload:
        if not isinstance(payload["show_hidden"], bool):
            raise ValueError("show_hidden must be true or false")
        updates["show_hidden"] = payload["show_hidden"]

    return updates


# Path handling is centralized here so uploads, downloads, ZIPs, and deletes all
# follow the same traversal, dot-hidden path, and symlink escape rules.
def relative_path_from_root(root_dir: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(root_dir.resolve())
    except ValueError as exc:
        raise ValueError("path escapes shared directory") from exc


def is_hidden_relative_path(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def ensure_shared_path_allowed(root_dir: Path, path: Path, show_hidden: bool) -> Path:
    relative_path = relative_path_from_root(root_dir, path)

    if not show_hidden and is_hidden_relative_path(relative_path):
        raise PermissionError("hidden paths are not shared")

    return relative_path


def is_shared_path_allowed(root_dir: Path, path: Path, show_hidden: bool) -> bool:
    try:
        ensure_shared_path_allowed(root_dir, path, show_hidden)
    except (PermissionError, ValueError, OSError):
        return False

    return True


def resolve_request_path(root_dir: Path, request_path: str, show_hidden: bool = False) -> Path:
    """Resolve a URL path to a real filesystem path inside the shared root."""
    root = root_dir.resolve()
    parsed_path = unquote(urlsplit(request_path).path)
    relative_path = Path(parsed_path.lstrip("/"))

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("invalid upload path")

    target_path = (root / relative_path).resolve()

    ensure_shared_path_allowed(root, target_path, show_hidden)

    return target_path


def resolve_upload_path(upload_dir: Path, request_path: str, show_hidden: bool = False) -> Path:
    target_path = resolve_request_path(upload_dir, request_path, show_hidden)
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

    # Use exclusive creation so simultaneous duplicate uploads cannot choose the
    # same name between the existence check and the file open.
    for candidate_path in candidate_upload_paths(target_path):
        try:
            return candidate_path, candidate_path.open("xb")
        except FileExistsError:
            continue

    raise RuntimeError("could not find an available upload filename")


def iter_shared_files(root_dir: Path, show_hidden: bool = False) -> list[Path]:
    root = root_dir.resolve()
    return sorted(
        path.resolve()
        for path in root.rglob("*")
        if path.is_file() and is_shared_path_allowed(root, path, show_hidden)
    )


def resolve_selected_path(root_dir: Path, selected_path: str, show_hidden: bool = False) -> Path:
    root = root_dir.resolve()
    relative_path = Path(selected_path.strip("/"))

    if not selected_path or relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("invalid selected path")

    target_path = (root / relative_path).resolve()
    ensure_shared_path_allowed(root, target_path, show_hidden)

    return target_path


def resolve_selected_original_path(
    root_dir: Path,
    selected_path: str,
    show_hidden: bool = False,
) -> Path:
    """Resolve selected paths for delete while preserving the selected symlink."""
    root = root_dir.resolve()
    relative_path = Path(selected_path.strip("/"))

    if not selected_path or relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("invalid selected path")

    target_path = root / relative_path
    resolved_path = target_path.resolve()

    try:
        resolved_relative_path = resolved_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes shared directory") from exc

    if not show_hidden and (
        is_hidden_relative_path(relative_path) or is_hidden_relative_path(resolved_relative_path)
    ):
        raise PermissionError("hidden paths are not shared")

    return target_path


def files_for_zip(
    root_dir: Path,
    selected_paths: list[str] | None = None,
    show_hidden: bool = False,
) -> list[Path]:
    root = root_dir.resolve()

    if not selected_paths:
        return iter_shared_files(root, show_hidden)

    selected_files = []
    seen_paths = set()

    for selected_path in selected_paths:
        target_path = resolve_selected_path(root, selected_path, show_hidden)

        if not target_path.exists():
            raise FileNotFoundError(selected_path)

        if target_path.is_dir():
            candidate_paths = iter_shared_files(target_path, show_hidden)
        elif target_path.is_file():
            candidate_paths = [target_path]
        else:
            candidate_paths = []

        for candidate_path in candidate_paths:
            resolved_path = candidate_path.resolve()
            if resolved_path in seen_paths:
                continue

            seen_paths.add(resolved_path)
            selected_files.append(resolved_path)

    return sorted(selected_files)


def delete_selected_paths(
    root_dir: Path,
    selected_paths: list[str],
    show_hidden: bool = False,
) -> dict:
    if not selected_paths:
        raise ValueError("no selected paths")

    root = root_dir.resolve()
    deleted_files = []
    deleted_dirs = []
    seen_paths = set()

    def delete_file(path: Path) -> None:
        # Symlinks should delete the link itself, while regular files are
        # deduplicated by their resolved target.
        path_key = path.absolute() if path.is_symlink() else path.resolve()
        if path_key in seen_paths:
            return

        seen_paths.add(path_key)
        path.unlink()
        deleted_files.append(path)

    for selected_path in selected_paths:
        target_path = resolve_selected_original_path(root, selected_path, show_hidden)

        if not target_path.exists() and not target_path.is_symlink():
            raise FileNotFoundError(selected_path)

        if target_path.is_dir() and not target_path.is_symlink():
            # Delete files first, then remove empty directories from the leaves
            # upward. Hidden or escaping children are skipped unless enabled.
            for child_path in sorted(target_path.rglob("*"), reverse=True):
                if child_path.is_dir() and not child_path.is_symlink():
                    continue

                try:
                    resolve_selected_original_path(
                        root,
                        child_path.relative_to(root).as_posix(),
                        show_hidden,
                    )
                except (PermissionError, ValueError):
                    continue
                delete_file(child_path)

            for child_path in sorted(
                (path for path in target_path.rglob("*") if path.is_dir() and not path.is_symlink()),
                reverse=True,
            ):
                try:
                    child_path.rmdir()
                except OSError:
                    continue
                deleted_dirs.append(child_path)

            try:
                target_path.rmdir()
            except OSError:
                continue
            deleted_dirs.append(target_path)
        else:
            delete_file(target_path)

    return {
        "deleted_files": len(deleted_files),
        "deleted_dirs": len(deleted_dirs),
    }


def path_to_url(path: Path) -> str:
    return "/" + quote(path.as_posix(), safe="/")


# Network and rendering helpers for the startup output and single-page browser UI.
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
    checkbox_value = html.escape(relative_path.as_posix(), quote=True)
    display_name = html.escape(relative_path.name)
    checkbox_label = html.escape(f"Select {relative_path.as_posix()}", quote=True)
    modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))

    return (
        "<div class=\"file-row\">"
        f"<input class=\"tree-check file-check\" type=\"checkbox\" value=\"{checkbox_value}\" aria-label=\"{checkbox_label}\">"
        f"<a class=\"file-name\" href=\"{file_url}\">{display_name}</a>"
        f"<span class=\"file-meta\">{format_size(stat.st_size)}</span>"
        f"<span class=\"file-meta\">{modified}</span>"
        f"<a class=\"button small\" href=\"{file_url}\" download>Download</a>"
        "</div>"
    )


def render_tree_node(root_dir: Path, node: dict, current_path: Path = Path()) -> str:
    parts = []

    for dirname, child in sorted(node["dirs"].items(), key=lambda item: item[0].lower()):
        folder_path = current_path / dirname
        checkbox_value = html.escape(folder_path.as_posix(), quote=True)
        folder_name = html.escape(dirname)
        checkbox_label = html.escape(f"Select {folder_path.as_posix()}", quote=True)
        file_count = tree_file_count(child)
        parts.append(
            "<details class=\"folder\">"
            "<summary>"
            "<span class=\"arrow\">&gt;</span>"
            f"<input class=\"tree-check folder-check\" type=\"checkbox\" value=\"{checkbox_value}\" aria-label=\"{checkbox_label}\">"
            f"<span class=\"folder-name\">{folder_name}</span>"
            f"<span class=\"folder-count\">{file_count}</span>"
            "</summary>"
            f"<div class=\"children\">{render_tree_node(root_dir, child, folder_path)}</div>"
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
    command_timeout: int | None = 30,
    stop_after: int | None = None,
    show_hidden: bool = False,
) -> bytes:
    """Build the complete browser UI as one self-contained HTML document."""
    root = upload_dir.resolve()
    files = iter_shared_files(root, show_hidden)
    file_tree = render_file_tree(root, files)
    overwrite_text = "Overwrite" if overwrite_uploads else "Rename"
    max_size_value = html.escape(setting_size_value(max_upload_size), quote=True)
    command_timeout_value = html.escape(setting_duration_value(command_timeout), quote=True)
    stop_after_value = html.escape(setting_duration_value(stop_after), quote=True)
    overwrite_pressed = str(overwrite_uploads).lower()
    hidden_text = "Visible" if show_hidden else "Hidden"
    hidden_pressed = str(show_hidden).lower()
    terminal_initial = f"$ pwd\n{html.escape(str(root))}"

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
      width: min(1280px, calc(100% - 32px));
      margin: 32px auto;
    }}
    h2 {{
      margin: 0;
      font-weight: 700;
      letter-spacing: 0;
    }}
    h2 {{ font-size: 18px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      background: var(--panel);
      white-space: nowrap;
    }}
    .toggle-group {{
      display: inline-flex;
      gap: 6px;
    }}
    .pill-button {{
      background: var(--accent);
      color: #fff;
      font: inherit;
      cursor: pointer;
    }}
    .pill-button:hover,
    .pill-button[aria-pressed="true"] {{
      border-color: var(--accent);
      background: var(--accent-strong);
      color: #fff;
    }}
    .pill-button:disabled {{
      cursor: not-allowed;
      opacity: .65;
    }}
    .notice {{
      margin: -4px 0 16px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--warn);
      border-radius: 8px;
      padding: 10px 12px;
      background: color-mix(in srgb, var(--warn) 8%, var(--panel));
      color: var(--text);
      font-size: 13px;
    }}
    .notice strong {{
      color: var(--warn);
    }}
    .settings-panel {{
      padding: 12px;
      margin-bottom: 16px;
    }}
    .settings-form {{
      display: grid;
      grid-template-columns: repeat(3, minmax(120px, 1fr)) auto;
      gap: 10px;
      align-items: end;
    }}
    .setting-field {{
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .setting-field input[type="text"] {{
      min-width: 0;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: transparent;
      color: var(--text);
      font: 14px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-weight: 400;
    }}
    .settings-status {{
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
      grid-column: 1 / -1;
    }}
    .settings-status.error {{
      color: var(--warn);
    }}
    .workbench {{
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 16px;
      align-items: stretch;
      margin-bottom: 24px;
    }}
    .examples-grid {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .command-example {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }}
    .command-example code {{
      min-height: 36px;
      display: flex;
      align-items: center;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: transparent;
      color: var(--text);
      font: 13px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      white-space: nowrap;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .command-presets {{
      padding: 12px;
    }}
    .command-presets .examples-grid {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
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
    .button:disabled {{
      border-color: var(--line);
      background: transparent;
      color: var(--muted);
      cursor: not-allowed;
    }}
    .button.secondary {{
      background: transparent;
      color: var(--accent);
    }}
    .button.small {{
      min-height: 30px;
      padding: 0 10px;
      font-size: 13px;
    }}
    .terminal-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .terminal {{
      height: 420px;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      overflow: hidden;
    }}
    .terminal-head {{
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 12px;
      border-bottom: 1px solid var(--line);
    }}
    .terminal-output {{
      margin: 0;
      min-height: 0;
      overflow: auto;
      padding: 12px;
      background: #10110f;
      color: #e8f0e8;
      font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .command-form {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      padding: 10px;
      border-top: 1px solid var(--line);
    }}
    .command-actions {{
      grid-column: 2;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .prompt {{
      color: var(--muted);
      font: 700 14px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
    }}
    .command-input {{
      min-width: 0;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: transparent;
      color: var(--text);
      font: 14px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
    }}
    .files-layout {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(0, 2fr);
      gap: 16px;
      align-items: start;
    }}
    .file-control-panel {{
      display: grid;
      gap: 12px;
      align-content: start;
      padding: 12px;
    }}
    .file-title {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }}
    .file-actions {{
      display: grid;
      gap: 8px;
    }}
    .file-actions .button {{
      width: 100%;
    }}
    .upload-feedback {{
      display: grid;
      gap: 6px;
    }}
    .file-list-panel {{
      min-width: 0;
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
      grid-template-columns: 24px 20px minmax(0, 1fr) auto;
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
    .tree-check {{
      width: 16px;
      height: 16px;
      margin: 0;
      accent-color: var(--accent);
    }}
    .children {{
      margin-left: 24px;
      border-left: 1px solid var(--line);
    }}
    .file-row {{
      min-height: 44px;
      display: grid;
      grid-template-columns: 20px minmax(0, 1fr) auto auto auto;
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
    #status:empty {{ display: none; }}
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
      main {{ width: min(100% - 20px, 1280px); margin-top: 18px; }}
      .workbench {{ grid-template-columns: minmax(0, 1fr); }}
      .terminal-grid {{ grid-template-columns: minmax(0, 1fr); }}
      .command-presets .examples-grid {{ grid-template-columns: minmax(0, 1fr); }}
      .command-example {{ grid-template-columns: minmax(0, 1fr); }}
      .settings-form {{ grid-template-columns: minmax(0, 1fr); }}
      .files-layout {{ grid-template-columns: minmax(0, 1fr); }}
      .file-title {{ align-items: flex-start; }}
      summary {{ grid-template-columns: 24px 20px minmax(0, 1fr); }}
      .folder-count {{ grid-column: 3; }}
      .file-row {{ grid-template-columns: 20px minmax(0, 1fr); align-items: start; gap: 6px; }}
      .file-meta, .file-row .button {{ grid-column: 2; justify-self: start; }}
      .file-meta {{ white-space: normal; }}
      .children {{ margin-left: 14px; }}
    }}
  </style>
</head>
<body data-max-upload-size="{max_upload_size or 0}">
  <main>
    <section class="notice" role="note">
      <strong>Personal local use only.</strong>
      Security is minimal: anyone who can reach this server can upload, download, delete,
      change settings, and run CLI commands. Use only on trusted networks and stop the
      server when finished.
    </section>

    <section class="panel settings-panel">
      <form id="settings-form" class="settings-form" onsubmit="return false">
        <label class="setting-field">
          Max size
          <input id="settings-max-size" type="text" value="{max_size_value}" placeholder="unlimited">
        </label>
        <label class="setting-field">
          Command timeout
          <input id="settings-command-timeout" type="text" value="{command_timeout_value}" placeholder="off">
        </label>
        <label class="setting-field">
          Stop after
          <input id="settings-stop-after" type="text" value="{stop_after_value}" placeholder="off">
        </label>
        <button id="settings-save" class="button" type="submit">Save</button>
        <div id="settings-status" class="settings-status"></div>
      </form>
    </section>

    <div class="workbench">
      <section class="panel command-presets">
        <h2>Commands</h2>
        <div class="examples-grid">
          <div class="command-example">
            <code id="command-list-selected"></code>
            <button class="button secondary small run-command-preset" type="button" data-command-target="command-list-selected">Run</button>
          </div>
          <div class="command-example">
            <code id="command-size-selected"></code>
            <button class="button secondary small run-command-preset" type="button" data-command-target="command-size-selected">Run</button>
          </div>
          <div class="command-example">
            <code id="command-stat-selected"></code>
            <button class="button secondary small run-command-preset" type="button" data-command-target="command-stat-selected">Run</button>
          </div>
        </div>
      </section>

      <div class="terminal-grid">
        <section class="panel terminal">
          <header class="terminal-head">
            <h2>CLI 1</h2>
            <span class="muted">shell</span>
          </header>
          <pre id="terminal-output" class="terminal-output">{terminal_initial}</pre>
          <form id="command-form" class="command-form" onsubmit="return false">
            <span class="prompt">$</span>
            <input id="command-input" class="command-input" type="text" autocomplete="off" spellcheck="false" aria-label="Command">
            <div class="command-actions">
              <button id="run-command" class="button run-command" type="submit">Run</button>
              <button id="clear-command" class="button secondary clear-command" type="button">Clear</button>
            </div>
          </form>
        </section>

        <section class="panel terminal">
          <header class="terminal-head">
            <h2>CLI 2</h2>
            <span class="muted">shell</span>
          </header>
          <pre id="terminal-output-2" class="terminal-output">{terminal_initial}</pre>
          <form id="command-form-2" class="command-form" onsubmit="return false">
            <span class="prompt">$</span>
            <input id="command-input-2" class="command-input" type="text" autocomplete="off" spellcheck="false" aria-label="Command">
            <div class="command-actions">
              <button id="run-command-2" class="button run-command" type="submit">Run</button>
              <button id="clear-command-2" class="button secondary clear-command" type="button">Clear</button>
            </div>
          </form>
        </section>
      </div>
    </div>

    <section class="files-layout">
      <aside class="panel file-control-panel">
        <div class="file-title">
          <h2>Files</h2>
          <span class="toggle-group">
            <button id="stat-overwrite" class="pill pill-button" type="button" data-enabled="{overwrite_pressed}" aria-pressed="{overwrite_pressed}">{overwrite_text}</button>
            <button id="stat-hidden" class="pill pill-button" type="button" data-visible="{hidden_pressed}" aria-pressed="{hidden_pressed}">{hidden_text}</button>
          </span>
          <button id="refresh-files" class="button secondary small" type="button">Refresh</button>
        </div>
        <div class="file-actions">
          <input id="file-picker" type="file" multiple hidden>
          <button id="choose-files" class="button" type="button">Choose Files</button>
          <button id="download-selected" class="button" type="button" disabled>Download Selected</button>
          <button id="delete-selected" class="button secondary" type="button" disabled>Delete Selected</button>
          <a class="button secondary" href="/download.zip">Download ZIP</a>
        </div>
        <div class="upload-feedback">
          <progress id="progress" value="0" max="100" hidden></progress>
          <p id="status"></p>
        </div>
      </aside>
      <div class="file-list-panel">
        {file_tree}
      </div>
    </section>
  </main>

  <script>
    const picker = document.getElementById("file-picker");
    const choose = document.getElementById("choose-files");
    const progress = document.getElementById("progress");
    const status = document.getElementById("status");
    let maxUploadSize = Number(document.body.dataset.maxUploadSize || "0");
    const selectedDownload = document.getElementById("download-selected");
    const deleteSelected = document.getElementById("delete-selected");
    const refreshFiles = document.getElementById("refresh-files");
    const commandListSelected = document.getElementById("command-list-selected");
    const commandSizeSelected = document.getElementById("command-size-selected");
    const commandStatSelected = document.getElementById("command-stat-selected");
    const runPresetButtons = Array.from(document.querySelectorAll(".run-command-preset"));
    const treeChecks = Array.from(document.querySelectorAll(".tree-check"));
    const settingsForm = document.getElementById("settings-form");
    const settingsMaxSize = document.getElementById("settings-max-size");
    const settingsCommandTimeout = document.getElementById("settings-command-timeout");
    const settingsStopAfter = document.getElementById("settings-stop-after");
    const settingsSave = document.getElementById("settings-save");
    const settingsStatus = document.getElementById("settings-status");
    const statOverwrite = document.getElementById("stat-overwrite");
    const statHidden = document.getElementById("stat-hidden");
    const terminalStates = Array.from(document.querySelectorAll(".terminal")).map(panel => ({{
      panel: panel,
      form: panel.querySelector(".command-form"),
      input: panel.querySelector(".command-input"),
      output: panel.querySelector(".terminal-output"),
      runButton: panel.querySelector(".run-command"),
      clearButton: panel.querySelector(".clear-command"),
      running: false
    }}));
    let activeTerminal = terminalStates[0];

    choose.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => uploadFiles(picker.files));
    selectedDownload.addEventListener("click", downloadSelected);
    deleteSelected.addEventListener("click", deleteSelectedFiles);
    refreshFiles.addEventListener("click", () => window.location.reload());
    settingsForm.addEventListener("submit", saveSettings);
    statOverwrite.addEventListener("click", toggleOverwriteMode);
    statHidden.addEventListener("click", toggleHiddenVisibility);
    updateCommandPresets();

    for (const terminal of terminalStates) {{
      terminal.panel.addEventListener("pointerdown", () => setActiveTerminal(terminal));
      terminal.input.addEventListener("focus", () => setActiveTerminal(terminal));
      terminal.form.addEventListener("submit", event => runCommand(event, terminal));
      terminal.clearButton.addEventListener("click", () => runClearCommand(terminal));
    }}

    for (const button of runPresetButtons) {{
      button.addEventListener("click", runCommandPreset);
    }}

    for (const check of treeChecks) {{
      check.addEventListener("click", event => event.stopPropagation());
      check.addEventListener("change", () => {{
        if (check.classList.contains("folder-check")) {{
          setDescendantChecks(check);
        }}

        updateAncestorChecks(check);
        updateSelectedDownload();
        updateCommandPresets();
      }});
    }}

    function setDescendantChecks(folderCheck) {{
      const folder = folderCheck.closest("details.folder");
      if (!folder) return;

      for (const check of folder.querySelectorAll(":scope > .children .tree-check")) {{
        check.checked = folderCheck.checked;
        check.indeterminate = false;
      }}
    }}

    function parentFolder(folder) {{
      if (!folder || !folder.parentElement) return null;
      return folder.parentElement.closest("details.folder");
    }}

    function updateAncestorChecks(changedCheck) {{
      let folder = changedCheck.closest("details.folder");

      if (changedCheck.classList.contains("folder-check")) {{
        folder = parentFolder(folder);
      }}

      while (folder) {{
        const folderCheck = folder.querySelector(":scope > summary > .folder-check");
        const childChecks = Array.from(folder.querySelectorAll(":scope > .children .tree-check"));
        const checkedCount = childChecks.filter(check => check.checked).length;
        const partialCount = childChecks.filter(check => check.indeterminate).length;

        folderCheck.checked = childChecks.length > 0 && checkedCount === childChecks.length;
        folderCheck.indeterminate = checkedCount > 0 && checkedCount < childChecks.length || partialCount > 0;
        folder = parentFolder(folder);
      }}
    }}

    function hasCheckedAncestorFolder(check) {{
      let folder = check.closest("details.folder");

      if (check.classList.contains("folder-check")) {{
        folder = parentFolder(folder);
      }}

      while (folder) {{
        const folderCheck = folder.querySelector(":scope > summary > .folder-check");
        if (folderCheck && folderCheck.checked) return true;
        folder = parentFolder(folder);
      }}

      return false;
    }}

    function selectedPaths() {{
      return treeChecks
        .filter(check => check.checked && !hasCheckedAncestorFolder(check))
        .map(check => check.value);
    }}

    function updateSelectedDownload() {{
      const nothingSelected = selectedPaths().length === 0;
      selectedDownload.disabled = nothingSelected;
      deleteSelected.disabled = nothingSelected;
    }}

    function downloadSelected() {{
      const paths = selectedPaths();
      if (!paths.length) return;

      const query = new URLSearchParams();
      for (const path of paths) {{
        query.append("path", path);
      }}

      window.location.href = "/download.zip?" + query.toString();
    }}

    async function deleteSelectedFiles() {{
      const paths = selectedPaths();
      if (!paths.length) return;

      const label = paths.length === 1 ? paths[0] : `${{paths.length}} selected items`;
      if (!window.confirm(`Delete ${{label}}?`)) return;

      deleteSelected.disabled = true;
      try {{
        const response = await fetch("/delete", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json"
          }},
          body: JSON.stringify({{ paths: paths }})
        }});
        const result = await response.json();

        if (!response.ok) {{
          window.alert(result.error || "Delete failed");
          return;
        }}

        window.location.reload();
      }} catch (error) {{
        window.alert(error.message);
      }} finally {{
        updateSelectedDownload();
      }}
    }}

    function shellQuote(path) {{
      return "'" + path.split("'").join("'\\\"'\\\"'") + "'";
    }}

    function selectedCommandArgs() {{
      const paths = selectedPaths();
      if (!paths.length) return ".";
      return paths.map(shellQuote).join(" ");
    }}

    function updateCommandPresets() {{
      const args = selectedCommandArgs();
      commandListSelected.textContent = `ls -lah -- ${{args}}`;
      commandSizeSelected.textContent = `du -sh -- ${{args}}`;
      commandStatSelected.textContent = `stat -- ${{args}}`;
    }}

    async function runCommandPreset(event) {{
      const targetId = event.currentTarget.dataset.commandTarget;
      const target = document.getElementById(targetId);
      await executeCommand(target ? target.textContent.trim() : "", activeTerminal);
    }}

    function setActiveTerminal(terminal) {{
      activeTerminal = terminal;
    }}

    function setCommandRunning(terminal, isRunning) {{
      terminal.running = isRunning;
      terminal.runButton.disabled = isRunning;
      terminal.clearButton.disabled = isRunning;

      const anyRunning = terminalStates.some(state => state.running);
      for (const button of runPresetButtons) {{
        button.disabled = anyRunning;
      }}
    }}

    function appendTerminal(text, terminal) {{
      terminal.output.textContent += text;
      terminal.output.scrollTop = terminal.output.scrollHeight;
    }}

    function setSettingsRunning(isRunning) {{
      settingsSave.disabled = isRunning;
      statOverwrite.disabled = isRunning;
      statHidden.disabled = isRunning;
    }}

    function applySettings(settings) {{
      maxUploadSize = settings.max_upload_size || 0;
      document.body.dataset.maxUploadSize = String(maxUploadSize);
      settingsMaxSize.value = settings.max_size || "";
      settingsCommandTimeout.value = settings.command_timeout || "";
      settingsStopAfter.value = settings.stop_after || "";
      statOverwrite.textContent = settings.overwrite_label;
      statOverwrite.dataset.enabled = String(Boolean(settings.overwrite));
      statOverwrite.setAttribute("aria-pressed", String(Boolean(settings.overwrite)));
      statHidden.textContent = settings.show_hidden_label;
      statHidden.dataset.visible = String(Boolean(settings.show_hidden));
      statHidden.setAttribute("aria-pressed", String(Boolean(settings.show_hidden)));
    }}

    async function postSettings(updates) {{
      setSettingsRunning(true);
      settingsStatus.className = "settings-status";
      settingsStatus.textContent = "Saving";

      try {{
        const response = await fetch("/settings", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json"
          }},
          body: JSON.stringify(updates)
        }});
        const result = await response.json();

        if (!response.ok) {{
          settingsStatus.className = "settings-status error";
          settingsStatus.textContent = result.error || "Settings failed";
          return;
        }}

        applySettings(result);
        settingsStatus.textContent = "Saved";
        return result;
      }} catch (error) {{
        settingsStatus.className = "settings-status error";
        settingsStatus.textContent = error.message;
        return null;
      }} finally {{
        setSettingsRunning(false);
      }}
    }}

    async function saveSettings(event) {{
      event.preventDefault();
      await postSettings({{
        max_size: settingsMaxSize.value.trim(),
        command_timeout: settingsCommandTimeout.value.trim(),
        stop_after: settingsStopAfter.value.trim()
      }});
    }}

    async function toggleOverwriteMode() {{
      await postSettings({{ overwrite: statOverwrite.dataset.enabled !== "true" }});
    }}

    async function toggleHiddenVisibility() {{
      const result = await postSettings({{ show_hidden: statHidden.dataset.visible !== "true" }});
      if (result) {{
        window.location.reload();
      }}
    }}

    async function runCommand(event, terminal = activeTerminal) {{
      event.preventDefault();
      const command = terminal.input.value.trim();
      terminal.input.value = "";
      await executeCommand(command, terminal);
    }}

    async function runClearCommand(terminal = activeTerminal) {{
      await executeCommand("clear", terminal);
    }}

    async function executeCommand(command, terminal = activeTerminal) {{
      if (!command) return;

      if (command === "clear") {{
        terminal.output.textContent = "";
        terminal.input.focus();
        return;
      }}

      setCommandRunning(terminal, true);
      appendTerminal(`\\n$ ${{command}}\\n`, terminal);

      try {{
        const response = await fetch("/run-command", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json"
          }},
          body: JSON.stringify({{ command: command }})
        }});
        const result = await response.json();

        if (!response.ok) {{
          appendTerminal(`${{result.error || "Command failed"}}\\n`, terminal);
          return;
        }}

        if (result.stdout) appendTerminal(result.stdout, terminal);
        if (result.stderr) appendTerminal(result.stderr, terminal);
        if (!result.stdout && !result.stderr && result.returncode === 0) appendTerminal("exit 0\\n", terminal);
        if (result.returncode !== 0) appendTerminal(`[exit ${{result.returncode}}]\\n`, terminal);
      }} catch (error) {{
        appendTerminal(`${{error.message}}\\n`, terminal);
      }} finally {{
        setCommandRunning(terminal, false);
        terminal.input.focus();
      }}
    }}

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
    """HTTP endpoints for browsing, uploading, zipping, deleting, and CLI."""

    server_version = "UploadHTTP/0.2"
    upload_dir: Path
    runtime_settings: RuntimeSettings

    def do_GET(self) -> None:
        request_url = urlsplit(self.path)
        path = request_url.path
        settings = self.runtime_settings.snapshot()

        if path in {"/", "/index.html"}:
            self.send_index_page()
            return

        if path == "/settings":
            self.send_json(settings_to_json(self.runtime_settings))
            return

        if path == "/download.zip":
            selected_paths = parse_qs(request_url.query).get("path")
            self.send_zip_archive(selected_paths)
            return

        # Let SimpleHTTPRequestHandler stream regular files after the same path
        # validation used by ZIP and delete operations.
        try:
            requested_path = resolve_request_path(
                self.upload_dir,
                self.path,
                settings["show_hidden"],
            )
        except PermissionError as exc:
            self.send_error(403, str(exc))
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except OSError:
            requested_path = None

        if requested_path and requested_path.is_dir():
            self.send_error(403, "directory listing is disabled")
            return

        super().do_GET()

        if requested_path and requested_path.is_file():
            relative_path = requested_path.relative_to(self.upload_dir.resolve()).as_posix()
            self.log_event(
                f"Downloaded {relative_path} ({format_size(requested_path.stat().st_size)})"
            )

    def do_PUT(self) -> None:
        settings = self.runtime_settings.snapshot()

        try:
            requested_path = resolve_upload_path(
                self.upload_dir,
                self.path,
                settings["show_hidden"],
            )
        except PermissionError as exc:
            self.send_error(403, str(exc))
            return
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

        max_upload_size = settings["max_upload_size"]
        if max_upload_size is not None and remaining > max_upload_size:
            self.send_error(413, f"Upload limit is {format_size(max_upload_size)}")
            return

        try:
            target_path, upload_file = open_upload_target(
                requested_path,
                settings["overwrite_uploads"],
            )
        except OSError as exc:
            self.send_error(500, f"Could not open upload target: {exc}")
            return

        with upload_file:
            while remaining:
                # Stream large uploads in bounded chunks instead of loading the
                # whole request body into memory.
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

    def do_POST(self) -> None:
        path = urlsplit(self.path).path

        if path == "/run-command":
            self.run_command()
            return

        if path == "/settings":
            self.update_settings()
            return

        if path == "/delete":
            self.delete_selected()
            return

        self.send_error(404, "Not found")

    def run_command(self) -> None:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json({"error": "Content-Length header is required"}, status=411)
            return

        try:
            body_size = int(content_length)
        except ValueError:
            self.send_json({"error": "Invalid Content-Length header"}, status=400)
            return

        if body_size < 0 or body_size > MAX_COMMAND_BODY_SIZE:
            self.send_json({"error": "Command request is too large"}, status=413)
            return

        try:
            payload = json.loads(self.rfile.read(body_size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, status=400)
            return

        command = payload.get("command") if isinstance(payload, dict) else None
        if not isinstance(command, str):
            self.send_json({"error": "Missing command"}, status=400)
            return

        try:
            settings = self.runtime_settings.snapshot()
            result = run_shell_command(command, self.upload_dir, settings["command_timeout"])
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return

        self.send_json(result)
        self.log_event(
            f"Ran command {command!r} "
            f"(exit {result['returncode']}, {result['elapsed']:.3f}s)"
        )

    def update_settings(self) -> None:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json({"error": "Content-Length header is required"}, status=411)
            return

        try:
            body_size = int(content_length)
        except ValueError:
            self.send_json({"error": "Invalid Content-Length header"}, status=400)
            return

        if body_size < 0 or body_size > MAX_COMMAND_BODY_SIZE:
            self.send_json({"error": "Settings request is too large"}, status=413)
            return

        try:
            payload = json.loads(self.rfile.read(body_size).decode("utf-8"))
            updates = parse_settings_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, status=400)
            return
        except (ValueError, argparse.ArgumentTypeError) as exc:
            self.send_json({"error": str(exc)}, status=400)
            return

        self.runtime_settings.apply_updates(self.server, updates)
        settings_payload = settings_to_json(self.runtime_settings)
        self.send_json(settings_payload)
        self.log_event(
            "Updated settings "
            f"(limit {settings_payload['max_size_label']}, "
            f"CLI {settings_payload['command_timeout_label']}, "
            f"stop {settings_payload['stop_after_label']}, "
            f"{settings_payload['overwrite_label']})"
        )

    def delete_selected(self) -> None:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json({"error": "Content-Length header is required"}, status=411)
            return

        try:
            body_size = int(content_length)
        except ValueError:
            self.send_json({"error": "Invalid Content-Length header"}, status=400)
            return

        if body_size < 0 or body_size > MAX_COMMAND_BODY_SIZE:
            self.send_json({"error": "Delete request is too large"}, status=413)
            return

        try:
            payload = json.loads(self.rfile.read(body_size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, status=400)
            return

        paths = payload.get("paths") if isinstance(payload, dict) else None
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            self.send_json({"error": "Missing selected paths"}, status=400)
            return

        settings = self.runtime_settings.snapshot()
        try:
            result = delete_selected_paths(self.upload_dir, paths, settings["show_hidden"])
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, status=403)
            return
        except FileNotFoundError as exc:
            self.send_json({"error": f"Selected path not found: {exc}"}, status=404)
            return
        except OSError as exc:
            self.send_json({"error": f"Delete failed: {exc}"}, status=500)
            return

        self.send_json(result)
        self.log_event(
            f"Deleted selected paths "
            f"({result['deleted_files']} files, {result['deleted_dirs']} folders)"
        )

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_index_page(self) -> None:
        settings = self.runtime_settings.snapshot()
        body = build_index_html(
            self.upload_dir,
            settings["max_upload_size"],
            settings["overwrite_uploads"],
            settings["command_timeout"],
            settings["stop_after"],
            settings["show_hidden"],
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_zip_archive(self, selected_paths: list[str] | None = None) -> None:
        root = self.upload_dir.resolve()
        settings = self.runtime_settings.snapshot()
        try:
            files = files_for_zip(root, selected_paths, settings["show_hidden"])
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except FileNotFoundError as exc:
            self.send_error(404, f"Selected path not found: {exc}")
            return

        total_size = 0
        archive_name = "selected-files.zip" if selected_paths else "shared-files.zip"

        # Build the archive in a temporary file so Content-Length is known before
        # streaming it back to the browser.
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
            self.send_header("Content-Disposition", f'attachment; filename="{archive_name}"')
            self.send_header("Content-Length", str(archive_size))
            self.end_headers()
            shutil.copyfileobj(archive, self.wfile)

        zip_kind = "selected ZIP" if selected_paths else "ZIP"
        self.log_event(f"Downloaded {zip_kind} ({len(files)} files, {format_size(total_size)})")

    def log_event(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} {message}")

    def log_message(self, format: str, *args) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} {format % args}")


def make_handler(
    upload_dir: Path,
    max_upload_size: int | None = None,
    overwrite_uploads: bool = False,
    command_timeout: int | None = 30,
    stop_after: int | None = None,
    show_hidden: bool = False,
    runtime_settings: RuntimeSettings | None = None,
) -> type[UploadHandler]:
    """Bind one configured upload directory/settings object to the handler class."""
    upload_dir = upload_dir.resolve()
    if runtime_settings is None:
        runtime_settings = RuntimeSettings(
            max_upload_size,
            overwrite_uploads,
            command_timeout,
            stop_after,
            show_hidden,
        )

    class ConfiguredUploadHandler(UploadHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(upload_dir), **kwargs)

    ConfiguredUploadHandler.upload_dir = upload_dir
    ConfiguredUploadHandler.runtime_settings = runtime_settings
    return ConfiguredUploadHandler


# Command-line entry point and startup messages.
def print_useful_options() -> None:
    print("Useful options:")
    print("  --max-size 500MB    Reject uploads larger than this size")
    print("  --stop-after 30m    Stop automatically after a short session")
    print("  --command-timeout 30s  Stop long browser CLI commands")
    print("  --port 9000         Use a different port")
    print("  --host 127.0.0.1    Listen only on this computer")
    print("  --help              Show all options")


def run_server(
    host: str,
    port: int,
    max_upload_size: int | None,
    stop_after: int | None,
    command_timeout: int | None,
) -> None:
    upload_dir = Path(".").resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    runtime_settings = RuntimeSettings(
        max_upload_size,
        False,
        command_timeout,
        stop_after,
        False,
    )
    handler_class = make_handler(upload_dir, runtime_settings=runtime_settings)

    with ThreadingHTTPServer((host, port), handler_class) as server:
        actual_host, actual_port = server.server_address[:2]
        runtime_settings.start_auto_stop(server)

        print(f"Serving directory: {upload_dir.resolve()}")
        print(f"Upload limit: {format_size(max_upload_size)}")
        print(f"Command timeout: {format_duration(command_timeout)}")
        if stop_after is not None:
            print(f"Auto-stop: {format_duration(stop_after)}")
        if host in {"", "0.0.0.0"}:
            print(
                "Warning: anyone on this network can upload, download, delete, "
                "change settings, and run CLI commands."
            )
        print("Open:")
        for url in server_urls(host if host else actual_host, actual_port):
            print(f"  {url}")
        print_useful_options()

        try:
            server.serve_forever()
        finally:
            runtime_settings.cancel_auto_stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a small HTTP upload server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
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
    parser.add_argument(
        "--command-timeout",
        type=parse_duration,
        default=30,
        help="Stop a browser CLI command after this duration. Use 0 to disable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        run_server(
            args.host,
            args.port,
            args.max_size,
            args.stop_after,
            args.command_timeout,
        )
    except KeyboardInterrupt:
        print("\nServer stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
