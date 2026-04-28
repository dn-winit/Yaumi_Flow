"""
Rule data model + vectorized condition evaluation.

A rule has a ``when`` block (conditions), optional ``allow`` / ``exclude`` /
``prefer`` lists for model pool manipulation, and optional modifiers
(``skip_hp_tuning``, ``hp_trials_multiplier``). The engine evaluates every
rule's ``when`` as a boolean mask over the full signal frame in one pass —
no per-pair Python iteration.

Supported condition operators
-----------------------------
Scalar equality is implicit:   ``class: smooth``       → ``class == "smooth"``
Mapping form explicit operators:
  ``lt``, ``lte``, ``gt``, ``gte``, ``eq``, ``ne``
  ``in`` (value in list), ``not_in``
  ``abs_gte``, ``abs_lte``
  ``between`` ([low, high], inclusive both sides)
  ``is_true`` / ``is_false`` for booleans

Unknown operators fail loud at rule load time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


_OPERATORS = frozenset({
    "lt", "lte", "gt", "gte", "eq", "ne",
    "in", "not_in",
    "abs_gte", "abs_lte",
    "between",
    "is_true", "is_false",
})


@dataclass
class Rule:
    name: str
    priority: int = 0
    when: dict = field(default_factory=dict)
    allow: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    prefer: list[str] = field(default_factory=list)
    skip_hp_tuning: bool = False
    hp_trials_multiplier: float = 1.0

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        name = d.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"Rule missing string 'name': {d!r}")
        rule = cls(
            name=name,
            priority=int(d.get("priority", 0)),
            when=dict(d.get("when") or {}),
            allow=list(d["allow"]) if "allow" in d and d["allow"] is not None else None,
            exclude=list(d.get("exclude") or []),
            prefer=list(d.get("prefer") or []),
            skip_hp_tuning=bool(d.get("skip_hp_tuning", False)),
            hp_trials_multiplier=float(d.get("hp_trials_multiplier", 1.0)),
        )
        rule.validate()
        return rule

    def validate(self) -> None:
        if self.hp_trials_multiplier <= 0:
            raise ValueError(f"Rule '{self.name}': hp_trials_multiplier must be > 0")
        for signal, predicate in self.when.items():
            if isinstance(predicate, dict):
                unknown = set(predicate) - _OPERATORS
                if unknown:
                    raise ValueError(
                        f"Rule '{self.name}': unknown operator(s) {sorted(unknown)} "
                        f"on signal '{signal}'. Allowed: {sorted(_OPERATORS)}"
                    )

    def referenced_signals(self) -> set[str]:
        return set(self.when.keys())

    def referenced_models(self) -> set[str]:
        names: set[str] = set()
        if self.allow:
            names.update(self.allow)
        names.update(self.exclude)
        names.update(self.prefer)
        return names


def evaluate_condition(signals: pd.DataFrame, when: dict) -> pd.Series:
    """Return a boolean mask of rows matching ``when``.

    If ``when`` references a column that isn't in ``signals``, the rule cannot
    fire and the mask is all-False (caller decides whether to warn — we just
    report ``False`` rather than raise so missing optional signals don't
    crash the pipeline)."""
    if not when:
        return pd.Series(True, index=signals.index)

    mask = pd.Series(True, index=signals.index)
    for col, predicate in when.items():
        if col not in signals.columns:
            return pd.Series(False, index=signals.index)
        series = signals[col]
        if isinstance(predicate, dict):
            for op, value in predicate.items():
                mask &= _apply_operator(series, op, value)
        else:
            mask &= series == predicate
        if not mask.any():
            return mask  # short-circuit
    return mask


def _apply_operator(series: pd.Series, op: str, value) -> pd.Series:
    if op == "lt":   return series < value
    if op == "lte":  return series <= value
    if op == "gt":   return series > value
    if op == "gte":  return series >= value
    if op == "eq":   return series == value
    if op == "ne":   return series != value
    if op == "in":   return series.isin(list(value))
    if op == "not_in": return ~series.isin(list(value))
    if op == "abs_gte": return series.abs() >= value
    if op == "abs_lte": return series.abs() <= value
    if op == "between":
        lo, hi = value
        return (series >= lo) & (series <= hi)
    if op == "is_true":  return series.fillna(False).astype(bool)
    if op == "is_false": return ~series.fillna(False).astype(bool)
    # _OPERATORS set is validated at rule construction, so this is unreachable.
    raise ValueError(f"Unknown operator: {op}")
