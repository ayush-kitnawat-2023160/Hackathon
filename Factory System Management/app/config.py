"""Settings from config.yaml and the environment (.env)."""
import os
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@lru_cache
def cfg() -> dict:
    with open(Path(__file__).with_name("config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def planning_start() -> date:
    return date.fromisoformat(str(cfg()["planning_start"]))


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).split("#")[0].strip()


def mock_llm() -> bool:
    return env("MOCK_LLM", "true").lower() in ("1", "true", "yes")


def db_path() -> str:
    return env("DB_PATH", str(ROOT / "allocator.db"))
