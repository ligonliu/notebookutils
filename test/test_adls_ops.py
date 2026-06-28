"""
Tests for ADLS-related operations in notebookutils.fs.

Coverage:

1. **fsspec-backed data-plane ops** — ``cp``, ``ls``, ``rm``, ``exists``,
   ``mkdirs``, ``put``, ``head``, ``append`` — use ``MemoryFileSystem`` to
   exercise real code paths without Azure calls. URIs are built from the
   real account name discovered in ``~/.notebookutils/storage/``.

2. **Azure SDK-specific operations** — credential loading, service client
   creation, client factories, ``_adls_is_directory``.

3. **Pure helpers** — ``_adls_to_https_uri``, ``_build_azcopy_cmd``.

4. **mv HNS rename** — mocks ``_get_file_client`` / ``_get_dir_client`` /
   ``_adls_is_directory`` to exercise the native rename path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from notebookutils import fs
import notebookutils.fs_adapter as fs_adapter

# ---------------------------------------------------------------------------
# Fixtures — discover real account from credential config
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adls_account(credential_configs: dict) -> str:
    """The real storage account name from ``~/.notebookutils/storage/*.azure.yaml``."""
    assert credential_configs, (
        "No credential files found in ~/.notebookutils/storage/.\n"
        "Create at least one e.g. myaccount.azure.yaml."
    )
    return next(iter(credential_configs))


@pytest.fixture(scope="module")
def adls_container(credential_configs: dict, adls_account: str) -> str:
    """The container name from the credential config."""
    azstorage = credential_configs[adls_account]
    container = azstorage.get("container", "")
    assert container, f"Credential for {adls_account!r} has no 'container' field"
    return container


@pytest.fixture(scope="module")
def adls_prefix(adls_account: str, adls_container: str) -> str:
    """The ``abfss://container@account.dfs.core.windows.net`` prefix."""
    return f"abfss://{adls_container}@{adls_account}.dfs.core.windows.net"


@pytest.fixture
def adls_uri(adls_prefix: str) -> str:
    """A unique ADLS path for a test (no real write — used with MemoryFileSystem)."""
    import uuid

    return f"{adls_prefix}/test_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def adls_dir_uri(adls_prefix: str) -> str:
    """A unique ADLS directory path."""
    import uuid

    return f"{adls_prefix}/testdir_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# fsspec redirect — route ADLS URIs to MemoryFileSystem
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_fs():
    """A fresh in-memory fsspec filesystem for each test."""
    from fsspec.implementations.memory import MemoryFileSystem

    return MemoryFileSystem()


@pytest.fixture(autouse=True)
def _patch_get_fs(monkeypatch, mem_fs):
    """Redirect ADLS ``_get_fs()`` calls to the MemoryFileSystem.

    Local paths still go to the real local filesystem so that ADLS↔local
    copy tests work correctly.
    """
    real_get_fs = fs_adapter._get_fs

    def _get_fs_conditional(path=None):
        if path and (str(path).startswith("abfss://") or str(path).startswith("abfs://")):
            return mem_fs
        return real_get_fs(path)

    monkeypatch.setattr(fs_adapter, "_get_fs", _get_fs_conditional)


@pytest.fixture(autouse=True)
def _clear_creds_cache():
    """Clear the module-level credential cache between tests."""
    fs._creds_cache.clear()


# ===================================================================
# fsspec-backed data-plane operations
# ===================================================================


class TestCpAdls:
    """cp with ADLS URIs (routed through MemoryFileSystem)."""

    def test_cp_file_same_filesystem(self, mem_fs, adls_uri) -> None:
        src = adls_uri
        dst = f"{adls_uri}.copy"
        mem_fs.pipe_file(src, b"hello-cp")

        assert fs.cp(src, dst) is True
        assert mem_fs.cat_file(dst) == b"hello-cp"

    def test_cp_directory_no_recurse_raises(self, mem_fs, adls_dir_uri) -> None:
        d = adls_dir_uri
        dst = f"{d}_out"
        mem_fs.mkdirs(d, exist_ok=True)

        with pytest.raises(IsADirectoryError, match="recurse"):
            fs.cp(d, dst)

    def test_cp_file_adls_to_local(self, mem_fs, adls_uri, tmp_path) -> None:
        mem_fs.pipe_file(adls_uri, b"download-me")
        dst = str(tmp_path / "local.txt")

        assert fs.cp(adls_uri, dst) is True
        assert Path(dst).read_text() == "download-me"

    def test_cp_file_local_to_adls(self, mem_fs, adls_uri, tmp_path) -> None:
        local = tmp_path / "upload.txt"
        local.write_text("upload-me")

        assert fs.cp(str(local), adls_uri) is True
        assert mem_fs.cat_file(adls_uri) == b"upload-me"


class TestLsAdls:
    """ls with ADLS URIs through MemoryFileSystem."""

    def test_ls_directory(self, mem_fs, adls_dir_uri) -> None:
        d = adls_dir_uri
        mem_fs.mkdirs(d, exist_ok=True)
        mem_fs.pipe_file(f"{d}/f1.txt", b"hello")
        mem_fs.pipe_file(f"{d}/f2.txt", b"world")

        results = fs.ls(d)
        names = {r.name for r in results}
        assert names == {"f1.txt", "f2.txt"}
        for r in results:
            assert isinstance(r, fs.MSFileInfo)
            assert r.isFile is True
            assert r.isDir is False

    def test_ls_empty_directory(self, mem_fs, adls_dir_uri) -> None:
        mem_fs.mkdirs(adls_dir_uri, exist_ok=True)
        assert fs.ls(adls_dir_uri) == []

    def test_ls_nonexistent_returns_empty(self, mem_fs, adls_prefix) -> None:
        assert fs.ls(f"{adls_prefix}/missing_dir") == []

    def test_ls_root(self, mem_fs, adls_prefix) -> None:
        mem_fs.pipe_file(f"{adls_prefix}/rootfile.txt", b"x")
        results = fs.ls(adls_prefix)
        names = {r.name for r in results}
        assert "rootfile.txt" in names

    def test_ls_includes_directories(self, mem_fs, adls_dir_uri) -> None:
        d = adls_dir_uri
        mem_fs.mkdirs(f"{d}/child", exist_ok=True)
        mem_fs.pipe_file(f"{d}/f.txt", b"x")
        results = fs.ls(d)
        types = {r.name: r.isDir for r in results}
        assert types == {"child": True, "f.txt": False}


class TestExistsAdls:
    """exists with ADLS URIs."""

    def test_exists_true(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"data")
        assert fs.exists(adls_uri) is True

    def test_exists_false(self, mem_fs, adls_prefix) -> None:
        assert fs.exists(f"{adls_prefix}/nonexistent.txt") is False

    def test_exists_directory(self, mem_fs, adls_dir_uri) -> None:
        mem_fs.mkdirs(adls_dir_uri, exist_ok=True)
        assert fs.exists(adls_dir_uri) is True

    def test_exists_after_delete(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"tmp")
        assert fs.exists(adls_uri) is True
        fs.rm(adls_uri)
        assert fs.exists(adls_uri) is False


class TestRmAdls:
    """rm with ADLS URIs."""

    def test_rm_file(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"delete-me")
        assert fs.rm(adls_uri) is True
        assert mem_fs.exists(adls_uri) is False

    def test_rm_directory_with_recurse(self, mem_fs, adls_dir_uri) -> None:
        d = adls_dir_uri
        mem_fs.mkdirs(d, exist_ok=True)
        mem_fs.pipe_file(f"{d}/child.txt", b"x")
        assert fs.rm(d, recurse=True) is True
        assert mem_fs.exists(d) is False

    def test_rm_directory_no_recurse(self, mem_fs, adls_dir_uri) -> None:
        mem_fs.mkdirs(adls_dir_uri, exist_ok=True)
        with pytest.raises(IsADirectoryError, match="recurse"):
            fs.rm(adls_dir_uri)

    def test_rm_missing_returns_false(self, mem_fs, adls_prefix) -> None:
        assert fs.rm(f"{adls_prefix}/missing.txt") is False


class TestMkdirsAdls:
    """mkdirs with ADLS URIs."""

    def test_mkdirs_creates_directory(self, mem_fs, adls_dir_uri) -> None:
        assert fs.mkdirs(adls_dir_uri) is True
        assert mem_fs.isdir(adls_dir_uri)

    def test_mkdirs_nested(self, mem_fs, adls_dir_uri) -> None:
        d = f"{adls_dir_uri}/a/b/c"
        assert fs.mkdirs(d) is True
        assert mem_fs.isdir(d)

    def test_mkdirs_idempotent(self, mem_fs, adls_dir_uri) -> None:
        assert fs.mkdirs(adls_dir_uri) is True
        assert fs.mkdirs(adls_dir_uri) is True


class TestPutAdls:
    """put with ADLS URIs."""

    def test_put_new_file(self, mem_fs, adls_uri) -> None:
        assert fs.put(adls_uri, "hello world") is True
        assert mem_fs.cat_file(adls_uri) == b"hello world"

    def test_put_overwrite(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"original")
        assert fs.put(adls_uri, "overwritten", overwrite=True) is True
        assert mem_fs.cat_file(adls_uri) == b"overwritten"

    def test_put_no_overwrite_existing(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"original")
        assert fs.put(adls_uri, "new", overwrite=False) is False
        assert mem_fs.cat_file(adls_uri) == b"original"

    def test_put_empty_content(self, mem_fs, adls_uri) -> None:
        assert fs.put(adls_uri, "") is True
        assert mem_fs.exists(adls_uri)
        assert mem_fs.cat_file(adls_uri) == b""

    def test_put_unicode(self, mem_fs, adls_uri) -> None:
        content = "\u4f60\u597d\uff0c\u4e16\u754c\uff01\U0001f30d"
        assert fs.put(adls_uri, content) is True
        assert fs.head(adls_uri) == content


class TestHeadAdls:
    """head with ADLS URIs."""

    def test_head_full_content(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"data content")
        assert fs.head(adls_uri) == "data content"

    def test_head_partial(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"abcdefghijklmnopqrstuvwxyz")
        assert fs.head(adls_uri, max_bytes=5) == "abcde"

    def test_head_empty_file(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"")
        assert fs.head(adls_uri) == ""


class TestAppendAdls:
    """append with ADLS URIs."""

    def test_append_to_existing(self, mem_fs, adls_uri) -> None:
        mem_fs.pipe_file(adls_uri, b"start-")
        assert fs.append(adls_uri, "END") is True
        assert mem_fs.cat_file(adls_uri) == b"start-END"

    def test_append_create_if_not_exists(self, mem_fs, adls_uri) -> None:
        assert fs.append(adls_uri, "data", createFileIfNotExists=True) is True
        assert mem_fs.cat_file(adls_uri) == b"data"

    def test_append_missing_raises(self, mem_fs, adls_prefix) -> None:
        uri = f"{adls_prefix}/append_missing.txt"
        with pytest.raises(FileNotFoundError, match="not found"):
            fs.append(uri, "data")


# ===================================================================
# mv — HNS native rename path  (Azure SDK mocks)
# ===================================================================


class TestMvHns:
    """mv with ADLS URIs exercises the HNS native rename path.

    Uses the real account name from ``~/.notebookutils/storage/`` for
    constructing URIs but mocks ``_get_file_client`` / ``_get_dir_client`` /
    ``_adls_is_directory`` so no real Azure calls are made."""

    @pytest.fixture(autouse=True)
    def _mock_hns_clients(self, monkeypatch):
        """Mock ``_get_file_client``, ``_get_dir_client``, and
        ``_adls_is_directory`` so the HNS rename path is exercised
        without real Azure SDK calls."""

        mock_dir = MagicMock()
        mock_file = MagicMock()

        def get_dir(uri):
            return mock_dir

        def get_file(uri):
            return mock_file

        monkeypatch.setattr(fs, "_get_dir_client", get_dir)
        monkeypatch.setattr(fs, "_get_file_client", get_file)
        return mock_dir, mock_file

    def test_mv_file_hns(self, adls_prefix) -> None:
        src = f"{adls_prefix}/src/file.txt"
        dst = f"{adls_prefix}/dst/file.txt"

        # We need _adls_is_directory to return False (it is a file)
        with patch.object(fs, "_adls_is_directory", return_value=False):
            result = fs.mv(src, dst)
        assert result is True
        # Called with "container/relative_path"
        fs._get_file_client(src).rename_file.assert_called_once()
        rename_arg = fs._get_file_client(src).rename_file.call_args[1]["new_name"]
        assert "dst/file.txt" in rename_arg

    def test_mv_directory_hns(self, adls_prefix) -> None:
        src = f"{adls_prefix}/src/dir"
        dst = f"{adls_prefix}/dst/dir"

        with patch.object(fs, "_adls_is_directory", return_value=True):
            result = fs.mv(src, dst)
        assert result is True
        fs._get_dir_client(src).rename_directory.assert_called_once()
        rename_arg = fs._get_dir_client(src).rename_directory.call_args[1]["new_name"]
        assert "dst/dir" in rename_arg

    def test_mv_hns_different_account_falls_back_to_fsspec(
        self, mem_fs, adls_prefix
    ) -> None:
        """When accounts differ, the HNS path is skipped → fsspec mv."""
        src = adls_prefix + "/diff_acct.txt"
        dst = "abfss://c@other_acct.dfs.core.windows.net/diff_acct.txt"  # different host
        mem_fs.pipe_file(src, b"different-account")

        mock_is_dir = MagicMock(return_value=False)
        with patch.object(fs, "_adls_is_directory", mock_is_dir):
            result = fs.mv(src, dst)
        assert result is True
        mock_is_dir.assert_not_called()  # HNS path skipped
        assert not mem_fs.exists(src)
        assert mem_fs.cat_file(dst) == b"different-account"

    def test_mv_hns_different_container_falls_back_to_fsspec(
        self, mem_fs, adls_prefix, adls_account
    ) -> None:
        """When containers differ, the HNS path is skipped → fsspec mv."""
        src = adls_prefix + "/diff_cont.txt"
        dst = f"abfss://other_cont@{adls_account}.dfs.core.windows.net/diff_cont.txt"
        mem_fs.pipe_file(src, b"different-container")

        mock_is_dir = MagicMock(return_value=False)
        with patch.object(fs, "_adls_is_directory", mock_is_dir):
            result = fs.mv(src, dst)
        assert result is True
        mock_is_dir.assert_not_called()
        assert not mem_fs.exists(src)
        assert mem_fs.cat_file(dst) == b"different-container"


# ===================================================================
# _adls_is_directory
# ===================================================================


class TestAdlsIsDirectory:
    def test_is_directory(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs,
            "_get_dir_client",
            lambda uri: MagicMock(get_directory_properties=lambda: None),
        )
        assert fs._adls_is_directory("abfss://c@a.dfs.core.windows.net/dir") is True

    def test_is_not_directory(self, monkeypatch) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        mock_dc = MagicMock()
        mock_dc.get_directory_properties.side_effect = ResourceNotFoundError("nope")
        monkeypatch.setattr(fs, "_get_dir_client", lambda uri: mock_dc)
        assert (
            fs._adls_is_directory("abfss://c@a.dfs.core.windows.net/file.txt")
            is False
        )

    def test_root_is_directory(self) -> None:
        assert fs._adls_is_directory("abfss://c@a.dfs.core.windows.net") is True

    def test_trailing_slash_is_directory(self) -> None:
        assert fs._adls_is_directory("abfss://c@a.dfs.core.windows.net/dir/") is True

    def test_extension_heuristic(self, monkeypatch) -> None:
        """Paths ending in ``.ext`` are treated as files without probing."""
        monitor = MagicMock()
        monkeypatch.setattr(fs, "_get_dir_client", monitor)
        result = fs._adls_is_directory("abfss://c@a.dfs.core.windows.net/x.txt")
        assert result is False
        monitor.assert_not_called()


# ===================================================================
# Credential loading
# ===================================================================


class TestLoadCreds:
    def test_load_creds_from_yaml(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {
                    "azstorage": {
                        "account-name": "testacct",
                        "mode": "msi",
                        "container": "testcont",
                    },
                },
                f,
            )
            tmp_path = f.name

        try:
            with patch.object(fs, "_get_config_path", return_value=tmp_path):
                creds = fs._load_creds("testacct")
                assert creds["mode"] == "msi"
                assert creds["account-name"] == "testacct"
                assert creds["container"] == "testcont"
        finally:
            os.unlink(tmp_path)

    def test_load_creds_missing_file(self) -> None:
        with patch.object(
            fs, "_get_config_path", return_value="/nonexistent/path.yaml"
        ):
            with pytest.raises(FileNotFoundError, match="Credential file"):
                fs._load_creds("noaccount")

    def test_load_creds_missing_azstorage(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump({"other": "stuff"}, f)
            tmp_path = f.name

        try:
            with patch.object(fs, "_get_config_path", return_value=tmp_path):
                with pytest.raises(KeyError, match="Missing 'azstorage:'"):
                    fs._load_creds("noazstorage")
        finally:
            os.unlink(tmp_path)

    def test_creds_cache(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {
                    "azstorage": {"account-name": "cached", "mode": "key", "account-key": "k1"},
                },
                f,
            )
            tmp_path = f.name

        try:
            with patch.object(fs, "_get_config_path", return_value=tmp_path):
                creds1 = fs._load_creds("cached")
                # Modify the YAML file on disk
                with open(tmp_path, "w") as f2:
                    yaml.dump(
                        {
                            "azstorage": {"account-name": "cached", "mode": "key", "account-key": "k2"},
                        },
                        f2,
                    )
                creds2 = fs._load_creds("cached")
                assert creds1["account-key"] == creds2["account-key"]
        finally:
            os.unlink(tmp_path)

    def test_get_config_path(self) -> None:
        path = fs._get_config_path("myacct")
        assert path.endswith("myacct.yaml")
        assert ".notebookutils/storage" in path


# ===================================================================
# _get_service_client
# ===================================================================


class TestGetServiceClient:
    def test_key_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs,
            "_load_creds",
            lambda name: {"mode": "key", "account-key": "fake-key"},
        )
        with patch("notebookutils.fs.DataLakeServiceClient") as mock_svc:
            fs._get_service_client("acct")
            mock_svc.assert_called_once()
            _, kwargs = mock_svc.call_args
            assert kwargs["credential"] == "fake-key"

    def test_sas_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs,
            "_load_creds",
            lambda name: {"mode": "sas", "sas": "sv=2020&sig=..."},
        )
        with patch("notebookutils.fs.DataLakeServiceClient") as mock_svc:
            fs._get_service_client("acct")
            _, kwargs = mock_svc.call_args
            assert kwargs["credential"] == "sv=2020&sig=..."

    def test_spn_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs, "_load_creds", lambda name: {"mode": "spn"}
        )
        with patch("notebookutils.fs.DefaultAzureCredential") as mock_cred:
            fs._get_service_client("acct")
            mock_cred.assert_called_once()

    def test_msi_mode_default(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs, "_load_creds", lambda name: {"mode": "msi"}
        )
        with patch("notebookutils.fs.DefaultAzureCredential") as mock_cred:
            fs._get_service_client("acct")
            mock_cred.assert_called_once()

    def test_msi_mode_with_client_id(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs,
            "_load_creds",
            lambda name: {"mode": "msi", "appid": "my-client-id"},
        )
        with patch("notebookutils.fs.ManagedIdentityCredential") as mock_mic:
            fs._get_service_client("acct")
            mock_mic.assert_called_once_with(client_id="my-client-id")

    def test_unknown_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs, "_load_creds", lambda name: {"mode": "magic"}
        )
        with pytest.raises(ValueError, match="unsupported auth mode"):
            fs._get_service_client("acct")

    def test_missing_key_for_key_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs, "_load_creds", lambda name: {"mode": "key"}
        )
        with pytest.raises(KeyError, match="no account-key"):
            fs._get_service_client("acct")

    def test_missing_sas_for_sas_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs, "_load_creds", lambda name: {"mode": "sas"}
        )
        with pytest.raises(KeyError, match="no SAS token"):
            fs._get_service_client("acct")

    def test_get_service_client_for_uri(self, monkeypatch) -> None:
        monkeypatch.setattr(
            fs, "_load_creds", lambda name: {"mode": "msi"}
        )
        with patch("notebookutils.fs.DataLakeServiceClient") as mock_svc:
            fs._get_service_client_for_uri(
                "abfss://c@myacct.dfs.core.windows.net/path"
            )
            mock_svc.assert_called_once()
            account_url = mock_svc.call_args[0][0]
            assert "myacct.dfs.core.windows.net" in account_url


# ===================================================================
# _get_file_client / _get_dir_client / _get_fs_client  (Azure SDK)
# ===================================================================


class TestAzureSdkClients:
    def test_get_fs_client(self, monkeypatch) -> None:
        mock_service = MagicMock()
        mock_fs_client = MagicMock()
        mock_service.get_file_system_client.return_value = mock_fs_client
        monkeypatch.setattr(fs, "_get_service_client", lambda *a, **kw: mock_service)

        result = fs._get_fs_client("abfss://mycont@acct.dfs.core.windows.net/x")
        assert result is mock_fs_client
        mock_service.get_file_system_client.assert_called_once_with("mycont")

    def test_get_file_client(self, monkeypatch) -> None:
        mock_service = MagicMock()
        mock_fs_client = MagicMock()
        mock_file_client = MagicMock()
        mock_service.get_file_system_client.return_value = mock_fs_client
        mock_fs_client.get_file_client.return_value = mock_file_client
        monkeypatch.setattr(fs, "_get_service_client", lambda *a, **kw: mock_service)

        result = fs._get_file_client("abfss://c@a.dfs.core.windows.net/a/b/file.txt")
        assert result is mock_file_client
        mock_fs_client.get_file_client.assert_called_once_with("a/b/file.txt")

    def test_get_dir_client(self, monkeypatch) -> None:
        mock_service = MagicMock()
        mock_fs_client = MagicMock()
        mock_dir_client = MagicMock()
        mock_service.get_file_system_client.return_value = mock_fs_client
        mock_fs_client.get_directory_client.return_value = mock_dir_client
        monkeypatch.setattr(fs, "_get_service_client", lambda *a, **kw: mock_service)

        result = fs._get_dir_client("abfss://c@a.dfs.core.windows.net/a/b")
        assert result is mock_dir_client
        mock_fs_client.get_directory_client.assert_called_once_with("a/b")

    def test_get_dir_client_root(self, monkeypatch) -> None:
        mock_service = MagicMock()
        mock_fs_client = MagicMock()
        mock_dir_client = MagicMock()
        mock_service.get_file_system_client.return_value = mock_fs_client
        mock_fs_client.get_directory_client.return_value = mock_dir_client
        monkeypatch.setattr(fs, "_get_service_client", lambda *a, **kw: mock_service)

        result = fs._get_dir_client("abfss://c@a.dfs.core.windows.net/")
        mock_fs_client.get_directory_client.assert_called_once_with("/")


# ===================================================================
# _adls_to_https_uri
# ===================================================================


class TestAdlsToHttps:
    def test_conversion(self) -> None:
        assert (
            fs._adls_to_https_uri("abfss://c@a.dfs.core.windows.net/x/y")
            == "https://a.dfs.core.windows.net/c/x/y"
        )

    def test_onelake_conversion(self) -> None:
        assert (
            fs._adls_to_https_uri(
                "abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Files/x"
            )
            == "https://onelake.dfs.fabric.microsoft.com/ws/lh/Files/x"
        )

    def test_local_passthrough(self) -> None:
        assert fs._adls_to_https_uri("/local") == "/local"

    def test_s3_passthrough(self) -> None:
        assert fs._adls_to_https_uri("s3://bucket/key") == "s3://bucket/key"


# ===================================================================
# _build_azcopy_cmd
# ===================================================================


class TestBuildAzcopyCmd:
    def test_basic(self) -> None:
        cmd, endpoint = fs._build_azcopy_cmd(
            "abfss://c@a.dfs.core.windows.net/src",
            "abfss://c@a.dfs.core.windows.net/dst",
            recurse=False,
            flags="",
        )
        assert endpoint == "azcopy"
        assert cmd[0] == "azcopy"
        assert cmd[1] == "copy"
        assert "https://a.dfs.core.windows.net/c/src" in cmd
        assert "https://a.dfs.core.windows.net/c/dst" in cmd

    def test_with_recurse(self) -> None:
        cmd, _ = fs._build_azcopy_cmd(
            "abfss://c@a.dfs.core.windows.net/s",
            "abfss://c@a.dfs.core.windows.net/d",
            recurse=True,
            flags="",
        )
        assert "--recursive" in cmd

    def test_with_flags(self) -> None:
        cmd, _ = fs._build_azcopy_cmd(
            "abfss://c@a.dfs.core.windows.net/s",
            "abfss://c@a.dfs.core.windows.net/d",
            recurse=False,
            flags="--overwrite true --dry-run",
        )
        assert "--overwrite" in cmd
        assert "true" in cmd
        assert "--dry-run" in cmd

    def test_non_adls_source(self) -> None:
        cmd, _ = fs._build_azcopy_cmd(
            "/local/src",
            "abfss://c@a.dfs.core.windows.net/dst",
            recurse=False,
            flags="",
        )
        assert "/local/src" in cmd
        assert "https://a.dfs.core.windows.net/c/dst" in cmd
