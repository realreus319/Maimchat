"""TLS configuration helpers shared by HTTP clients."""

from __future__ import annotations

import os
import ssl


_CA_BUNDLE_ENV = (
    "CLAUDE_CODE_CA_BUNDLE",
    "CLAUDE_CODE_PROXY_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)
_CLIENT_CERT_ENV = (
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_PROXY_CLIENT_CERT",
)
_CLIENT_KEY_ENV = (
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_PROXY_CLIENT_KEY",
)
_CLIENT_KEY_PASSPHRASE_ENV = (
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_PROXY_CLIENT_KEY_PASSPHRASE",
)


def build_ssl_context_from_env() -> ssl.SSLContext | None:
    ca_bundle = _first_env(_CA_BUNDLE_ENV)
    client_cert = _first_env(_CLIENT_CERT_ENV)
    client_key = _first_env(_CLIENT_KEY_ENV)
    client_key_passphrase = _first_env(_CLIENT_KEY_PASSPHRASE_ENV)
    if not ca_bundle and not client_cert and not client_key:
        return None

    context = ssl.create_default_context(cafile=ca_bundle or None)
    if client_cert:
        passphrase = client_key_passphrase.encode() if client_key_passphrase else None
        context.load_cert_chain(
            certfile=client_cert,
            keyfile=client_key or None,
            password=passphrase,
        )
    return context


def urllib3_ssl_pool_kwargs_from_env() -> dict[str, object]:
    context = build_ssl_context_from_env()
    if context is None:
        return {}
    return {"ssl_context": context}


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


__all__ = ["build_ssl_context_from_env", "urllib3_ssl_pool_kwargs_from_env"]
