"""
Tests for path-parsing helpers in ``notebookutils.fs``.

These are pure functions with no I/O or Azure dependencies.
"""

from __future__ import annotations

import pytest

from notebookutils import fs


# ===================================================================
# _is_adls_path
# ===================================================================


class TestIsAdlsPath:
    def test_abfss_uri(self) -> None:
        assert fs._is_adls_path("abfss://cont@acct.dfs.core.windows.net/path")

    def test_abfs_uri(self) -> None:
        assert fs._is_adls_path("abfs://cont@acct.dfs.core.windows.net/path")

    def test_root_abfss(self) -> None:
        assert fs._is_adls_path("abfss://cont@acct.dfs.core.windows.net/")

    def test_no_path(self) -> None:
        assert fs._is_adls_path("abfss://cont@acct.dfs.core.windows.net")

    def test_local_path_is_not_adls(self) -> None:
        assert not fs._is_adls_path("/mnt/data/file.txt")

    def test_relative_local_path(self) -> None:
        assert not fs._is_adls_path("data/file.txt")

    def test_https_is_not_adls(self) -> None:
        assert not fs._is_adls_path("https://acct.dfs.core.windows.net/cont/path")

    def test_empty_string(self) -> None:
        assert not fs._is_adls_path("")

    def test_abfss_missing_at(self) -> None:
        assert not fs._is_adls_path("abfss://cont.dfs.core.windows.net/path")


# ===================================================================
# _parse_adls_uri
# ===================================================================


class TestParseAdlsUri:
    def test_parse_full_path(self) -> None:
        host, container, path = fs._parse_adls_uri(
            "abfss://mycont@myacct.dfs.core.windows.net/a/b"
        )
        assert host == "myacct.dfs.core.windows.net"
        assert container == "mycont"
        assert path == "/a/b"

    def test_parse_root_path(self) -> None:
        host, container, path = fs._parse_adls_uri(
            "abfss://cont@acct.dfs.core.windows.net/foo"
        )
        assert host == "acct.dfs.core.windows.net"
        assert container == "cont"
        assert path == "/foo"

    def test_parse_no_path(self) -> None:
        host, container, path = fs._parse_adls_uri(
            "abfss://cont@acct.dfs.core.windows.net"
        )
        assert host == "acct.dfs.core.windows.net"
        assert container == "cont"
        assert path == "/"

    def test_parse_abfs(self) -> None:
        host, container, path = fs._parse_adls_uri(
            "abfs://c@a.dfs.core.windows.net/x/y/z"
        )
        assert host == "a.dfs.core.windows.net"
        assert container == "c"
        assert path == "/x/y/z"

    def test_parse_onelake(self) -> None:
        host, container, path = fs._parse_adls_uri(
            "abfss://MyWorkspace@onelake.dfs.fabric.microsoft.com/MyLakehouse/Files/data.csv"
        )
        assert host == "onelake.dfs.fabric.microsoft.com"
        assert container == "MyWorkspace"
        assert path == "/MyLakehouse/Files/data.csv"

    def test_parse_invalid_uri(self) -> None:
        with pytest.raises(ValueError, match="Invalid ADLS URI"):
            fs._parse_adls_uri("https://other.url/path")

    def test_parse_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid ADLS URI"):
            fs._parse_adls_uri("")

    def test_parse_missing_scheme(self) -> None:
        with pytest.raises(ValueError, match="Invalid ADLS URI"):
            fs._parse_adls_uri("cont@acct.dfs.core.windows.net/path")


# ===================================================================
# _is_local_path
# ===================================================================


class TestIsLocalPath:
    def test_absolute_path(self) -> None:
        assert fs._is_local_path("/mnt/data")

    def test_relative_path(self) -> None:
        assert fs._is_local_path("data/file.txt")

    def test_adls_path_is_not_local(self) -> None:
        assert not fs._is_local_path("abfss://cont@acct.dfs.core.windows.net/path")

    def test_adls_short(self) -> None:
        assert not fs._is_local_path("abfs://c@a.dfs.core.windows.net")


# ===================================================================
# _adls_to_https_uri
# ===================================================================


class TestAdlsToHttpsUri:
    def test_converts_abfss_to_https(self) -> None:
        result = fs._adls_to_https_uri(
            "abfss://cont@acct.dfs.core.windows.net/path/to/file"
        )
        assert result == "https://acct.dfs.core.windows.net/cont/path/to/file"

    def test_converts_abfs_to_https(self) -> None:
        result = fs._adls_to_https_uri(
            "abfs://c@a.dfs.core.windows.net/x/y"
        )
        assert result == "https://a.dfs.core.windows.net/c/x/y"

    def test_root_path(self) -> None:
        result = fs._adls_to_https_uri(
            "abfss://cont@acct.dfs.core.windows.net"
        )
        assert result == "https://acct.dfs.core.windows.net/cont/"

    def test_local_path_passthrough(self) -> None:
        result = fs._adls_to_https_uri("/local/path/file.txt")
        assert result == "/local/path/file.txt"

    def test_relative_path_passthrough(self) -> None:
        result = fs._adls_to_https_uri("data/file.txt")
        assert result == "data/file.txt"


# ===================================================================
# _adls_account_from_host / _adls_account_url
# ===================================================================


class TestAdlsAccountHelpers:
    def test_account_from_host_standard(self) -> None:
        assert fs._adls_account_from_host("myacct.dfs.core.windows.net") == "myacct"

    def test_account_from_host_onelake(self) -> None:
        assert fs._adls_account_from_host("onelake.dfs.fabric.microsoft.com") == "onelake"

    def test_account_url_standard(self) -> None:
        assert fs._adls_account_url("myacct.dfs.core.windows.net") == "https://myacct.dfs.core.windows.net"

    def test_account_url_onelake(self) -> None:
        assert fs._adls_account_url("onelake.dfs.fabric.microsoft.com") == "https://onelake.dfs.fabric.microsoft.com"


# ===================================================================
# _is_s3_path / _is_gcs_path
# ===================================================================


class TestS3Path:
    def test_s3_url(self) -> None:
        assert fs._is_s3_path("s3://my-bucket/key")

    def test_non_s3(self) -> None:
        assert not fs._is_s3_path("/local/path")


class TestGcsPath:
    def test_gcs_url(self) -> None:
        assert fs._is_gcs_path("gs://my-bucket/object")

    def test_non_gcs(self) -> None:
        assert not fs._is_gcs_path("/local/path")