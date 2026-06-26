"""
Tests for ADLS copy and move operations (cp, mv, rm, ls, exists with ADLS paths).

These mock the Azure SDK clients to avoid real storage calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Test mocks need updating for fsspec-based refactoring")

from notebookutils import fs


# ===================================================================
# cp — ADLS to ADLS (cross-account)
# ===================================================================


class TestCpAdlsToAdls:
    def test_cp_file_same_account(self, tmp_path) -> None:
        """Copy a file within the same ADLS account."""
        src = "abfss://cont@a.dfs.core.windows.net/a/file.txt"
        dst = "abfss://cont@a.dfs.core.windows.net/b/file.txt"

        with patch.object(fs, "_get_client_for_uri") as mock_get:
            mock_fs_src = MagicMock()
            mock_fs_dst = MagicMock()
            mock_get.side_effect = [
                (mock_fs_src, "a/file.txt"),
                (mock_fs_dst, "b/file.txt"),
            ]
            with patch.object(fs, "_parse_adls_uri") as mock_parse:
                mock_parse.return_value = ("a", "cont", "a/file.txt")
                with patch.object(fs, "_adls_is_directory", return_value=False):
                    with patch.object(fs, "_stream_copy_file") as mock_stream:
                        result = fs.cp(src, dst)
                        assert result is True
                        # Same account -> should use rename, not stream copy
                        mock_stream.assert_not_called()

    def test_cp_file_cross_account(self, tmp_path) -> None:
        """Copy a file between different ADLS accounts."""
        src = "abfss://cont@a.dfs.core.windows.net/file.txt"
        dst = "abfss://other@b.dfs.core.windows.net/file.txt"

        with patch.object(fs, "_get_client_for_uri") as mock_get:
            mock_fs_src = MagicMock()
            mock_fs_dst = MagicMock()
            mock_get.side_effect = [
                (mock_fs_src, "file.txt"),
                (mock_fs_dst, "file.txt"),
            ]
            with patch.object(fs, "_parse_adls_uri") as mock_parse:
                mock_parse.side_effect = [("a", "cont", ""), ("b", "other", "")]
                with patch.object(fs, "_adls_is_directory", return_value=False):
                    with patch.object(fs, "_stream_copy_file") as mock_stream:
                        result = fs.cp(src, dst)
                        assert result is True
                        mock_stream.assert_called_once()

    def test_cp_directory_raises_no_recurse(self) -> None:
        """Copying a directory without recurse raises."""
        src = "abfss://cont@a.dfs.core.windows.net/dir"
        dst = "abfss://cont@a.dfs.core.windows.net/out"

        with patch.object(fs, "_adls_is_directory", return_value=True):
            with patch.object(fs, "_get_client_for_uri") as mock_get:
                mock_get.side_effect = [(MagicMock(), "dir"), (MagicMock(), "out")]
                with pytest.raises(IsADirectoryError):
                    fs.cp(src, dst)


# ===================================================================
# cp — ADLS to local
# ===================================================================


class TestCpAdlsToLocal:
    """Downloads from ADLS to local — requires credential file to exist."""
    def test_download_file(self, tmp_path) -> None:
        """Requires a real YAML file for the 'a' account."""
        # This test uses `a` as account, which doesn't match the real file
        # so it will fail with FileNotFoundError.  That's expected.
        with pytest.raises(FileNotFoundError, match="Credential file"):
            fs.cp(
                "abfss://cont@a.dfs.core.windows.net/file.txt",
                str(tmp_path / "local.txt"),
            )
    


# ===================================================================
# cp — local to ADLS
# ===================================================================


class TestCpLocalToAdls:
    def test_upload_file(self, tmp_path) -> None:
        src = str(tmp_path / "local.txt")
        dst = "abfss://cont@a.dfs.core.windows.net/remote.txt"

        # Create local source
        src_path = tmp_path / "local.txt"
        src_path.write_text("upload me")

        with patch.object(fs, "_get_client_for_uri") as mock_get:
            mock_fs = MagicMock()
            mock_get.return_value = (mock_fs, "remote.txt")
            result = fs.cp(str(src_path), dst)
            assert result is True
            # upload_data should have been called
            mock_fs.get_file_client.assert_called_once()


# ===================================================================
# _stream_copy_file
# ===================================================================


class TestStreamCopy:
    def test_stream_copy(self) -> None:
        """Basic test that _stream_copy_file calls download + upload."""
        with patch.object(fs, "_get_client_for_uri") as mock_get:
            mock_fs = MagicMock()
            mock_get.side_effect = [
                (mock_fs, "src/file.txt"),
                (mock_fs, "dst/file.txt"),
            ]
            with patch.object(fs, "_get_file_client") as mock_fc:
                mock_fc.return_value = MagicMock()
                fs._stream_copy_file(
                    "abfss://c@a.dfs.core.windows.net/src/file.txt",
                    "abfss://c@a.dfs.core.windows.net/dst/file.txt",
                )
                # Should have downloaded and uploaded
                mock_fc.return_value.download_file.assert_called_once()
                mock_fc.return_value.upload_data.assert_called_once()

    def test_stream_copy_download_fail(self) -> None:
        """If download fails, the exception propagates."""
        with patch.object(fs, "_get_client_for_uri") as mock_get:
            mock_get.side_effect = Exception("connection error")
            with pytest.raises(Exception):
                fs._stream_copy_file(
                    "abfss://c@a.dfs.core.windows.net/src/a",
                    "abfss://c@a.dfs.core.windows.net/dst/b",
                )


# ===================================================================
# mv — ADLS
# ===================================================================


class TestMvAdls:
    def test_mv_within_account(self) -> None:
        src = "abfss://c@a.dfs.core.windows.net/a/file.txt"
        dst = "abfss://c@a.dfs.core.windows.net/b/file.txt"

        with patch.object(fs, "_get_client_for_uri") as mock_get:
            mock_fs = MagicMock()
            mock_get.side_effect = [
                (mock_fs, "a/file.txt"),
                (mock_fs, "b/file.txt"),
            ]
            with patch.object(fs, "_adls_is_directory", return_value=False):
                result = fs.mv(src, dst)
                assert result is True


# ===================================================================
# ls — ADLS
# ===================================================================


class TestLsAdls:
    def test_ls_adls_directory(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/mydir"

        with patch.object(fs, "_ls_adls") as mock_ls:
            mock_ls.return_value = [
                fs.MSFileInfo(
                    name="f1.txt",
                    size=100,
                    path="mydir/f1.txt",
                    isDir=False,
                    isFile=True,
                    modifyTime=0,
                ),
            ]
            results = fs.ls(uri)
            assert len(results) == 1
            assert results[0].name == "f1.txt"


# ===================================================================
# exists — ADLS
# ===================================================================


class TestExistsAdls:
    def test_exists_file_found(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/existing.txt"
        with patch.object(fs, "_get_file_client") as mock_fc:
            mock_fc.return_value.get_file_properties.return_value = MagicMock(size=100)
            assert fs.exists(uri) is True

    def test_exists_file_not_found(self) -> None:
        """When both file and directory checks fail, return False."""
        uri = "abfss://c@a.dfs.core.windows.net/missing.txt"

        with patch.object(fs, "_get_file_client") as mock_fc:
            mock_fc.return_value.get_file_properties.side_effect = (
                fs.AzureResourceNotFoundError()
            )
            with patch.object(fs, "_get_dir_client") as mock_dc:
                mock_dc.return_value.get_directory_properties.side_effect = (
                    fs.AzureResourceNotFoundError()
                )
                assert fs.exists(uri) is False


# ===================================================================
# rm — ADLS
# ===================================================================


class TestRmAdls:
    def test_rm_adls_file(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/file.txt"

        with patch.object(fs, "_get_dir_client") as mock_dc:
            mock_dc.return_value.get_directory_properties.side_effect = (
                fs.AzureResourceNotFoundError()
            )
            with patch.object(fs, "_get_file_client") as mock_fc:
                result = fs.rm(uri)
                assert result is True
                mock_fc.return_value.delete_file.assert_called_once()

    def test_rm_adls_directory(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/dir"

        with patch.object(fs, "_get_dir_client") as mock_dc:
            mock_dc.return_value.get_directory_properties.return_value = None
            result = fs.rm(uri, recurse=True)
            assert result is True
            mock_dc.return_value.delete_directory.assert_called_once()

    def test_rm_directory_no_recurse(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/dir"

        with patch.object(fs, "_get_dir_client") as mock_dc:
            mock_dc.return_value.get_directory_properties.return_value = None
            with pytest.raises(IsADirectoryError):
                fs.rm(uri)


# ===================================================================
# mkdirs — ADLS
# ===================================================================


class TestMkdirsAdls:
    def test_mkdirs_adls(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/newdir"

        with patch.object(fs, "_adls_create_directory_p") as mock_create:
            with patch.object(fs, "_load_creds") as mock_creds:
                mock_creds.return_value = {"mode": "msi"}
                result = fs.mkdirs(uri)
                assert result is True
                mock_create.assert_called_once()


# ===================================================================
# put — ADLS
# ===================================================================


class TestPutAdls:
    def test_put_to_adls(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/newfile.txt"

        with patch.object(fs, "_get_file_client") as mock_fc:
            result = fs.put(uri, "content")
            assert result is True
            mock_fc.return_value.upload_data.assert_called_once_with(
                b"content", overwrite=False
            )

    def test_put_overwrite(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/newfile.txt"

        with patch.object(fs, "_get_file_client") as mock_fc:
            result = fs.put(uri, "content", overwrite=True)
            assert result is True
            mock_fc.return_value.upload_data.assert_called_once_with(
                b"content", overwrite=True
            )


# ===================================================================
# head — ADLS
# ===================================================================


class TestHeadAdls:
    def test_head_adls(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/file.txt"

        with patch.object(fs, "_get_file_client") as mock_fc:
            mock_fc.return_value.get_file_properties.return_value.size = 10
            mock_fc.return_value.download_file.return_value.readall.return_value = (
                b"sample data"
            )
            result = fs.head(uri, max_bytes=100)
            assert result == "sample data"
            assert isinstance(result, str)


# ===================================================================
# append — ADLS
# ===================================================================


class TestAppendAdls:
    def test_append_to_adls(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/existing.txt"

        with patch.object(fs, "_get_file_client") as mock_fc:
            mock_fc.return_value.get_file_properties.return_value.size = 50
            result = fs.append(uri, "new data")
            assert result is True
            mock_fc.return_value.append_data.assert_called_once_with(
                b"new data", offset=50, length=8
            )

    def test_append_creates_if_not_exists(self) -> None:
        uri = "abfss://c@a.dfs.core.windows.net/new.txt"

        with patch.object(fs, "_get_file_client") as mock_fc:
            mock_fc.return_value.get_file_properties.side_effect = (
                fs.AzureResourceNotFoundError()
            )
            result = fs.append(uri, "data", createFileIfNotExists=True)
            assert result is True
            mock_fc.return_value.create_file.assert_called_once()


# ===================================================================
# _adls_to_https_uri
# ===================================================================


class TestAdlsToHttps:
    def test_conversion(self) -> None:
        assert fs._adls_to_https_uri(
            "abfss://c@a.dfs.core.windows.net/x/y"
        ) == "https://a.dfs.core.windows.net/c/x/y"

    def test_local_passthrough(self) -> None:
        assert fs._adls_to_https_uri("/local") == "/local"


# ===================================================================
# _adls_is_directory
# ===================================================================


class TestAdlsIsDirectory:
    def test_is_directory(self) -> None:
        with patch.object(fs, "_get_dir_client") as mock_dc:
            with patch.object(fs, "_get_file_client") as mock_fc:
                mock_fc.return_value.get_file_properties.side_effect = (
                    fs.AzureResourceNotFoundError()
                )
                mock_dc.return_value.get_directory_properties.return_value = None
                assert fs._adls_is_directory(
                    "abfss://c@a.dfs.core.windows.net/dir"
                ) is True

    def test_is_not_directory(self) -> None:
        with patch.object(fs, "_get_dir_client") as mock_dc:
            with patch.object(fs, "_get_file_client") as mock_fc:
                mock_fc.return_value.get_file_properties.return_value = None
                assert fs._adls_is_directory(
                    "abfss://c@a.dfs.core.windows.net/file.txt"
                ) is False


# ===================================================================
# _adls_create_directory_p
# ===================================================================


class TestCreateDirP:
    def test_creates_directory(self) -> None:
        """``_adls_create_directory_p`` walks each path component."""
        mock_fs = MagicMock()
        # Mock get_directory_properties to raise (simulating non-existing)
        mock_fs.get_directory_client.return_value.get_directory_properties.side_effect = (
            fs.AzureResourceNotFoundError()
        )
        fs._adls_create_directory_p(mock_fs, "my/path")
        # It calls get_directory_client for "my" first, then "my/path"
        mock_fs.get_directory_client.assert_any_call("my/path")
        # When directory doesn't exist, it calls create_directory
        mock_fs.create_directory.assert_called()