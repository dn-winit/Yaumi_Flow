"""
Training-summary metadata helpers.

Centralises the ``training_summary.json`` provenance fields so both the
training pipeline (writer) and the inference pipeline (validator) read
the same canonical names and shapes.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as _md
import json
import logging
import os
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Frameworks whose installed version we record alongside the training
# summary. Picked because they are the ones most likely to break a
# loaded pickle if their major version changes.
_TRACKED_FRAMEWORKS = (
    "scikit-learn",
    "lightgbm",
    "xgboost",
    "pandas",
    "numpy",
    "statsmodels",
    "optuna",
    # Calendar libraries are tracked because their slug names / Hijri
    # boundary dates can change between releases; if the running version
    # is a different MAJOR than the one used at training, pickled models
    # may silently see a different feature set.
    "holidays",
    "hijridate",
)

def _safe_version(pkg: str) -> str:
    try:
        return _md.version(pkg)
    except Exception:
        return "unknown"

def framework_versions() -> dict[str, str]:
    return {pkg: _safe_version(pkg) for pkg in _TRACKED_FRAMEWORKS}

def _git_sha() -> str:
    """Best-effort capture of the current commit hash. Returns ``unknown``
    when the working tree isn't a git checkout or git is missing."""
    env_sha = os.getenv("GIT_COMMIT") or os.getenv("CI_COMMIT_SHA")
    if env_sha:
        return env_sha[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:12]
    except Exception:
        pass
    return "unknown"

def feature_schema_hash(feature_cols: Iterable[str]) -> str:
    """Stable digest of the union feature set. Hashing the sorted list
    means a column-order shuffle (which doesn't change the model)
    doesn't trip the hash, but adding/removing a column does."""
    payload = json.dumps(sorted(set(feature_cols)), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]

def config_hash(cfg: dict) -> str:
    """Stable digest of the resolved YAML. Used to detect when the
    config file changed between training and inference (which is a
    signal that the saved models may not be valid anymore)."""
    payload = json.dumps(cfg, separators=(",", ":"), sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]

def build_training_metadata(
    cfg: dict,
    *,
    feature_cols: Iterable[str],
    resolved_recursive_iterations: int,
    test_date_start: str | None = None,
    test_date_end: str | None = None,
) -> dict[str, Any]:
    """Assemble the metadata block written to ``training_summary.json``.

    Schema is stable; reading code in inference depends on these keys
    existing. Add fields freely - never rename or remove without a
    coordinated update to ``check_inference_compatibility``.

    ``test_date_end`` is the last date of the Test split. Inference
    anchors its future-skeleton at ``test_date_end + 1`` so the Forecast
    split picks up exactly where Test left off, eliminating the
    "training-day gap" where a date between test_end and inference's
    own per-pair last-date would have neither a Test nor a Forecast row.
    """
    return {
        "schema_version": "1.0",
        "trained_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "config_hash": config_hash(cfg),
        "feature_schema_hash": feature_schema_hash(feature_cols),
        "forecast_horizon": int(cfg.get("inference", {}).get("forecast_horizon", 0)),
        "test_date_start": test_date_start,
        "test_date_end": test_date_end,
        "recursive_iterations": int(resolved_recursive_iterations),
        "horizon_safe_lags": bool(
            cfg.get("feature_engineering", {}).get("horizon_safe_lags", False)
        ),
        # Strategy is part of the contract. Training writes a per-pair
        # lookup OR a class-winner shape based on this; inference must
        # honour the same choice or it silently routes pairs to the
        # wrong model. Persisting + checking here closes that gap.
        "model_selection_strategy": str(
            cfg.get("model_selection", {}).get("strategy", "per_class")
        ),
        # Two more knobs that change feature semantics across the
        # train/serve boundary. Encoding flag controls whether inference
        # MUST find a target_encoding.json artifact; outlier flag controls
        # whether inference MUST find outlier_bounds.csv.
        "target_encoding_enabled": bool(
            cfg.get("feature_engineering", {})
               .get("target_encoding", {})
               .get("enabled", False)
        ),
        "outlier_clipping_enabled": bool(
            cfg.get("cleaning", {})
               .get("per_pair_outlier", {})
               .get("enabled", False)
        ),
        "framework_versions": framework_versions(),
    }

class ConfigMismatchError(RuntimeError):
    """Raised when an inference run is incompatible with the saved training."""

def check_inference_compatibility(
    cfg: dict,
    summary: dict,
    *,
    feature_cols: Iterable[str],
) -> None:
    """Validate the runtime config + feature set against the saved metadata.

    Mismatches that change model semantics raise ``ConfigMismatchError``
    with a remediation hint. Mismatches in non-load-bearing fields
    (framework versions, git_sha) only log a warning.
    """
    meta = summary.get("metadata") or {}
    if not meta:
        # Backwards compat: pre-metadata training summaries skip the check.
        # Inference still runs (the rest of the pipeline checks feature
        # presence anyway) but a warning surfaces the gap.
        logger.warning(
            "training_summary.json has no metadata block - cannot validate "
            "runtime/training compatibility. Retrain to populate provenance fields."
        )
        return

    saved_horizon = meta.get("forecast_horizon")
    runtime_horizon = int(cfg.get("inference", {}).get("forecast_horizon", 0))
    if saved_horizon and runtime_horizon and saved_horizon != runtime_horizon:
        raise ConfigMismatchError(
            f"forecast_horizon mismatch: training={saved_horizon}, "
            f"runtime={runtime_horizon}. Models were fit for a different "
            f"horizon -- predictions would be out-of-distribution. "
            f"Either restore inference.forecast_horizon to {saved_horizon} "
            f"or retrain at {runtime_horizon}."
        )

    saved_hash = meta.get("feature_schema_hash")
    runtime_hash = feature_schema_hash(feature_cols)
    if saved_hash and saved_hash != runtime_hash:
        raise ConfigMismatchError(
            f"feature_schema_hash mismatch: training={saved_hash}, "
            f"runtime={runtime_hash}. Feature set changed between training "
            f"and inference -- saved models cannot be applied without retraining. "
            f"Retrain the pipeline."
        )

    saved_strategy = meta.get("model_selection_strategy")
    runtime_strategy = str(
        cfg.get("model_selection", {}).get("strategy", "per_class")
    )
    if saved_strategy and saved_strategy != runtime_strategy:
        raise ConfigMismatchError(
            f"model_selection.strategy mismatch: training={saved_strategy!r}, "
            f"runtime={runtime_strategy!r}. Training emitted a "
            f"{saved_strategy!r}-shaped pair_model_lookup.csv; reading it as "
            f"{runtime_strategy!r} routes pairs to the wrong model. "
            f"Either restore the strategy to {saved_strategy!r} or retrain."
        )

    saved_te = meta.get("target_encoding_enabled")
    runtime_te = bool(
        cfg.get("feature_engineering", {})
           .get("target_encoding", {})
           .get("enabled", False)
    )
    if saved_te is not None and saved_te != runtime_te:
        raise ConfigMismatchError(
            f"target_encoding.enabled mismatch: training={saved_te}, "
            f"runtime={runtime_te}. Encoded features only exist in the "
            f"persisted artifact when training had it enabled; toggling "
            f"the flag at inference produces a different feature set."
        )

    saved_outlier = meta.get("outlier_clipping_enabled")
    runtime_outlier = bool(
        cfg.get("cleaning", {})
           .get("per_pair_outlier", {})
           .get("enabled", False)
    )
    if saved_outlier is not None and saved_outlier != runtime_outlier:
        raise ConfigMismatchError(
            f"per_pair_outlier.enabled mismatch: training={saved_outlier}, "
            f"runtime={runtime_outlier}. The clipped/unclipped target "
            f"distributions differ; the model would see out-of-distribution "
            f"input. Restore the flag or retrain."
        )

    saved_versions = meta.get("framework_versions") or {}
    runtime_versions = framework_versions()
    diverged = {
        pkg: (saved_versions.get(pkg), runtime_versions.get(pkg))
        for pkg in saved_versions
        if saved_versions.get(pkg) and runtime_versions.get(pkg)
        and saved_versions[pkg].split(".")[0] != runtime_versions[pkg].split(".")[0]
    }
    if diverged:
        # Major-version divergence is a strong signal that pickled models
        # may no longer load (e.g. lightgbm 3 -> 4). Log loudly but don't
        # block -- the user may have validated on staging.
        logger.warning(
            "framework_versions major-version drift between training and inference: %s. "
            "Pickled models may fail to load. Consider retraining.",
            diverged,
        )
