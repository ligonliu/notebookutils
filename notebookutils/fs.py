"""
notebookutils.fs provides utilities for working with file systems across
multiple cloud providers (Azure ADLS Gen2, OneLake, AWS S3, GCP GCS) and
local storage.

Data-plane operations (cp, ls, rm, etc.) are handled via fsspec, with a
special-case optimization for ADLS Gen2 / OneLake hierarchical namespace
(HNS) mv operations using the azure-storage-file-datalake SDK for
metadata-only atomic renames.

Mount operations use per-cloud FUSE drivers: blobfuse2 (Azure), mount-s3 (S3),
gcsfuse (GCS), and hdfs-fuse (HDFS).

Credentials are loaded from YAML files in blobfuse2 configuration format
or the unified multi-cloud spec, stored under:
    $HOME/.notebookutils/storage/<profile-name>.yaml

Supported URI formats:
    abfss://<container>@<account>.dfs.core.windows.net/<path>    (ADLS Gen2)
    abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<lh>/... (OneLake)
    s3://<bucket>/<key>                                          (AWS S3 / compatible)
    gs://<bucket>/<object>                                       (GCP GCS)
    file:/<path>  or  /<path>                                    (local)

Local paths (including /mnt/...) are handled via fsspec's local filesystem.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml

from azure.core.exceptions import ResourceNotFoundError as AzureResourceNotFoundError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.filedatalake import (
    DataLakeDirectoryClient,
    DataLakeFileClient,
    DataLakeServiceClient,
    FileSystemClient,
)

__all__ = [
    "help",
    "cp",
    "mv",
    "ls",
    "mkdirs",
    "put",
    "head",
    "append",
    "rm",
    "exists",
    "mount",
    "unmount",
    "mounts",
    "refreshMounts",
    "getMountPath",
    "mountToDriverNode",
    "unmountFromDriverNode",
    "fastcp",
    "MSFileInfo",
    "MountPointInfo",
    "nbResPath",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_STORAGE_CONFIG_DIR = os.path.join(os.environ.get("HOME", "/tmp"), ".notebookutils", "storage")

# Generalized ADLS URI regex: accepts any DFS host (standard ADLS Gen2, OneLake, etc.)
# Matches: abfss://<container>@<host>/<path>
#   host = account.dfs.core.windows.net  (standard ADLS Gen2)
#   host = onelake.dfs.fabric.microsoft.com  (OneLake)
_ADLS_URI_RE = re.compile(
    r"^abfss?://"                        # scheme
    r"(?P<container>[^@]+)"              # container or workspace name
    r"@"                                 # literal @
    r"(?P<host>[^/]+)"                   # host (account.dfs.core.windows.net or onelake.dfs.fabric.microsoft.com)
    r"(?P<path>/.*)?$"                   # optional path
)

# Other recognised schemes for dispatch
_S3_URI_RE = re.compile(r"^s3://")
_GCS_URI_RE = re.compile(r"^gs://")
_FILE_URI_RE = re.compile(r"^file:/")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MSFileInfo:
    """Represents a file or directory entry as returned by ls()."""

    name: str
    """Base name of the file or directory."""
    size: int
    """Size in bytes (0 for directories)."""
    path: str
    """Full path (including parent directory)."""
    isDir: bool
    """True if this is a directory."""
    isFile: bool
    """True if this is a file."""
    modifyTime: int
    """Last modification time as epoch milliseconds."""


@dataclass
class MountPointInfo:
    """Represents an active mount point."""

    mountPoint: str
    """Local mount path."""
    source: str
    """Remote source URI (abfss://...)."""
    fileSystemType: str
    """Filesystem type (typically 'fuse' or 'blobfuse2')."""


# ---------------------------------------------------------------------------
# Internal helpers -- path parsing / scheme detection
# ---------------------------------------------------------------------------

def _is_adls_path(path: str) -> bool:
    """Return True if *path* looks like an ADLS / OneLake URI."""
    return bool(_ADLS_URI_RE.match(path))


def _is_s3_path(path: str) -> bool:
    """Return True if *path* looks like an S3 URI."""
    return bool(_S3_URI_RE.match(path))


def _is_gcs_path(path: str) -> bool:
    """Return True if *path* looks like a GCS URI."""
    return bool(_GCS_URI_RE.match(path))


def _is_cloud_path(path: str) -> bool:
    """Return True if *path* looks like any cloud storage URI."""
    return _is_adls_path(path) or _is_s3_path(path) or _is_gcs_path(path)


def _is_local_path(path: str) -> bool:
    """Return True if *path* looks like a local filesystem path."""
    return not _is_cloud_path(path)


def _parse_adls_uri(uri: str) -> tuple[str, str, str]:
    """Parse an ADLS or OneLake URI into (host, container, path).

    For standard ADLS Gen2, host = ``account.dfs.core.windows.net``
    and account name can be extracted by splitting on the first ``.``.
    For OneLake, host = ``onelake.dfs.fabric.microsoft.com``.

    Examples
    --------
    >>> _parse_adls_uri("abfss://mycont@myacct.dfs.core.windows.net/a/b")
    ('myacct.dfs.core.windows.net', 'mycont', '/a/b')

    >>> _parse_adls_uri("abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Files/x")
    ('onelake.dfs.fabric.microsoft.com', 'ws', '/lh/Files/x')
    """
    m = _ADLS_URI_RE.match(uri)
    if not m:
        raise ValueError(f"Invalid ADLS URI: {uri!r}")
    path = m.group("path") or "/"
    return m.group("host"), m.group("container"), path


def _adls_account_from_host(host: str) -> str:
    """Extract the storage account name from an ADLS host.

    For standard ADLS (``myacct.dfs.core.windows.net``) returns ``myacct``.
    For OneLake (``onelake.dfs.fabric.microsoft.com``) returns the first label.
    """
    return host.split(".")[0]


def _adls_account_url(host: str) -> str:
    """Return the DFS endpoint URL for an ADLS host.

    >>> _adls_account_url("myacct.dfs.core.windows.net")
    'https://myacct.dfs.core.windows.net'

    >>> _adls_account_url("onelake.dfs.fabric.microsoft.com")
    'https://onelake.dfs.fabric.microsoft.com'
    """
    return f"https://{host}"


# ---------------------------------------------------------------------------
# Internal helpers -- credential loading
# ---------------------------------------------------------------------------

_creds_cache: dict[str, dict[str, Any]] = {}
"""Module-level cache mapping account_name -> parsed azstorage config dict."""


def _get_config_path(account_name: str) -> str:
    """Return the path to the YAML config file for *account_name*."""
    return os.path.join(_STORAGE_CONFIG_DIR, f"{account_name}.yaml")


def _load_creds(account_name: str) -> dict[str, Any]:
    """Load and cache the azstorage config section for *account_name*.

    Looks for ``$HOME/.notebookutils/storage/<account_name>.yaml``.
    The YAML file must follow the unified multi-cloud spec format, i.e. it
    must contain an ``azstorage:`` top-level key.

    Raises
    ------
    FileNotFoundError
        If the YAML file does not exist.
    KeyError
        If the ``azstorage`` section is missing.
    """
    if account_name in _creds_cache:
        return _creds_cache[account_name]

    config_path = _get_config_path(account_name)
    if not os.path.isfile(config_path):
        raise FileNotFoundError(
            f"Credential file for account '{account_name}' not found: {config_path}"
        )

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    azstorage = config.get("azstorage")
    if not azstorage:
        raise KeyError(
            f"Missing 'azstorage:' section in {config_path}"
        )

    _creds_cache[account_name] = azstorage
    return azstorage


def _get_service_client(account_name: str, host: str | None = None) -> DataLakeServiceClient:
    """Create an authenticated DataLakeServiceClient for *account_name*.

    Parameters
    ----------
    account_name : str
        Name used to look up the credential YAML file (e.g. ``myacct``).
    host : str or None
        DFS host for constructing the account URL. If None, defaults to
        ``{account_name}.dfs.core.windows.net``. For OneLake, pass
        ``onelake.dfs.fabric.microsoft.com``.

    Authentication mode is determined by the ``mode`` field in the YAML
    config ``azstorage`` section. Supported modes:

    * ``key``   -- account-key
    * ``sas``   -- shared access signature (token)
    * ``spn``   -- service principal (clientid + clientsecret + tenantid)
    * ``msi``   -- managed identity (DefaultAzureCredential)
    * ``azcli`` -- Azure CLI credential (DefaultAzureCredential)
    """
    azstorage = _load_creds(account_name)
    mode: str = azstorage.get("mode", "key")
    account_url = _adls_account_url(host) if host else f"https://{account_name}.dfs.core.windows.net"

    if mode == "key":
        account_key = azstorage.get("account-key") or azstorage.get("accountkey")
        if not account_key:
            raise KeyError(
                f"'{account_name}.yaml': mode='key' but no account-key provided"
            )
        return DataLakeServiceClient(account_url, credential=account_key)

    elif mode == "sas":
        sas_token = azstorage.get("sas")
        if not sas_token:
            # Some configs use 'sastoken' as key
            sas_token = azstorage.get("sastoken")
        if not sas_token:
            raise KeyError(
                f"'{account_name}.yaml': mode='sas' but no SAS token provided"
            )
        return DataLakeServiceClient(account_url, credential=sas_token)

    elif mode in ("spn", "azcli"):
        # For SPN, DefaultAzureCredential can use env vars (AZURE_TENANT_ID,
        # AZURE_CLIENT_ID, AZURE_CLIENT_SECRET). For azcli it
        # picks up the appropriate credential automatically.
        return DataLakeServiceClient(
            account_url, credential=DefaultAzureCredential()
        )

    elif mode == "msi":
        # Use ManagedIdentityCredential with the explicit UAMI client-id
        # if provided, so we don't mistakenly fall through to SAMI.
        client_id = azstorage.get("appid")
        if client_id:
            credential = ManagedIdentityCredential(client_id=client_id)
        else:
            credential = DefaultAzureCredential()
        return DataLakeServiceClient(account_url, credential=credential)

    else:
        raise ValueError(
            f"'{account_name}.yaml': unsupported auth mode '{mode}'"
        )


def _get_service_client_for_uri(uri: str) -> DataLakeServiceClient:
    """Create a DataLakeServiceClient from an ADLS URI.

    Extracts the host and account name from the URI; the account name
    is used to look up credentials.
    """
    host, container, _ = _parse_adls_uri(uri)
    account = _adls_account_from_host(host)
    return _get_service_client(account, host=host)


def _get_fs_client(uri: str) -> FileSystemClient:
    """Return a FileSystemClient for the container in *uri*."""
    host, container, _ = _parse_adls_uri(uri)
    account = _adls_account_from_host(host)
    service = _get_service_client(account, host=host)
    return service.get_file_system_client(container)


def _get_dir_client(uri: str) -> DataLakeDirectoryClient:
    """Return a DataLakeDirectoryClient for a directory URI."""
    host, container, path = _parse_adls_uri(uri)
    account = _adls_account_from_host(host)
    service = _get_service_client(account, host=host)
    fs_client = service.get_file_system_client(container)
    rel = path.lstrip("/")
    if not rel:
        return fs_client.get_directory_client("/")
    return fs_client.get_directory_client(rel)


def _get_file_client(uri: str) -> DataLakeFileClient:
    """Return a DataLakeFileClient for a file URI."""
    host, container, path = _parse_adls_uri(uri)
    account = _adls_account_from_host(host)
    service = _get_service_client(account, host=host)
    fs_client = service.get_file_system_client(container)
    rel = path.lstrip("/")
    return fs_client.get_file_client(rel)


# ---------------------------------------------------------------------------
# fsspec adapter imports (lazy, at function level to avoid import cycles)
# ---------------------------------------------------------------------------
# The fsspec adapter provides _get_fs, _to_msfileinfo, _to_mountpointinfo.
# All fsspec imports are lazy to avoid hard dependency at module import time
# and to allow graceful degradation when only azstorage is used.


def _import_fs_adapter():
    """Lazily import the fsspec adapter. Raises ImportError if fsspec not installed."""
    from . import fs_adapter
    return fs_adapter


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def help(method_name: str | None = None) -> None:
    """Print help for the module or a specific method."""
    if method_name:
        fn = globals().get(method_name)
        if fn and callable(fn) and fn.__doc__:
            print(fn.__doc__)
        else:
            print(f"No help available for {method_name!r}")
    else:
        print(__doc__ or "notebookutils.fs -- file system utilities")


# ===================================================================
# Core file operations  (fsspec-backed)
# ===================================================================

def _normalise(path: str) -> str:
    """Normalise a local path by stripping the optional ``file:/`` prefix."""
    if path.startswith("file:/"):
        return path[5:]
    return path


def cp(src: str, dest: str, recurse: bool = False) -> bool:
    """Copies a file or directory, possibly across file systems.

    Uses server-side copy where supported (S3: ``copy_object``,
    GCS: ``rewriteTo``, Azure: ``start_copy_from_url``).
    Cross-cloud copies fall back to download-then-upload.

    Example: ``cp("abfss://cont@acct.dfs.core.windows.net/a",
                  "s3://my-bucket/b")``.
    """
    from .fs_adapter import _get_fs

    src_fs = _get_fs(src)
    src_p = src if not _is_local_path(src) else _normalise(src)

    # Enforce recurse for directories
    if not recurse and src_fs.isdir(src_p):
        raise IsADirectoryError(f"Source is a directory: {src!r}. Use recurse=True.")

    dst_fs = _get_fs(dest)
    dst_p = dest if not _is_local_path(dest) else _normalise(dest)

    # Same filesystem → use native copy
    if src_fs is dst_fs or type(src_fs) is type(dst_fs):
        if recurse and src_fs.isdir(src_p):
            if _is_local_path(src) and _is_local_path(dest):
                shutil.copytree(src_p, dst_p, dirs_exist_ok=True)
            else:
                dst_fs.put(src_p, dst_p, recursive=True)
            return True
        else:
            if _is_local_path(src) and _is_local_path(dest) and not os.path.isdir(src_p):
                os.makedirs(os.path.dirname(dst_p) or ".", exist_ok=True)
                shutil.copy2(src_p, dst_p)
            else:
                src_fs.cp_file(src_p, dst_p)
            return True

    # Cross-filesystem: download then upload
    import tempfile as _tmpmod
    with _tmpmod.TemporaryDirectory() as tmp:
        tmp_file = os.path.join(tmp, "cp_tmp")
        src_fs.get(src_p, tmp_file, recursive=recurse)
        dst_fs.put(tmp_file, dst_p, recursive=recurse)
    return True


def mv(src: str, dest: str, create_path: bool = False, overwrite: bool = False) -> bool:
    """Moves a file or directory, possibly across file systems.

    For ADLS Gen2 / OneLake same-account moves, uses the native HNS
    ``rename_file`` / ``rename_directory`` APIs (metadata-only, atomic).
    All other moves use fsspec's copy-then-delete semantics (server-side
    where supported).

    Example: ``mv("abfss://cont@acct.dfs.core.windows.net/a",
                  "abfss://cont@acct.dfs.core.windows.net/b")``.
    """
    from .fs_adapter import _get_fs

    if create_path:
        if _is_local_path(dest):
            os.makedirs(os.path.dirname(dest.rstrip("/")) or ".", exist_ok=True)
        else:
            fs = _get_fs(dest)
            parent = os.path.dirname(dest.rstrip("/"))
            if parent and not fs.exists(parent):
                fs.mkdirs(parent, exist_ok=True)

    if overwrite and exists(dest):
        rm(dest, recurse=True)

    # --- HNS native rename (ADLS Gen2 / OneLake same-account+container) ---
    if _is_adls_path(src) and _is_adls_path(dest):
        src_host, src_container, _ = _parse_adls_uri(src)
        dst_host, dst_container, _ = _parse_adls_uri(dest)
        if src_host == dst_host and src_container == dst_container:
            src_is_dir = _adls_is_directory(src)
            if src_is_dir:
                dc = _get_dir_client(src)
                # rename_directory requires "container/relative_path" format
                _, dst_rel = _parse_adls_uri(dest)
                dc.rename_directory(
                    new_name=f"{dst_container}/{dst_rel.lstrip('/')}"
                )
            else:
                fc = _get_file_client(src)
                _, dst_rel = _parse_adls_uri(dest)
                fc.rename_file(
                    new_name=f"{dst_container}/{dst_rel.lstrip('/')}"
                )

            # Invalidate the fsspec cache so subsequent ls/ exists calls see the move
            try:
                adlfs = _get_fs(src)
                adlfs.invalidate_cache()
            except Exception:
                pass
            return True

    # --- All other cases: fsspec mv (server-side copy + delete) ---
    src_fs = _get_fs(src)
    dst_fs = _get_fs(dest)

    # Normalise local paths
    src_p = src if not _is_local_path(src) else _normalise(src)
    dst_p = dest if not _is_local_path(dest) else _normalise(dest)

    if src_fs is dst_fs or type(src_fs) is type(dst_fs):
        src_fs.mv(src_p, dst_p, recursive=True, maxdepth=None)
    else:
        # Cross-cloud
        cp(src, dest, recurse=True)
        rm(src, recurse=True)

    return True


def ls(dir: str) -> list[MSFileInfo]:
    """Lists the contents of a directory.

    Returns a list of MSFileInfo objects with fields:
    ``name``, ``size``, ``path``, ``isDir``, ``isFile``, ``modifyTime``.

    Examples::

        ls("abfss://cont@acct.dfs.core.windows.net/my-folder/")
        ls("s3://my-bucket/prefix/")
        ls("gs://my-bucket/prefix/")
        ls("/tmp/data")
    """
    from .fs_adapter import _get_fs, _to_msfileinfo

    fs = _get_fs(dir)
    dir_p = dir if not _is_local_path(dir) else _normalise(dir)
    try:
        entries = fs.ls(dir_p, detail=True)
        return _to_msfileinfo(entries)
    except FileNotFoundError:
        return []
    except OSError:
        return []


def mkdirs(dir: str) -> bool:
    """Creates the given directory if it does not exist, also creating any
    necessary parent directories.

    Example: ``mkdirs("abfss://cont@acct.dfs.core.windows.net/a/b/c")``.
    """
    from .fs_adapter import _get_fs

    fs = _get_fs(dir)
    dir_p = dir if not _is_local_path(dir) else _normalise(dir)
    fs.mkdirs(dir_p, exist_ok=True)
    return True


def put(file: str, content: str, overwrite: bool = False) -> bool:
    """Writes the given String out to a file, encoded in UTF-8.

    Example: ``put("abfss://cont@acct.dfs.core.windows.net/myfile", "Hello!")``.
    """
    from .fs_adapter import _get_fs

    data = content.encode("utf-8")
    fs = _get_fs(file)
    file_p = file if not _is_local_path(file) else _normalise(file)

    if _is_local_path(file):
        os.makedirs(os.path.dirname(file_p) or ".", exist_ok=True)

    if not data:
        fs.touch(file_p)
        return True

    if overwrite:
        fs.pipe_file(file_p, data)
    else:
        if fs.exists(file_p):
            # fsspec pipe_file with mode="create" is not universally supported
            # so we use open with xb mode for exclusive creation
            try:
                with fs.open(file_p, "xb") as f:
                    f.write(data)
            except FileExistsError:
                return False
        else:
            fs.pipe_file(file_p, data)
    return True


def head(file: str, max_bytes: int = 1024 * 100) -> str:
    """Returns up to the first 'maxBytes' bytes of the given file as a String
    encoded in UTF-8.

    Example: ``head("abfss://cont@acct.dfs.core.windows.net/myfile")``.
    """
    from .fs_adapter import _get_fs

    fs = _get_fs(file)
    file_p = file if not _is_local_path(file) else _normalise(file)
    data = fs.cat_file(file_p, start=0, end=max_bytes)
    if isinstance(data, memoryview):
        data = bytes(data)
    return data.decode("utf-8", errors="replace")


def append(file: str, content: str, createFileIfNotExists: bool = False) -> bool:
    """Append the given String to a file, encoded in UTF-8.

    Example: ``append("abfss://cont@acct.dfs.core.windows.net/myfile", "Hello!")``.
    """
    from .fs_adapter import _get_fs

    data = content.encode("utf-8")
    fs = _get_fs(file)
    file_p = file if not _is_local_path(file) else _normalise(file)

    if not fs.exists(file_p):
        if createFileIfNotExists:
            if _is_local_path(file):
                os.makedirs(os.path.dirname(file_p) or ".", exist_ok=True)
            fs.touch(file_p)
        else:
            raise FileNotFoundError(f"File not found: {file!r}")

    with fs.open(file_p, "ab") as f:
        f.write(data)
    return True


def rm(dir: str, recurse: bool = False) -> bool:
    """Removes a file or directory.

    Example: ``rm("abfss://cont@acct.dfs.core.windows.net/myfile")``.
    """
    from .fs_adapter import _get_fs

    fs = _get_fs(dir)
    dir_p = dir if not _is_local_path(dir) else _normalise(dir)

    if not fs.exists(dir_p):
        return False

    is_dir = fs.isdir(dir_p)

    if is_dir and not recurse:
        raise IsADirectoryError(f"Path is a directory: {dir!r}. Use recurse=True.")

    if is_dir:
        if _is_local_path(dir):
            shutil.rmtree(dir_p)
        else:
            fs.rm(dir_p, recursive=True)
    else:
        fs.rm_file(dir_p)
    return True


def exists(file: str) -> bool:
    """Check if a file or directory exists.

    Example: ``exists("abfss://cont@acct.dfs.core.windows.net/myfile")``.
    """
    from .fs_adapter import _get_fs

    fs = _get_fs(file)
    file_p = file if not _is_local_path(file) else _normalise(file)
    return fs.exists(file_p)


# ---------------------------------------------------------------------------
# ADLS HNS helpers  (kept for the mv special case)
# ---------------------------------------------------------------------------

def _adls_is_directory(uri: str) -> bool:
    """Check whether an ADLS URI points to a directory.

    Uses a file-extension heuristic; falls back to probing the directory client.
    """
    host, container, path = _parse_adls_uri(uri)
    rel = path.strip("/")
    if not rel:
        return True

    if uri.endswith("/"):
        return True

    base = rel.rsplit("/", 1)[-1]
    if "." in base:
        return False

    dc = _get_dir_client(uri)
    try:
        dc.get_directory_properties()
        return True
    except AzureResourceNotFoundError:
        pass
    return False


# ===================================================================
# Mount operations  (multi-cloud FUSE dispatch)
# ===================================================================

# Known FUSE filesystem types for mount detection
_FUSE_MOUNT_TYPES = frozenset({
    "fuse", "blobfuse2", "blobfuse", "gcsfuse",
    "s3fs", "goofys", "mount-s3", "hdfs-fuse", "fuse_dfs",
})

_MOUNT_INFO_RE = re.compile(
    r"^(?P<source>\S+)\s+on\s+(?P<mountPoint>\S+)\s+type\s+(?P<fsType>\S+)\s+.*"
)


def _is_adls_uri_for_mount(source: str) -> bool:
    """Check whether *source* can be mounted as an ADLS path."""
    return _is_adls_path(source)


def _is_s3_uri_for_mount(source: str) -> bool:
    """Check whether *source* can be mounted as an S3 path."""
    return source.startswith("s3://")


def _is_gcs_uri_for_mount(source: str) -> bool:
    """Check whether *source* can be mounted as a GCS path."""
    return source.startswith("gs://")


def mount(source: str, mountPoint: str, extraConfigs: dict[str, Any] = {}) -> bool:
    """Mounts the given remote storage at the given mount point.

    Dispatches to the correct FUSE driver based on the source URI:
    - ``abfss://...`` → blobfuse2
    - ``s3://...`` → mount-s3 (primary) / s3fs-fuse (fallback)
    - ``gs://...`` → gcsfuse

    Credentials are loaded from ``~/.notebookutils/storage/<config>.yaml``.

    Example::

        mount("abfss://cont@myacct.dfs.core.windows.net", "/mnt/data",
              {"accountKey": "****"})
        mount("s3://my-bucket", "/mnt/s3data")
        mount("gs://my-bucket", "/mnt/gsdata")
    """
    if not os.path.isabs(mountPoint):
        raise ValueError(f"Mount point must be an absolute path, got: {mountPoint!r}")

    if _is_adls_uri_for_mount(source):
        return _mount_blobfuse2(source, mountPoint, extraConfigs)
    elif _is_s3_uri_for_mount(source):
        return _mount_s3(source, mountPoint, extraConfigs)
    elif _is_gcs_uri_for_mount(source):
        return _mount_gcsfuse(source, mountPoint, extraConfigs)
    else:
        raise ValueError(f"Unsupported mount source URI: {source!r}")


def _mount_blobfuse2(source: str, mountPoint: str, extraConfigs: dict[str, Any]) -> bool:
    """Mount an ADLS Gen2 / OneLake path using blobfuse2."""
    host, container, path = _parse_adls_uri(source)
    account = _adls_account_from_host(host)

    # Load base config from YAML
    try:
        azstorage = _load_creds(account)
    except (FileNotFoundError, KeyError) as e:
        raise RuntimeError(
            f"Failed to load credentials for account '{account}': {e}"
        ) from e

    # Merge extraConfigs into azstorage
    _merge_extra_configs(azstorage, extraConfigs)

    # Ensure required fields
    azstorage["container"] = container
    azstorage.setdefault("type", "adls")

    config = {
        "logging": {"type": "syslog", "level": "log_warning"},
        "components": ["libfuse", "file_cache", "attr_cache", "azstorage"],
        "libfuse": {
            "attribute-expiration-sec": 120,
            "entry-expiration-sec": 120,
            "negative-entry-expiration-sec": 240,
        },
        "file_cache": {
            "path": os.path.join(os.environ.get("HOME", "/tmp"), ".blobfuse2", "file_cache"),
            "timeout-sec": 120,
            "max-size-mb": 4096,
        },
        "attr_cache": {"timeout-sec": 7200},
        "azstorage": azstorage,
    }

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="blobfuse2_cfg_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        os.makedirs(mountPoint, exist_ok=True)
        os.makedirs(config["file_cache"]["path"], exist_ok=True)

        cmd = ["blobfuse2", "mount", mountPoint, "--config-file", tmp_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            _log.error("blobfuse2 mount failed: %s", result.stderr.strip())
            return False
        return True
    except FileNotFoundError:
        raise RuntimeError(
            "blobfuse2 is not installed. Install from "
            "https://learn.microsoft.com/azure/storage/blobs/blobfuse2-install"
        )
    except subprocess.TimeoutExpired:
        _log.error("blobfuse2 mount timed out")
        return False
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _mount_s3(source: str, mountPoint: str, extraConfigs: dict[str, Any]) -> bool:
    """Mount an S3 bucket using mount-s3 (primary) or s3fs-fuse (fallback)."""
    bucket = source.replace("s3://", "").rstrip("/")

    # Try mount-s3 first
    try:
        cmd = ["mount-s3", bucket, mountPoint]
        if extraConfigs.get("prefix"):
            cmd += ["--prefix", extraConfigs["prefix"]]
        if extraConfigs.get("read_only", False):
            cmd.append("--read-only")
        os.makedirs(mountPoint, exist_ok=True)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return True
        _log.warning("mount-s3 failed, falling back to s3fs-fuse: %s", result.stderr.strip())
    except FileNotFoundError:
        _log.warning("mount-s3 not found, trying s3fs-fuse")

    # Fallback to s3fs-fuse
    try:
        cmd = ["s3fs", bucket, mountPoint]
        if extraConfigs.get("read_only", False):
            cmd.append("-o")
            cmd.append("ro")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return True
        _log.error("s3fs-fuse mount failed: %s", result.stderr.strip())
        return False
    except FileNotFoundError:
        raise RuntimeError(
            "Neither mount-s3 nor s3fs-fuse is installed. Install mount-s3 from "
            "https://github.com/awslabs/mountpoint-s3"
        )


def _mount_gcsfuse(source: str, mountPoint: str, extraConfigs: dict[str, Any]) -> bool:
    """Mount a GCS bucket using gcsfuse."""
    bucket = source.replace("gs://", "").rstrip("/")

    # Build a gcsfuse YAML config for operational settings
    gcsfuse_config = {
        "file-cache": {
            "max-size-mb": extraConfigs.get("cache_max_size_mb", 4096),
        },
        "file-system": {
            "temp-dir": extraConfigs.get("temp_dir", "/tmp/notebookutils"),
        },
        "logging": {
            "severity": extraConfigs.get("log_severity", "warn"),
        },
        "write": {
            "enable-streaming-writes": extraConfigs.get("streaming_writes", True),
        },
    }

    tmp_path = ""
    cmd = ["gcsfuse", bucket, mountPoint]

    if extraConfigs.get("config_file"):
        cmd = ["gcsfuse", "--config-file", extraConfigs["config_file"], bucket, mountPoint]
    elif any(extraConfigs.get(k) for k in ("cache_max_size_mb", "temp_dir", "log_severity")):
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="gcsfuse_cfg_")
        with os.fdopen(tmp_fd, "w") as f:
            yaml.dump(gcsfuse_config, f, default_flow_style=False)
        cmd = ["gcsfuse", "--config-file", tmp_path, bucket, mountPoint]

    try:
        os.makedirs(mountPoint, exist_ok=True)
        if extraConfigs.get("only_dir"):
            cmd += ["--only-dir", extraConfigs["only_dir"]]
        if extraConfigs.get("implicit_dirs", False):
            cmd.append("--implicit-dirs")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            _log.error("gcsfuse mount failed: %s", result.stderr.strip())
            return False
        return True
    except FileNotFoundError:
        raise RuntimeError(
            "gcsfuse is not installed. Install from "
            "https://cloud.google.com/storage/docs/gcsfuse-install"
        )
    except subprocess.TimeoutExpired:
        _log.error("gcsfuse mount timed out")
        return False
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _merge_extra_configs(azstorage: dict[str, Any], extraConfigs: dict[str, Any]) -> None:
    """Merge extraConfigs into azstorage dict, mapping known keys."""
    key_map = {
        "accountKey": "account-key",
        "sasToken": "sas",
        "accountkey": "account-key",
        "sastoken": "sas",
        "mode": "mode",
        "aadToken": "oauth-token-path",
        "clientId": "clientid",
        "clientSecret": "clientsecret",
        "tenantId": "tenantid",
    }
    for k, v in extraConfigs.items():
        mapped = key_map.get(k, k)
        azstorage[mapped] = v


def unmount(mountPoint: str, extraOptions: dict[str, Any] = {}) -> bool:
    """Deletes a mount point.

    Tries blobfuse2 unmount first, then fusermount, then umount.

    Example: ``unmount("/mnt/data")``.
    """
    if not os.path.isabs(mountPoint):
        raise ValueError(f"Mount point must be an absolute path: {mountPoint!r}")

    # Try blobfuse2 unmount
    try:
        subprocess.run(
            ["blobfuse2", "unmount", mountPoint],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        pass

    # Try gcsfuse unmount
    try:
        subprocess.run(
            ["fusermount", "-u", mountPoint],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        pass

    # Last resort: umount
    try:
        result = subprocess.run(
            ["umount", mountPoint],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except FileNotFoundError:
        pass

    return not os.path.ismount(mountPoint)


def mounts(extraOptions: dict[str, Any] = {}) -> list[MountPointInfo]:
    """Show information about what is mounted.

    Returns a list of MountPointInfo objects across all supported
    FUSE types (blobfuse2, gcsfuse, s3fs, mount-s3, goofys, hdfs-fuse).

    Example: ``mounts()``.
    """
    results: list[MountPointInfo] = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                line = line.strip()
                # Match any known FUSE type
                parts = line.split()
                if len(parts) >= 3 and parts[2] in _FUSE_MOUNT_TYPES:
                    source = parts[0] if parts[0] != parts[2] else parts[2]
                    results.append(MountPointInfo(
                        source=source,
                        mountPoint=parts[1],
                        fileSystemType=parts[2],
                    ))
    except FileNotFoundError:
        pass
    return results


def refreshMounts() -> bool:
    """Refresh mount cache. Currently a no-op on this platform."""
    return True


def getMountPath(mountPoint: str, scope: str = "") -> str:
    """Gets the local path of the mount point.

    Returns the *mountPoint* itself as it is already a local path.
    """
    return mountPoint


def mountToDriverNode(source: str, mountPoint: str, extraConfigs: dict[str, Any] = {}) -> bool:
    """Mounts on the driver node. Delegates to mount()."""
    return mount(source, mountPoint, extraConfigs)


def unmountFromDriverNode(mountPoint: str) -> bool:
    """Unmounts from the driver node. Delegates to unmount()."""
    return unmount(mountPoint)


# ===================================================================
# fastcp  (cloud-aware high-throughput copy via native CLIs)
# ===================================================================

def _adls_to_https_uri(uri: str) -> str:
    """Convert an ADLS URI to an HTTPS URL suitable for azcopy.

    ``abfss://container@account.dfs.core.windows.net/path``
    becomes ``https://account.dfs.core.windows.net/container/path``.
    Local paths and non-ADLS URIs are returned unchanged.
    """
    if not _is_adls_path(uri):
        return uri
    host, container, path = _parse_adls_uri(uri)
    path = path.lstrip("/")
    return f"https://{host}/{container}/{path}"


def fastcp(
    src: str, dest: str, recurse: bool = True, extraConfigs: dict[str, Any] = {}
) -> bool:
    """Copies a file or directory via native cloud CLIs for high throughput.

    Dispatch rules (based on URI schemes of *both* src and dest):

    +---------------------+----------------------------------------------+
    | Any ``abfss://`` URI | ``azcopy`` (handles Azure ↔ S3, Azure ↔ GCS)|
    +---------------------+----------------------------------------------+
    | Both ``s3://``       | ``aws s3 cp`` (server-side COPY within AWS)|
    +---------------------+----------------------------------------------+
    | Both ``gs://``       | ``gcloud storage cp`` (server-side rewrite) |
    +---------------------+----------------------------------------------+
    | ``s3://`` ↔ ``gs://``| ``gcloud storage cp`` (handles S3 ↔ GCS)    |
    +---------------------+----------------------------------------------+
    | Local paths          | ``shutil`` (fallback)                       |
    +---------------------+----------------------------------------------+

    Credentials are loaded from YAML config profiles passed via
    ``extraConfigs`` and injected as environment variables for the
    CLI subprocess.

    Parameters
    ----------
    src : str
        Source URI (``abfss://``, ``s3://``, ``gs://``, ``file:/``, or plain).
    dest : str
        Destination URI.
    recurse : bool
        If True, copy recursively (default True).
    extraConfigs : dict
        May contain:
        - ``profile``: single profile name (``<name>.yaml``) used for both sides.
        - ``profiles``: ``(src_profile, dst_profile)`` for separate credentials.
        - ``flags``: additional CLI flag string.
        - ``timeout``: subprocess timeout in seconds (default 3600).

    Example::

        fastcp("s3://src-bucket/data", "s3://dst-bucket/data",
               extraConfigs={"profile": "my-aws"})
        fastcp("abfss://cont@acct.dfs.core.windows.net/a",
               "abfss://cont@acct2.dfs.core.windows.net/b",
               extraConfigs={"profiles": ("azure-src", "azure-dst")})
    """
    flags = extraConfigs.get("flags", "")
    timeout = extraConfigs.get("timeout", 3600)

    # ── resolve credential env vars from YAML profiles ──
    cred_env = _resolve_fastcp_creds(src, dest, extraConfigs)

    # ── dispatch ──
    if _is_adls_path(src) or _is_adls_path(dest):
        cmd, endpoint = _build_azcopy_cmd(src, dest, recurse, flags)
    elif _is_s3_path(src) and _is_s3_path(dest):
        cmd, endpoint = _build_aws_s3_cmd(src, dest, recurse, flags)
    elif _is_gcs_path(src) and _is_gcs_path(dest):
        cmd, endpoint = _build_gcloud_storage_cmd(src, dest, recurse, flags)
    elif (_is_s3_path(src) and _is_gcs_path(dest)) or (_is_gcs_path(src) and _is_s3_path(dest)):
        # gcloud storage cp supports S3↔GCS natively
        cmd, endpoint = _build_gcloud_storage_cmd(src, dest, recurse, flags)
    else:
        # Local → local fallback
        if recurse:
            shutil.copytree(_normalise(src), _normalise(dest), dirs_exist_ok=True)
        else:
            shutil.copy2(_normalise(src), _normalise(dest))
        return True

    # Merge credential env vars into the subprocess environment
    run_env = os.environ.copy()
    run_env.update(cred_env)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=int(timeout), env=run_env
        )
        if result.returncode != 0:
            _log.error("%s failed: %s", endpoint, result.stderr.strip())
            return False
        return True
    except FileNotFoundError:
        raise RuntimeError(
            f"{endpoint} is not installed. Please install it before using fastcp."
        )
    except subprocess.TimeoutExpired:
        _log.error("%s timed out after %s seconds", endpoint, timeout)
        return False


# ─────────────────────────────────────────────────────────────────────
# fastcp CLI builders
# ─────────────────────────────────────────────────────────────────────

def _build_azcopy_cmd(
    src: str, dest: str, recurse: bool, flags: str
) -> tuple[list, str]:
    """Build an azcopy command.

    ADLS URIs are converted to HTTPS; S3 / GCS URIs are kept as-is
    since azcopy understands ``s3://`` and ``gs://`` natively
    (tested in recent versions).
    """
    s = _adls_to_https_uri(src) if _is_adls_path(src) else src
    d = _adls_to_https_uri(dest) if _is_adls_path(dest) else dest
    cmd = ["azcopy", "copy", s, d]
    if recurse:
        cmd += ["--recursive"]
    if flags:
        cmd += flags.split()
    return cmd, "azcopy"


def _build_aws_s3_cmd(
    src: str, dest: str, recurse: bool, flags: str
) -> tuple[list, str]:
    """Build an aws s3 cp command."""
    cmd = ["aws", "s3", "cp", src, dest]
    if recurse:
        cmd.append("--recursive")
    if flags:
        cmd += flags.split()
    return cmd, "aws s3"


def _build_gcloud_storage_cmd(
    src: str, dest: str, recurse: bool, flags: str
) -> tuple[list, str]:
    """Build a gcloud storage cp command."""
    cmd = ["gcloud", "storage", "cp", src, dest]
    if recurse:
        cmd.append("--recursive")
    if flags:
        cmd += flags.split()
    return cmd, "gcloud storage"


# ─────────────────────────────────────────────────────────────────────
# fastcp credential injection
# ─────────────────────────────────────────────────────────────────────

def _resolve_fastcp_creds(
    src: str, dest: str, extraConfigs: dict
) -> dict[str, str]:
    """Build a dict of environment variables for the fastcp subprocess.

    Resolution order:
    1. If ``extraConfigs`` contains ``profile`` or ``profiles``, use those.
    2. Otherwise, auto-discover matching profiles by scanning all YAML
       configs and matching the URI host / bucket with each profile.
    3. If no profiles found at all, return empty (CLI uses ambient creds).

    ``extraConfigs`` may contain:
    - ``profile``: single profile name applied to both source and destination.
    - ``profiles``: ``(src_profile, dst_profile)`` tuple.
    """
    env: dict[str, str] = {}

    # ── explicit profile(s) ──
    profile_names = extraConfigs.get("profiles")
    if profile_names is None:
        single = extraConfigs.get("profile")
        if single:
            profile_names = (single, single)

    if profile_names:
        for pname in profile_names:
            _try_load_and_inject(env, pname)
        return env

    # ── auto-discover from URIs ──
    _autodiscover_and_inject(env, src)
    if src != dest:
        _autodiscover_and_inject(env, dest)

    return env


def _try_load_and_inject(env: dict[str, str], profile_name: str) -> None:
    """Load a single profile and inject its creds into *env*."""
    try:
        from .config import load_profile

        profile = load_profile(profile_name)
        _inject_profile_creds(env, profile)
    except FileNotFoundError:
        _log.debug("fastcp: profile %r not found, skipping", profile_name)
    except Exception:
        _log.debug("fastcp: failed to load profile %r", profile_name, exc_info=True)


def _autodiscover_and_inject(env: dict[str, str], uri: str) -> None:
    """Scan YAML profiles for one matching *uri* and inject its credentials.

    Matching logic:
    - ``abfss://container@host/path`` → find an azstorage profile whose
      ``endpoint`` (or ``account_name.dfs.core.windows.net``) equals *host*.
    - ``s3://bucket/...`` → find an s3 profile; the bucket name is used if
      a profile has an explicit bucket field, otherwise the first s3 profile.
    - ``gs://bucket/...`` → find a gs profile; match by project or first gs.
    """
    try:
        from .config import list_profiles, load_profile
    except ImportError:
        return

    for pname in list_profiles():
        try:
            profile = load_profile(pname)
        except Exception:
            continue

        if _profile_matches_uri(profile, uri):
            _inject_profile_creds(env, profile)
            break  # first match wins


def _profile_matches_uri(profile, uri: str) -> bool:
    """Check whether *profile* matches the given *uri*."""
    if _is_adls_path(uri) and profile.backend == "azstorage" and profile.azstorage:
        host, _, _ = _parse_adls_uri(uri)
        cfg = profile.azstorage
        expected = cfg.endpoint or f"{cfg.account_name}.dfs.core.windows.net"
        return host == expected

    if _is_s3_path(uri) and profile.backend == "s3":
        return True  # any s3 profile matches

    if _is_gcs_path(uri) and profile.backend == "gs":
        return True  # any gs profile matches

    return False


def _inject_profile_creds(env: dict[str, str], profile) -> None:
    """Set env vars for a loaded StorageProfile."""
    if profile.backend == "azstorage" and profile.azstorage:
        c = profile.azstorage
        if c.account_key:
            env["AZURE_STORAGE_ACCOUNT_KEY"] = c.account_key
        if c.sas_token:
            env["AZURE_STORAGE_SAS_TOKEN"] = c.sas_token
        if c.tenant_id:
            env["AZURE_TENANT_ID"] = c.tenant_id
        if c.client_id:
            env["AZURE_CLIENT_ID"] = c.client_id
        if c.client_secret:
            env["AZURE_CLIENT_SECRET"] = c.client_secret
    elif profile.backend == "s3" and profile.s3:
        c = profile.s3
        if c.access_key_id:
            env["AWS_ACCESS_KEY_ID"] = c.access_key_id
        if c.secret_access_key:
            env["AWS_SECRET_ACCESS_KEY"] = c.secret_access_key
        if c.region:
            env["AWS_DEFAULT_REGION"] = c.region
        if c.endpoint_url:
            env["AWS_ENDPOINT_URL"] = c.endpoint_url
    elif profile.backend == "gs" and profile.gs:
        c = profile.gs
        if c.project:
            env["GOOGLE_CLOUD_PROJECT"] = c.project
        if c.service_account_key_file:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = c.service_account_key_file


# ===================================================================
# Notebook resource path
# ===================================================================

def nbResPath() -> str:
    """Returns the notebook resources path (empty on this platform)."""
    return os.environ.get("NB_RES_PATH", "")
