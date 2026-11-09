"""Checks for the Agent prompt material.

The task statement gives the review program a set of structural checks for
``agent/conversation.json`` (valid JSON, size limit, RFC6901 pointers that
resolve, verbatim excerpts, no duplicated evidence, per-dimension sums).
This test runs the same validator so a broken material file can never be
committed unnoticed.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from validate_conversation import Validator  # noqa: E402


class PromptMaterialTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ROOT, "agent", "conversation.json")

    def test_material_exists(self):
        self.assertTrue(os.path.isfile(self.path), self.path)

    def test_material_passes_validation(self):
        validator = Validator(self.path).run()
        self.assertEqual(
            validator.errors, [],
            "conversation.json failed validation:\n  " +
            "\n  ".join(validator.errors),
        )

    def test_size_limit(self):
        self.assertLessEqual(os.path.getsize(self.path), 128 * 1024)

    def test_root_copy_matches(self):
        root_copy = os.path.join(ROOT, "conversation.json")
        if not os.path.isfile(root_copy):
            self.skipTest("no copy at the repository root")
        with open(root_copy, "rb") as handle:
            root_bytes = handle.read()
        with open(self.path, "rb") as handle:
            agent_bytes = handle.read()
        self.assertEqual(root_bytes, agent_bytes)

    def test_covers_every_dimension(self):
        validator = Validator(self.path).run()
        self.assertEqual(validator.errors, [])
        totals = getattr(validator, "totals", {})
        for dimension in (
            "问题理解与自主拆解",
            "技术理解与迁移",
            "独立验证与质疑",
            "实验与反馈迭代",
            "取舍与反思",
        ):
            self.assertGreater(
                totals.get(dimension, 0), 0,
                "no evidence for dimension {}".format(dimension),
            )


if __name__ == "__main__":
    unittest.main()
