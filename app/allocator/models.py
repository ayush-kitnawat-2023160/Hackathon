"""Small typed data containers. No LLM framework dependency."""

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
    capacity: int  # Shared across ALL products at the same plant, for each week.
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
    """Schema the external Planning Agent should produce later.

    Strict mode solves all orders on time. If impossible and allow_fallback=True,
    a separately labeled best-effort plan is attempted.
    """

    objective: str = "cost"  # cost, carbon, balanced
    cost_weight: float = 0.7
    carbon_weight: float = 0.3
    max_carbon_kg: float | None = None
    max_cost: float | None = None
    excluded_plants: tuple[str, ...] = ()
    excluded_plant_products: tuple[tuple[str, str], ...] = ()
    forced_assignments: dict[str, str] = field(default_factory=dict)
    use_capacity_events: bool = False
    additional_capacity_events: tuple[CapacityEvent, ...] = ()
    time_limit_seconds: float = 20.0
    allow_fallback: bool = True
    previous_plan: dict[str, tuple[str, int]] = field(default_factory=dict)
    stability_weight: float = 0.0

    @staticmethod
    def from_dict(value: dict[str, Any] | None = None):
        """Strict input guardrail; unknown fields produce an error, not silent behavior."""
        v = dict(value or {})
        # Simple aliases for the later agent. Do not silently infer unspecified weights.
        allowed = set(AllocationConfig.__dataclass_fields__)
        extra = set(v) - allowed
        if extra:
            raise ValueError(f"Unsupported optimization parameters: {sorted(extra)}")
        events = []
        for event in v.get("additional_capacity_events", ()):
            if isinstance(event, CapacityEvent):
                events.append(event)
            else:
                events.append(CapacityEvent(
                    id=str(event.get("event_id", "SCENARIO")),
                    plant=str(event["plant"]),
                    start=date.fromisoformat(str(event["from_date"])),
                    end=date.fromisoformat(str(event["to_date"])),
                    loss=float(event["capacity_loss_pct"]),
                ))
        v["additional_capacity_events"] = tuple(events)
        for k in ("excluded_plants",):
            if k in v:
                v[k] = tuple(v[k])
        for k in ("cost_weight", "carbon_weight", "max_carbon_kg", "max_cost", "time_limit_seconds", "stability_weight"):
            if k in v and v[k] is not None:
                try:
                    v[k] = float(v[k])
                except (TypeError, ValueError) as e:
                    raise ValueError(f"{k} must be numeric") from e
                if not math.isfinite(v[k]):
                    raise ValueError(f"{k} must be a finite number")
        if "excluded_plant_products" in v:
            pairs = v["excluded_plant_products"]
            if any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in pairs):
                raise ValueError("Each excluded_plant_products entry must be [plant, product]")
            v["excluded_plant_products"] = tuple(tuple(pair) for pair in pairs)
        if "previous_plan" in v:
            v["previous_plan"] = {str(k): (str(p[0]), int(p[1])) for k, p in v["previous_plan"].items()}
        obj = AllocationConfig(**v)
        if obj.objective not in ("cost", "carbon", "balanced"):
            raise ValueError("objective must be cost, carbon, or balanced")
        if obj.cost_weight < 0 or obj.carbon_weight < 0 or (obj.cost_weight + obj.carbon_weight) == 0:
            raise ValueError("Both weights must be nonnegative and not both zero")
        if obj.time_limit_seconds <= 0 or obj.stability_weight < 0:
            raise ValueError("Time limit must be positive; stability weight cannot be negative")
        if obj.max_carbon_kg is not None and obj.max_carbon_kg < 0:
            raise ValueError("Carbon budget cannot be negative")
        if obj.max_cost is not None and obj.max_cost < 0:
            raise ValueError("Cost budget cannot be negative")
        for k in ("use_capacity_events", "allow_fallback"):
            if not isinstance(getattr(obj, k), bool):
                raise ValueError(f"{k} must be a boolean")
        for event in obj.additional_capacity_events:
            if not (0 <= event.loss <= 1) or event.end < event.start:
                raise ValueError("Invalid capacity-loss event")
        return obj
