"""
Per-pair model routing.

The router takes one row of signals per pair and returns an ordered list of
candidate models for that pair, plus any modifiers (skip_hp_tuning, etc.).
Rules are fully config-driven; nothing about which model suits which pair
lives in code.
"""

from .router import Route, Router
from .rules import Rule, evaluate_condition
from .signals import build_pair_signals

__all__ = [
    "Route",
    "Router",
    "Rule",
    "build_pair_signals",
    "evaluate_condition",
]
