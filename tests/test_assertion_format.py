"""The output format has to match the published assertion schema.

The organisers' `.result` files are JSON arrays whose elements carry only
the fields that are actually asserted:

* ``{}``                       - the statement has to succeed
* ``{"affected_rows": N}``     - the changed row count
* ``{"error": "<name>"}``      - the statement has to fail
* ``{"columns": [...], "rows": [[...]]}`` - the result set

These checks keep the rendering aligned with that schema, because the
grading driver parses our stdout into those fields.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from minivectordb.results import (Result, error_name,  # noqa: E402
                                   render_assertion)


class AssertionFormatTest(unittest.TestCase):
    def test_success_without_a_result_set_is_an_empty_object(self):
        result = Result.status("Table 'd' created")
        self.assertEqual(json.loads(render_assertion(result)), {})

    def test_dml_carries_the_affected_row_count(self):
        result = Result.ok(message="3 row(s) affected", affected_rows=3)
        self.assertEqual(
            json.loads(render_assertion(result)),
            {"affected_rows": 3},
        )

    def test_query_carries_columns_and_rows(self):
        result = Result.ok(columns=["id", "distance"], rows=[[1, 5.0]])
        self.assertEqual(
            json.loads(render_assertion(result)),
            {"columns": ["id", "distance"], "rows": [[1, 5.0]]},
        )

    def test_error_carries_a_semantic_name(self):
        result = Result.error(1062, "Duplicate entry '1' for key 'PRIMARY'")
        self.assertEqual(
            json.loads(render_assertion(result)), {"error": "duplicate_key"}
        )

    def test_unsupported_statement_has_its_own_name(self):
        result = Result.error(1064, "unsupported statement")
        self.assertEqual(
            json.loads(render_assertion(result)),
            {"error": "unsupported_sql"},
        )

    def test_unknown_error_codes_still_get_a_name(self):
        self.assertTrue(error_name(9999))
        self.assertTrue(error_name(1062))

    def test_vectors_are_rendered_as_lists(self):
        # one row, one column, the column holds a vector
        result = Result.ok(columns=["e"], rows=[[(1.0, 2.0)]])
        self.assertEqual(
            json.loads(render_assertion(result))["rows"], [[[1.0, 2.0]]]
        )

    def test_no_extra_fields_leak_into_the_assertion(self):
        """A statement that only reports a status must compare equal to {}."""
        result = Result.ok(message="Table 'd' created")
        self.assertEqual(set(json.loads(render_assertion(result))), set())


if __name__ == "__main__":
    unittest.main()
