"""Deterministic case identity.

A case is identified by *what it is about*, not by when it was noticed. The
same ``(namespace, kind, source_id)`` triple always hashes to the same case ID,
on any machine, in any process, in any Python version. That is what makes
polling safe: a source that reports the same item twice re-registers the same
case instead of opening a second one.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

__all__ = ["encode_json", "fingerprint", "case_id", "normalise_slug", "SLUG_PATTERN"]

#: Namespaces and kinds are short, lowercase, filesystem- and log-friendly
#: slugs. Keeping them restricted means a case ID is always safe to print,
#: embed in a URL, or use as a metric label.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalise_slug(value: Any, *, label: str) -> str:
    """Validate a namespace or kind slug and return it unchanged.

    Raises ``ValueError`` (via the caller's error type) when the value is not a
    lowercase slug of 1-64 characters.
    """
    if not isinstance(value, str) or not SLUG_PATTERN.match(value):
        raise ValueError(
            f"{label} must match {SLUG_PATTERN.pattern!r}; got {value!r}"
        )
    return value


def encode_json(value: Any) -> str:
    """Encode a value to canonical JSON.

    Sorted keys, no insignificant whitespace, no NaN or Infinity. Two equal
    payloads therefore always produce byte-identical text, which is what makes
    identities and action fingerprints stable and comparable.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    """Return the SHA-256 hex digest of the canonical encoding of ``value``."""
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def case_id(namespace: str, kind: str, source_id: str) -> str:
    """Return the stable ID of the case describing ``source_id``.

    The readable prefix makes the ID greppable; the digest suffix keeps it
    fixed-length and collision-resistant regardless of how long or how odd the
    upstream source identifier is.
    """
    namespace = normalise_slug(namespace, label="namespace")
    kind = normalise_slug(kind, label="kind")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be a non-empty string")
    digest = fingerprint([namespace, kind, source_id])[:20]
    return f"{namespace}:{kind}:{digest}"
