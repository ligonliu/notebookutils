"""
notebookutils.config — unified multi-cloud storage configuration loader.

Reads YAML configuration files from:
    ``$HOME/.notebookutils/storage/<profile-name>.yaml``

The top-level key IS the backend type:

- ``azstorage`` — Azure ADLS Gen2 / OneLake
- ``s3`` — AWS S3 / S3-compatible (MinIO, OVH, Cloudflare R2, etc.)
- ``gs`` — Google Cloud Storage
- ``hdfs`` — Hadoop HDFS

Each file must contain a ``version: 1`` key for forward compatibility.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CONFIG_DIR = os.path.join(
    os.environ.get("HOME", "/tmp"), ".notebookutils", "storage"
)
_CURRENT_VERSION = 1

# Well-known backends detected by their top-level key.
_VALID_BACKENDS = frozenset({"azstorage", "s3", "gs", "hdfs"})


# ---------------------------------------------------------------------------
# Dataclasses — typed configuration objects
# ---------------------------------------------------------------------------


@dataclass
class AzureStorageConfig:
    """Configuration for Azure Data Lake Storage Gen2 / OneLake."""

    mode: str = "key"                          # key, sas, spn, msi, azcli
    account_name: str = ""
    container: str = ""
    account_key: str = ""
    sas_token: str = ""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    endpoint: str = ""                          # custom DFS host (e.g. OneLake)
    storage_type: str = "adls"                 # adls (HNS) or block (flat)

    @property
    def account_url(self) -> str:
        """Construct the DFS endpoint URL."""
        host = self.endpoint or f"{self.account_name}.dfs.core.windows.net"
        return f"https://{host}"


@dataclass
class S3Config:
    """Configuration for AWS S3 or S3-compatible storage."""

    mode: str = "key"                          # key, ambient, assume_role
    access_key_id: str = ""
    secret_access_key: str = ""
    endpoint_url: str = ""                     # empty = default AWS
    region: str = "us-east-1"
    addressing_style: str = "virtual"          # virtual, path, auto
    signature_version: str = "s3v4"
    use_ssl: bool = True
    verify_ssl: bool = True
    ca_bundle: str = ""
    role_arn: str = ""
    features: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        if not self.features:
            self.features = {
                "versioning": True,
                "multipart_upload": True,
                "server_side_copy": True,
                "bucket_creation": True,
                "presigned_urls": True,
            }


@dataclass
class GCSConfig:
    """Configuration for Google Cloud Storage."""

    project: str = ""
    service_account_key_file: str = ""         # optional path to JSON key
    # Credentials flow through ambient ADC by default


@dataclass
class HDFSConfig:
    """Configuration for Hadoop HDFS."""

    namenode: str = ""
    port: int = 8020
    hadoop_conf_dir: str = ""
    # Authentication via ambient Kerberos / delegation token


@dataclass
class MountConfig:
    """Operational settings for FUSE mounts (cross-cloud)."""

    cache_dir: str = ""
    cache_max_size_mb: int = 4096
    temp_dir: str = "/tmp/notebookutils"
    options: list[str] = field(default_factory=list)  # passthrough FUSE flags


@dataclass
class StorageProfile:
    """A fully-parsed storage connection profile."""

    name: str
    backend: str                                # 'azstorage', 's3', 'gs', 'hdfs'
    azstorage: Optional[AzureStorageConfig] = None
    s3: Optional[S3Config] = None
    gs: Optional[GCSConfig] = None
    hdfs: Optional[HDFSConfig] = None
    mount: MountConfig = field(default_factory=MountConfig)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _resolve_profile_file(profile_name: str) -> str:
    """Resolve the config file path for *profile_name*."""
    if profile_name.endswith((".yaml", ".yml")):
        return profile_name if os.path.isabs(profile_name) else os.path.join(
            _CONFIG_DIR, profile_name
        )
    return os.path.join(_CONFIG_DIR, f"{profile_name}.yaml")


def load_profile(profile_name: str) -> StorageProfile:
    """Load and parse a storage profile YAML file.

    Parameters
    ----------
    profile_name : str
        Name of the profile (maps to ``<name>.yaml`` in the config dir)
        or a full path to a YAML file.

    Returns
    -------
    StorageProfile

    Raises
    ------
    FileNotFoundError
        If the YAML file does not exist.
    ValueError
        If the YAML is malformed or has an unsupported version/backend.
    """
    file_path = _resolve_profile_file(profile_name)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(
            f"Storage profile '{profile_name}' not found: {file_path}"
        )

    with open(file_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    version = raw.get("version")
    if version != _CURRENT_VERSION:
        raise ValueError(
            f"Unsupported config version {version!r} in {file_path}. "
            f"Expected {_CURRENT_VERSION}."
        )

    # Detect backend from top-level keys
    backends = [k for k in _VALID_BACKENDS if k in raw]
    if len(backends) != 1:
        raise ValueError(
            f"Config file {file_path} must contain exactly one backend key "
            f"from {sorted(_VALID_BACKENDS)}, got {backends or 'none'}."
        )

    backend = backends[0]
    backend_raw = raw[backend]
    mount_raw = raw.get("mount", {})

    profile = StorageProfile(
        name=os.path.splitext(os.path.basename(file_path))[0],
        backend=backend,
        mount=MountConfig(
            cache_dir=mount_raw.get("cache_dir", ""),
            cache_max_size_mb=mount_raw.get("cache_max_size_mb", 4096),
            temp_dir=mount_raw.get("temp_dir", "/tmp/notebookutils"),
            options=mount_raw.get("options", []),
        ),
    )

    if backend == "azstorage":
        profile.azstorage = AzureStorageConfig(
            mode=backend_raw.get("mode", "key"),
            account_name=backend_raw.get("account-name", backend_raw.get("account_name", profile.name)),
            container=backend_raw.get("container", ""),
            account_key=backend_raw.get("account-key", backend_raw.get("accountkey", "")),
            sas_token=backend_raw.get("sas", backend_raw.get("sastoken", "")),
            tenant_id=backend_raw.get("tenantid", backend_raw.get("tenant-id", "")),
            client_id=backend_raw.get("clientid", backend_raw.get("client-id", "")),
            client_secret=backend_raw.get("clientsecret", backend_raw.get("client-secret", "")),
            endpoint=backend_raw.get("endpoint", ""),
            storage_type=backend_raw.get("type", "adls"),
        )
    elif backend == "s3":
        features = backend_raw.get("features", {})
        profile.s3 = S3Config(
            mode=backend_raw.get("mode", "key"),
            access_key_id=backend_raw.get("access_key_id", ""),
            secret_access_key=backend_raw.get("secret_access_key", ""),
            endpoint_url=backend_raw.get("endpoint_url", ""),
            region=backend_raw.get("region", "us-east-1"),
            addressing_style=backend_raw.get("addressing_style", "virtual"),
            signature_version=backend_raw.get("signature_version", "s3v4"),
            use_ssl=backend_raw.get("use_ssl", True),
            verify_ssl=backend_raw.get("verify_ssl", True),
            ca_bundle=backend_raw.get("ca_bundle", ""),
            role_arn=backend_raw.get("role_arn", ""),
            features=features,
        )
    elif backend == "gs":
        profile.gs = GCSConfig(
            project=backend_raw.get("project", ""),
            service_account_key_file=backend_raw.get("service_account_key_file", ""),
        )
    elif backend == "hdfs":
        profile.hdfs = HDFSConfig(
            namenode=backend_raw.get("namenode", ""),
            port=backend_raw.get("port", 8020),
            hadoop_conf_dir=backend_raw.get("hadoop_conf_dir", ""),
        )

    _log.debug("Loaded profile %r (backend=%s)", profile.name, backend)
    return profile


def list_profiles() -> list[str]:
    """List available storage profile names (without extension)."""
    if not os.path.isdir(_CONFIG_DIR):
        return []
    profiles = []
    for fn in sorted(os.listdir(_CONFIG_DIR)):
        if fn.endswith((".yaml", ".yml")):
            profiles.append(os.path.splitext(fn)[0])
    return profiles
