"""
Shared fixtures and mocks for notebookutils.fs tests.

All fixtures use the real ``~/.notebookutils/storage/`` directory.
No YAML files are written — only the user's existing credential
files are used. Tests that need a credential type not present on
the system will naturally fail.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures: discover real credential files from ~/.notebookutils/storage/
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def credential_dir() -> Path:
    """Path to the real ``~/.notebookutils/storage/`` directory."""
    return Path(os.path.expanduser("~/.notebookutils/storage"))


@pytest.fixture(scope="session")
def credential_configs(credential_dir: Path) -> dict[str, dict[str, Any]]:
    """Discover every ``*.azure.yaml`` file and return ``{account_name: azstorage_dict}``.

    The account name is derived both from the filename (``<name>.azure.yaml``)
    and from the ``account-name`` field inside the YAML (which takes precedence
    if present).
    """
    configs: dict[str, dict[str, Any]] = {}
    if not credential_dir.is_dir():
        return configs
    for yaml_file in sorted(credential_dir.glob("*.azure.yaml")):
        with open(yaml_file) as f:
            cfg = yaml.safe_load(f) or {}
        azstorage = cfg.get("azstorage") or {}
        # Prefer the in-file account-name, fall back to filename stem
        name = azstorage.get("account-name") or yaml_file.stem.replace(".azure", "")
        configs[name] = azstorage
    return configs


@pytest.fixture
def first_account(credential_configs: dict[str, dict[str, Any]]) -> str:
    """The first (and typically only) storage account with credentials.

    Raises ``AssertionError`` if no credential files exist.
    """
    assert credential_configs, (
        "No credential files found in ~/.notebookutils/storage/.\n"
        "Create at least one e.g. my_stor_account.azure.yaml."
    )
    return next(iter(credential_configs))


# ---------------------------------------------------------------------------
# Fixtures: clear the module-level credential cache
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_creds_cache() -> None:
    """Clear :data:`notebookutils.fs._creds_cache` before each test.

    This prevents tests from leaking cached credentials to each other.
    """
    import notebookutils.fs as fs

    fs._creds_cache.clear()


# ---------------------------------------------------------------------------
# Mock helpers for Azure SDK calls
# ---------------------------------------------------------------------------


class MockFileProperties:
    """Simulates ``StorageFileProperties`` returned by ``get_file_properties()``."""

    def __init__(self, size: int = 0):
        self.size = size


class MockFileClient:
    """Simulates a minimal ``DataLakeFileClient`` for unit tests."""

    def __init__(self, name: str = "/mock/file", exists: bool = True, size: int = 0):
        self.name = name
        self._exists = exists
        self._size = size

    def get_file_properties(self) -> MockFileProperties:  # noqa
        if not self._exists:
            raise _mock_not_found()
        return MockFileProperties(size=self._size)

    def delete_file(self) -> None:
        if not self._exists:
            raise _mock_not_found()

    def upload_data(self, data: bytes, overwrite: bool = False) -> None:  # noqa
        ...

    def rename_file(self, new_name: str) -> None:  # noqa
        ...

    def download_file(self, length: int = 0) -> "MockDownload":  # noqa
        ...


class MockDownload:
    """Simulates ``StorageStreamDownloader``."""

    def __init__(self, data: bytes = b"hello world"):
        self._data = data

    def readall(self) -> bytes:
        return self._data


class MockDirClient:
    """Simulates ``DataLakeDirectoryClient``."""

    def __init__(self, name: str = "/mock/dir", exists: bool = True):
        self.name = name
        self._exists = exists

    def get_directory_properties(self) -> None:
        if not self._exists:
            raise _mock_not_found()

    def delete_directory(self) -> None:
        ...

    def rename_directory(self, new_name: str) -> None:  # noqa
        ...

    def create_directory(self) -> None:
        ...


def _mock_not_found() -> Exception:
    """Return an exception that looks like ``ResourceNotFoundError``."""
    from azure.core.exceptions import ResourceNotFoundError

    return ResourceNotFoundError("mock: not found")


@pytest.fixture()
def mock_adls_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``notebookutils.fs._get_file_client`` and ``_get_dir_client``
    to return mock objects.

    Returns a dict with keys *file_client* and *dir_client* so tests can
    assert on calls.
    """
    import notebookutils.fs as fs

    registry: dict[str, Any] = {}

    def mock_get_file_client(uri: str) -> MockFileClient:
        return MockFileClient(name=uri)

    def mock_get_dir_client(uri: str) -> MockDirClient:
        return MockDirClient(name=uri, exists=True)

    monkeypatch.setattr(fs, "_get_file_client", mock_get_file_client)
    monkeypatch.setattr(fs, "_get_dir_client", mock_get_dir_client)
    return registry


# ---------------------------------------------------------------------------
# Fixtures: mock subprocess for mount/unmount
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock ``subprocess.run`` to return a successful result.

    ``subprocess.CompletedProcess(returncode=0, stdout=b"", stderr=b"")``
    """

    import subprocess as real_subprocess

    class MockCompletedProcess:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(*args, **kwargs):  # noqa
        return MockCompletedProcess()

    monkeypatch.setattr(real_subprocess, "run", fake_run)
