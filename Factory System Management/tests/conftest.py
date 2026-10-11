import os
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest

os.environ["MOCK_LLM"] = "true"
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

START = date(2026, 10, 12)


def d(days: int) -> date:
    return START + timedelta(days=days)


@pytest.fixture
def tiny():
    """Two plants, one destination. Hand-worked numbers in test_formulas."""
    plants = pd.DataFrame([
        {"plant": "A", "product": "P1", "weekly_capacity": 1000, "unit_cost": 10.0, "energy_kwh_per_unit": 2.0,
         "carbon_kg_per_kwh": 0.5, "energy_price_per_kwh": 0.1, "week_start": None},
        {"plant": "C", "product": "P1", "weekly_capacity": 1000, "unit_cost": 12.0, "energy_kwh_per_unit": 2.0,
         "carbon_kg_per_kwh": 0.1, "energy_price_per_kwh": 0.2, "week_start": None},
    ])
    lanes = pd.DataFrame([
        {"plant": "A", "destination": "DE", "transport_cost_per_unit": 1.0, "transit_days": 10, "duty_pct": 10.0,
         "carbon_kg_per_unit": 0.5},
        {"plant": "C", "destination": "DE", "transport_cost_per_unit": 0.5, "transit_days": 1, "duty_pct": 0.0,
         "carbon_kg_per_unit": 0.2},
    ])
    orders = pd.DataFrame([{"order_id": "O-1", "product": "P1", "qty": 100, "destination": "DE", "due": d(8)}])
    return {"orders": orders, "plants": plants, "transport_duties": lanes}
