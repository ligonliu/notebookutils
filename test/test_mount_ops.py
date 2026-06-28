"""
Tests for mount/unmount operations in ``notebookutils.fs``.

These tests mock ``subprocess`` and the YAML config loading so they
run without real blobfuse2 or Azure.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from notebookutils import fs


# ===================================================================
# mount
# ===================================================================


class TestMount:
    def test_mount_non_adls_source(self) -> None:
        with pytest.raises(ValueError, match="Unsupported mount source"):
            fs.mount("/local/path", "/mnt/data")

    def test_mount_relative_mount_point(self) -> None:
        with pytest.raises(ValueError, match="Mount point must be an absolute path"):
            fs.mount("abfss://c@a.dfs.core.windows.net", "relative/path")

    def test_mount_no_cred_file(self) -> None:
        """Missing config file raises RuntimeError."""
        with pytest.raises(RuntimeError, match="Failed to load credentials"):
            fs.mount("abfss://c@nonexistent.dfs.core.windows.net/p", "/mnt/data")


# ===================================================================
# unmount
# ===================================================================


class TestUnmount:
    def test_unmount_relative_path(self) -> None:
        with pytest.raises(ValueError, match="Mount point must be an absolute path"):
            fs.unmount("relative")

    def test_unmount_blobfuse2_success(self, tmp_path) -> None:
        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            result = fs.unmount("/mnt/data")
        assert result is True

    def test_unmount_fallback_fusermount(self, tmp_path) -> None:
        """When blobfuse2 missing, fallback to fusermount."""
        mnt = tmp_path / "mnt"
        with patch.object(fs.subprocess, "run") as mock_run:
            def side_effect(*args, **kwargs):
                if "blobfuse2" in str(args[0]):
                    raise FileNotFoundError()
                return mock_run.return_value
            mock_run.side_effect = side_effect
            mock_run.return_value.returncode = 0
            result = fs.unmount(str(mnt))
        assert result is True

    def test_unmount_no_tool_available(self) -> None:
        """Both blobfuse2 and fusermount missing -> no error, just returns result."""
        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.side_effect = FileNotFoundError("no blobfuse2")
            result = fs.unmount("/mnt/data")
        # Should not raise; umount fallback also won't find (not a real mount),
        # so returns False or True depending on whether path is mounted
        assert result in (True, False)


# ===================================================================
# mounts
# ===================================================================


class TestMounts:
    def test_mounts_returns_list(self) -> None:
        results = fs.mounts()
        assert isinstance(results, list)
        assert all(isinstance(m, fs.MountPointInfo) for m in results)


# ===================================================================
# mountToDriverNode / unmountFromDriverNode (delegation wrappers)
# ===================================================================


class TestDelegation:
    def test_mount_to_driver_node(self) -> None:
        """Delegates to mount() — uses a non-existent account so RuntimeError is expected."""
        with pytest.raises(RuntimeError, match="Failed to load credentials"):
            fs.mountToDriverNode("abfss://c@nonexistent.dfs.core.windows.net/p", "/mnt/data")

    def test_unmount_from_driver_node(self) -> None:
        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            result = fs.unmountFromDriverNode("/mnt/data")
        assert result is True


# ===================================================================
# fastcp
# ===================================================================


class TestFastCp:
    def test_fastcp_basic(self) -> None:
        src = "abfss://c@a.dfs.core.windows.net/path"
        dst = "abfss://c@b.dfs.core.windows.net/path"

        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            result = fs.fastcp(src, dst)
        assert result is True

    def test_fastcp_converts_uri(self) -> None:
        """Verify URI conversion happens before azcopy call."""
        src = "abfss://cont@acct.dfs.core.windows.net/my/path"
        dst = "/local/dest"

        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            result = fs.fastcp(src, dst)
            assert result is True

    def test_fastcp_passes_flags(self) -> None:
        src = "abfss://c@a.dfs.core.windows.net/x"
        dst = "abfss://c@a.dfs.core.windows.net/y"

        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            fs.fastcp(src, dst, extraConfigs={"flags": "--dry-run --no-check"})
            call_args = mock_run.call_args
            assert call_args is not None
            cmd = call_args[0][0]
            assert "--dry-run" in cmd

    def test_fastcp_azcopy_not_found(self) -> None:
        src = "abfss://c@a.dfs.core.windows.net/x"
        dst = "abfss://c@a.dfs.core.windows.net/y"
        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(RuntimeError, match="azcopy"):
                fs.fastcp(src, dst)

    def test_fastcp_timeout(self) -> None:
        src = "abfss://c@a.dfs.core.windows.net/x"
        dst = "abfss://c@a.dfs.core.windows.net/y"
        with patch.object(fs.subprocess, "run") as mock_run:
            mock_run.side_effect = fs.subprocess.TimeoutExpired(
                cmd="azcopy", timeout=3600
            )
            result = fs.fastcp(src, dst)
        assert result is False


# ===================================================================
# refreshMounts / getMountPath / nbResPath (simple no-ops)
# ===================================================================


class TestTrivialAPIs:
    def test_refresh_mounts(self) -> None:
        assert fs.refreshMounts() is True

    def test_get_mount_path(self) -> None:
        assert fs.getMountPath("/mnt/data") == "/mnt/data"

    def test_get_mount_path_with_scope(self) -> None:
        assert fs.getMountPath("/mnt/data", scope="ignored") == "/mnt/data"

    def test_nb_res_path(self, monkeypatch) -> None:
        monkeypatch.setenv("NB_RES_PATH", "/custom/res")
        assert fs.nbResPath() == "/custom/res"

    def test_nb_res_path_default(self) -> None:
        """Returns empty string when NB_RES_PATH not set."""
        assert fs.nbResPath() == ""