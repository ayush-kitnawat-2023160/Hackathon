"""check_numbers: every figure in LLM text must match a figure in the tool output it was given."""
import json
import re

NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*(?:[kKmM](?![a-zA-Z])|bn)?")
# Numbers that are labels rather than figures: list numbering, years, small counts in prose are still checked
LIST_MARKER = re.compile(r"^\s*\d+\.\s", re.M)


def _parse(tok: str) -> float | None:
    s = tok.strip().replace(",", "")
    mult = 1.0
    if s[-1:] in "kK":
        mult, s = 1e3, s[:-1]
    elif s[-1:] in "mM":
        mult, s = 1e6, s[:-1]
    elif s.endswith("bn"):
        mult, s = 1e9, s[:-2]
    try:
        return float(s.strip()) * mult
    except ValueError:
        return None


def evidence_numbers(evidence) -> set[float]:
    """All numbers in the evidence JSON (values and numbers inside strings), plus their usual renderings."""
    base = set()
    text = json.dumps(evidence, default=str)
    for tok in re.findall(r"-?\d+(?:\.\d+)?", text):
        base.add(float(tok))
    out = set()
    for v in base:
        for d in (v, abs(v), v / 1000, v * 100, round(v), round(v, 1)):
            out.add(round(d, 6))
    return out


def check_numbers(text: str, evidence) -> dict:
    allowed = evidence_numbers(evidence)
    body = LIST_MARKER.sub("", text)
    bad = []
    for m in NUM.finditer(body):
        v = _parse(m.group(0))
        if v is None:
            continue
        if not any(abs(v - a) <= max(0.005 * abs(a), 0.051) for a in allowed):
            bad.append(m.group(0).strip())
    return {"passed": not bad, "unmatched": bad}
