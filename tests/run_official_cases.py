#!/usr/bin/env python3
"""Run the organisers' public cases against MiniVectorDB.

The organisers publish the case pack at
`oceanbase/seekdb@2026-competition-round2` under `mysqltest/`.  Its format
is documented in that directory's README and differs from classic
mysqltest:

* ``manifest.json``  - the case list: id, group, and the test/result paths.
* ``cases/<name>.test``   - UTF-8; every non-empty line is one complete SQL
                            statement **or** one driver directive.
* ``cases/<name>.result`` - a JSON array with one expected assertion per
                            *SQL statement*; directives do not occupy a slot.

Driver directives (not SQL):

* ``--restart``  - shut down normally (waiting for the exit) and start
                   again against the same data directory; the shutdown must
                   roll back any uncommitted transaction.
* ``--mode ann`` - subsequent vector requests use the ann mode.  Every case
                   starts in the exact mode.

Assertion fields:

* ``{}``            - the statement must succeed; nothing else is checked.
* ``error``         - the statement must fail; the value is a semantic name
                       such as ``duplicate_key``.  The error code mapping is
                       not frozen yet, so any non-empty error code counts.
* ``affected_rows`` - the changed row count, compared exactly.
* ``columns``       - column names and their order, compared exactly.
* ``rows``          - result rows compared in order, or as a multiset when
                       ``unordered: true`` (duplicates must be preserved).

Integers and strings match exactly; floats use an absolute *and* relative
tolerance of 1e-5.  Vectors are stored as float32 and ``L2_DISTANCE``
returns the Euclidean distance, not its square.

Each case starts from its own empty data directory.

    python3 tests/run_official_cases.py            # all cases
    python3 tests/run_official_cases.py -v         # show mismatches
    python3 tests/run_official_cases.py sql_crud   # one case
"""

import argparse
import json
import os
import shutil
import subprocess
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


def _as_number(text):
    """Parse a cell that came from the text protocol into a number.

    The text protocol carries every cell as a string, while the published
    expectations use JSON numbers, so a numeric cell has to be converted
    before it can be compared.  A cell that is not numeric stays a string.
    """
    if isinstance(text, str):
        stripped = text.strip()
        try:
            return int(stripped)
        except ValueError:
            pass
        try:
            return float(stripped)
        except ValueError:
            return text
    return text


def values_equal(expected, actual):
    """Compare one cell using the published rules."""
    actual = _as_number(actual)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if isinstance(expected, int) and isinstance(actual, int):
            return expected == actual
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
# the database under test
# ---------------------------------------------------------------------------


class Database:
    """Runs the server as a subprocess, like the grading driver does."""

    def __init__(self, data_dir, mode="exact"):
        self.data_dir = data_dir
        self.mode = mode
        self.process = None
        self.start()

    def start(self):
        env = dict(os.environ)
        env["MVD_DATA_DIR"] = self.data_dir
        env["MVD_PROTOCOL"] = "line"
        env["MVD_ECHO"] = "0"
        env["MVD_TERMINATOR"] = "0"
        env["MVD_MODE"] = self.mode
        env["MVD_RESULT_SENTINEL"] = "--MVD-END--"
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "minivectordb"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=ROOT,
            env=env,
        )

    def query(self, sql):
        """Run one statement and return (columns, rows, error_code, message)."""
        self.process.stdin.write((sql + "\n").encode("utf-8"))
        self.process.stdin.flush()
        lines = []
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("the database closed the connection")
            text = line.decode("utf-8", "replace").rstrip("\n")
            if text == "--MVD-END--":
                break
            if text.startswith("ERROR"):
                code, _, message = text.partition(":")
                try:
                    error_code = int(code.split()[1])
                except (IndexError, ValueError):
                    error_code = 0
                # The response ends with the sentinel; it must still be
                # consumed, otherwise it would be read as the end of the
                # NEXT statement's response.
                while True:
                    trailer = self.process.stdout.readline()
                    if not trailer:
                        break
                    if trailer.decode(
                        "utf-8", "replace"
                    ).rstrip("\n") == "--MVD-END--":
                        break
                return None, None, error_code, message.strip(), None
            if text:
                lines.append(text)
        if not lines:
            return None, [], 0, None, None
        affected = None
        if len(lines) == 1 and lines[0].endswith(" row(s) affected"):
            # A DML response: the count is the only line.
            affected = int(lines[0].split(" ")[0])
            return None, [], 0, None, affected
        columns = lines[0].split("\t")
        rows = [line.split("\t") for line in lines[1:]]
        return columns, rows, 0, None, affected

    def restart(self):
        """--restart: shut down normally and start again on the same dir."""
        self.process.stdin.close()
        self.process.wait(timeout=60)
        self.start()


def parse_lines(path):
    """Split a .test file into (kind, payload) items."""
    items = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle.read().splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("--"):
                items.append(("directive", line))
            else:
                items.append(("sql", line))
    return items


def case_paths(name):
    """The (test, result) paths of a case, honouring the manifest layout."""
    manifest = os.path.join(OFFICIAL_DIR, "manifest.json")
    if os.path.isfile(manifest):
        with open(manifest, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for case in data.get("cases", []):
            if case.get("id") == name:
                return (
                    os.path.join(OFFICIAL_DIR, case["test"]),
                    os.path.join(OFFICIAL_DIR, case["result"]),
                )
    return (
        os.path.join(OFFICIAL_DIR, name + ".test"),
        os.path.join(OFFICIAL_DIR, name + ".result"),
    )


def run_case(name, verbose=False):
    test_path, result_path = case_paths(name)
    if not os.path.isfile(test_path) or not os.path.isfile(result_path):
        print("[  MISSING ] {} ({} {})".format(name, test_path, result_path))
        return False

    items = parse_lines(test_path)
    with open(result_path, "r", encoding="utf-8") as handle:
        expectations = json.load(handle)

    data_dir = tempfile.mkdtemp(prefix="mvd-official-")
    database = Database(data_dir)
    failures = []
    try:
        expectation_index = 0
        for kind, payload in items:
            if kind == "directive":
                if payload == "--restart":
                    database.restart()
                elif payload == "--mode ann":
                    database.mode = "ann"
                    database.restart()
                elif payload == "--mode exact":
                    database.mode = "exact"
                    database.restart()
                else:
                    failures.append("unknown directive: {}".format(payload))
                continue
            expectation = (
                expectations[expectation_index]
                if expectation_index < len(expectations) else {}
            )
            expectation_index += 1
            columns, rows, error_code, message, affected = database.query(
                payload
            )
            if "error" in expectation:
                if error_code == 0:
                    failures.append(
                        "stmt {}: expected error {!r} but the statement "
                        "succeeded ({})".format(
                            expectation_index, expectation["error"],
                            columns,
                        )
                    )
                continue
            if error_code:
                failures.append(
                    "stmt {}: unexpected error {}: {}".format(
                        expectation_index, error_code, message
                    )
                )
                continue
            if "affected_rows" in expectation:
                actual_count = affected
                if actual_count != expectation["affected_rows"]:
                    failures.append(
                        "stmt {}: affected_rows {} != expected {}".format(
                            expectation_index, actual_count,
                            expectation["affected_rows"],
                        )
                    )
            if "columns" in expectation:
                if list(columns or []) != list(expectation["columns"]):
                    failures.append(
                        "stmt {}: columns {} != expected {}".format(
                            expectation_index, columns,
                            expectation["columns"],
                        )
                    )
            if "rows" in expectation:
                if not rows_equal(
                    expectation["rows"], rows or [],
                    bool(expectation.get("unordered")),
                ):
                    failures.append(
                        "stmt {}: rows {} != expected {}".format(
                            expectation_index, rows, expectation["rows"]
                        )
                    )
        if expectation_index != len(expectations):
            failures.append(
                "executed {} statements but the result file has {} "
                "assertions".format(expectation_index, len(expectations))
            )
    finally:
        try:
            database.process.stdin.close()
            database.process.wait(timeout=30)
        except Exception:
            database.process.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

    if failures:
        print("[  FAILED  ] {}".format(name))
        if verbose:
            for failure in failures:
                print("      " + failure)
        return False
    print("[       OK ] {}".format(name))
    return True


def discover():
    if not os.path.isdir(OFFICIAL_DIR):
        return []
    manifest = os.path.join(OFFICIAL_DIR, "manifest.json")
    if os.path.isfile(manifest):
        with open(manifest, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [
            case["id"] for case in data.get("cases", [])
            if os.path.isfile(
                os.path.join(OFFICIAL_DIR, case.get("test", ""))
            )
        ]
    return sorted(
        name[: -len(".test")]
        for name in os.listdir(OFFICIAL_DIR)
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
