"""Data records and allocation settings."""

from dataclasses import dataclass, field
from datetime import date
import math
from typing import Any


@dataclass(frozen=True)
class Order:
    id: str
    product: str
    qty: int
    destination: str
    due: date


@dataclass(frozen=True)
class PlantProduct:
    plant: str
    product: str
    capacity: int  # Shared plant capacity per week.
    unit_cost: float
    energy_kwh: float
    carbon_factor: float
    energy_price: float
    weekly_carbon: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Transport:
    plant: str
    destination: str
    cost: float
    days: int
    duty: float
    carbon: float


@dataclass(frozen=True)
class CapacityEvent:
    id: str
    plant: str
    start: date
    end: date
    loss: float


@dataclass(frozen=True)
class Dataset:
    orders: dict[str, Order]
    products: dict[tuple[str, str], PlantProduct]
    transport: dict[tuple[str, str], Transport]
    events: tuple[CapacityEvent, ...]
    capacities: dict[str, int]
    start: date
    weeks: int
    source: str


@dataclass(frozen=True)
class Candidate:
    order_id: str
    plant: str
    week: int  # 1 indexed
    arrival: date
    late_days: int
    cost: float
    carbon_kg: float

    @property
    def key(self):
        return self.order_id, self.plant, self.week


@dataclass(frozen=True)
class AllocationConfig:
    """Solver preferences; capacity and eligibility always stay enforced."""

    objective: str = "cost"  # cost, carbon, balanced
    cost_weight: float = 0.7
    carbon_weight: float = 0.3
    max_carbon_kg: float | None = None
    max_cost: float | None = None
    excluded_plants: tuple[str, ...] = ()
    excluded_plant_products: tuple[tuple[str, str], ...] = ()
    forced_assignments: dict[str, str] = field(default_factory=dict)
    frozen_assignments: dict[str, tuple[str, int]] = field(default_factory=dict)
    use_capacity_events: bool = False
    additional_capacity_events: tuple[CapacityEvent, ...] = ()
    time_limit_seconds: float = 20.0
    allow_fallback: bool = True
    previous_plan: dict[str, tuple[str, int]] = field(default_factory=dict)
    stability_weight: float = 0.0

    def __post_init__(self):
        if self.objective not in ("cost", "carbon", "balanced"):
            raise ValueError("objective must be cost, carbon, or balanced")
        for name in ("cost_weight", "carbon_weight", "time_limit_seconds", "stability_weight", "max_cost", "max_carbon_kg"):
            value = getattr(self, name)
            if value is None and name in ("max_cost", "max_carbon_kg"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        if self.time_limit_seconds == 0:
            raise ValueError("time_limit_seconds must be positive")
        if self.cost_weight + self.carbon_weight == 0 or not math.isfinite(self.cost_weight + self.carbon_weight):
            raise ValueError("Weights must have a finite positive sum")
        for name in ("use_capacity_events", "allow_fallback"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in ("excluded_plants", "excluded_plant_products", "additional_capacity_events"):
            if not isinstance(getattr(self, name), (list, tuple)):
                raise ValueError(f"{name} must be a list or tuple")
        if any(not isinstance(plant, str) or not plant.strip() for plant in self.excluded_plants):
            raise ValueError("excluded_plants must contain plant names")
        for pair in self.excluded_plant_products:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(not isinstance(item, str) or not item.strip() for item in pair)):
                raise ValueError("Each excluded_plant_products entry must be [plant, product]")
        if not isinstance(self.forced_assignments, dict) or any(
            not isinstance(oid, str) or not isinstance(plant, str) or not oid.strip() or not plant.strip()
            for oid, plant in self.forced_assignments.items()
        ):
            raise ValueError("forced_assignments must map order IDs to plant names")
        if not isinstance(self.previous_plan, dict):
            raise ValueError("previous_plan must map order IDs to [plant, week]")
        if not isinstance(self.frozen_assignments, dict):
            raise ValueError("frozen_assignments must map order IDs to [plant, week]")
        for oid, pair in self.previous_plan.items():
            if (not isinstance(oid, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or not isinstance(pair[0], str) or type(pair[1]) is not int or pair[1] < 1):
                raise ValueError("previous_plan must map order IDs to [plant, integer week]")
        for oid, pair in self.frozen_assignments.items():
            if (not isinstance(oid, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or not isinstance(pair[0], str) or type(pair[1]) is not int or pair[1] < 1):
                raise ValueError("frozen_assignments must map order IDs to [plant, integer week]")
        for event in self.additional_capacity_events:
            if not isinstance(event, CapacityEvent):
                raise ValueError("additional_capacity_events must contain CapacityEvent records")
            check_event(event)

    @staticmethod
    def from_dict(value: dict[str, Any] | None = None):
        """Read structured settings and reject unknown fields."""
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError("Allocation config must be a JSON object")
        settings = dict(value)
        extra = set(settings) - set(AllocationConfig.__dataclass_fields__)
        if extra:
            raise ValueError(f"Unsupported optimization parameters: {sorted(extra)}")
        events = settings.get("additional_capacity_events", ())
        if not isinstance(events, (list, tuple)):
            raise ValueError("additional_capacity_events must be a list")
        parsed = []
        for event in events:
            if isinstance(event, CapacityEvent):
                parsed.append(event)
                continue
            if not isinstance(event, dict):
                raise ValueError("Capacity event must be an object")
            external = {"plant", "from_date", "to_date", "capacity_loss_pct"}
            recorded = {"plant", "start", "end", "loss"}
            if external <= event.keys() and not set(event) - external - {"event_id"}:
                start, end, loss, event_id = event["from_date"], event["to_date"], event["capacity_loss_pct"], event.get("event_id", "SCENARIO")
            elif recorded <= event.keys() and not set(event) - recorded - {"id"}:
                start, end, loss, event_id = event["start"], event["end"], event["loss"], event.get("id", "SCENARIO")
            else:
                raise ValueError("Capacity event needs plant, dates, and loss")
            try:
                parsed.append(CapacityEvent(
                    event_id, event["plant"],
                    date.fromisoformat(start) if isinstance(start, str) else start,
                    date.fromisoformat(end) if isinstance(end, str) else end, loss,
                ))
            except (TypeError, ValueError) as error:
                raise ValueError("Capacity event dates must use YYYY-MM-DD") from error
        settings["additional_capacity_events"] = tuple(parsed)
        return AllocationConfig(**settings)


def check_event(event):
    if (not isinstance(event.id, str) or not event.id.strip()
            or not isinstance(event.plant, str) or not event.plant.strip()
            or type(event.start) is not date or type(event.end) is not date
            or event.end < event.start or isinstance(event.loss, bool)
            or not isinstance(event.loss, (int, float)) or not math.isfinite(event.loss)
            or not 0 <= event.loss <= 1):
        raise ValueError("Invalid capacity-loss event")
