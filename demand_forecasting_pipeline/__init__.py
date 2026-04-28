"""
Demand Forecasting Pipeline -- production ML forecasting service.

Single boot path: ``python -m demand_forecasting_pipeline`` starts the
FastAPI service. All training and inference is triggered through the
service's ``/pipeline/train`` and ``/pipeline/inference`` endpoints.
"""

__version__ = "1.0.0"
