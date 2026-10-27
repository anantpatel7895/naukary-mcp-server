"""Shared HTTP client construction.

Exists for one reason: corporate networks that terminate TLS with their own root CA.
Python's ssl module ships with certifi's bundle and never consults the OS trust store, so
on such a network every request fails with CERTIFICATE_VERIFY_FAILED even though the
machine itself trusts the certificate perfectly well.

`truststore` fixes that by backing the SSL context with the OS trust store (macOS
Keychain, Windows CryptoAPI, Linux ca-certificates). Verification stays fully enabled --
this trusts the same CAs your browser does, rather than disabling checks.
"""

from __future__ import annotations

import logging
import os
import ssl

log = logging.getLogger("naukri_mcp.http")

_warned = False


def build_ssl_context() -> ssl.SSLContext | str | bool:
    """Return something httpx accepts as `verify=`.

    Order of preference:
      1. An explicit CA bundle via SSL_CERT_FILE / REQUESTS_CA_BUNDLE, if set.
      2. The OS trust store via truststore -- handles corporate TLS interception.
      3. httpx's default bundle.
    """
    global _warned

    for env_var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        bundle = os.environ.get(env_var)
        if bundle and os.path.exists(bundle):
            return bundle

    try:
        import truststore
    except ImportError:  # pragma: no cover - truststore is a declared dependency
        if not _warned:
            log.warning(
                "truststore is not installed; falling back to the bundled CA list. "
                "On a network with TLS interception this will fail to verify."
            )
            _warned = True
        return True

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
