"""Standalone, solver-based multi-plant allocator."""

from .engine import solve_allocation, compare_plans, solve_tradeoff
from .loader import load_dataset
from .analysis import analyze_alternative, analyze_tradeoffs, diagnose_infeasibility

__all__ = ["load_dataset", "solve_allocation", "compare_plans", "solve_tradeoff",
           "analyze_alternative", "analyze_tradeoffs", "diagnose_infeasibility"]
