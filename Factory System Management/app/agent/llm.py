"""The only module that talks to a language model. Two uses: parse a request, word verified evidence.

Provider is chosen by LLM_PROVIDER / LLM_MODEL. MOCK_LLM=true (or any model failure) falls back to the
rule parser and the templates, so plans keep working without a model.
"""
import json
import time

from app.agent.intent import Intent, rule_parse
from app.config import env, mock_llm

ROUTER_PROMPT = (
    "You turn a planner's request about allocating manufacturing orders to plants A, B and C into a "
    "typed intent. Do not answer the request. Do not invent values the user did not state; leave them null. "
    "Order ids look like O-077 (\"order 77\" means O-077)."
)
WRITER_PROMPT = (
    "You write short, plain-English text for operations managers about a production plan. Use ONLY the "
    "figures in the EVIDENCE JSON, written the same way or rounded (1840000 may be written 1,840,000 or "
    "1.84M; carbon_kg/1000 = tonnes). Never compute new figures, never estimate, never add numbers that "
    "are not in the evidence. If the evidence does not contain a figure the user asked for, say it was "
    "not computed. No headings unless asked for a brief."
)


def _client():
    provider = env("LLM_PROVIDER", "anthropic").lower()
    model = env("LLM_MODEL", "claude-sonnet-5-5")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=0, max_tokens=1200, timeout=60)
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=0, base_url=env("OLLAMA_BASE_URL", "http://localhost:11434"))
    raise ValueError(f"Unsupported LLM_PROVIDER {provider}")


def model_name() -> str:
    return "mock (rules and templates)" if mock_llm() else f"{env('LLM_PROVIDER', 'anthropic')}:{env('LLM_MODEL', 'claude-sonnet-5-5')}"


def health() -> dict:
    if mock_llm():
        return {"status": "mock", "model": model_name()}
    try:
        _client()
        return {"status": "configured", "model": model_name()}
    except Exception as e:  # noqa: BLE001 - report, never crash /health
        return {"status": "unavailable", "model": model_name(), "error": type(e).__name__}


def _tokens(msg) -> int:
    u = getattr(msg, "usage_metadata", None) or {}
    return int(u.get("total_tokens", 0))


def parse_intent(text: str) -> tuple[Intent, dict]:
    if mock_llm():
        return rule_parse(text), {"source": "rules", "llm_calls": 0, "tokens": 0}
    calls = tokens = 0
    t0 = time.perf_counter()
    for _ in range(2):  # one retry, then rules
        try:
            chain = _client().with_structured_output(Intent, include_raw=True)
            calls += 1
            out = chain.invoke([("system", ROUTER_PROMPT), ("human", text)])
            tokens += _tokens(out.get("raw"))
            if out.get("parsed") is not None:
                return out["parsed"], {"source": "llm", "llm_calls": calls, "tokens": tokens,
                                       "ms": round((time.perf_counter() - t0) * 1000)}
        except Exception:  # noqa: BLE001 - provider errors fall back to rules
            pass
    return rule_parse(text), {"source": "rules_fallback", "llm_calls": calls, "tokens": tokens}


def narrate(task: str, evidence: dict, bad: list[str] | None = None) -> tuple[str | None, dict]:
    """Return (text, meta); text is None when no model is available, so the caller uses the template."""
    if mock_llm():
        return None, {"llm_calls": 0, "tokens": 0}
    msg = f"TASK: {task}\nEVIDENCE: {json.dumps(evidence, default=str)}"
    if bad:
        msg += f"\nYour previous answer used figures not in the evidence: {', '.join(bad)}. Remove or correct them."
    try:
        out = _client().invoke([("system", WRITER_PROMPT), ("human", msg)])
        content = out.content if isinstance(out.content, str) else "".join(
            p.get("text", "") for p in out.content if isinstance(p, dict))
        return content.strip(), {"llm_calls": 1, "tokens": _tokens(out)}
    except Exception as e:  # noqa: BLE001
        return None, {"llm_calls": 1, "tokens": 0, "error": type(e).__name__}
