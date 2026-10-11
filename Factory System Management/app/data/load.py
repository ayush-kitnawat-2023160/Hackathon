"""CLI loader for judge test data in the Annex B schema.

Usage: python -m app.data.load --dir <folder>
Loads orders.csv, plants.csv, transport_duties.csv and capacity_events.csv (any that exist),
then prints the validation report.
"""
import argparse
import sys
from pathlib import Path

from app.data.loader import TABLES, LoadError
from app.data.store import ingest_bytes, read_all
from app.data.validate import FileRejected, cross_checks

# plants first so capacity_events can be checked against known plants
ORDER = ("plants", "orders", "transport_duties", "capacity_events")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="folder with the CSV files")
    a = ap.parse_args(argv)
    folder = Path(a.dir)
    failed = False
    for table in ORDER:
        path = folder / f"{table}.csv"
        if not path.exists():
            print(f"{table:17} not found (skipped)")
            continue
        try:
            r = ingest_bytes(path.read_bytes(), table)
        except (LoadError, FileRejected) as e:
            print(f"{table:17} REJECTED  {e}")
            failed = True
            continue
        print(f"{table:17} {r['status']:9} rows_loaded={r['rows_loaded']} rows_rejected={r['rows_rejected']}")
        for rej in r["rejects"]:
            print(f"    row {rej['row']}  {rej['column']}: {rej['reason']}")
    print("\nValidation checks")
    checks = cross_checks(read_all())
    for c in checks:
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check']}" + (f"  ({c['detail']})" if c["detail"] else ""))
    print(f"{sum(c['passed'] for c in checks)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    assert set(ORDER) == set(TABLES)
    sys.exit(main())
