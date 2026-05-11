"""
Router - turns pair signals into pair-level routing decisions.

Design:

  1. Rules are declared in config, sorted by priority (DESC).
  2. Each rule's ``when`` is evaluated *vectorized* across the full signal
     frame - one pandas mask per rule, not per pair. That's the only
     iteration over the frame.
  3. Rule actions (allow / exclude / prefer / modifiers) are accumulated
     per pair. ``allow`` is the first winner (highest priority allow rule
     wins); ``exclude`` unions; ``prefer`` orders.
  4. The ``models`` for each pair is: effective allow list (or full class
     menu if no ``allow`` fired) minus exclude, then reordered so
     ``prefer`` entries come first.

No rule -> pair uses the class's full menu (i.e. today's behaviour).
Reason string is the semicolon-joined list of fired rule names; the raw
list is also persisted so downstream tooling can parse it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .rules import Rule, evaluate_condition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    """Immutable per-pair routing decision.

    ``models`` and ``fired_rules`` are tuples (not lists) so the value
    survives a ``frozen=True`` dataclass and downstream code can never
    mutate a routing decision in place. Construction sites in this
    module pass tuples explicitly.
    """

    models: tuple[str, ...] = ()
    skip_hp_tuning: bool = False
    hp_trials_multiplier: float = 1.0
    fired_rules: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return ";".join(self.fired_rules) if self.fired_rules else "default"


class Router:
    """Resolve per-pair routes from declarative rules."""

    def __init__(
        self,
        rules: list[Rule],
        class_menus: dict[str, list[str]],
        *,
        known_signals: set[str] | None = None,
    ) -> None:
        self._rules = sorted(rules, key=lambda r: r.priority, reverse=True)
        self._class_menus = {cls: list(menu) for cls, menu in class_menus.items()}

        # Startup validation: fail loud on typos or orphaned rules.
        self._validate_unique_names()
        self._validate_model_references()
        if known_signals is not None:
            self._log_unknown_signals(known_signals)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: dict | None,
        class_menus: dict[str, list[str]],
        *,
        known_signals: set[str] | None = None,
    ) -> "Router":
        cfg = cfg or {}
        raw_rules = cfg.get("rules") or []
        rules = [Rule.from_dict(r) for r in raw_rules]
        return cls(rules=rules, class_menus=class_menus, known_signals=known_signals)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(
        self,
        signals: pd.DataFrame,
        group_keys: list[str],
        *,
        class_col: str = "class",
    ) -> dict[tuple, Route]:
        """Return ``{pair_key: Route}`` for every pair in ``signals``."""
        if signals.empty:
            return {}
        if class_col not in signals.columns:
            raise ValueError(f"Signals frame missing required column '{class_col}'")

        n = len(signals)
        idx = signals.index

        # Per-row accumulators.
        allow_override: list[list[str] | None] = [None] * n
        excludes: list[set[str]] = [set() for _ in range(n)]
        prefers: list[list[str]] = [[] for _ in range(n)]
        skip_hp = [False] * n
        hp_mult = [1.0] * n
        fired: list[list[str]] = [[] for _ in range(n)]

        position = {row_idx: i for i, row_idx in enumerate(idx)}

        # Evaluate each rule once across the frame (vectorised).
        for rule in self._rules:
            mask = evaluate_condition(signals, rule.when)
            if not mask.any():
                continue
            for row_idx in signals.index[mask]:
                i = position[row_idx]
                fired[i].append(rule.name)
                if rule.allow is not None and allow_override[i] is None:
                    allow_override[i] = list(rule.allow)
                if rule.exclude:
                    excludes[i].update(rule.exclude)
                for m in rule.prefer:
                    if m not in prefers[i]:
                        prefers[i].append(m)
                if rule.skip_hp_tuning and not skip_hp[i]:
                    skip_hp[i] = True
                if rule.hp_trials_multiplier != 1.0 and hp_mult[i] == 1.0:
                    hp_mult[i] = rule.hp_trials_multiplier

        # Materialise routes per pair.
        routes: dict[tuple, Route] = {}
        classes = signals[class_col].tolist()
        key_tuples = list(
            signals[group_keys].itertuples(index=False, name=None)
        )

        for i, pair_key in enumerate(key_tuples):
            base = allow_override[i] if allow_override[i] is not None else self._class_menus.get(classes[i], [])
            filtered = [m for m in base if m not in excludes[i]]
            if prefers[i]:
                preferred = [m for m in prefers[i] if m in filtered]
                rest = [m for m in filtered if m not in preferred]
                final_models = preferred + rest
            else:
                final_models = filtered

            routes[pair_key] = Route(
                models=tuple(final_models),
                skip_hp_tuning=skip_hp[i],
                hp_trials_multiplier=hp_mult[i],
                fired_rules=tuple(fired[i]),
            )
        return routes

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def class_model_coverage(
        routes: dict[tuple, Route],
        pair_to_class: dict[tuple, str],
    ) -> dict[str, set[str]]:
        """Union of models across all pairs of each class.

        Training can skip fitting any model that isn't in a class's coverage
        - nothing selects it, so fitting it is wasted work.
        """
        out: dict[str, set[str]] = {}
        for pair_key, route in routes.items():
            cls = pair_to_class.get(pair_key)
            if cls is None:
                continue
            out.setdefault(cls, set()).update(route.models)
        return out

    # ------------------------------------------------------------------
    # Startup validation
    # ------------------------------------------------------------------

    def _validate_unique_names(self) -> None:
        seen: set[str] = set()
        for rule in self._rules:
            if rule.name in seen:
                raise ValueError(f"Duplicate routing rule name: {rule.name!r}")
            seen.add(rule.name)

    def _validate_model_references(self) -> None:
        known_models: set[str] = set()
        for menu in self._class_menus.values():
            known_models.update(menu)
        for rule in self._rules:
            referenced = rule.referenced_models()
            unknown = referenced - known_models
            if unknown:
                raise ValueError(
                    f"Rule '{rule.name}' references unknown model(s) "
                    f"{sorted(unknown)}. Known (from models.enabled): "
                    f"{sorted(known_models)}"
                )

    def _log_unknown_signals(self, known_signals: set[str]) -> None:
        for rule in self._rules:
            unknown = rule.referenced_signals() - known_signals
            if unknown:
                logger.warning(
                    "Routing rule '%s' references unknown signal(s) %s - rule will never fire.",
                    rule.name, sorted(unknown),
                )
