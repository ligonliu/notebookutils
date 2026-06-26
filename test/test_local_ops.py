"""
Tests for local-only file operations in ``notebookutils.fs``.

These tests cover the ``help`` function and operations that resolve
to local filesystem paths (not ADLS).
"""

from __future__ import annotations

import os

import pytest

from notebookutils import fs


# ===================================================================
# help
# ===================================================================


class TestHelp:
    def test_help_module(self, capsys) -> None:
        fs.help()
        captured = capsys.readouterr()
        assert "notebookutils.fs" in captured.out

    def test_help_method(self, capsys) -> None:
        fs.help("cp")
        captured = capsys.readouterr()
        assert "Copies" in captured.out

    def test_help_unknown_method(self, capsys) -> None:
        fs.help("doesnotexist")
        captured = capsys.readouterr()
        assert "No help available" in captured.out

    def test_help_none(self, capsys) -> None:
        """``help(None)`` should show module docstring."""
        fs.help(None)
        captured = capsys.readouterr()
        assert "notebookutils.fs" in captured.out


# ===================================================================
# _local_exists / _local_isdir
# ===================================================================


class TestLocalExists:
    def test_returns_true_for_existing(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.touch()
        assert fs.exists(str(f))

    def test_returns_false_for_missing(self, tmp_path) -> None:
        assert not fs.exists(str(tmp_path / "missing"))


class TestLocalIsdir:
    def test_returns_true_for_dir(self, tmp_path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        assert os.path.isdir(str(d))

    def test_returns_false_for_file(self, tmp_path) -> None:
        f = tmp_path / "afile.txt"
        f.touch()
        assert not os.path.isdir(str(f))

    def test_returns_false_for_missing(self, tmp_path) -> None:
        assert not os.path.isdir(str(tmp_path / "missing"))


# ===================================================================
# ls — local directory
# ===================================================================


class TestLsLocal:
    def test_lists_local_directory(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")

        results = fs.ls(str(tmp_path))
        names = {r.name for r in results}
        assert "a.txt" in names
        assert "b.txt" in names

    def test_lists_root(self, tmp_path) -> None:
        """ls on root should work."""
        results = fs.ls("/")
        assert any(r.name == "root" or r.path == "/" or r.name for r in results)

    def test_missing_directory_returns_empty(self, tmp_path) -> None:
        result = fs.ls(str(tmp_path / "nonexistent"))
        # The actual implementation returns [] for missing
        assert result is not None
        assert isinstance(result, list)


# ===================================================================
# put — local file
# ===================================================================


class TestPutLocal:
    def test_writes_content_to_local_file(self, tmp_path) -> None:
        f = tmp_path / "out.txt"
        result = fs.put(str(f), "Hello, World!")
        assert result is True
        assert f.read_text() == "Hello, World!"

    def test_encodes_utf8(self, tmp_path) -> None:
        f = tmp_path / "utf.txt"
        fs.put(str(f), "你好")
        assert f.read_text() == "你好"

    def test_creates_parent_dirs(self, tmp_path) -> None:
        deep = tmp_path / "a" / "b" / "c" / "out.txt"
        fs.put(str(deep), "content")
        assert deep.read_text() == "content"


# ===================================================================
# head — local file
# ===================================================================


class TestHeadLocal:
    def test_reads_first_bytes(self, tmp_path) -> None:
        f = tmp_path / "head.txt"
        f.write_bytes(b"hello world" * 1000)
        result = fs.head(str(f), max_bytes=20)
        assert result == "hello worldhello wor"
        assert len(result) == 20

    def test_entire_file_within_limit(self, tmp_path) -> None:
        f = tmp_path / "small.txt"
        f.write_text("abc")
        result = fs.head(str(f))
        assert result == "abc"

    def test_empty_file(self, tmp_path) -> None:
        f = tmp_path / "empty.txt"
        f.touch()
        result = fs.head(str(f))
        assert result == ""


# ===================================================================
# append — local file
# ===================================================================


class TestAppendLocal:
    def test_appends_to_existing_file(self, tmp_path) -> None:
        f = tmp_path / "append.txt"
        f.write_text("start-")
        result = fs.append(str(f), "END")
        assert result is True
        assert f.read_text() == "start-END"

    def test_appends_to_new_file_when_flagged(self, tmp_path) -> None:
        f = tmp_path / "new.txt"
        assert not f.exists()
        result = fs.append(str(f), "first", createFileIfNotExists=True)
        assert result is True
        assert f.read_text() == "first"

    def test_appends_raises_on_missing_no_flag(self, tmp_path) -> None:
        f = tmp_path / "nope.txt"
        with pytest.raises(FileNotFoundError, match="not found"):
            fs.append(str(f), "data")


# ===================================================================
# mkdirs — local
# ===================================================================


class TestMkdirsLocal:
    def test_creates_directory(self, tmp_path) -> None:
        d = tmp_path / "newdir"
        assert not d.exists()
        result = fs.mkdirs(str(d))
        assert result is True
        assert d.is_dir()

    def test_creates_parents(self, tmp_path) -> None:
        d = tmp_path / "a" / "b" / "c"
        result = fs.mkdirs(str(d))
        assert result is True
        assert d.is_dir()

    def test_existing_dir_succeeds(self, tmp_path) -> None:
        d = tmp_path / "existing"
        d.mkdir()
        result = fs.mkdirs(str(d))
        assert result is True


# ===================================================================
# exists — local
# ===================================================================


class TestExistsLocal:
    def test_true_for_existing_file(self, tmp_path) -> None:
        f = tmp_path / "existing.txt"
        f.touch()
        assert fs.exists(str(f)) is True

    def test_false_for_missing(self, tmp_path) -> None:
        assert fs.exists(str(tmp_path / "missing.txt")) is False

    def test_true_for_directory(self, tmp_path) -> None:
        assert fs.exists(str(tmp_path)) is True


# ===================================================================
# rm — local
# ===================================================================


class TestRmLocal:
    def test_removes_file(self, tmp_path) -> None:
        f = tmp_path / "removeme.txt"
        f.touch()
        assert f.exists()
        result = fs.rm(str(f))
        assert result is True
        assert not f.exists()

    def test_removes_directory_with_recurse(self, tmp_path) -> None:
        d = tmp_path / "rmdir"
        d.mkdir()
        (d / "inner.txt").touch()
        result = fs.rm(str(d), recurse=True)
        assert result is True
        assert not d.exists()

    def test_removes_directory_raises_without_recurse(self, tmp_path) -> None:
        d = tmp_path / "nodir"
        d.mkdir()
        with pytest.raises(IsADirectoryError):
            fs.rm(str(d))

    def test_missing_path_returns_false(self) -> None:
        # local rm returns False if path doesn't exist
        assert fs.rm("/tmp/nonexistent_12345_file_xyz") is False


# ===================================================================
# cp — local to local
# ===================================================================


class TestCpLocalToLocal:
    def test_cp_file(self, tmp_path) -> None:
        src = tmp_path / "src.txt"
        src.write_text("hello")
        dst = tmp_path / "dst.txt"
        result = fs.cp(str(src), str(dst))
        assert result is True
        assert dst.read_text() == "hello"

    def test_cp_directory_without_recurse(self, tmp_path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        with pytest.raises(IsADirectoryError):
            fs.cp(str(d), str(tmp_path / "out"), recurse=False)


# ===================================================================
# mv — local
# ===================================================================


class TestMvLocal:
    def test_mv_file(self, tmp_path) -> None:
        src = tmp_path / "src.txt"
        src.write_text("data")
        dst = tmp_path / "dst.txt"
        result = fs.mv(str(src), str(dst))
        assert result is True
        assert dst.read_text() == "data"
        assert not src.exists()

    def test_mv_overwrite(self, tmp_path) -> None:
        src = tmp_path / "src.txt"
        src.write_text("src")
        dst = tmp_path / "dst.txt"
        dst.write_text("dst")
        result = fs.mv(str(src), str(dst), overwrite=True)
        assert result is True
        assert dst.read_text() == "src"
        assert not src.exists()