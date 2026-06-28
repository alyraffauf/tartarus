"""Cross-cutting constants shared across the harness.

This is a leaf module — it imports nothing from other tartarus modules, so it
can be imported anywhere without circular-dependency risk.
"""

from pydantic import ConfigDict

STRICT_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)

DEFAULT_OUTPUT_TRUNCATE_CHARS = 10_000

# Cert-bundle env var names every jailed call points at the manifest's CA bundle.
# Also reserved against agent shell.env overrides (see manifest.py).
CERT_ENV_VARS = (
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
)
