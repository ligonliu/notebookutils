"""
End-to-end integration tests that exercise every ``notebookutils.fs.*``
function against the real ADLS Gen2 storage account.

All tests use the real credential files discovered from
``~/.notebookutils/storage/*.azure.yaml``.  No Azure SDK calls are mocked.

Requires the managed identity to have **Storage Blob Data Contributor**
RBAC on the discovered storage account(s).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Generator

import pytest

from notebookutils import fs


# ---------------------------------------------------------------------------
# Fixtures — discover the real account from the credential directory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def adls_config(credential_configs: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Return ``(account_name, container_name)`` from the first credential file.

    Raises ``AssertionError`` if no credential files are found.
    """
    assert credential_configs, "No credential files found in ~/.notebookutils/storage/"
    account = next(iter(credential_configs))
    azstorage = credential_configs[account]
    container = azstorage.get("container", "")
    assert container, f"Credential for {account!r} has no 'container' field"
    return account, container


@pytest.fixture
def adls_prefix(adls_config: tuple[str, str]) -> str:
    """The ``abfss://container@account.dfs.core.windows.net`` prefix."""
    account, container = adls_config
    return f"abfss://{container}@{account}.dfs.core.windows.net"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_file(adls_prefix: str) -> str:
    """A unique ADLS path for the test."""
    return f"{adls_prefix}/e2e_{uuid.uuid4().hex[:12]}.txt"


@pytest.fixture
def test_dir(adls_prefix: str) -> str:
    """A unique ADLS directory for the test."""
    return f"{adls_prefix}/e2e_dir_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    """A local file path for download/upload tests."""
    return tmp_path / "local.txt"


@pytest.fixture(autouse=True)
def auto_cleanup(adls_prefix: str) -> Generator[None, None, None]:
    """Clean up ALL e2e test objects after each test."""
    yield
    _cleanup_test_objects(adls_prefix)


def _cleanup_test_objects(adls_prefix: str) -> None:
    """Remove every file/directory whose name starts with ``e2e_``."""
    try:
        for p in fs.ls(adls_prefix):
            name = p.name
            if name.startswith("e2e_"):
                uri = f"{adls_prefix}/{name}"
                try:
                    if fs.exists(uri):
                        fs.rm(uri, recurse=True)
                except Exception:
                    pass
    except Exception:
        pass


# ===================================================================
# put / head
# ===================================================================


class TestPutAndHead:
    def test_put_new_file(self, test_file: str) -> None:
        """Write a new file and read it back via head()."""
        content = "Hello, ADLS Gen2!"
        assert fs.put(test_file, content) is True
        result = fs.head(test_file)
        assert result == content

    def test_put_overwrite(self, test_file: str) -> None:
        """Overwrite an existing file."""
        fs.put(test_file, "original")
        fs.put(test_file, "overwritten", overwrite=True)
        assert fs.head(test_file) == "overwritten"

    def test_put_head_partial(self, test_file: str) -> None:
        """head() with max_bytes reads only that many bytes."""
        fs.put(test_file, "abcdefghijklmnopqrstuvwxyz")
        assert fs.head(test_file, max_bytes=5) == "abcde"

    def test_head_empty(self, test_file: str) -> None:
        """head() on an empty file returns empty string."""
        fs.put(test_file, "")
        assert fs.head(test_file) == ""

    def test_put_utf8(self, test_file: str) -> None:
        """Unicode content round-trips correctly."""
        content = "你好，世界！🌍"
        fs.put(test_file, content)
        assert fs.head(test_file) == content


# ===================================================================
# exists
# ===================================================================


class TestExists:
    def test_exists_true(self, test_file: str) -> None:
        """exists() returns True for a file that exists."""
        fs.put(test_file, "exists")
        assert fs.exists(test_file) is True

    def test_exists_false(self, adls_prefix: str) -> None:
        """exists() returns False for a non-existent file."""
        nowhere = f"{adls_prefix}/e2e_{uuid.uuid4().hex}.txt"
        assert fs.exists(nowhere) is False

    def test_exists_after_delete(self, test_file: str) -> None:
        """Writing, deleting, then checking returns False."""
        fs.put(test_file, "temp")
        assert fs.exists(test_file) is True
        fs.rm(test_file)
        assert fs.exists(test_file) is False

    def test_exists_directory(self, test_dir: str) -> None:
        """exists() returns True for a directory."""
        fs.mkdirs(test_dir)
        assert fs.exists(test_dir) is True


# ===================================================================
# mkdirs / ls
# ===================================================================


class TestMkdirsAndLs:
    def test_mkdirs_creates_directory(self, test_dir: str) -> None:
        """mkdirs creates a directory that ls can see."""
        fs.mkdirs(test_dir)
        ls_result = fs.ls(test_dir)
        assert isinstance(ls_result, list)

    def test_mkdirs_nested(self, test_dir: str) -> None:
        """mkdirs creates nested directories."""
        nested = f"{test_dir}/a/b/c"
        fs.mkdirs(nested)
        assert fs.exists(nested) is True

    def test_ls_lists_children(self, test_dir: str, test_file: str) -> None:
        """ls on a directory finds files created inside it."""
        fs.mkdirs(test_dir)
        file_in_dir = f"{test_dir}/{test_file.split('/')[-1]}"
        fs.put(file_in_dir, "content")
        children = fs.ls(test_dir)
        names = [c.name for c in children]
        assert any(n == file_in_dir.split("/")[-1] for n in names)

    def test_ls_root_returns_list(self, adls_prefix: str) -> None:
        """ls on the container root returns a list."""
        result = fs.ls(adls_prefix)
        assert isinstance(result, list)

    def test_ls_file_info_fields(self, test_file: str) -> None:
        """ls entries have the expected MSFileInfo fields."""
        fs.put(test_file, "info")
        parent = "/".join(test_file.split("/")[:-1])
        children = fs.ls(parent)
        for c in children:
            assert c.name is not None
            assert isinstance(c.size, int)
            assert isinstance(c.path, str)
            assert isinstance(c.isDir, bool)
            assert isinstance(c.isFile, bool)

    def test_mkdirs_idempotent(self, test_dir: str) -> None:
        """Calling mkdirs twice on the same directory is safe."""
        assert fs.mkdirs(test_dir) is True
        assert fs.mkdirs(test_dir) is True


# ===================================================================
# cp (ADLS ↔ local)
# ===================================================================


class TestCpAdlsToLocal:
    def test_download_file(self, test_file: str, local_file: Path) -> None:
        """cp ADLS→local downloads the full content."""
        content = "download me"
        fs.put(test_file, content)
        assert fs.cp(test_file, str(local_file)) is True
        assert local_file.read_text() == content

    def test_download_with_special_chars(self, test_file: str, local_file: Path) -> None:
        """Download a file with special characters."""
        content = "line1\nline2\tend"
        fs.put(test_file, content)
        fs.cp(test_file, str(local_file))
        assert local_file.read_text() == content


class TestCpLocalToAdls:
    def test_upload_file(self, test_file: str, local_file: Path) -> None:
        """cp local→ADLS uploads content."""
        local_file.write_text("upload content")
        assert fs.cp(str(local_file), test_file) is True
        assert fs.head(test_file) == "upload content"

    def test_upload_binary(self, test_file: str, local_file: Path) -> None:
        """Binary content is uploaded but head() decodes as UTF-8 with replacement."""
        local_file.write_bytes(b"\x00\x01\x02\xff\xfe")
        fs.cp(str(local_file), test_file)
        # head() decodes with errors="replace", so non-UTF-8 bytes become 
        result = fs.head(test_file)
        assert "\ufffd" in result


class TestCpAdlsToAdls:
    def test_copy_within_account(self, test_file: str) -> None:
        """cp ADLS→ADLS (same account) uses rename, and content is preserved."""
        fs.put(test_file, "copy-test")
        dest = f"{test_file}.copy"
        try:
            assert fs.cp(test_file, dest) is True
            assert fs.head(dest) == "copy-test"
        finally:
            try:
                fs.rm(dest)
            except Exception:
                pass

    def test_copy_directory(self, test_dir: str, adls_prefix: str) -> None:
        """cp a directory with recurse=True."""
        fs.mkdirs(test_dir)
        file1 = f"{test_dir}/a.txt"
        file2 = f"{test_dir}/b.txt"
        fs.put(file1, "one")
        fs.put(file2, "two")
        dest_dir = f"{adls_prefix}/e2e_cp_dir_{uuid.uuid4().hex[:8]}"
        try:
            assert fs.cp(test_dir, dest_dir, recurse=True) is True
            assert fs.head(f"{dest_dir}/a.txt") == "one"
            assert fs.head(f"{dest_dir}/b.txt") == "two"
        finally:
            try:
                fs.rm(dest_dir, recurse=True)
            except Exception:
                pass


# ===================================================================
# mv (rename)
# ===================================================================


class TestMv:
    def test_mv_file(self, test_file: str) -> None:
        """mv renames a file; source no longer exists."""
        fs.put(test_file, "rename-me")
        dest = f"{test_file}.renamed"
        try:
            assert fs.mv(test_file, dest) is True
            assert fs.exists(test_file) is False
            assert fs.head(dest) == "rename-me"
        finally:
            try:
                fs.rm(dest)
            except Exception:
                pass

    def test_mv_overwrite(self, test_file: str) -> None:
        """mv with overwrite=True replaces the destination."""
        src = f"{test_file}.src"
        dst = f"{test_file}.dst"
        fs.put(src, "source")
        fs.put(dst, "destination")
        try:
            assert fs.mv(src, dst, overwrite=True) is True
            assert fs.exists(src) is False
            assert fs.head(dst) == "source"
        finally:
            try:
                fs.rm(dst)
            except Exception:
                pass


# ===================================================================
# append
# ===================================================================


class TestAppend:
    def test_append_to_existing(self, test_file: str) -> None:
        """append adds content to an existing file."""
        fs.put(test_file, "start-")
        assert fs.append(test_file, "END") is True
        assert fs.head(test_file) == "start-END"

    def test_append_creates_when_flagged(self, test_file: str) -> None:
        """append with createFileIfNotExists=True creates a new file."""
        assert fs.append(test_file, "new", createFileIfNotExists=True) is True
        assert fs.head(test_file) == "new"

    def test_append_missing_raises(self, test_file: str) -> None:
        """append on a non-existent file raises FileNotFoundError."""
        import pytest
        with pytest.raises(FileNotFoundError):
            fs.append(test_file, "data")


# ===================================================================
# rm
# ===================================================================


class TestRm:
    def test_rm_file(self, test_file: str) -> None:
        """rm removes a file; exists returns False."""
        fs.put(test_file, "delete-me")
        assert fs.rm(test_file) is True
        assert fs.exists(test_file) is False

    def test_rm_directory(self, test_dir: str) -> None:
        """rm with recurse=True removes a directory and its children."""
        fs.mkdirs(test_dir)
        fs.put(f"{test_dir}/child.txt", "child")
        assert fs.rm(test_dir, recurse=True) is True
        assert fs.exists(test_dir) is False

    def test_rm_missing_returns_false(self, adls_prefix: str) -> None:
        """rm on a non-existent file returns False."""
        nowhere = f"{adls_prefix}/e2e_{uuid.uuid4().hex}.txt"
        assert fs.rm(nowhere) is False

    def test_rm_directory_no_recurse(self, test_dir: str) -> None:
        """rm on a directory without recurse raises IsADirectoryError."""
        fs.mkdirs(test_dir)
        import pytest
        with pytest.raises(IsADirectoryError):
            fs.rm(test_dir)
