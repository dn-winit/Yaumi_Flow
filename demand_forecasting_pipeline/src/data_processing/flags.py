"""
Canonical row-flag column names used by data-processing and feature
engineering.

These are not features the model learns from -- they are train-time
annotations (lifecycle stage, anomaly detection) that gate downstream
behaviour: outlier clipping skips them, the feature builder excludes
them from the model matrix, the data-quality report counts them.

Centralised here so a future flag (e.g. ``is_promo``, ``is_outage``)
is added in exactly one place; every reader picks it up via
``FLAG_COLUMNS``.
"""

from __future__ import annotations


# Tuple (immutable) so callers can't mutate the canonical list.
FLAG_COLUMNS: tuple[str, ...] = (
    "suspicious_zero",
    "is_new_launch",
    "is_likely_eol",
)
