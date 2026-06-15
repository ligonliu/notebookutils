"""
notebookutils.fs provides utilities for working with file systems on Azure VMs
accessing Azure Data Lake Storage Gen2 (ADLS Gen2).

Uses the azure-storage-file-datalake SDK for ADLS Gen2 operations, and the
blobfuse2 CLI (via subprocess) for mount/unmount operations.

Credentials are loaded from YAML files in blobfuse2 configuration format,
stored under: $HOME/.notebookutils/storage/<account-name>.yaml

Supported ADLS URI format:
    abfss://<container>@<account>.dfs.core.windows.net/<path>

Local paths (including /mnt/...) are handled via os/shutil.
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
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from azure.core.exceptions import ResourceNotFoundError as AzureResourceNotFoundError
from azure.identity import DefaultAzureCredential
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
_ADLS_URI_RE = re.compile(
    r"^abfss?://"                        # scheme
    r"(?P<container>[^@]+)"              # container name
    r"@"                                 # literal @
    r"(?P<account>[^.]+)"                # storage account name
    r"\.dfs\.core\.windows\.net"          # endpoint
    r"(?P<path>/.*)?$"                   # optional path
)


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
# Internal helpers -- path parsing
# ---------------------------------------------------------------------------

def _is_adls_path(path: str) -> bool:
    """Return True if *path* looks like an ADLS Gen2 URI."""
    return bool(_ADLS_URI_RE.match(path))


def _parse_adls_uri(uri: str) -> tuple[str, str, str]:
    """Parse an ADLS URI into (account, container, path).

    Examples
    --------
    >>> _parse_adls_uri("abfss://mycont@myacct.dfs.core.windows.net/a/b")
    ('myacct', 'mycont', '/a/b')
    """
    m = _ADLS_URI_RE.match(uri)
    if not m:
        raise ValueError(f"Invalid ADLS URI: {uri!r}")
    path = m.group("path") or "/"
    return m.group("account"), m.group("container"), path


def _is_local_path(path: str) -> bool:
    """Return True if *path* looks like a local filesystem path."""
    return not _is_adls_path(path)


# ---------------------------------------------------------------------------
# Internal helpers -- credential loading
# ---------------------------------------------------------------------------

_creds_cache: dict[str, dict[str, Any]] = {}
"""Module-level cache mapping account_name -> parsed azstorage config dict."""


def _get_config_path(account_name: str) -> str:
    """Return the path to the YAML config file for *account_name*."""
    return os.path.join(_STORAGE_CONFIG_DIR, f"{account_name}.azure.yaml")


def _load_creds(account_name: str) -> dict[str, Any]:
    """Load and cache the azstorage config section for *account_name*.

    Looks for ``$HOME/.notebookutils/storage/<account_name>.yaml``.
    The YAML file must follow the blobfuse2 configuration format, i.e. it
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


def _get_service_client(account_name: str) -> DataLakeServiceClient:
    """Create an authenticated DataLakeServiceClient for *account_name*.

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
    account_url = f"https://{account_name}.dfs.core.windows.net"

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

    elif mode in ("spn", "msi", "azcli"):
        # For SPN, DefaultAzureCredential can use env vars (AZURE_TENANT_ID,
        # AZURE_CLIENT_ID, AZURE_CLIENT_SECRET). For MSI / azcli it
        # picks up the appropriate credential automatically.
        return DataLakeServiceClient(
            account_url, credential=DefaultAzureCredential()
        )

    else:
        raise ValueError(
            f"'{account_name}.yaml': unsupported auth mode '{mode}'"
        )


def _get_fs_client(account_name: str, container: str) -> FileSystemClient:
    """Return a FileSystemClient for a given container."""
    service = _get_service_client(account_name)
    return service.get_file_system_client(container)


def _get_client_for_uri(uri: str) -> tuple[FileSystemClient, str]:
    """Parse an ADLS URI and return ``(FileSystemClient, relative_path)``."""
    account, container, path = _parse_adls_uri(uri)
    fs_client = _get_fs_client(account, container)
    # Normalise path: strip leading / and treat ''/'/'/'/' as root
    rel = path.lstrip("/")
    return fs_client, rel


def _get_dir_client(uri: str) -> DataLakeDirectoryClient:
    """Return a DataLakeDirectoryClient for a directory URI."""
    fs_client, rel = _get_client_for_uri(uri)
    if not rel:
        return fs_client.get_directory_client("/")
    return fs_client.get_directory_client(rel)


def _get_file_client(uri: str) -> DataLakeFileClient:
    """Return a DataLakeFileClient for a file URI."""
    fs_client, rel = _get_client_for_uri(uri)
    return fs_client.get_file_client(rel)


# ---------------------------------------------------------------------------
# Internal helpers -- local file ops (fallback when path is not ADLS)
# ---------------------------------------------------------------------------

def _local_exists(path: str) -> bool:
    return os.path.exists(path)


def _local_isdir(path: str) -> bool:
    return os.path.isdir(path)


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
# Core file operations
# ===================================================================

def cp(src: str, dest: str, recurse: bool = False) -> bool:
    """Copies a file or directory, possibly across file systems.

    Example: ``cp("abfss://cont@acct.dfs.core.windows.net/a", "abfss://cont@acct.dfs.core.windows.net/b")``.
    """
    # --- both ADLS ---
    if _is_adls_path(src) and _is_adls_path(dest):
        return _cp_adls_to_adls(src, dest, recurse)

    # --- ADLS → local ---
    if _is_adls_path(src) and _is_local_path(dest):
        return _cp_adls_to_local(src, dest, recurse)

    # --- local → ADLS ---
    if _is_local_path(src) and _is_adls_path(dest):
        return _cp_local_to_adls(src, dest, recurse)

    # --- local → local ---
    return _cp_local_to_local(src, dest, recurse)


def _cp_adls_to_adls(src: str, dest: str, recurse: bool) -> bool:
    src_fs, src_rel = _get_client_for_uri(src)
    dst_fs, dst_rel = _get_client_for_uri(dest)
    src_account, src_container, _ = _parse_adls_uri(src)
    dst_account, dst_container, _ = _parse_adls_uri(dest)

    same_account = src_account == dst_account and src_container == dst_container

    src_is_dir = _adls_is_directory(src)

    if not recurse and src_is_dir:
        raise IsADirectoryError(f"Source is a directory: {src!r}. Use recurse=True.")

    if not src_is_dir:
        # Single file – try rename when same account; otherwise download & upload
        if same_account:
            src_fc: DataLakeFileClient = src_fs.get_file_client(src_rel)
            dst_fc: DataLakeFileClient = dst_fs.get_file_client(dst_rel)
            src_fc.rename_file(new_name=f"{dst_container}/{dst_rel}")
        else:
            _stream_copy_file(src, dest)
        return True
    else:
        # Directory
        return _copy_directory(src, dest, recurse, same_account, src_fs,
                              dst_fs, src_rel, dst_rel, src_container, dst_container)


def _copy_directory(src_uri: str, dest_uri: str, recurse: bool,
                    same_account: bool, src_fs: FileSystemClient,
                    dst_fs: FileSystemClient, src_rel: str, dst_rel: str,
                    src_container: str, dst_container: str) -> bool:
    """Recursively copy a directory from one ADLS location to another."""
    # Ensure the destination base directory exists
    if dst_rel:
        _adls_create_directory_p(dst_fs, dst_rel)

    if same_account:
        # Try rename at the container level; rename_directory works for intra-account
        src_dc: DataLakeDirectoryClient = src_fs.get_directory_client(src_rel)
        src_dc.rename_directory(new_name=f"{dst_container}/{dst_rel}")
        return True

    # Cross-account: walk and copy each item
    paths = list(src_fs.get_paths(path=src_rel if src_rel else None))
    for p in paths:
        item_rel = p.name  # relative to container root
        item_src_uri = f"abfss://{src_container}@{src_fs.account_name}.dfs.core.windows.net/{item_rel}"
        item_dest_uri = f"abfss://{dst_container}@{dst_fs.account_name}.dfs.core.windows.net/{item_rel}"

        if p.is_directory:
            if recurse:
                _copy_directory(item_src_uri, item_dest_uri, recurse, False,
                                src_fs, dst_fs, item_rel, item_rel,
                                src_container, dst_container)
        else:
            _stream_copy_file(item_src_uri, item_dest_uri)
    return True


def _cp_adls_to_local(src: str, dest: str, recurse: bool) -> bool:
    src_is_dir = _adls_is_directory(src)
    if not recurse and src_is_dir:
        raise IsADirectoryError(f"Source is a directory: {src!r}. Use recurse=True.")

    # Ensure local destination directory exists
    dest_parent = os.path.dirname(dest.rstrip("/")) or dest
    os.makedirs(dest_parent, exist_ok=True)

    if not src_is_dir:
        _, _, src_path = _parse_adls_uri(src)
        fc = _get_file_client(src)
        download = fc.download_file()
        data = download.readall()
        with open(dest, "wb") as f:
            f.write(data)
        return True

    # Directory: download all items
    os.makedirs(dest, exist_ok=True)
    fs_client, src_rel = _get_client_for_uri(src)
    _, container, _ = _parse_adls_uri(src)
    account = fs_client.account_name
    for p in fs_client.get_paths(path=src_rel if src_rel else None):
        item_rel = p.name
        local_item_path = os.path.join(dest, os.path.relpath(item_rel, src_rel) if src_rel else item_rel)
        item_uri = f"abfss://{container}@{account}.dfs.core.windows.net/{item_rel}"
        if p.is_directory:
            os.makedirs(local_item_path, exist_ok=True)
        else:
            _cp_adls_to_local(item_uri, local_item_path, recurse=True)
    return True


def _cp_local_to_adls(src: str, dest: str, recurse: bool) -> bool:
    src_is_dir = os.path.isdir(src)
    if not recurse and src_is_dir:
        raise IsADirectoryError(f"Source is a directory: {src!r}. Use recurse=True.")

    if not src_is_dir:
        # Single file
        fc = _get_file_client(dest)
        with open(src, "rb") as f:
            fc.upload_data(f.read(), overwrite=True)
        return True

    # Directory: walk and upload
    _adls_create_directory_p(*_get_client_for_uri(dest)[:-1], _get_client_for_uri(dest)[1])

    # Actually, ensure dest directory exists:
    dest_fs, dest_rel = _get_client_for_uri(dest)
    _adls_create_directory_p(dest_fs, dest_rel)

    for root, dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src)
        for d in dirs:
            adls_dir = f"{dest.rstrip('/')}/{rel_root}/{d}" if rel_root != "." else f"{dest.rstrip('/')}/{d}"
            adls_dir = adls_dir.replace("//", "/")
            dfs, drel = _get_client_for_uri(adls_dir)
            _adls_create_directory_p(dfs, drel)
        for fname in files:
            local_file = os.path.join(root, fname)
            adls_file = f"{dest.rstrip('/')}/{rel_root}/{fname}" if rel_root != "." else f"{dest.rstrip('/')}/{fname}"
            adls_file = adls_file.replace("//", "/")
            fc = _get_file_client(adls_file)
            with open(local_file, "rb") as f:
                fc.upload_data(f.read(), overwrite=True)
    return True


def _cp_local_to_local(src: str, dest: str, recurse: bool) -> bool:
    src_is_dir = os.path.isdir(src)
    if not recurse and src_is_dir:
        raise IsADirectoryError(f"Source is a directory: {src!r}. Use recurse=True.")
    if src_is_dir:
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(src, dest)
    return True


def _stream_copy_file(src_uri: str, dest_uri: str) -> None:
    """Copy a single ADLS file by streaming via memory (for cross-account)."""
    src_fc = _get_file_client(src_uri)
    dst_fc = _get_file_client(dest_uri)
    download = src_fc.download_file()
    data = download.readall()
    dst_fc.upload_data(data, overwrite=True)


def _adls_is_directory(uri: str) -> bool:
    """Check whether an ADLS URI points to a directory."""
    try:
        dc = _get_dir_client(uri)
        dc.get_directory_properties()
        return True
    except AzureResourceNotFoundError:
        return False
    except Exception:
        # Might be a file -- try file properties
        try:
            fc = _get_file_client(uri)
            fc.get_file_properties()
            return False
        except Exception:
            # Default: guess based on trailing slash
            return uri.rstrip("/").endswith("/") if uri.endswith("/") else False


def _adls_create_directory_p(fs_client: FileSystemClient, path: str) -> DataLakeDirectoryClient:
    """Create directory recursively on ADLS; return the directory client."""
    if not path:
        return fs_client.get_directory_client("/")
    parts = path.strip("/").split("/")
    current = ""
    dc: DataLakeDirectoryClient = None  # type: ignore[assignment]
    for part in parts:
        current = f"{current}/{part}" if current else part
        dc = fs_client.get_directory_client(current)
        try:
            dc.get_directory_properties()
        except AzureResourceNotFoundError:
            fs_client.create_directory(current)
        except Exception:
            pass
    return dc  # type: ignore[return-value]


def mv(src: str, dest: str, create_path: bool = False, overwrite: bool = False) -> bool:
    """Moves a file or directory, possibly across file systems.

    Example: ``mv("abfss://cont@acct.dfs.core.windows.net/a", "abfss://cont@acct.dfs.core.windows.net/b")``.
    """
    if create_path:
        # Ensure parent directories exist
        if _is_adls_path(dest):
            _, dest_rel = _get_client_for_uri(dest)
            parent_rel = os.path.dirname(dest_rel)
            if parent_rel:
                dest_fs, _ = _get_client_for_uri(dest)
                _adls_create_directory_p(dest_fs, parent_rel)
        else:
            os.makedirs(os.path.dirname(dest.rstrip("/")) or ".", exist_ok=True)

    if overwrite and exists(dest):
        rm(dest, recurse=True)

    # If same account & container, use rename
    if _is_adls_path(src) and _is_adls_path(dest):
        src_acc, src_cont, _ = _parse_adls_uri(src)
        dst_acc, dst_cont, _ = _parse_adls_uri(dest)
        if src_acc == dst_acc and src_cont == dst_cont:
            src_is_dir = _adls_is_directory(src)
            if src_is_dir:
                dc = _get_dir_client(src)
                _, dst_rel = _get_client_for_uri(dest)
                dc.rename_directory(new_name=f"{dst_cont}/{dst_rel}")
            else:
                fc = _get_file_client(src)
                _, dst_rel = _get_client_for_uri(dest)
                fc.rename_file(new_name=f"{dst_cont}/{dst_rel}")
            return True

    # Fallback: copy then delete
    success = cp(src, dest, recurse=True)
    if success:
        rm(src, recurse=True)
    return success


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------

def ls(dir: str) -> list[MSFileInfo]:
    """Lists the contents of a directory.

    Example: ``ls("abfss://cont@acct.dfs.core.windows.net/my-folder/")``.

    Returns a list of MSFileInfo objects with fields:
    ``name``, ``size``, ``path``, ``isDir``, ``isFile``, ``modifyTime``.
    """
    if _is_local_path(dir):
        return _ls_local(dir)
    return _ls_adls(dir)


def _ls_adls(dir: str) -> list[MSFileInfo]:
    fs_client, rel = _get_client_for_uri(dir)
    account, container, _ = _parse_adls_uri(dir)
    prefix = rel if rel else None
    results: list[MSFileInfo] = []
    for p in fs_client.get_paths(path=prefix):
        # Filter: only immediate children when rel is specified and not root
        if prefix:
            remaining = p.name[len(prefix):].lstrip("/")
            if "/" in remaining:
                # This is deeper than one level; skip unless we want recursive
                continue
        full_path = f"abfss://{container}@{account}.dfs.core.windows.net/{p.name}"
        modify_time_ms = 0
        if p.last_modified:
            modify_time_ms = int(p.last_modified.timestamp() * 1000)
        results.append(MSFileInfo(
            name=os.path.basename(p.name.rstrip("/")),
            size=p.content_length or 0,
            path=full_path,
            isDir=p.is_directory or False,
            isFile=not p.is_directory,
            modifyTime=modify_time_ms,
        ))
    return results


def _ls_local(dir: str) -> list[MSFileInfo]:
    results: list[MSFileInfo] = []
    if not os.path.isdir(dir):
        return results
    with os.scandir(dir) as it:
        for entry in it:
            stat = entry.stat()
            results.append(MSFileInfo(
                name=entry.name,
                size=stat.st_size if entry.is_file() else 0,
                path=os.path.join(dir, entry.name),
                isDir=entry.is_dir(),
                isFile=entry.is_file(),
                modifyTime=int(stat.st_mtime * 1000),
            ))
    return results


# ---------------------------------------------------------------------------
# mkdirs
# ---------------------------------------------------------------------------

def mkdirs(dir: str) -> bool:
    """Creates the given directory if it does not exist, also creating any
    necessary parent directories.

    Example: ``mkdirs("abfss://cont@acct.dfs.core.windows.net/a/b/c")``.
    """
    if _is_local_path(dir):
        os.makedirs(dir, exist_ok=True)
        return True
    fs_client, rel = _get_client_for_uri(dir)
    _adls_create_directory_p(fs_client, rel)
    return True


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------

def put(file: str, content: str, overwrite: bool = False) -> bool:
    """Writes the given String out to a file, encoded in UTF-8.

    Example: ``put("abfss://cont@acct.dfs.core.windows.net/myfile", "Hello world!")``.
    """
    data = content.encode("utf-8")
    if _is_local_path(file):
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
        with open(file, "wb") as f:
            f.write(data)
        return True
    fc = _get_file_client(file)
    fc.upload_data(data, overwrite=overwrite)
    return True


# ---------------------------------------------------------------------------
# head
# ---------------------------------------------------------------------------

def head(file: str, max_bytes: int = 1024 * 100) -> str:
    """Returns up to the first 'maxBytes' bytes of the given file as a String
    encoded in UTF-8.

    Example: ``head("abfss://cont@acct.dfs.core.windows.net/myfile")``.
    """
    if _is_local_path(file):
        with open(file, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")
    fc = _get_file_client(file)
    download = fc.download_file(length=max_bytes)
    return download.readall().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------

def append(file: str, content: str, createFileIfNotExists: bool = False) -> bool:
    """Append the given String to a file, encoded in UTF-8.

    Example: ``append("abfss://cont@acct.dfs.core.windows.net/myfile", "Hello!")``.
    """
    data = content.encode("utf-8")
    if _is_local_path(file):
        if createFileIfNotExists and not os.path.exists(file):
            os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
            Path(file).touch()
        with open(file, "ab") as f:
            f.write(data)
        return True

    fc = _get_file_client(file)
    try:
        props = fc.get_file_properties()
        current_size = props.size
    except AzureResourceNotFoundError:
        if createFileIfNotExists:
            fc.create_file()
            current_size = 0
        else:
            raise FileNotFoundError(f"File not found: {file!r}")

    fc.append_data(data, offset=current_size, length=len(data))
    fc.flush_data(current_size + len(data))
    return True


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------

def rm(dir: str, recurse: bool = False) -> bool:
    """Removes a file or directory.

    Example: ``rm("abfss://cont@acct.dfs.core.windows.net/myfile")``.
    """
    if _is_local_path(dir):
        if os.path.isdir(dir):
            if not recurse:
                raise IsADirectoryError(f"Path is a directory: {dir!r}. Use recurse=True.")
            shutil.rmtree(dir)
        elif os.path.isfile(dir):
            os.remove(dir)
        else:
            return False  # doesn't exist → nothing to remove
        return True

    # ADLS
    try:
        dc = _get_dir_client(dir)
        dc.get_directory_properties()
        # It's a directory
        if not recurse:
            raise IsADirectoryError(f"Path is a directory: {dir!r}. Use recurse=True.")
        dc.delete_directory()
        return True
    except AzureResourceNotFoundError:
        pass
    except IsADirectoryError:
        raise

    # Try as file
    try:
        fc = _get_file_client(dir)
        fc.delete_file()
        return True
    except AzureResourceNotFoundError:
        return False


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------

def exists(file: str) -> bool:
    """Check if a file or directory exists.

    Example: ``exists("abfss://cont@acct.dfs.core.windows.net/myfile")``.
    """
    if _is_local_path(file):
        return os.path.exists(file)

    # Try as file
    try:
        fc = _get_file_client(file)
        fc.get_file_properties()
        return True
    except AzureResourceNotFoundError:
        pass

    # Try as directory
    try:
        dc = _get_dir_client(file)
        dc.get_directory_properties()
        return True
    except AzureResourceNotFoundError:
        pass

    return False


# ===================================================================
# Mount operations
# ===================================================================

_MOUNT_INFO_RE = re.compile(
    r"^(?P<source>\S+)\s+on\s+(?P<mountPoint>\S+)\s+type\s+(?P<fsType>\S+)\s+.*"
)


def mount(source: str, mountPoint: str, extraConfigs: dict[str, Any] = {}) -> bool:
    """Mounts the given remote ADLS storage directory at the given mount point.

    Requires blobfuse2 to be installed on the system. The credential config
    is loaded from ``~/.notebookutils/storage/<account>.yaml`` and merged
    with any *extraConfigs* provided (e.g. ``{"accountKey": "...", "sasToken": "..."}``).

    Example::

        mount("abfss://cont@myacct.dfs.core.windows.net", "/mnt/data",
              {"accountKey": "****"})
    """
    if not _is_adls_path(source):
        raise ValueError(f"Mount source must be an ADLS URI, got: {source!r}")

    if not os.path.isabs(mountPoint):
        raise ValueError(f"Mount point must be an absolute path, got: {mountPoint!r}")

    account, container, path = _parse_adls_uri(source)

    # Load base config from YAML
    try:
        azstorage = _load_creds(account)
    except (FileNotFoundError, KeyError) as e:
        raise RuntimeError(
            f"Failed to load credentials for account '{account}': {e}"
        ) from e

    # Merge extraConfigs into azstorage (shallow merge, extraConfigs wins)
    # Map known extraConfigs keys to azstorage keys
    _merge_extra_configs(azstorage, extraConfigs)

    # Ensure required fields
    azstorage["container"] = container
    azstorage.setdefault("type", "adls")

    # Build a complete blobfuse2 YAML config
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

    # Write temp config YAML
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="blobfuse2_cfg_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        # Ensure mount point exists
        os.makedirs(mountPoint, exist_ok=True)

        # Ensure cache dirs exist
        os.makedirs(config["file_cache"]["path"], exist_ok=True)

        cmd = [
            "blobfuse2", "mount",
            mountPoint,
            "--config-file", tmp_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            _log.error("blobfuse2 mount failed: %s", result.stderr.strip())
            # Keep temp config for debugging
            return False
        return True
    except FileNotFoundError:
        raise RuntimeError(
            "blobfuse2 is not installed. Install it with: "
            "https://learn.microsoft.com/azure/storage/blobs/blobfuse2-install"
        )
    except subprocess.TimeoutExpired:
        _log.error("blobfuse2 mount timed out")
        return False
    finally:
        # Clean up temp config on success; keep on failure for debugging
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

    Example: ``unmount("/mnt/data")``.
    """
    if not os.path.isabs(mountPoint):
        raise ValueError(f"Mount point must be an absolute path: {mountPoint!r}")

    # Try blobfuse2 unmount first
    try:
        result = subprocess.run(
            ["blobfuse2", "unmount", mountPoint],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        pass

    # Fallback: fusermount -u
    try:
        result = subprocess.run(
            ["fusermount", "-u", mountPoint],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except FileNotFoundError:
        raise RuntimeError(
            "Neither blobfuse2 nor fusermount is available to unmount."
        )


def mounts(extraOptions: dict[str, Any] = {}) -> list[MountPointInfo]:
    """Show information about what is mounted.

    Example: ``mounts()``.

    Returns a list of MountPointInfo objects.
    """
    results: list[MountPointInfo] = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                line = line.strip()
                if "blobfuse2" in line or "blobfuse" in line or "fuse" in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        results.append(MountPointInfo(
                            source=parts[0] if parts[0] != "blobfuse2" else "blobfuse2",
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
# fastcp
# ===================================================================

def _adls_to_https_uri(uri: str) -> str:
    """Convert an ADLS URI to an HTTPS URL suitable for azcopy.

    ``abfss://container@account.dfs.core.windows.net/path``
    becomes ``https://account.dfs.core.windows.net/container/path``.
    Local paths are returned unchanged.
    """
    if not _is_adls_path(uri):
        return uri
    account, container, path = _parse_adls_uri(uri)
    path = path.lstrip("/")
    return f"https://{account}.dfs.core.windows.net/{container}/{path}"


def fastcp(
    src: str, dest: str, recurse: bool = True, extraConfigs: dict[str, Any] = {}
) -> bool:
    """Copies a file or directory via azcopy, possibly across file systems.

    ADLS URIs (``abfss://...``) are converted to HTTPS URLs compatible
    with azcopy.  Local ``file:/`` or plain paths are passed through.

    Example::

        fastcp("abfss://cont@acct.dfs.core.windows.net/a",
               "abfss://cont@acct2.dfs.core.windows.net/b")
    """
    flags = extraConfigs.get("flags", "")
    timeout = extraConfigs.get("timeout", 3600)

    src_url = _adls_to_https_uri(src)
    dest_url = _adls_to_https_uri(dest)

    try:
        cmd = ["azcopy", "copy", src_url, dest_url]
        if recurse:
            cmd += ["--recursive"]
        if flags:
            cmd += flags.split()
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=int(timeout))
        if result.returncode != 0:
            _log.error("azcopy failed: %s", result.stderr.strip())
            return False
        return True
    except FileNotFoundError:
        raise RuntimeError(
            "azcopy is not installed. Install it from "
            "https://learn.microsoft.com/azure/storage/common/storage-use-azcopy-v10"
        )
    except subprocess.TimeoutExpired:
        _log.error("azcopy timed out after %s seconds", timeout)
        return False


# ===================================================================
# Notebook resource path
# ===================================================================

def nbResPath() -> str:
    """Returns the notebook resources path (empty on this platform)."""
    return os.environ.get("NB_RES_PATH", "")
