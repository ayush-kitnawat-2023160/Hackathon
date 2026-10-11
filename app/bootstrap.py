"""Import a folder of Annex B CSVs through the same validation as /ingest."""

import argparse
import os
from pathlib import Path

from app.allocator.loader import REQUIRED
from app.ingestion import validate_upload
from app.store import Store


def main():
    parser = argparse.ArgumentParser(description="Load the provided CSV data into SQLite")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "data/triplan.sqlite"))
    args = parser.parse_args()
    store = Store(args.db)
    for name in REQUIRED:
        path = args.data_dir / (name + ".csv")
        if not path.is_file():
            if name == "capacity_events":
                continue
            parser.error(f"Required file missing: {path}")
        content, rows = validate_upload(name, path.read_bytes())
        store.put_table(name, content, rows)
        print(f"Loaded {name}: {rows} rows")
    print("Data ready:", sorted(store.snapshot()[0]))


if __name__ == "__main__":
    main()
