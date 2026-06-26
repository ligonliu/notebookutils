"""
notebookutils.fs_adapter — thin adapter between the notebookutils.fs public API
and fsspec-compatible filesystem implementations.

This module provides:
- ``_get_fs(path)`` → the correct ``fsspec.AbstractFileSystem`` for a URI
- ``_to_msfileinfo(entries)`` → convert fsspec detail dicts to MSFileInfo
- ``_to_mountpointinfo(entries)`` → convert mount entries to MountPointInfo
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

import fsspec

from .config import (
    AzureStorageConfig,
    GCSConfig,
    HDFSConfig,
    S3Config,
    StorageProfile,
    load_profile,
)

# Re-use the data classes from fs.py to avoid circular imports.
# They are imported lazily inside _to_msfileinfo / _to_mountpointinfo.

# ---------------------------------------------------------------------------
# URI scheme constants
# ---------------------------------------------------------------------------
_SCHEME_ADLS = "abfss"
_SCHEME_S3 = "s3"
_SCHEME_GCS = "gs"


# ---------------------------------------------------------------------------
# FileSystem factory
# ---------------------------------------------------------------------------

def _get_fs_for_profile(profile: StorageProfile):
    """Create an fsspec filesystem from a StorageProfile.

    Returns
    -------
    fsspec.AbstractFileSystem
    """
    if profile.backend == "azstorage":
        return _make_adlfs(profile.azstorage)
    elif profile.backend == "s3":
        return _make_s3fs(profile.s3)
    elif profile.backend == "gs":
        return _make_gcsfs(profile.gs)
    elif profile.backend == "hdfs":
        return _make_hdfs_fs(profile.hdfs)
    else:
        raise ValueError(f"Unknown backend: {profile.backend}")


def _get_fs(path: str) -> fsspec.AbstractFileSystem:
    """Return the appropriate fsspec filesystem for *path*.

    Supports:
    - ``abfss://...`` → adlfs.AzureBlobFileSystem
    - ``s3://...`` → s3fs.S3FileSystem
    - ``gs://...`` → gcsfs.GCSFileSystem
    - ``file:/...`` or plain paths → fsspec local filesystem
    """
    if path.startswith(f"{_SCHEME_ADLS}://") or path.startswith(f"{_SCHEME_ADLS}://"):
        return _get_cached_adlfs()
    elif path.startswith(f"{_SCHEME_S3}://"):
        return _get_cached_s3fs()
    elif path.startswith(f"{_SCHEME_GCS}://"):
        return _get_cached_gcsfs()
    else:
        return fsspec.filesystem("file")


# ---------------------------------------------------------------------------
# Azure (adlfs)
# ---------------------------------------------------------------------------

_adlfs_cache: Optional[Any] = None


def _get_cached_adlfs():
    """Return a cached adlfs filesystem using ambient Azure credentials."""
    global _adlfs_cache
    if _adlfs_cache is None:
        from adlfs import AzureBlobFileSystem
        _adlfs_cache = AzureBlobFileSystem(anon=False)
    return _adlfs_cache


def _make_adlfs(cfg: AzureStorageConfig):
    """Build an adlfs filesystem from explicit config."""
    from adlfs import AzureBlobFileSystem

    if cfg.mode == "key":
        return AzureBlobFileSystem(
            account_name=cfg.account_name,
            account_key=cfg.account_key,
            account_host=cfg.endpoint or None,
        )
    elif cfg.mode == "sas":
        return AzureBlobFileSystem(
            account_name=cfg.account_name,
            sas_token=cfg.sas_token,
            account_host=cfg.endpoint or None,
        )
    elif cfg.mode in ("spn", "azcli", "msi"):
        return AzureBlobFileSystem(
            account_name=cfg.account_name,
            tenant_id=cfg.tenant_id,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            account_host=cfg.endpoint or None,
            anon=False,
        )
    else:
        return AzureBlobFileSystem(anon=False)


# ---------------------------------------------------------------------------
# S3 (s3fs)
# ---------------------------------------------------------------------------

_s3fs_cache: Optional[Any] = None


def _get_cached_s3fs():
    """Return a cached s3fs filesystem using ambient AWS credentials."""
    global _s3fs_cache
    if _s3fs_cache is None:
        import s3fs
        _s3fs_cache = s3fs.S3FileSystem()
    return _s3fs_cache


def _make_s3fs(cfg: S3Config):
    """Build an s3fs filesystem from explicit config."""
    import s3fs

    kwargs: dict = {}

    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url

    client_kwargs: dict = {}
    if cfg.region:
        client_kwargs["region_name"] = cfg.region
    if client_kwargs:
        kwargs["client_kwargs"] = client_kwargs

    if cfg.mode == "key" and cfg.access_key_id:
        kwargs["key"] = cfg.access_key_id
        kwargs["secret"] = cfg.secret_access_key

    if cfg.addressing_style:
        ck = kwargs.setdefault("config_kwargs", {})
        ck.setdefault("s3", {})["addressing_style"] = cfg.addressing_style

    if cfg.verify_ssl is False:
        kwargs["verify"] = False

    return s3fs.S3FileSystem(**kwargs)


# ---------------------------------------------------------------------------
# GCS (gcsfs)
# ---------------------------------------------------------------------------

_gcsfs_cache: Optional[Any] = None


def _get_cached_gcsfs():
    """Return a cached gcsfs filesystem using ambient GCP credentials."""
    global _gcsfs_cache
    if _gcsfs_cache is None:
        import gcsfs
        _gcsfs_cache = gcsfs.GCSFileSystem()
    return _gcsfs_cache


def _make_gcsfs(cfg: GCSConfig):
    """Build a gcsfs filesystem from explicit config."""
    import gcsfs

    kwargs: dict = {}
    if cfg.project:
        kwargs["project"] = cfg.project
    if cfg.service_account_key_file:
        kwargs["token"] = cfg.service_account_key_file
    return gcsfs.GCSFileSystem(**kwargs)


# ---------------------------------------------------------------------------
# HDFS
# ---------------------------------------------------------------------------

def _make_hdfs_fs(cfg: HDFSConfig):
    """Build an HDFS filesystem via fsspec / pyarrow."""
    kwargs: dict = {
        "host": cfg.namenode,
        "port": cfg.port,
    }
    if cfg.hadoop_conf_dir:
        os.environ.setdefault("HADOOP_CONF_DIR", cfg.hadoop_conf_dir)
    return fsspec.filesystem("hdfs", **kwargs)


# ---------------------------------------------------------------------------
# Type conversion helpers
# ---------------------------------------------------------------------------

def _to_msfileinfo(entries: list[dict]) -> list:
    """Convert fsspec ``detail=True`` listing to MSFileInfo objects.

    Parameters
    ----------
    entries : list[dict]
        Result of ``fs.ls(path, detail=True)``.

    Returns
    -------
    list[MSFileInfo]
    """
    # Lazy import to avoid circular dependency with fs.py
    from .fs import MSFileInfo

    result = []
    for entry in entries:
        name = entry.get("name", "")
        entry_type = entry.get("type", "file")
        is_dir = entry_type == "directory"
        size = entry.get("size", 0) or 0

        # Normalise modifyTime: some backends return datetime, others epoch
        mtime_val = entry.get("last_modified") or entry.get("modifyTime")
        if isinstance(mtime_val, datetime):
            modify_time = int(mtime_val.timestamp() * 1000)
        elif isinstance(mtime_val, (int, float)):
            modify_time = int(mtime_val * 1000) if mtime_val < 1e12 else int(mtime_val)
        else:
            modify_time = 0

        result.append(MSFileInfo(
            name=os.path.basename(name.rstrip("/")),
            size=size,
            path=name,
            isDir=is_dir,
            isFile=not is_dir,
            modifyTime=modify_time,
        ))
    return result


def _to_mountpointinfo(entries: list[dict]) -> list:
    """Convert raw mount entries to MountPointInfo objects.

    Parameters
    ----------
    entries : list[dict]
        Each entry should have keys ``mountPoint``, ``source``, ``fileSystemType``.

    Returns
    -------
    list[MountPointInfo]
    """
    from .fs import MountPointInfo

    result = []
    for entry in entries:
        result.append(MountPointInfo(
            mountPoint=entry.get("mountPoint", ""),
            source=entry.get("source", ""),
            fileSystemType=entry.get("fileSystemType", ""),
        ))
    return result


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

def _normalise_path(path: str) -> str:
    """Strip the ``file:/`` prefix from a path if present.

    >>> _normalise_path("file:/tmp/data")
    '/tmp/data'

    >>> _normalise_path("/tmp/data")
    '/tmp/data'

    >>> _normalise_path("abfss://c@a.dfs.core.windows.net/d")
    'abfss://c@a.dfs.core.windows.net/d'
    """
    if path.startswith("file:/"):
        return path[5:]
    return path
