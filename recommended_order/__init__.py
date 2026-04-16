"""
Recommended Order Module -- production-grade recommendation engine.

Usage:
    # As a standalone service
    python -m recommended_order

    # Programmatic usage
    from recommended_order.core import RecommendationEngine
    from recommended_order.config.constants import RecommendationConstants

    engine = RecommendationEngine()
    df = engine.generate(customer_df, journey_customers, van_items, ...)
"""

__version__ = "1.0.0"
