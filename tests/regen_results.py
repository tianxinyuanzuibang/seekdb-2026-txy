#!/usr/bin/env python3
"""Regenerate the expected ``.result`` files for the test cases.

The expected output is produced by the current implementation and then
reviewed by hand, so a change in behaviour shows up as a diff in the
review rather than as a silent pass.

    python3 tests/regen_results.py            # all cases
    python3 tests/regen_results.py basic      # one case
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

from run_tests import read_statements  # noqa: E402


def run_case(test_path):
    statements = read_statements(test_path)
    data_dir = tempfile.mkdtemp(prefix="mvd-regen-")
    try:
        env = dict(os.environ)
        env["MVD_DATA_DIR"] = data_dir
        env["MVD_PROTOCOL"] = "line"
        env["MVD_ECHO"] = "1"
        # These cases are compared byte-for-byte against a stored .result in
        # the classic mysqltest text form: statement echo, no blank-line
        # terminator.
        env["MVD_TERMINATOR"] = "0"
        env.pop("MVD_MODE", None)
        payload = "\n".join(statements) + "\n"
        proc = subprocess.run(
            [sys.executable, "-m", "minivectordb"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=env,
            timeout=120,
        )
        return proc.stdout.decode("utf-8"), proc.stderr.decode(
            "utf-8", "replace"
        )
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern", nargs="?", default=None)
    args = parser.parse_args()

    test_dir = os.path.join(HERE, "t")
    for name in sorted(os.listdir(test_dir)):
        if not name.endswith(".test"):
            continue
        if args.pattern and args.pattern not in name:
            continue
        test_path = os.path.join(test_dir, name)
        result_path = test_path[: -len(".test")] + ".result"
        output, stderr = run_case(test_path)
        with open(result_path, "w", encoding="utf-8") as handle:
            handle.write(output)
        print("wrote {}".format(os.path.relpath(result_path, ROOT)))
        if "Traceback" in stderr:
            print("  WARNING: the server logged a traceback")
            for line in stderr.splitlines()[-6:]:
                print("    " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
