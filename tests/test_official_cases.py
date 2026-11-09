"""Runs the organisers' public cases as part of the test suite.

`tests/run_official_cases.py` implements the comparison rules documented in
`tests/official/README.md` (exact match for integers, column names and
strings; a combined absolute/relative tolerance of 1e-5 for floats; multiset
comparison when `unordered` is set).  Running them here means a regression in
any of the shapes the public cases exercise - quoted vector literals,
`ORDER BY <alias>`, an un-aliased expression in the projection, or the DML
`affected_rows` field - fails the suite instead of only failing on the
grading platform.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import run_official_cases as runner  # noqa: E402


class OfficialCasesTest(unittest.TestCase):
    def test_cases_are_vendored(self):
        cases = runner.discover()
        self.assertGreaterEqual(
            len(cases), 5,
            "the public case pack is missing from tests/official",
        )

    def test_manifest_matches_files(self):
        manifest_path = os.path.join(runner.OFFICIAL_DIR, "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        listed = {case["id"] for case in manifest["cases"]}
        self.assertEqual(listed, set(runner.discover()))

    def test_statements_match_expectations(self):
        for name in runner.discover():
            with self.subTest(case=name):
                statements = runner.read_statements(
                    os.path.join(runner.OFFICIAL_DIR, name + ".test")
                )
                with open(
                    os.path.join(runner.OFFICIAL_DIR, name + ".result"),
                    "r", encoding="utf-8",
                ) as handle:
                    expectations = json.load(handle)
                self.assertEqual(
                    len(statements), len(expectations),
                    "{}: statement/expectation count mismatch".format(name),
                )

    def test_every_case_passes(self):
        for name in runner.discover():
            with self.subTest(case=name):
                self.assertTrue(
                    runner.run_case(name, verbose=True),
                    "official case {} failed".format(name),
                )


if __name__ == "__main__":
    unittest.main()
