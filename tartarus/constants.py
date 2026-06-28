"""Cross-cutting constants shared across the harness.

This is a leaf module — it imports nothing from other tartarus modules, so it
can be imported anywhere without circular-dependency risk.
"""

from pydantic import ConfigDict

STRICT_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)

DEFAULT_OUTPUT_TRUNCATE_CHARS = 10_000
