"""
Business constants -- scoring weights, accuracy zones.
All configurable, nothing hardcoded in logic files.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccuracyZone:
    """Item accuracy sweet-spot boundaries."""
    perfect_low: float = 0.75    # 75% of recommended
    perfect_high: float = 1.20   # 120% of recommended
    max_over: float = 2.00       # 200% = 0% accuracy


@dataclass(frozen=True)
class ScoringWeights:
    """Customer score = coverage * w_coverage + accuracy * w_accuracy."""
    coverage: float = 0.40
    accuracy: float = 0.60


@dataclass(frozen=True)
class SupervisionConstants:
    """Top-level container for all business constants."""
    accuracy: AccuracyZone = field(default_factory=AccuracyZone)
    scoring: ScoringWeights = field(default_factory=ScoringWeights)
