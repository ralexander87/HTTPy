from pathlib import Path

import pytest

from upload_server.server import resolve_upload_path


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
