"""Standalone, solver-based multi-plant allocator."""

from .engine import solve_allocation, compare_plans, solve_tradeoff
from .loader import load_dataset

__all__ = ["load_dataset", "solve_allocation", "compare_plans", "solve_tradeoff"]
