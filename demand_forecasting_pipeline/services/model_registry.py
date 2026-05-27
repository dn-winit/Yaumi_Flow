"""Snapshot-based model artifact versioning + rollback.

Each successful retrain copies live artifacts into versions/<version_id>/ with a
manifest; current.json points at the live version. Rollback restores atomically
per file and updates the pointer.

Snapshot-based (not read-through): zero impact on hot read path; self-contained
versions; retention bounded by model_retention_max_versions.

Module-level lock serialises promote/rollback; current.json + .staging dir give
atomic publish (rename) and torn-write protection (tmp+os.replace).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from demand_forecasting_pipeline.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Layout names; all paths relative to settings.artifacts_dir.
_VERSIONS_SUBDIR = "versions"
_STAGING_SUBDIR = ".staging"          # hidden so list_versions() never shows it
_CURRENT_POINTER = "current.json"
_MANIFEST_FILENAME = "manifest.json"

# Bump only on non-backward-compatible schema changes.
_MANIFEST_VERSION = 1

# Names that must NOT be snapshotted (would create recursion or copy
# stale state). Anything starting with "." is also skipped.
_EXCLUDED_TOP_NAMES = frozenset({_VERSIONS_SUBDIR, _CURRENT_POINTER})

# Safe version_id: alnum + ``_-``, length-capped; prevents path traversal.
_VERSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# Process-wide lock. Promotion and rollback take this; lookups
# (current_version, list_versions) only take it briefly to read JSON.
_REGISTRY_LOCK = threading.Lock()


@dataclass(frozen=True)
class VersionInfo:
    """Wire-shape returned by list_versions() / current_version().

    Frozen so a caller can't accidentally mutate a recorded manifest.
    Field names mirror manifest.json keys plus the discoverable
    ``version_id`` and ``path`` for traceability.

    Champion-challenger gate fields
    -------------------------------
    ``decision`` records the gate's verdict at record_version time:
      * ``"promote"``    -- challenger passed the gate, current.json updated
      * ``"reject"``     -- challenger regressed beyond threshold, snapshot
                            kept for audit but current.json was NOT updated
      * ``"cold_start"`` -- no prior champion existed; promoted unconditionally
      * ``None``         -- gate disabled or unavailable (legacy rows)

    ``champion_accuracy`` and ``delta_pp`` capture the comparison the
    gate evaluated against, so an operator inspecting a rejected
    version can see exactly why it was held back.
    """
    version_id: str
    path: Path
    promoted_at: Optional[str]
    promoted_by: Optional[str]
    trigger: Optional[str]
    accuracy_before: Optional[float]
    accuracy_after: Optional[float]
    duration_seconds: Optional[float]
    is_current: bool
    decision: Optional[str] = None
    champion_accuracy: Optional[float] = None
    delta_pp: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id":        self.version_id,
            "path":              str(self.path),
            "promoted_at":       self.promoted_at,
            "promoted_by":       self.promoted_by,
            "trigger":           self.trigger,
            "accuracy_before":   self.accuracy_before,
            "accuracy_after":    self.accuracy_after,
            "duration_seconds":  self.duration_seconds,
            "is_current":        self.is_current,
            "decision":          self.decision,
            "champion_accuracy": self.champion_accuracy,
            "delta_pp":          self.delta_pp,
        }


class ModelRegistry:
    """Snapshot-based artifact versioning with rollback + retention.

    All operations are referenced to ``settings.artifacts_dir`` so the
    layout follows the existing path conventions automatically.

    Methods
    -------
    record_version(entry, trigger)   -> snapshot live artifacts + write manifest + promote
    rollback(version_id)             -> restore version's files into live, update pointer
    list_versions()                  -> all versions with manifest summary, current first
    current_version()                -> the one currently pointed at by current.json
    prune(max_versions)              -> delete oldest beyond cap (called automatically after record)
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._root = Path(self._s.artifacts_dir)
        self._versions_root = self._root / _VERSIONS_SUBDIR
        self._staging_root = self._root / _STAGING_SUBDIR
        self._pointer_path = self._root / _CURRENT_POINTER

    # ---------------------------------------------------------------- paths

    def version_path(self, version_id: str) -> Path:
        """Resolve the on-disk folder for ``version_id``. Caller is
        responsible for checking existence."""
        return self._versions_root / version_id

    # ------------------------------------------------------------- pointer

    def current_version_id(self) -> Optional[str]:
        """Read ``current.json`` and return the active version id, or
        ``None`` when no pointer exists yet (legacy / first-run state).
        """
        try:
            with self._pointer_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            vid = data.get("version_id") or data.get("version")
            return str(vid) if vid else None
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            # Corrupt pointer: log loudly, treat as "no current". The
            # next promote will rewrite a clean pointer.
            logger.warning("model_registry: current.json unreadable: %s", exc)
            return None

    def _write_pointer(self, version_id: str, promoted_by: str) -> None:
        """Atomic write of ``current.json``. Caller must already hold
        ``_REGISTRY_LOCK``. Delegates to ``io_utils.save_json`` so the
        atomic-write contract (tmp + ``os.replace``) lives in ONE
        place across the codebase -- model registry, retrain config,
        optuna study, target encoding all use the same primitive."""
        from demand_forecasting_pipeline.src.utils.io_utils import save_json
        payload = {
            "version_id":  version_id,
            "promoted_at": _utcnow_iso(),
            "promoted_by": promoted_by,
        }
        self._root.mkdir(parents=True, exist_ok=True)
        save_json(payload, str(self._pointer_path))

    # ------------------------------------------------------------- queries

    def current_version(self) -> Optional[VersionInfo]:
        vid = self.current_version_id()
        if not vid:
            return None
        path = self.version_path(vid)
        if not path.exists():
            logger.warning(
                "model_registry: current.json points at %s but folder is missing",
                vid,
            )
            return None
        return _info_from_manifest(path, vid, is_current=True)

    def list_versions(self) -> List[VersionInfo]:
        """All retained versions: current first, then newest->oldest.

        Folder names are ISO timestamps (see ``_mint_version_id``) so a
        name-sort yields chronological order; reversing gives newest
        first. The current version is then lifted to the head so the UI
        and API never have to re-sort by ``is_current``."""
        if not self._versions_root.exists():
            return []
        current = self.current_version_id()
        out: List[VersionInfo] = []
        # Single pass + single sort. iterdir yields in OS-specific order;
        # we sort once at the end so we don't pay for a wasted in-place
        # sort that the final split-and-concat would discard.
        for child in self._versions_root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue  # .staging and similar
            info = _info_from_manifest(
                child, child.name, is_current=(child.name == current),
            )
            if info is not None:
                out.append(info)
        out.sort(key=lambda v: v.version_id, reverse=True)
        # Current first (preserves the newest->oldest order of the rest):
        head = [v for v in out if v.is_current]
        tail = [v for v in out if not v.is_current]
        return head + tail

    # ----------------------------------------------------------- promotion

    def record_version(
        self,
        entry: Dict[str, Any],
        *,
        trigger: Optional[str] = None,
        promoted_by: str = "auto-retrain",
        update_pointer: bool = True,
        decision: Optional[str] = None,
        champion_accuracy: Optional[float] = None,
        delta_pp: Optional[float] = None,
    ) -> str:
        """Snapshot the live ``artifacts_dir`` into a new version
        folder, write the manifest, optionally update ``current.json``,
        and prune old versions beyond the retention cap.

        Returns the newly minted ``version_id`` so the caller can
        record it on the run-history entry.

        ``entry`` should be the same dict the auto-retrain scheduler
        appends to its history (``date``, ``accuracy_before``,
        ``accuracy_after``, ``duration_seconds``, ``status``). The
        registry copies a flat subset into the manifest and leaves the
        original entry untouched so the scheduler's own audit trail
        and the per-version manifest never disagree.

        Champion-challenger gate parameters
        -----------------------------------
        ``update_pointer`` defaults to True (full promote). The
        scheduler's promotion gate sets it to False when a challenger
        regresses: the snapshot is still recorded for audit but
        ``current.json`` is held back so production keeps serving the
        previous champion. ``decision``/``champion_accuracy``/
        ``delta_pp`` carry the gate verdict into the manifest so an
        operator inspecting the version sees the comparison that
        triggered the hold-back.
        """
        with _REGISTRY_LOCK:
            version_id = _mint_version_id()
            staging = self._staging_root / version_id
            final = self.version_path(version_id)

            # Defensive: a leftover staging folder from a crashed run
            # would block this run -- clean it before starting.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=True, exist_ok=False)

            try:
                # 1. Snapshot live artifacts. Single recursive copy of
                #    every top-level child of artifacts_dir EXCEPT the
                #    versions/ subdir, the .staging subdir, and the
                #    current.json pointer (those are registry-managed,
                #    not artifact content).
                snapshot_bytes = self._copy_live_artifacts(staging)

                # 2. Build the manifest from the entry the scheduler
                #    will (or already has) recorded. Anything missing
                #    on the entry is recorded as None -- the deeper
                #    provenance lives inside the snapshotted
                #    training_summary.json, untouched.
                #
                #    ``promoted_at`` is the snapshot timestamp -- it
                #    records WHEN this version came into existence, not
                #    necessarily WHEN it became current. For rejected
                #    challengers (update_pointer=False) the field is
                #    still set; ``decision="reject"`` is the signal that
                #    the version was held back from current.json.
                manifest = {
                    # ``manifest_version`` stamps the schema so any
                    # future field rename/removal has a migration
                    # anchor. v1 covers Phases 1-6; bump only on a
                    # non-backward-compatible change.
                    "manifest_version":  _MANIFEST_VERSION,
                    "version_id":        version_id,
                    "promoted_at":       _utcnow_iso(),
                    "promoted_by":       promoted_by,
                    "trigger":           trigger or entry.get("trigger"),
                    "accuracy_before":   entry.get("accuracy_before"),
                    "accuracy_after":    entry.get("accuracy_after"),
                    "duration_seconds":  entry.get("duration_seconds"),
                    "status":            entry.get("status"),
                    "snapshot_bytes":    int(snapshot_bytes),
                    "decision":          decision,
                    "champion_accuracy": champion_accuracy,
                    "delta_pp":          delta_pp,
                }
                _write_json_atomic(staging / _MANIFEST_FILENAME, manifest)

                # 3. Publish: atomic rename staging -> final. Anything
                #    that sees the versions root mid-rename either sees
                #    the new folder or doesn't -- never half of it.
                #    Ensure the parent ``versions/`` exists -- on the
                #    very first promotion it won't.
                self._versions_root.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    # Should be impossible given _mint_version_id uses
                    # a microsecond-resolution timestamp, but defensive
                    # for the case where wall clock jitters back.
                    shutil.rmtree(final, ignore_errors=True)
                os.rename(staging, final)

                # 4. Update the pointer (atomic tmp + replace) UNLESS
                #    the caller is recording a held-back challenger.
                #    When update_pointer=False the previous champion
                #    remains current; the snapshot exists purely for
                #    audit + a future manual rollback.
                if update_pointer:
                    self._write_pointer(version_id, promoted_by)

                logger.info(
                    "model_registry: recorded version %s (bytes=%d, trigger=%s)",
                    version_id, snapshot_bytes, trigger,
                )

                # 5. Retention sweep -- delete oldest beyond the cap.
                self._prune_locked()
                return version_id
            except Exception:
                # On failure, cleanup the staging dir so it doesn't
                # accumulate. Pointer is unchanged so the previous
                # version (if any) remains current.
                shutil.rmtree(staging, ignore_errors=True)
                raise

    # ------------------------------------------------------------- rollback

    def rollback_if_current(
        self,
        version_id: str,
        *,
        expected_current: Optional[str],
        promoted_by: str = "rollback",
    ) -> Optional[VersionInfo]:
        """Conditional rollback: restore ``version_id`` ONLY if the
        registry's current pointer still matches ``expected_current``.

        Used by the champion-challenger gate to close the race window
        between a held-back snapshot and the auto-rollback to champion:
        if an operator's manual ``POST /retrain/rollback`` interleaves
        and moves the pointer in the microseconds between the gate's
        ``record_version(update_pointer=False)`` and the auto-rollback,
        the operator's intent wins and the auto-rollback defers.

        Returns the restored VersionInfo on success, or ``None`` when
        the pointer no longer matches ``expected_current`` (deferred).
        Raises the same exceptions as ``rollback()`` for malformed or
        unknown ``version_id``.
        """
        version_id = _validate_version_id(version_id)
        with _REGISTRY_LOCK:
            current_now = self.current_version_id()
            if current_now != expected_current:
                logger.warning(
                    "model_registry.rollback_if_current: pointer moved "
                    "from %s to %s; deferring rollback to %s",
                    expected_current, current_now, version_id,
                )
                return None
            return self._rollback_locked(version_id, promoted_by)

    def rollback(self, version_id: str, *, promoted_by: str = "rollback") -> VersionInfo:
        """Restore the live artifacts to those captured in
        ``version_id``. Updates the pointer to record that this
        version is now current.

        Raises ``ValueError`` if ``version_id`` contains path-traversal
        characters; ``FileNotFoundError`` if the id is well-formed but
        unknown to the registry.
        """
        version_id = _validate_version_id(version_id)
        with _REGISTRY_LOCK:
            return self._rollback_locked(version_id, promoted_by)

    def _rollback_locked(self, version_id: str, promoted_by: str) -> VersionInfo:
        """Inner rollback body. Caller MUST hold ``_REGISTRY_LOCK``.
        Exists as a separate method so ``rollback`` and
        ``rollback_if_current`` share the same critical-section body
        without nested-lock acquisition."""
        target = self.version_path(version_id)
        # ``target.resolve()`` MUST stay inside ``versions_root``;
        # the regex above already blocks separators but the explicit
        # containment check is belt-and-braces against any future
        # change that relaxes the pattern.
        if not _is_within(target, self._versions_root):
            raise ValueError(
                f"version_id {version_id!r} escapes the versions root"
            )
        if not target.is_dir():
            # Generic message -- the absolute on-disk path is INFO-
            # disclosure to a pre-auth caller of the rollback endpoint;
            # the version_id is enough for the operator to act on.
            raise FileNotFoundError(
                f"version_id {version_id!r} not found in the registry"
            )

        # Copy version contents back into the live root. We do NOT
        # wipe the live root first -- the snapshot includes every
        # registry-managed file, so unrelated content (logs, ad-hoc
        # exports, etc.) is preserved.
        for child in target.iterdir():
            if child.name == _MANIFEST_FILENAME:
                continue  # manifest stays inside the version folder
            if child.name in _EXCLUDED_TOP_NAMES or child.name.startswith("."):
                continue
            dst = self._root / child.name
            _replace_path(child, dst)

        self._write_pointer(version_id, promoted_by)
        logger.info(
            "model_registry: rolled back to version %s (promoted_by=%s)",
            version_id, promoted_by,
        )

        info = _info_from_manifest(target, version_id, is_current=True)
        if info is None:
            raise RuntimeError(
                f"manifest missing for restored version {version_id!r}"
            )
        return info

    # ---------------------------------------------------------- maintenance

    def prune(self, max_versions: Optional[int] = None) -> int:
        """Delete the oldest versions beyond the retention cap.
        Returns the number deleted. Safe to call any time; the
        current version is always preserved regardless of age."""
        with _REGISTRY_LOCK:
            return self._prune_locked(max_versions)

    # ----------------------------------------------------------- internals

    def _prune_locked(self, max_versions: Optional[int] = None) -> int:
        """Inner prune logic. Caller MUST hold ``_REGISTRY_LOCK``."""
        cap = int(max_versions or getattr(
            self._s, "model_retention_max_versions", 10,
        ))
        if cap <= 0:
            return 0
        if not self._versions_root.exists():
            return 0
        current = self.current_version_id()
        # Folder names are ISO timestamps so a name-sort gives
        # chronological order; oldest first.
        all_versions = sorted(
            (p for p in self._versions_root.iterdir()
             if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name,
        )
        if len(all_versions) <= cap:
            return 0
        excess = len(all_versions) - cap
        deleted = 0
        for path in all_versions:
            if deleted >= excess:
                break
            if path.name == current:
                continue  # never delete what we're currently serving
            shutil.rmtree(path, ignore_errors=True)
            deleted += 1
            logger.info("model_registry: pruned old version %s", path.name)
        return deleted

    def _copy_live_artifacts(self, dst_root: Path) -> int:
        """Recursive copy of every top-level child of ``artifacts_dir``
        into ``dst_root``, skipping the registry's own subdirs and
        pointer file. Returns total bytes copied (best effort)."""
        if not self._root.exists():
            return 0
        total_bytes = 0
        for child in self._root.iterdir():
            if child.name in _EXCLUDED_TOP_NAMES or child.name.startswith("."):
                continue
            dst = dst_root / child.name
            if child.is_dir():
                shutil.copytree(child, dst, dirs_exist_ok=False)
            else:
                shutil.copy2(child, dst)
            total_bytes += _path_size(dst)
        return total_bytes


# -------------------------------------------------------- module-level helpers

def _utcnow_iso() -> str:
    """ISO-8601 UTC with seconds resolution. Centralised so the
    timestamp format is identical everywhere the registry writes one
    (manifest.promoted_at, current.json.promoted_at, version_id)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mint_version_id() -> str:
    """Generate a version id of the form ``v_<UTC-iso>`` with
    millisecond resolution to avoid collisions when two promotions
    land in the same second."""
    now = datetime.now(timezone.utc)
    return "v_" + now.strftime("%Y-%m-%dT%H-%M-%S-") + f"{now.microsecond // 1000:03d}Z"


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Atomic JSON write. Thin wrapper around ``io_utils.save_json`` so
    the rest of this module reads a Path-typed helper. The canonical
    tmp+os.replace implementation lives in io_utils -- one home for the
    atomic-write contract across model_registry, retrain_config,
    optuna_tuner, target_encoding, and pair_origins."""
    from demand_forecasting_pipeline.src.utils.io_utils import save_json
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(payload, str(path))


def _info_from_manifest(
    version_path: Path,
    version_id: str,
    *,
    is_current: bool,
) -> Optional[VersionInfo]:
    """Read ``manifest.json`` from ``version_path`` and produce the
    wire-shape. Returns ``None`` if the manifest is missing or
    unreadable -- a folder without a manifest isn't a valid version,
    so list_versions() filters it out.

    For ``is_current=True`` callers a corrupt manifest is escalated to
    a warning (the pointer claims this version is live, so its
    metadata being unreadable is operationally significant -- the gate
    can't compare against it, the UI can't render its score). Non-
    current corrupt manifests just get filtered silently."""
    manifest_path = version_path / _MANIFEST_FILENAME
    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            m = json.load(fh) or {}
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        if is_current:
            logger.warning(
                "model_registry: current version %s has unreadable manifest "
                "(%s); promotion gate will treat it as cold-start until "
                "fixed", version_id, exc,
            )
        return None
    return VersionInfo(
        version_id=str(m.get("version_id") or version_id),
        path=version_path,
        promoted_at=m.get("promoted_at"),
        promoted_by=m.get("promoted_by"),
        trigger=m.get("trigger"),
        accuracy_before=_coerce_float(m.get("accuracy_before")),
        accuracy_after=_coerce_float(m.get("accuracy_after")),
        duration_seconds=_coerce_float(m.get("duration_seconds")),
        is_current=is_current,
        # Champion-challenger fields default to None on legacy
        # manifests written by Phase 1-5 code (pre-Phase 6) so list_
        # versions on a mixed registry keeps working.
        decision=m.get("decision"),
        champion_accuracy=_coerce_float(m.get("champion_accuracy")),
        delta_pp=_coerce_float(m.get("delta_pp")),
    )


def _replace_path(src: Path, dst: Path) -> None:
    """Atomic per-path replace via stage-and-swap.

    Industry-standard pattern for non-transactional filesystem
    restores:

      1. Stage the new content into a sibling temp path under the same
         parent directory (so the eventual rename stays on the same
         filesystem and is therefore atomic).
      2. Rename-swap: for files ``os.replace`` is single-syscall
         atomic; for directories on Windows we can't atomically
         overwrite a non-empty dir, so we move the old one aside,
         rename the new one into place, then delete the old. The
         brief window where ``dst`` doesn't exist is microseconds and
         is bounded by two rename syscalls.
      3. On any failure during the swap we restore the backup so the
         caller never sees a half-replaced path.

    Each invocation is per-path atomic: a concurrent reader sees
    either the old content or the new content for any single file or
    directory, never a mix. (Cross-path atomicity for the whole
    snapshot is not guaranteed -- if rollback restores 5 files and
    fails on the 4th, the first 3 are new and the rest are old. The
    pointer update at the end records the intended state; on a partial
    failure the operator is alerted and can retry, and ``_replace_path``
    on the SAME file is idempotent so retry converges.)
    """
    parent = dst.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Use monotonic time + pid for the temp suffix so concurrent
    # restorers can't collide and a leftover tmp from a crashed run is
    # trivially identifiable (and not in use, since pids don't repeat
    # within the lifetime of a leftover tmp file).
    suffix = f"{os.getpid()}_{time.monotonic_ns()}"
    tmp = parent / f".{dst.name}.rollback_{suffix}"
    backup = parent / f".{dst.name}.bak_{suffix}"

    # Defensive: clean any leftover from a previous crashed restore.
    # Same name collision is impossible in practice (suffix is unique
    # per call), but if it ever happened we'd rather start clean.
    for stale in (tmp, backup):
        if stale.exists():
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                try:
                    stale.unlink()
                except OSError:
                    pass

    # 1. Stage. If this raises (disk full, permission, etc.) ``dst``
    #    is completely untouched and the caller sees a clean failure.
    if src.is_dir():
        shutil.copytree(src, tmp, dirs_exist_ok=False)
    else:
        shutil.copy2(src, tmp)

    # 2. Swap.
    try:
        if dst.exists():
            if dst.is_file() and not src.is_dir():
                # File-over-file: single atomic syscall.
                os.replace(tmp, dst)
            else:
                # Dir-over-dir (or type mismatch): rename-aside then
                # rename-into-place. The brief no-dst window is the
                # only time a concurrent reader can see "missing"; it
                # bounds at two rename syscalls.
                os.rename(dst, backup)
                try:
                    os.rename(tmp, dst)
                except Exception:
                    # Restore the backup so dst is never lost.
                    if not dst.exists():
                        os.rename(backup, dst)
                    raise
                # Success -- discard the backup.
                if backup.is_dir():
                    shutil.rmtree(backup, ignore_errors=True)
                elif backup.exists():
                    try:
                        backup.unlink()
                    except OSError:
                        pass
        else:
            # No existing dst: a single rename is the publish step.
            os.rename(tmp, dst)
    except Exception:
        # Clean up the staged tmp so it doesn't accumulate.
        if tmp.exists():
            if tmp.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        raise


def _validate_version_id(version_id: Any) -> str:
    """Coerce ``version_id`` to a safe string identifier or raise
    ``ValueError``.

    Rejects path-traversal characters (``/``, ``\\``, ``..``, drive
    letters, NUL) and empty / whitespace-only inputs. The regex is
    deliberately stricter than what the filesystem alone would allow
    so a malicious caller can't, for example, supply
    ``rollback("..")`` to make ``versions_root / ".."`` resolve to the
    artifacts root and then trigger ``_replace_path`` of every live
    file onto itself."""
    if version_id is None:
        raise ValueError("version_id cannot be None")
    s = str(version_id).strip()
    if not s:
        raise ValueError("version_id cannot be empty")
    if not _VERSION_ID_PATTERN.match(s):
        raise ValueError(
            f"version_id {s!r} contains disallowed characters; "
            f"must match {_VERSION_ID_PATTERN.pattern}"
        )
    return s


def _is_within(child: Path, parent: Path) -> bool:
    """True iff ``child`` is at or below ``parent`` after resolving
    symlinks/.. components. Belt-and-braces against any future relaxing
    of ``_VERSION_ID_PATTERN``; the regex already blocks separators
    but if it's ever loosened, this check keeps the registry confined
    to its own directory tree."""
    try:
        child_r = child.resolve(strict=False)
        parent_r = parent.resolve(strict=False)
    except (OSError, RuntimeError):
        # ``resolve`` can fail on Windows for some weird paths; treat
        # any resolution failure as "not contained" so the caller
        # rejects rather than risking traversal.
        return False
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


def _path_size(p: Path) -> int:
    """Total bytes used by ``p`` (file size, or recursive sum for a
    directory). Best-effort; OSError on a transient unreadable file
    is swallowed."""
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for sub in _walk_files(p):
        try:
            total += sub.stat().st_size
        except OSError:
            continue
    return total


def _walk_files(root: Path) -> Iterator[Path]:
    for child in root.iterdir():
        if child.is_file():
            yield child
        elif child.is_dir():
            yield from _walk_files(child)


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Module-level singleton for callers that don't want to thread a
# Settings instance through. The retain_scheduler and API routes both
# use this so they share state automatically.
_default_registry: Optional[ModelRegistry] = None
_default_lock = threading.Lock()


def get_model_registry() -> ModelRegistry:
    """Process-wide ModelRegistry singleton. Settings come from
    ``get_settings()`` so all callers use the same artifacts root."""
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = ModelRegistry()
    return _default_registry
