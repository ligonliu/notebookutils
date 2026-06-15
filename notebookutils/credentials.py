"""
notebookutils.credentials provides utilities for obtaining access tokens and
managing secrets in Azure Key Vault.

Configuration is loaded from ``$HOME/.notebookutils/identity.yaml``, which
specifies the identity to use for authentication:

Managed Identity (system-assigned or user-assigned)::

    identity:
      type: managed_identity
      client_id: ""          # blank = use system-assigned MSI of the VM,
                             # or specify a user-assigned MSI client ID

Service Principal::

    identity:
      type: service_principal
      tenant_id: "your-tenant-id"
      client_id: "your-app-id"
      client_secret: "your-secret"
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import yaml

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import ClientSecretCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

__all__ = ["getToken", "isValidToken", "getSecret", "putSecret", "help"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_IDENTITY_CONFIG_PATH = os.path.join(
    os.environ.get("HOME", "/tmp"), ".notebookutils", "identity.yaml"
)

# Shorthand audience keys -> full Microsoft Entra ID resource URLs.
# (Matches the Fabric notebookutils / MSSparkUtils audience keys.)
_AUDIENCE_MAP: dict[str, str] = {
    "storage": "https://storage.azure.com",
    "pbi": "https://analysis.windows.net/powerbi/api",
    "keyvault": "https://vault.azure.net",
    "kusto": "https://help.kusto.windows.net",
}

# ---------------------------------------------------------------------------
# Internal helpers -- identity configuration loading
# ---------------------------------------------------------------------------

_identity_cache: Optional[dict] = None
"""Module-level cache for the parsed identity config, keyed by config path."""


def _load_identity_config() -> dict:
    """Load and cache the identity configuration from disk.

    Returns the ``'identity'`` section of the YAML file.

    Raises
    ------
    FileNotFoundError
        If ``identity.yaml`` does not exist.
    KeyError
        If the ``identity`` key is missing from the file.
    """
    global _identity_cache  # noqa: PLW0603
    if _identity_cache is not None:
        return _identity_cache

    if not os.path.isfile(_IDENTITY_CONFIG_PATH):
        raise FileNotFoundError(
            f"Identity config not found at {_IDENTITY_CONFIG_PATH!r}. "
            "Please create it with 'identity' settings for managed_identity "
            "or service_principal."
        )

    with open(_IDENTITY_CONFIG_PATH, "r") as fh:
        config = yaml.safe_load(fh) or {}

    identity = config.get("identity")
    if not identity:
        raise KeyError(
            f"Missing 'identity:' section in {_IDENTITY_CONFIG_PATH!r}"
        )

    _identity_cache = identity
    return identity


def _get_credential():
    """Return an ``azure.identity`` credential object based on identity.yaml.

    Supported identity types:

    * ``managed_identity`` -- uses ``ManagedIdentityCredential``.  If
      ``client_id`` is non-empty, authenticates as that user-assigned
      managed identity; otherwise uses the VM's system-assigned identity.
    * ``service_principal`` -- uses ``ClientSecretCredential`` with
      ``tenant_id``, ``client_id``, and ``client_secret``.

    Returns
    -------
    TokenCredential
        An ``azure.identity`` credential that implements ``get_token``.

    Raises
    ------
    ValueError
        If the identity type is unknown or required fields are missing.
    """
    identity = _load_identity_config()
    id_type: str = identity.get("type", "")

    if id_type == "managed_identity":
        client_id = identity.get("client_id", "").strip()
        _log.debug("Creating ManagedIdentityCredential (client_id=%r)", client_id or "<none>")
        return ManagedIdentityCredential(
            client_id=client_id if client_id else None
        )

    if id_type == "service_principal":
        tenant_id: str = identity.get("tenant_id", "")
        client_id: str = identity.get("client_id", "")
        client_secret: str = identity.get("client_secret", "")
        if not tenant_id or not client_id or not client_secret:
            raise ValueError(
                "service_principal identity requires 'tenant_id', "
                "'client_id', and 'client_secret' in identity.yaml"
            )
        _log.debug(
            "Creating ClientSecretCredential (tenant=%s, client=%s)",
            tenant_id, client_id,
        )
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    raise ValueError(
        f"Unknown identity type {id_type!r} in {_IDENTITY_CONFIG_PATH!r}. "
        "Expected 'managed_identity' or 'service_principal'."
    )


def _resolve_audience(raw_audience: str) -> str:
    """Map a shorthand audience key to a full resource URL if recognised.

    If *raw_audience* looks like an absolute URL already, return it unchanged.
    """
    stripped = raw_audience.strip()
    # If it looks like a URL, pass it through.
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return stripped
    return _AUDIENCE_MAP.get(stripped.lower(), stripped)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def getToken(audience: str) -> str:
    """Get a Microsoft Entra ID access token for the given *audience*.

    *audience* may be a shorthand key (``"storage"``, ``"pbi"``,
    ``"keyvault"``, ``"kusto"``) or a full resource URL.

    The identity used to request the token is configured in
    ``$HOME/.notebookutils/identity.yaml``.

    Args:
        audience: Target resource identifier or shorthand key.

    Returns:
        The access token string (JWT).
    """
    resolved = _resolve_audience(audience)
    _log.info("Requesting token for audience %r (resolved from %r)", resolved, audience)
    credential = _get_credential()
    token = credential.get_token(resolved)
    return token.token


def isValidToken(token: str) -> bool:
    """Check whether *token* has not yet expired.

    Decodes the JWT payload (without verifying the signature) and checks
    the ``exp`` claim against the current UTC time (with a 60-second
    clock-skew buffer).

    Args:
        token: The token (JWT) to validate.

    Returns:
        ``True`` if the token has a valid ``exp`` claim that is in the
        future, ``False`` otherwise (including malformed tokens).
    """
    if not token:
        return False
    try:
        parts = token.split(".")
        if len(parts) < 2:  # noqa: PLR2004
            return False
        # Add padding; JWTs use base64url (no padding).
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)
    except (ValueError, binascii.Error, json.JSONDecodeError):
        _log.debug("Token is not a valid JWT.", exc_info=True)
        return False

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        _log.debug("Token payload is missing 'exp' claim.")
        return False

    now = time.time()
    # 60-second clock-skew buffer to be safe.
    return now < exp - 60


def getSecret(akvName: str, secret: str) -> str:
    """Retrieve a secret from Azure Key Vault.

    *akvName* may be a short vault name (e.g. ``"my-vault"``) or a full
    vault URL (e.g. ``"https://my-vault.vault.azure.net"``).

    The identity used to authenticate to Key Vault is configured in
    ``$HOME/.notebookutils/identity.yaml``.

    Args:
        akvName: Azure Key Vault name or full URL.
        secret: Name of the secret to retrieve.

    Returns:
        The secret value, or an empty string on failure.
    """
    vault_url = _normalise_vault_url(akvName)
    _log.info("Getting secret %r from %s", secret, vault_url)
    try:
        credential = _get_credential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        return client.get_secret(secret).value or ""
    except (ClientAuthenticationError, HttpResponseError) as exc:
        _log.error("Failed to get secret %r: %s", secret, exc)
        return ""


def putSecret(akvName: str, secretName: str, secretValue: str) -> str:
    """Store a secret in Azure Key Vault.

    *akvName* may be a short vault name (e.g. ``"my-vault"``) or a full
    vault URL (e.g. ``"https://my-vault.vault.azure.net"``).

    The identity used to authenticate to Key Vault is configured in
    ``$HOME/.notebookutils/identity.yaml``.

    Args:
        akvName: Azure Key Vault name or full URL.
        secretName: Name for the secret.
        secretValue: Value to store.

    Returns:
        The secret value that was stored.
    """
    vault_url = _normalise_vault_url(akvName)
    _log.info("Putting secret %r in %s", secretName, vault_url)
    try:
        credential = _get_credential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        return client.set_secret(secretName, secretValue).value or ""
    except (ClientAuthenticationError, HttpResponseError) as exc:
        _log.error("Failed to put secret %r: %s", secretName, exc)
        return ""


def help(method_name: str | None = None) -> None:
    """Print help for the module or a specific method."""
    if method_name:
        fn = globals().get(method_name)
        if fn and callable(fn) and fn.__doc__:
            print(fn.__doc__)
        else:
            print(f"No help available for {method_name!r}")
    else:
        print(__doc__ or "notebookutils.credentials -- credential utilities")


# ---------------------------------------------------------------------------
# Internal helpers -- Key Vault URL normalisation
# ---------------------------------------------------------------------------

def _normalise_vault_url(raw: str) -> str:
    """Normalise *raw* into a full Azure Key Vault URL.

    >>> _normalise_vault_url("my-vault")
    'https://my-vault.vault.azure.net'
    >>> _normalise_vault_url("https://my-vault.vault.azure.net")
    'https://my-vault.vault.azure.net'
    """
    stripped = raw.strip().rstrip("/")
    if stripped.startswith("https://"):
        return stripped
    return f"https://{stripped}.vault.azure.net"
