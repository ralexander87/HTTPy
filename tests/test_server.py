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
    files_for_zip,
    make_handler,
    open_upload_target,
    parse_duration,
    parse_size,
    resolve_upload_path,
    run_shell_command,
)


@contextmanager
def running_server(
    upload_dir: Path,
    max_upload_size: int | None = None,
    overwrite_uploads: bool = False,
    command_timeout: int | None = 30,
):
    handler = make_handler(upload_dir, max_upload_size, overwrite_uploads, command_timeout)
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


def test_run_shell_command_uses_shared_directory(tmp_path: Path) -> None:
    result = run_shell_command("printf hello > made.txt", tmp_path, timeout=5)

    assert result["returncode"] == 0
    assert (tmp_path / "made.txt").read_text(encoding="utf-8") == "hello"


def test_run_command_endpoint_returns_json_output(tmp_path: Path) -> None:
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
    assert 'id="download-selected"' in page
    assert 'id="command-form"' in page
    assert 'id="terminal-output"' in page
