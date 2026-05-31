from pathlib import Path
import http.client
import io
import json
import threading
import zipfile
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from upload_server.server import (
    build_parser,
    build_index_html,
    command_output_to_text,
    delete_selected_paths,
    files_for_zip,
    iter_shared_files,
    log_file_path,
    make_handler,
    open_upload_target,
    parse_duration,
    parse_size,
    resolve_request_path,
    resolve_upload_path,
    run_shell_command,
    load_command_presets,
    save_command_presets,
)


@contextmanager
def running_server(
    upload_dir: Path,
    max_upload_size: int | None = None,
    overwrite_uploads: bool = False,
    command_timeout: int | None = 30,
    show_hidden: bool = False,
):
    handler = make_handler(
        upload_dir,
        max_upload_size,
        overwrite_uploads,
        command_timeout,
        show_hidden=show_hidden,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_resolve_upload_path_accepts_simple_file(tmp_path: Path) -> None:
    assert resolve_upload_path(tmp_path, "/example.txt") == tmp_path / "example.txt"


def test_resolve_upload_path_accepts_nested_file(tmp_path: Path) -> None:
    assert resolve_upload_path(tmp_path, "/folder/example.txt") == tmp_path / "folder/example.txt"


def test_resolve_upload_path_rejects_empty_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing filename"):
        resolve_upload_path(tmp_path, "/")


def test_resolve_upload_path_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid upload path"):
        resolve_upload_path(tmp_path, "/../secret.txt")


def test_parse_size_accepts_human_units() -> None:
    assert parse_size("500MB") == 500 * 1024 * 1024
    assert parse_size("2G") == 2 * 1024 * 1024 * 1024
    assert parse_size("0") is None


def test_parse_duration_accepts_human_units() -> None:
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("2h") == 2 * 60 * 60
    assert parse_duration("0") is None


def test_startup_parser_leaves_browser_only_settings_out_of_cli() -> None:
    help_text = build_parser().format_help()

    assert "--upload-dir" not in help_text
    assert "--overwrite" not in help_text
    assert "--show-hidden" not in help_text
    assert "--max-size" in help_text


def test_open_upload_target_renames_existing_file(tmp_path: Path) -> None:
    existing = tmp_path / "example.txt"
    existing.write_text("old", encoding="utf-8")

    target_path, upload_file = open_upload_target(existing, overwrite=False)
    with upload_file:
        upload_file.write(b"new")

    assert target_path == tmp_path / "example-1.txt"
    assert existing.read_text(encoding="utf-8") == "old"
    assert target_path.read_text(encoding="utf-8") == "new"


def test_put_upload_renames_duplicate(tmp_path: Path) -> None:
    with running_server(tmp_path) as (host, port):
        for content in (b"first", b"second"):
            connection = http.client.HTTPConnection(host, port)
            connection.request(
                "PUT",
                "/example.txt",
                body=content,
                headers={"Content-Length": str(len(content))},
            )
            response = connection.getresponse()
            assert response.status == 201
            response.read()
            connection.close()

    assert (tmp_path / "example.txt").read_bytes() == b"first"
    assert (tmp_path / "example-1.txt").read_bytes() == b"second"


def test_put_rejects_file_larger_than_limit(tmp_path: Path) -> None:
    with running_server(tmp_path, max_upload_size=3) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "PUT",
            "/too-large.txt",
            body=b"four",
            headers={"Content-Length": "4"},
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        connection.close()

    assert not (tmp_path / "too-large.txt").exists()


def test_download_zip_contains_shared_files(tmp_path: Path) -> None:
    (tmp_path / "alpha.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "beta.txt").write_text("beta", encoding="utf-8")

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/download.zip")
        response = connection.getresponse()
        assert response.status == 200
        archive_bytes = response.read()
        connection.close()

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert sorted(archive.namelist()) == ["alpha.txt", "nested/beta.txt"]


def test_download_zip_can_include_only_selected_folder(tmp_path: Path) -> None:
    (tmp_path / "alpha.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "beta.txt").write_text("beta", encoding="utf-8")

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/download.zip?path=nested")
        response = connection.getresponse()
        assert response.status == 200
        archive_bytes = response.read()
        connection.close()

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert archive.namelist() == ["nested/beta.txt"]


def test_files_for_zip_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid selected path"):
        files_for_zip(tmp_path, ["../secret.txt"])


def test_delete_selected_paths_removes_selected_files_and_folders(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "remove.txt").write_text("remove", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "nested").mkdir()
    (tmp_path / "folder" / "nested" / "note.txt").write_text("note", encoding="utf-8")

    result = delete_selected_paths(tmp_path, ["remove.txt", "folder"])

    assert result == {"deleted_files": 2, "deleted_dirs": 2}
    assert (tmp_path / "keep.txt").exists()
    assert not (tmp_path / "remove.txt").exists()
    assert not (tmp_path / "folder").exists()


def test_delete_selected_paths_blocks_hidden_by_default(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError, match="hidden paths"):
        delete_selected_paths(tmp_path, [".env"])

    assert (tmp_path / ".env").exists()


def test_hidden_files_are_filtered_by_default(tmp_path: Path) -> None:
    (tmp_path / "public.txt").write_text("public", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "activate").write_text("not hidden", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "module.pyc").write_bytes(b"secret")

    visible_paths = {path.relative_to(tmp_path) for path in iter_shared_files(tmp_path)}

    assert visible_paths == {
        Path("__pycache__/module.pyc"),
        Path("public.txt"),
        Path("venv/activate"),
    }

    with pytest.raises(PermissionError, match="hidden paths"):
        resolve_request_path(tmp_path, "/.env")


def test_show_hidden_allows_hidden_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    assert iter_shared_files(tmp_path, show_hidden=True) == [(tmp_path / ".env").resolve()]
    assert resolve_request_path(tmp_path, "/.env", show_hidden=True) == (tmp_path / ".env").resolve()


def test_hidden_file_get_is_blocked_by_default(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/.env")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()


def test_directory_listing_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "file.txt").write_text("file", encoding="utf-8")

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/folder/")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()


def test_symlink_escape_is_not_shared(tmp_path: Path) -> None:
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    secret_file = secret_dir / "secret.txt"
    secret_file.write_text("secret", encoding="utf-8")

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    link = shared_dir / "link.txt"
    link.symlink_to(secret_file)

    assert iter_shared_files(shared_dir) == []

    with pytest.raises(ValueError, match="path escapes shared directory"):
        resolve_request_path(shared_dir, "/link.txt")


def test_run_shell_command_uses_shared_directory(tmp_path: Path) -> None:
    result = run_shell_command("printf hello > made.txt", tmp_path, timeout=5)

    assert result["returncode"] == 0
    assert (tmp_path / "made.txt").read_text(encoding="utf-8") == "hello"


def test_command_output_to_text_decodes_bytes_and_strips_ansi() -> None:
    assert command_output_to_text(b"\x1b[H\x1b[2Jdone\r\n") == "done\n"


def test_run_shell_command_strips_ansi_sequences(tmp_path: Path) -> None:
    result = run_shell_command("printf '\\033[H\\033[2Jdone'", tmp_path, timeout=5)

    assert result["returncode"] == 0
    assert result["stdout"] == "done"


def test_run_shell_command_timeout_output_is_json_safe(tmp_path: Path) -> None:
    result = run_shell_command(
        "python3 -c 'import time; print(\"start\"); time.sleep(1)'",
        tmp_path,
        timeout=0.1,
    )

    json.dumps(result)
    assert result["returncode"] == 124
    assert isinstance(result["stdout"], str)
    assert "Command timed out" in result["stderr"]


def test_run_command_endpoint_is_always_available(tmp_path: Path) -> None:
    command = json.dumps({"command": "printf endpoint"})

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "POST",
            "/run-command",
            body=command,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(command)),
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

    assert payload["returncode"] == 0
    assert payload["stdout"] == "endpoint"


def test_activity_log_file_records_server_actions(tmp_path: Path) -> None:
    def request(
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(host, port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    with running_server(tmp_path) as (host, port):
        log_path = log_file_path(tmp_path)
        assert log_path.exists()

        status, _ = request(
            host,
            port,
            "PUT",
            "/example.txt",
            body=b"hello",
            headers={"Content-Length": "5"},
        )
        assert status == 201

        status, data = request(host, port, "GET", "/example.txt")
        assert status == 200
        assert data == b"hello"

        command = json.dumps({"command": "printf log"})
        status, _ = request(
            host,
            port,
            "POST",
            "/run-command",
            body=command,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(command)),
            },
        )
        assert status == 200

        settings = json.dumps({"max_size": "1MB", "overwrite": True})
        status, _ = request(
            host,
            port,
            "POST",
            "/settings",
            body=settings,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(settings)),
            },
        )
        assert status == 200

        delete_body = json.dumps({"paths": ["example.txt"]})
        status, _ = request(
            host,
            port,
            "POST",
            "/delete",
            body=delete_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(delete_body)),
            },
        )
        assert status == 200

        bad_command = json.dumps({})
        status, _ = request(
            host,
            port,
            "POST",
            "/run-command",
            body=bad_command,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(bad_command)),
            },
        )
        assert status == 400

    log_text = log_file_path(tmp_path).read_text(encoding="utf-8")
    assert "127.0.0.1 Uploaded example.txt (5 B)" in log_text
    assert "127.0.0.1 Downloaded example.txt (5 B)" in log_text
    assert "127.0.0.1 Ran command 'printf log'" in log_text
    assert "Updated settings (limit 1.0 MB" in log_text
    assert "Overwrite" in log_text
    assert "Hidden" in log_text
    assert "Log enabled" in log_text
    assert "Deleted selected paths (1 files, 0 folders; requested: example.txt)" in log_text
    assert "Rejected POST /run-command (400 Missing command)" in log_text


def test_logging_can_be_toggled_without_restart(tmp_path: Path) -> None:
    def request(
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(host, port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    with running_server(tmp_path) as (host, port):
        settings = json.dumps({"logging": False})
        status, data = request(
            host,
            port,
            "POST",
            "/settings",
            body=settings,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(settings)),
            },
        )
        assert status == 200
        payload = json.loads(data.decode("utf-8"))
        assert payload["logging"] is False
        assert payload["logging_label"] == "No Log"
        assert payload["logging_status_label"] == "Log disabled"

        status, _ = request(
            host,
            port,
            "PUT",
            "/quiet.txt",
            body=b"quiet",
            headers={"Content-Length": "5"},
        )
        assert status == 201

        settings = json.dumps({"logging": True})
        status, data = request(
            host,
            port,
            "POST",
            "/settings",
            body=settings,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(settings)),
            },
        )
        assert status == 200
        payload = json.loads(data.decode("utf-8"))
        assert payload["logging"] is True
        assert payload["logging_label"] == "Log"
        assert payload["logging_status_label"] == "Log enabled"

        status, _ = request(
            host,
            port,
            "PUT",
            "/loud.txt",
            body=b"loud",
            headers={"Content-Length": "4"},
        )
        assert status == 201

    log_text = log_file_path(tmp_path).read_text(encoding="utf-8")
    assert "Log disabled" in log_text
    assert "Uploaded quiet.txt" not in log_text
    assert "Log enabled" in log_text
    assert "Uploaded loud.txt (4 B)" in log_text


def test_command_presets_default_empty_and_persist(tmp_path: Path) -> None:
    assert load_command_presets(tmp_path) == ["", "", ""]

    saved = save_command_presets(tmp_path, ["pwd", "ls -lah", "whoami"])

    assert saved == ["pwd", "ls -lah", "whoami"]
    assert load_command_presets(tmp_path) == saved
    assert (tmp_path / ".upload_server_commands.json").exists()


def test_command_presets_endpoint_persists_commands(tmp_path: Path) -> None:
    request_body = json.dumps({"commands": ["pwd", "ls -lah", "whoami"]})

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "POST",
            "/command-presets",
            body=request_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(request_body)),
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

    assert payload == {"commands": ["pwd", "ls -lah", "whoami"]}
    assert load_command_presets(tmp_path) == ["pwd", "ls -lah", "whoami"]


def test_index_loads_saved_command_presets(tmp_path: Path) -> None:
    save_command_presets(tmp_path, ['echo "one"', "pwd", "whoami"])

    page = build_index_html(tmp_path, max_upload_size=None, overwrite_uploads=False).decode()

    assert 'value="echo &quot;one&quot;"' in page
    assert 'value="pwd"' in page
    assert 'value="whoami"' in page


def test_settings_endpoint_updates_upload_limit_without_restart(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    settings = json.dumps(
        {
            "max_size": "3",
            "command_timeout": "1s",
            "stop_after": "0",
            "overwrite": True,
            "show_hidden": True,
        }
    )

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "POST",
            "/settings",
            body=settings,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(settings)),
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        assert payload["max_upload_size"] == 3
        assert payload["command_timeout_seconds"] == 1
        assert payload["stop_after_seconds"] is None
        assert payload["overwrite"] is True
        assert payload["show_hidden"] is True
        assert payload["show_hidden_label"] == "Visible"

        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/.env")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"secret"
        connection.close()

        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "PUT",
            "/too-large.txt",
            body=b"four",
            headers={"Content-Length": "4"},
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        connection.close()

    assert not (tmp_path / "too-large.txt").exists()


def test_settings_endpoint_rejects_invalid_size(tmp_path: Path) -> None:
    settings = json.dumps({"max_size": "huge-ish"})

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "POST",
            "/settings",
            body=settings,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(settings)),
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

    assert "Use a size" in payload["error"]


def test_delete_endpoint_removes_selected_paths(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "remove.txt").write_text("remove", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "note.txt").write_text("note", encoding="utf-8")
    request_body = json.dumps({"paths": ["remove.txt", "folder"]})

    with running_server(tmp_path) as (host, port):
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "POST",
            "/delete",
            body=request_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(request_body)),
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

    assert payload == {"deleted_files": 2, "deleted_dirs": 1}
    assert (tmp_path / "keep.txt").exists()
    assert not (tmp_path / "remove.txt").exists()
    assert not (tmp_path / "folder").exists()


def test_index_groups_nested_files_in_collapsible_folders(tmp_path: Path) -> None:
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.txt").write_text("note", encoding="utf-8")

    page = build_index_html(tmp_path, max_upload_size=None, overwrite_uploads=False).decode()

    assert '<details class="folder">' in page
    assert '<span class="arrow">&gt;</span>' in page
    assert '<input class="tree-check folder-check" type="checkbox" value="docs"' in page
    assert '<input class="tree-check file-check" type="checkbox" value="docs/note.txt"' in page
    assert '<span class="folder-name">docs</span>' in page
    assert '<a class="file-name" href="/docs/note.txt">note.txt</a>' in page
    assert '<a class="file-name" href="/root.txt">root.txt</a>' in page
    assert '<header class="top">' not in page
    assert '<h1>File Share</h1>' not in page
    assert f'<div class="muted">{tmp_path}</div>' not in page
    assert 'class="notice" role="note"' in page
    assert "Personal local use only." in page
    assert "Security is minimal" in page
    assert "upload, download, delete" in page
    assert "Use only on trusted networks" in page
    assert "width: min(1440px, calc(100% - 32px));" in page
    assert ".workbench {" in page
    assert "grid-template-columns: minmax(0, 1fr);" in page
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in page
    assert "height: 420px;" in page
    assert "grid-template-rows: auto minmax(0, 1fr) auto;" in page
    assert 'class="panel command-presets"' in page
    assert 'class="upload-feedback"' in page
    assert "Drop files here" not in page
    assert "drop-zone" not in page
    assert ".upload.dragover" not in page
    assert "examples-panel" not in page
    assert 'class="files-layout"' in page
    assert "grid-template-columns: minmax(220px, 1fr) minmax(0, 2fr);" in page
    assert 'class="panel file-control-panel"' in page
    assert 'class="file-list-panel"' in page
    assert ".file-actions {" in page
    assert "display: grid;" in page
    assert "grid-template-columns: minmax(280px, var(--terminal-left-width)) 16px minmax(280px, 1fr);" in page
    assert ".terminal-splitter {" in page
    assert ".terminal-grid.resizing," in page
    assert ".file-actions .button {" in page
    assert "min-width: 0;" in page
    assert "width: 100%;" in page
    assert 'class="file-title"' in page
    assert ".file-title .icon-button {" in page
    assert "margin-left: auto;" in page
    assert '<span class="pill">2 files</span>' not in page
    assert '<span class="pill">8 B</span>' not in page
    assert page.index('id="refresh-files"') < page.index('id="choose-files"')
    assert 'class="stats"' not in page
    assert 'id="stat-upload-limit"' not in page
    assert 'id="stat-command-timeout"' not in page
    assert 'id="stat-auto-stop"' not in page
    assert 'id="file-picker"' in page
    assert 'id="choose-files"' in page
    assert 'id="download-selected"' in page
    assert 'id="delete-selected"' in page
    assert 'id="refresh-files"' in page
    assert 'class="button secondary small icon-button"' in page
    assert 'aria-label="Refresh"' in page
    assert 'title="Refresh"' in page
    assert "&#x21bb;" in page
    assert ">Refresh</button>" not in page
    assert page.index('id="download-selected"') < page.index('id="choose-files"')
    assert page.index('id="choose-files"') < page.index('href="/download.zip"')
    assert page.index('href="/download.zip"') < page.index('id="delete-selected"')
    assert page.index('class="panel file-control-panel"') < page.index('id="choose-files"')
    assert page.index('href="/download.zip"') < page.index('class="file-list-panel"')
    assert page.index('class="file-list-panel"') < page.index('<details class="folder">')
    assert 'id="command-list-selected"' in page
    assert 'id="command-size-selected"' in page
    assert 'id="command-stat-selected"' in page
    assert (
        'id="command-list-selected" class="command-preset-input" type="text" value=""'
        in page
    )
    assert (
        'id="command-size-selected" class="command-preset-input" type="text" value=""'
        in page
    )
    assert (
        'id="command-stat-selected" class="command-preset-input" type="text" value=""'
        in page
    )
    assert '<code id="command-list-selected"' not in page
    assert 'class="command-preset-input"' in page
    assert 'value="ls -lah' not in page
    assert 'value="du -sh' not in page
    assert 'value="stat --' not in page
    assert "<h2>Commands</h2>" not in page
    command_panel_start = page.index('class="panel command-presets"')
    terminal_start = page.index('class="terminal-grid"')
    assert command_panel_start < page.index('id="command-list-selected"') < terminal_start
    assert 'class="button secondary small run-command-preset"' in page
    assert 'data-command-target="command-list-selected"' in page
    assert 'const commandPresetInputs = Array.from(document.querySelectorAll(".command-preset-input"));' in page
    assert 'input.addEventListener("input", scheduleSaveCommandPresets);' in page
    assert 'async function saveCommandPresets()' in page
    assert 'fetch("/command-presets"' in page
    assert 'copy-command' not in page
    assert 'id="settings-form"' in page
    assert 'id="settings-max-size"' in page
    assert 'id="settings-command-timeout"' in page
    assert 'id="settings-stop-after"' in page
    assert "grid-template-columns: max-content minmax(0, 1fr);" in page
    assert "settings-status" not in page
    assert ">Saving<" not in page
    assert ">Saved<" not in page
    assert 'id="settings-overwrite"' not in page
    assert 'class="toggle-group"' in page
    assert ".pill-button {" in page
    assert "background: var(--accent);" not in page
    assert "background: var(--accent-strong);" not in page
    assert ".button:hover {" in page
    assert "border-color: var(--accent-strong);" in page
    assert "color: var(--accent-strong);" in page
    assert 'id="stat-overwrite"' in page
    assert page.index('class="file-title"') < page.index('id="stat-overwrite"')
    assert 'data-enabled="false"' in page
    assert ">Rename</button>" in page
    assert 'id="stat-hidden"' in page
    assert page.index('id="stat-hidden"') < page.index('id="stat-log"')
    assert 'data-visible="false"' in page
    assert ">Hidden</button>" in page
    assert 'id="stat-log"' in page
    assert page.index('id="stat-log"') < page.index('id="refresh-files"')
    assert 'data-logging="true"' in page
    assert 'aria-label="Log enabled"' in page
    assert 'title="Log enabled"' in page
    assert ">Log</button>" in page
    assert 'class="terminal-grid"' in page
    assert 'id="terminal-grid"' in page
    assert 'id="terminal-splitter"' in page
    assert 'role="separator"' in page
    assert 'aria-orientation="vertical"' in page
    assert 'aria-label="Resize terminal panels"' in page
    assert 'class="terminal-title"' in page
    assert page.count('class="panel terminal"') == 2
    assert 'id="command-form"' in page
    assert 'id="command-form-2"' in page
    assert 'class="command-actions"' in page
    assert 'id="run-command"' in page
    assert 'id="run-command-2"' in page
    assert 'id="clear-command"' in page
    assert 'id="clear-command-2"' in page
    assert 'form="command-form"' in page
    assert 'form="command-form-2"' in page
    assert page.index('id="run-command"') < page.index('id="command-form"')
    assert page.index('id="run-command-2"') < page.index('id="command-form-2"')
    assert 'id="terminal-output"' in page
    assert 'id="terminal-output-2"' in page
    assert ">CLI 1</h2>" in page
    assert ">CLI 2</h2>" in page
    assert "CLI enabled" not in page
    assert "<span class=\"muted\">shell</span>" in page
    assert "$ pwd" in page
    assert "data-cli-enabled" not in page
    assert 'onsubmit="return false"' in page
    assert 'appendTerminal(`\\n$ ${command}\\n`, terminal);' in page
    assert 'appendTerminal("exit 0\\n", terminal);' in page
    assert 'statOverwrite.addEventListener("click", toggleOverwriteMode);' in page
    assert 'statHidden.addEventListener("click", toggleHiddenVisibility);' in page
    assert 'statLog.addEventListener("click", toggleLogging);' in page
    assert 'const terminalResizeMedia = window.matchMedia("(max-width: 720px)");' in page
    assert 'terminalSplitter.addEventListener("pointerdown", beginTerminalResize);' in page
    assert 'terminalSplitter.addEventListener("keydown", resizeTerminalWithKeyboard);' in page
    assert "function beginTerminalResize(event)" in page
    assert "function resizeTerminalWithKeyboard(event)" in page
    assert ".terminal-splitter { display: none; }" in page
    assert 'async function postSettings(updates)' in page
    assert 'async function toggleOverwriteMode()' in page
    assert 'async function toggleHiddenVisibility()' in page
    assert 'show_hidden: statHidden.dataset.visible !== "true"' in page
    assert 'async function toggleLogging()' in page
    assert 'logging: statLog.dataset.logging !== "true"' in page
    assert 'function shellQuote(path)' not in page
    assert 'commandListSelected.textContent' not in page
    assert 'commandSizeSelected.textContent' not in page
    assert 'commandStatSelected.textContent' not in page
    assert 'const command = target && "value" in target ? target.value.trim() : "";' in page
    assert 'async function executeCommand(command, terminal = activeTerminal)' in page
    assert 'terminal.clearButton.addEventListener("click", () => runClearCommand(terminal));' in page
    assert 'async function runClearCommand(terminal = activeTerminal)' in page
    assert 'await executeCommand("clear", terminal);' in page
    assert 'appendTerminal("\nCLI is disabled' not in page
    assert 'String.raw`Invoke-WebRequest' not in page
    assert 'command === "clear"' in page
    assert "?." not in page
