#!/usr/bin/env python3
"""Run the official public cases against MiniVectorDB.

The organisers publish five sample cases on the `2026-competition-round2`
branch under `mysqltest/`.  Their format is documented in that directory's
README and differs from classic mysqltest:

* ``cases/<name>.test``   - UTF-8, every non-empty line is one complete SQL
                            statement, executed in file order;
* ``cases/<name>.result`` - a JSON array, one expected assertion object per
                            statement, in the same order.

Assertion fields:

* ``{}``               - the statement must succeed, nothing else is checked.
* ``affected_rows``    - the changed row count, must match exactly.
* ``columns``          - column names including aliases, order significant.
* ``rows``             - result rows; compared in order unless
                         ``unordered: true``, in which case it is compared as
                         a multiset (duplicates count).
* ``unordered: true``  - order-insensitive row comparison.

Comparison rules from the README: integers, column names and strings must
match exactly; floats use an absolute *and* relative tolerance of 1e-5
(distances are Euclidean, not squared).

Each case starts from its own empty data directory.

    python3 tests/run_official_cases.py            # all cases
    python3 tests/run_official_cases.py -v         # show mismatches
    python3 tests/run_official_cases.py sql_crud   # one case
"""

import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

OFFICIAL_DIR = os.path.join(HERE, "official")
FLOAT_TOL = 1e-5


# ---------------------------------------------------------------------------
# value comparison
# ---------------------------------------------------------------------------


def values_equal(expected, actual):
    """Compare one cell using the published rules."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if isinstance(expected, int) and isinstance(actual, int):
            return expected == actual
        # float comparison: absolute AND relative tolerance of 1e-5
        difference = abs(float(expected) - float(actual))
        if difference <= FLOAT_TOL:
            return True
        scale = max(abs(float(expected)), abs(float(actual)))
        return scale > 0 and difference / scale <= FLOAT_TOL
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, str) and isinstance(actual, str):
        return expected == actual
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        return len(expected) == len(actual) and all(
            values_equal(a, b) for a, b in zip(expected, actual)
        )
    return expected == actual


def rows_equal(expected_rows, actual_rows, unordered):
    if len(expected_rows) != len(actual_rows):
        return False
    if not unordered:
        return all(
            values_equal(e, a) for e, a in zip(expected_rows, actual_rows)
        )
    remaining = list(actual_rows)
    for expected_row in expected_rows:
        for index, actual_row in enumerate(remaining):
            if values_equal(expected_row, actual_row):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining


# ---------------------------------------------------------------------------
# case execution
# ---------------------------------------------------------------------------


def read_statements(path):
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return [line.strip() for line in lines if line.strip()]


def run_case(name, verbose=False):
    test_path = os.path.join(OFFICIAL_DIR, name + ".test")
    result_path = os.path.join(OFFICIAL_DIR, name + ".result")
    if not os.path.isfile(test_path) or not os.path.isfile(result_path):
        print("[  MISSING ] {}".format(name))
        return False

    statements = read_statements(test_path)
    with open(result_path, "r", encoding="utf-8") as handle:
        expectations = json.load(handle)
    if len(statements) != len(expectations):
        print("[  BAD PACK ] {}: {} statements vs {} expectations".format(
            name, len(statements), len(expectations)))
        return False

    from minivectordb.executor import Executor
    from minivectordb.parser import parse
    from minivectordb.storage import Storage

    data_dir = tempfile.mkdtemp(prefix="mvd-official-")
    storage = Storage(data_dir)
    storage.open()
    executor = Executor(storage)

    failures = []
    try:
        for index, (sql, expectation) in enumerate(
            zip(statements, expectations)
        ):
            try:
                results = [
                    executor.execute(statement) for statement in parse(sql)
                ]
                result = results[-1] if results else None
            except Exception as exc:                    # noqa: BLE001
                failures.append(
                    "stmt {}: raised {}: {}".format(index + 1, type(exc).__name__,
                                                    exc)
                )
                continue
            if result is None:
                continue
            if getattr(result, "is_error", False):
                failures.append("stmt {}: reported ERROR {}: {}".format(
                    index + 1, result.error_code, result.message))
                continue
            if not isinstance(expectation, dict):
                continue

            if "affected_rows" in expectation:
                if result.affected_rows != expectation["affected_rows"]:
                    failures.append(
                        "stmt {}: affected_rows {} != expected {}".format(
                            index + 1, result.affected_rows,
                            expectation["affected_rows"]
                        )
                    )
            if "columns" in expectation:
                if list(result.columns) != list(expectation["columns"]):
                    failures.append(
                        "stmt {}: columns {} != expected {}".format(
                            index + 1, list(result.columns),
                            expectation["columns"]
                        )
                    )
            if "rows" in expectation:
                if not rows_equal(
                    expectation["rows"], result.rows,
                    bool(expectation.get("unordered")),
                ):
                    failures.append(
                        "stmt {}: rows {} != expected {}".format(
                            index + 1, result.rows, expectation["rows"]
                        )
                    )
    finally:
        storage.close()
        shutil.rmtree(data_dir, ignore_errors=True)

    if failures:
        print("[  FAILED  ] {}".format(name))
        if verbose:
            for failure in failures:
                print("      " + failure)
        return False
    print("[       OK ] {} ({} statements)".format(name, len(statements)))
    return True


def discover():
    if not os.path.isdir(OFFICIAL_DIR):
        return []
    return sorted(
        name[: -len(".test")]
        for name in os.listdir(OFFICIAL_DIR)
        # skip macOS AppleDouble metadata ("._name.test"), which is not a
        # case and is not valid UTF-8
        if name.endswith(".test") and not name.startswith(".")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern", nargs="?", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    cases = discover()
    if args.pattern:
        cases = [c for c in cases if args.pattern in c]
    if not cases:
        print("no official cases found in {}".format(OFFICIAL_DIR))
        return 1

    results = [run_case(name, args.verbose) for name in cases]
    print("\n{} official case(s), {} failure(s)".format(
        len(results), results.count(False)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
