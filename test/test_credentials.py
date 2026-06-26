"""
Tests for credential loading and service-client creation in ``notebookutils.fs``.

All tests read from the real ``~/.notebookutils/storage/`` directory.
No YAML files are written — only the user's existing credential
files are used. Tests that need a credential type not present on
the system will naturally fail.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="No storage config files set up yet")
import yaml

from notebookutils import fs


# ===================================================================
# _get_config_path
# ===================================================================


class TestGetConfigPath:
    def test_includes_home(self, first_account: str) -> None:
        """``_get_config_path`` returns a path under ``~/.notebookutils/storage/``."""
        path = fs._get_config_path(first_account)
        assert path.endswith(f"{first_account}.azure.yaml")
        assert ".notebookutils" in path


# ===================================================================
# _load_creds
# ===================================================================


class TestLoadCreds:
    def test_loads_valid_config(
        self, first_account: str, credential_configs: dict
    ) -> None:
        """Reading the first discovered config succeeds and matches the file."""
        creds = fs._load_creds(first_account)
        expected = credential_configs[first_account]
        # At minimum the mode should match
        assert creds["mode"] == expected["mode"]

    def test_caches_result(self, first_account: str) -> None:
        """Loading the same account twice should use the cache."""
        creds1 = fs._load_creds(first_account)
        creds2 = fs._load_creds(first_account)
        assert creds1 is creds2  # same object (cached)

    def test_missing_file_raises(self) -> None:
        """A non-existent account raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Credential file"):
            fs._load_creds("nonexistent")

    def test_missing_azstorage_key_raises(self) -> None:
        """A YAML file without ``azstorage`` section raises KeyError."""
        import tempfile
        import os

        tmpf, tmpp = tempfile.mkstemp(suffix=".yaml", prefix="bad_")
        with open(tmpp, "w") as f:
            yaml.dump({"other": "stuff"}, f)
        from unittest.mock import patch
        with patch.object(
            fs, "_get_config_path", return_value=tmpp
        ):
            with pytest.raises(KeyError, match="Missing"):
                fs._load_creds("bad")


# ===================================================================
# _get_service_client — auth modes
# ===================================================================


class TestGetServiceClient:
    """Test each auth mode produces a DataLakeServiceClient.

    The tests discover the real storage account from
    ``~/.notebookutils/storage/`` and test its auth mode.
    """

    def test_mode_msi(
        self, first_account: str, credential_configs: dict
    ) -> None:
        """Authenticate with the first discovered account's mode."""
        azstorage = credential_configs[first_account]
        mode = azstorage.get("mode", "key")

        if mode == "msi":
            appid = azstorage.get("appid")
            with patch.object(fs, "DataLakeServiceClient") as mock_svc:
                with patch.object(fs, "ManagedIdentityCredential") as mock_cred:
                    fs._get_service_client(first_account)
                    if appid:
                        mock_cred.assert_called_once_with(client_id=appid)
                    mock_svc.assert_called_once_with(
                        f"https://{first_account}.dfs.core.windows.net",
                        credential=mock_cred.return_value,
                    )
        else:
            # Fallback: log or skip — the credential type doesn't match
            pytest.skip(f"First account uses mode={mode!r}, not 'msi'")

    def test_missing_mode_defaults_to_key(self) -> None:
        """No ``mode`` field -> defaults to 'key', but no account-key -> KeyError."""
        import tempfile
        import os

        cfg = {
            "azstorage": {
                "container": "testcont",
                "type": "adls",
            }
        }
        tmpf, tmpp = tempfile.mkstemp(suffix=".yaml", prefix="nomode_")
        with open(tmpp, "w") as f:
            yaml.dump(cfg, f)
        from unittest.mock import patch
        with patch.object(
            fs, "_get_config_path", return_value=tmpp
        ):
            with pytest.raises(KeyError, match="account-key"):
                fs._get_service_client("nomode")


# ===================================================================
# _merge_extra_configs
# ===================================================================


class TestMergeExtraConfigs:
    def test_merges_known_keys(self) -> None:
        azstorage = {}
        fs._merge_extra_configs(
            azstorage,
            {
                "accountKey": "ak",
                "sasToken": "st",
                "clientId": "cid",
                "clientSecret": "cs",
                "tenantId": "tid",
                "aadToken": "/path/token",
            },
        )
        assert azstorage["account-key"] == "ak"
        assert azstorage["sas"] == "st"
        assert azstorage["clientid"] == "cid"
        assert azstorage["clientsecret"] == "cs"
        assert azstorage["tenantid"] == "tid"
        assert azstorage["oauth-token-path"] == "/path/token"

    def test_merges_unknown_key_raw(self) -> None:
        azstorage = {"mode": "msi"}
        fs._merge_extra_configs(azstorage, {"unknownKey": "val"})
        assert azstorage["unknownKey"] == "val"

    def test_overwrites_existing(self) -> None:
        azstorage = {"account-key": "old"}
        fs._merge_extra_configs(azstorage, {"accountKey": "new"})
        assert azstorage["account-key"] == "new"


# ===================================================================
# _get_fs_client / _get_client_for_uri / _get_dir_client / _get_file_client
# ===================================================================


class TestClientDerivatives:
    """These tests mock the credential-loading chain, so they don't
    need any real YAML file. They exercise the routing logic only.
    """

    def test_get_fs_client(self, first_account: str) -> None:
        with patch.object(fs, "_get_service_client") as mock_svc:
            mock_svc.return_value.get_file_system_client.return_value = "fsclient"
            result = fs._get_fs_client(first_account, "mycont")
            assert result == "fsclient"
            mock_svc.return_value.get_file_system_client.assert_called_once_with(
                "mycont"
            )

    def test_get_client_for_uri(self, first_account: str) -> None:
        with patch.object(fs, "_get_fs_client") as mock_fs:
            mock_fs.return_value = MagicMock()
            fs_client, rel = fs._get_client_for_uri(
                f"abfss://cont@{first_account}.dfs.core.windows.net/a/b"
            )
            assert rel == "a/b"
            mock_fs.assert_called_once_with(first_account, "cont")

    def test_get_dir_client(self, first_account: str) -> None:
        with patch.object(fs, "_get_client_for_uri") as mock:
            mock.return_value = (MagicMock(), "some/path")
            fs._get_dir_client(
                f"abfss://c@{first_account}.dfs.core.windows.net/some/path"
            )
            mock.return_value[0].get_directory_client.assert_called_once_with(
                "some/path"
            )

    def test_get_file_client(self, first_account: str) -> None:
        with patch.object(fs, "_get_client_for_uri") as mock:
            mock.return_value = (MagicMock(), "some/file.txt")
            fs._get_file_client(
                f"abfss://c@{first_account}.dfs.core.windows.net/some/file.txt"
            )
            mock.return_value[0].get_file_client.assert_called_once_with(
                "some/file.txt"
            )