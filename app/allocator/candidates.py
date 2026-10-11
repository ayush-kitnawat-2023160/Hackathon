"""Calculate order-plant-week choices, delivery dates, landed cost, and carbon."""

from datetime import timedelta
from decimal import Decimal

from .models import Candidate, Dataset


def create_candidates(data: Dataset):
    """Eligible choices, including late options for fallback."""
    candidates = []
    for order in data.orders.values():
        for plant in data.capacities:
            pp = data.products.get((plant, order.product))
            if pp is None:
                continue
            route = data.transport[(plant, order.destination)]
            unit_subtotal = pp.unit_cost + pp.energy_kwh * pp.energy_price + route.cost
            # Duty applies to production, energy, and shipping.
            landed_cost = order.qty * unit_subtotal * (1 + route.duty)
            for week in range(1, data.weeks + 1):
                week_sunday = data.start + timedelta(days=(week - 1) * 7 + 6)
                arrival = week_sunday + timedelta(days=route.days)
                carbon_factor = pp.weekly_carbon.get(week, pp.carbon_factor)
                co2_kg = order.qty * (pp.energy_kwh * carbon_factor + route.carbon)
                candidates.append(Candidate(
                    order.id, plant, week, arrival,
                    max(0, (arrival - order.due).days),
                    landed_cost, co2_kg,
                ))
    return candidates


def weekly_capacities(data: Dataset, *, use_events=False, additional_events=()):
    """Apply the largest overlapping loss to each whole week."""
    events = (data.events if use_events else ()) + tuple(additional_events)
    capacities = {}
    for plant, full in data.capacities.items():
        for week in range(1, data.weeks + 1):
            monday = data.start + timedelta(days=(week - 1) * 7)
            sunday = monday + timedelta(days=6)
            loss = max((e.loss for e in events if e.plant == plant and e.start <= sunday and e.end >= monday), default=0)
            capacities[plant, week] = int(Decimal(full) * (Decimal(1) - Decimal(str(loss))))
    return capacities
