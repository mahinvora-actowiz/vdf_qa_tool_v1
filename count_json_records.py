"""Count entries in a top-level JSON array without loading the file into memory.

Usage:
    python count_json_records.py path\to\data.json
"""
from __future__ import annotations

import argparse

from vdf_qa.jsonstream import JsonStreamError, iter_json_array


def main() -> int:
    parser = argparse.ArgumentParser(description="Count records in a JSON array file.")
    parser.add_argument("json_file", help="path to a JSON file whose top-level value is an array")
    args = parser.parse_args()

    try:
        count = sum(1 for _ in iter_json_array(args.json_file))
    except (OSError, JsonStreamError) as exc:
        parser.exit(1, f"error: {exc}\n")

    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
