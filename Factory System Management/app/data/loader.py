"""Read an uploaded or on-disk CSV for one named table. Untrusted input: size and row limits apply."""
import io

import pandas as pd

from app.config import cfg

TABLES = ("orders", "plants", "transport_duties", "capacity_events")


class LoadError(ValueError):
    """The whole file is unusable (too big, not CSV, unknown table)."""


def load_csv(content: bytes, table: str) -> pd.DataFrame:
    if table not in TABLES:
        raise LoadError(f"Unknown table '{table}'. Allowed: {', '.join(TABLES)}")
    limits = cfg()["limits"]
    if len(content) > limits["max_upload_bytes"]:
        raise LoadError(f"File is larger than {limits['max_upload_bytes']} bytes")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise LoadError("File is not UTF-8 text") from e
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, skipinitialspace=True)
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        raise LoadError(f"Not a readable CSV: {e}") from e
    if len(df) > limits["max_rows"]:
        raise LoadError(f"File has {len(df)} rows; the limit is {limits['max_rows']}")
    df.columns = [c.strip() for c in df.columns]
    return df.apply(lambda s: s.str.strip())
