#!/usr/bin/env python3
"""Mysqltest-style test runner for MiniVectorDB.

The official grading driver compares the produced output against an
expected ``.result`` file, ignoring trailing horizontal whitespace.  This
runner does the same thing locally so we can validate behaviour without
the (unreleased) task package.

A test case is a ``.test`` file: a sequence of SQL statements, each on
its own line (or spanning lines until a ``;``).  A line starting with
``--`` is a comment.  The expected output lives next to it as
``.result``.

Usage::

    python3 tests/run_tests.py             # run every case
    python3 tests/run_tests.py basic       # run cases whose name matches
    python3 tests/run_tests.py -v          # show the diff for failures
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def normalize(content):
    """Ignore trailing horizontal whitespace, like the official driver."""
    lines = content.split(b"\n")
    out = []
    for line in lines:
        if line.endswith(b"\r"):
            out.append(line[:-1].rstrip(b" \t") + b"\r")
        else:
            out.append(line.rstrip(b" \t"))
    return b"\n".join(out)


def read_statements(path):
    """Split a .test file into statements."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    statements = []
    buf = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("--"):
            if not buf:
                continue
        buf += raw_line + "\n"
        if stripped.endswith(";"):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


def run_case(test_path, result_path, keep_data=False, verbose=False):
    statements = read_statements(test_path)
    data_dir = tempfile.mkdtemp(prefix="mvd-test-")
    try:
        env = dict(os.environ)
        env["MVD_DATA_DIR"] = data_dir
        env["MVD_PROTOCOL"] = "line"
        env["MVD_ECHO"] = "1"
        env.pop("MVD_MODE", None)
        payload = "\n".join(statements) + "\n"
        proc = subprocess.run(
            [sys.executable, "-m", "minivectordb.server"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=env,
            timeout=120,
        )
        actual = proc.stdout
        # The server echoes the statement it ran; the official mysqltest
        # result files contain the statement text too, so the comparison is
        # done on the echoed text.
        with open(result_path, "rb") as handle:
            expected = handle.read()
        ok = normalize(expected) == normalize(actual)
        return ok, expected, actual, proc.stderr
    finally:
        if not keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)


def discover():
    cases = []
    for root, _dirs, files in os.walk(HERE):
        for name in files:
            if not name.endswith(".test"):
                continue
            test_path = os.path.join(root, name)
            result_path = test_path[: -len(".test")] + ".result"
            if not os.path.exists(result_path):
                continue
            cases.append((name[: -len(".test")], test_path, result_path))
    cases.sort()
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern", nargs="?", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    cases = discover()
    if args.pattern:
        cases = [c for c in cases if args.pattern in c[0]]
    if not cases:
        print("no test cases found")
        return 1

    failures = []
    for name, test_path, result_path in cases:
        try:
            ok, expected, actual, stderr = run_case(
                test_path, result_path, args.keep_data, args.verbose
            )
        except subprocess.TimeoutExpired:
            failures.append(name)
            print("[ FAILED  ] {} (timeout)".format(name))
            continue
        if ok:
            print("[       OK ] {}".format(name))
        else:
            failures.append(name)
            print("[  FAILED  ] {}".format(name))
            if args.verbose:
                import difflib
                exp_lines = normalize(expected).decode(
                    "utf-8", "replace"
                ).splitlines()
                act_lines = normalize(actual).decode(
                    "utf-8", "replace"
                ).splitlines()
                for line in difflib.unified_diff(
                    exp_lines, act_lines, "expected", "actual", lineterm=""
                ):
                    print("    " + line)
                if stderr:
                    sys.stderr.write(stderr.decode("utf-8", "replace"))

    print("\n{} case(s), {} failure(s)".format(len(cases), len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
