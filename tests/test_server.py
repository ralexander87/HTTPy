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
    build_index_html,
    command_output_to_text,
    delete_selected_paths,
    files_for_zip,
    iter_shared_files,
    make_handler,
    open_upload_target,
    parse_duration,
    parse_size,
    resolve_request_path,
    resolve_upload_path,
    run_shell_command,
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
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "module.pyc").write_bytes(b"secret")

    assert [path.name for path in iter_shared_files(tmp_path)] == ["public.txt"]

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


def test_settings_endpoint_updates_upload_limit_without_restart(tmp_path: Path) -> None:
    settings = json.dumps(
        {
            "max_size": "3",
            "command_timeout": "1s",
            "stop_after": "0",
            "overwrite": True,
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
    assert 'class="notice" role="note"' in page
    assert "Personal local use only." in page
    assert "Security is minimal" in page
    assert "upload, download, delete" in page
    assert "Use only on trusted networks" in page
    assert "grid-template-columns: minmax(220px, 1fr) minmax(0, 2fr);" in page
    assert "height: 420px;" in page
    assert "grid-template-rows: auto minmax(0, 1fr) auto;" in page
    assert 'id="download-selected"' in page
    assert 'id="delete-selected"' in page
    assert 'id="refresh-files"' in page
    assert 'id="command-list-selected"' in page
    assert 'id="command-size-selected"' in page
    assert 'id="command-stat-selected"' in page
    assert 'class="button secondary small run-command-preset"' in page
    assert 'data-command-target="command-list-selected"' in page
    assert 'copy-command' not in page
    assert 'id="settings-form"' in page
    assert 'id="settings-max-size"' in page
    assert 'id="settings-command-timeout"' in page
    assert 'id="settings-stop-after"' in page
    assert 'id="settings-overwrite"' in page
    assert 'id="command-form"' in page
    assert 'class="command-actions"' in page
    assert 'id="run-command"' in page
    assert 'id="clear-command"' in page
    assert 'id="terminal-output"' in page
    assert "CLI enabled" not in page
    assert "<span class=\"muted\">shell</span>" in page
    assert "$ pwd" in page
    assert "data-cli-enabled" not in page
    assert 'onsubmit="return false"' in page
    assert 'appendTerminal(`\\n$ ${command}\\n`);' in page
    assert 'appendTerminal("exit 0\\n");' in page
    assert 'function shellQuote(path)' in page
    assert 'path.split("\'").join("\'\\"\'\\"\'")' in page
    assert 'commandListSelected.textContent = `ls -lah -- ${args}`;' in page
    assert 'commandSizeSelected.textContent = `du -sh -- ${args}`;' in page
    assert 'commandStatSelected.textContent = `stat -- ${args}`;' in page
    assert 'async function executeCommand(command)' in page
    assert 'clearCommandButton.addEventListener("click", runClearCommand);' in page
    assert 'async function runClearCommand()' in page
    assert 'await executeCommand("clear");' in page
    assert 'appendTerminal("\nCLI is disabled' not in page
    assert 'String.raw`Invoke-WebRequest' not in page
    assert 'command === "clear"' in page
    assert "?." not in page
