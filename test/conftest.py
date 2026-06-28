"""
Shared fixtures and mocks for notebookutils.fs tests.

All fixtures use the real ``~/.notebookutils/storage/`` directory.
No YAML files are written — only the user's existing credential
files are used.
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
