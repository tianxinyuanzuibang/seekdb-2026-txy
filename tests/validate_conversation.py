#!/usr/bin/env python3
"""Validate agent/conversation.json against the contest's stated rules.

The task statement (section 4.4 and 3.4.3) requires the prompt material to
be a single JSON file that a review program can check.  The checks the
statement calls out are:

  * UTF-8 encoded, valid JSON, top level is an object,
  * at most 128 KiB,
  * no duplicate JSON keys,
  * no NaN / Infinity (non-standard JSON numbers),
  * every evidence reference carries an RFC6901 JSON pointer *and* an exact
    excerpt of at most 240 characters,
  * the referenced text is really present at that pointer (locatable and
    verbatim),
  * student evidence and result evidence are separate excerpts,
  * an instance scored above 0.5 must provide result evidence, while the
    0.5 tier must leave it empty,
  * the same excerpt is not reused across instances or dimensions,
  * per-dimension scores equal the sum of that dimension's instance scores.

Run::

    python3 tests/validate_conversation.py [path]

Exit code 0 when the material passes every check.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MAX_BYTES = 128 * 1024
MAX_EXCERPT = 240
DIMENSIONS = (
    "问题理解与自主拆解",
    "技术理解与迁移",
    "独立验证与质疑",
    "实验与反馈迭代",
    "取舍与反思",
)
TIERS = (0.0, 0.5, 1.0, 1.5, 2.0)


class Validator:
    def __init__(self, path):
        self.path = path
        self.errors = []
        self.warnings = []
        self.document = None

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    # -- structural ----------------------------------------------------
    def load(self):
        with open(self.path, "rb") as handle:
            raw = handle.read()
        if len(raw) > MAX_BYTES:
            self.error(
                "file is {} bytes, over the {} byte limit".format(
                    len(raw), MAX_BYTES
                )
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.error("not valid UTF-8: {}".format(exc))
            return
        if "NaN" in text or "Infinity" in text:
            self.warn(
                "the text contains 'NaN' or 'Infinity'; only their use as "
                "JSON *numbers* is forbidden, so this is fine inside "
                "strings, but double check"
            )
        try:
            self.document = json.loads(
                text, object_pairs_hook=self._no_duplicate_keys
            )
        except ValueError as exc:
            self.error("not valid JSON: {}".format(exc))
            return
        if not isinstance(self.document, dict):
            self.error("the top level value is not an object")

    def _no_duplicate_keys(self, pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                self.error("duplicate JSON key: {!r}".format(key))
            seen.add(key)
        return dict(pairs)

    # -- RFC6901 ------------------------------------------------------
    def resolve(self, pointer):
        """Resolve an RFC6901 JSON pointer against the document."""
        if pointer == "":
            return True, None
        if not pointer.startswith("/"):
            return False, "pointer must start with '/'"
        current = self.document
        for raw_token in pointer.split("/")[1:]:
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                try:
                    index = int(token)
                except ValueError:
                    return False, "non-numeric index into a list"
                if index < 0 or index >= len(current):
                    return False, "list index out of range"
                current = current[index]
            elif isinstance(current, dict):
                if token not in current:
                    return False, "key not found: {!r}".format(token)
                current = current[token]
            else:
                return False, "cannot descend into a scalar"
        return True, current

    # -- evidence -----------------------------------------------------
    def check_reference(self, label, pointer, excerpt, index):
        where = "evidence_units[{}] {}".format(index, label)
        if not isinstance(pointer, str) or not pointer:
            self.error("{}: missing pointer".format(where))
            return None
        if not isinstance(excerpt, str) or not excerpt.strip():
            self.error("{}: missing excerpt".format(where))
            return None
        if len(excerpt) > MAX_EXCERPT:
            self.error(
                "{}: excerpt is {} chars, over the {} char limit".format(
                    where, len(excerpt), MAX_EXCERPT
                )
            )
        ok, target = self.resolve(pointer)
        if not ok:
            self.error("{}: pointer {} does not resolve ({})".format(
                where, pointer, target))
            return None
        if not isinstance(target, str):
            self.error(
                "{}: pointer {} resolves to {}, not a string".format(
                    where, pointer, type(target).__name__
                )
            )
            return None
        if excerpt not in target:
            self.error(
                "{}: excerpt not found verbatim at {} (excerpt starts "
                "{!r})".format(where, pointer, excerpt[:60])
            )
            return None
        return excerpt

    def check_units(self):
        units = self.document.get("evidence_units")
        if not isinstance(units, list) or not units:
            self.error("missing or empty evidence_units")
            return
        seen_excerpts = {}
        totals = {name: 0.0 for name in DIMENSIONS}
        tiers_by_dim = {name: [] for name in DIMENSIONS}

        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                self.error("evidence_units[{}] is not an object".format(index))
                continue
            dimension = unit.get("dimension")
            if dimension not in DIMENSIONS:
                self.error(
                    "evidence_units[{}]: unknown dimension {!r}".format(
                        index, dimension
                    )
                )
                continue
            tier = unit.get("score_tier")
            if tier not in TIERS:
                self.error(
                    "evidence_units[{}]: score_tier {!r} is not one of "
                    "{}".format(index, tier, TIERS)
                )
                continue

            student = self.check_reference(
                "student_evidence",
                unit.get("student_pointer"),
                unit.get("student_evidence"),
                index,
            )
            empty_result = unit.get("result_evidence") in (None, "")
            if tier == 0.5:
                if not empty_result:
                    self.error(
                        "evidence_units[{}]: the 0.5 tier must leave "
                        "result_evidence empty".format(index)
                    )
            else:
                if empty_result:
                    self.error(
                        "evidence_units[{}]: tier {} requires result "
                        "evidence".format(index, tier)
                    )
                else:
                    self.check_reference(
                        "result_evidence",
                        unit.get("result_pointer"),
                        unit.get("result_evidence"),
                        index,
                    )
                    if student is not None and (
                        unit.get("result_evidence") == student
                    ):
                        self.error(
                            "evidence_units[{}]: student and result "
                            "evidence are the same excerpt".format(index)
                        )

            for key in ("student_evidence", "result_evidence"):
                text = unit.get(key)
                if isinstance(text, str) and text.strip():
                    if text in seen_excerpts:
                        self.error(
                            "evidence_units[{}]: {} excerpt already used by "
                            "{} (duplicate evidence)".format(
                                index, key, seen_excerpts[text]
                            )
                        )
                    else:
                        seen_excerpts[text] = "evidence_units[{}]".format(
                            index
                        )

            totals[dimension] += tier
            tiers_by_dim[dimension].append(tier)

        declared = self.document.get("dimension_scores")
        if isinstance(declared, dict):
            for dimension, value in declared.items():
                if dimension not in DIMENSIONS:
                    self.error(
                        "dimension_scores: unknown dimension {!r}".format(
                            dimension
                        )
                    )
                    continue
                if abs(float(value) - totals[dimension]) > 1e-9:
                    self.error(
                        "dimension_scores[{!r}] = {} but the instances sum "
                        "to {}".format(dimension, value, totals[dimension])
                    )
        else:
            self.warn("no dimension_scores block; skipping the sum check")

        missing = [d for d in DIMENSIONS if not tiers_by_dim[d]]
        if missing:
            self.warn(
                "no evidence for these dimensions: {}".format(
                    ", ".join(missing)
                )
            )

        declared_total = self.document.get("agent_prompt_score")
        computed = sum(totals.values())
        if declared_total is not None and abs(
            float(declared_total) - computed
        ) > 1e-9:
            self.error(
                "agent_prompt_score = {} but the dimensions sum to "
                "{}".format(declared_total, computed)
            )

        self.totals = totals

    def run(self):
        self.load()
        if self.document is not None:
            self.check_units()
        return self

    def report(self):
        print("file: {}".format(self.path))
        for message in self.warnings:
            print("  WARN  {}".format(message))
        for message in self.errors:
            print("  ERROR {}".format(message))
        if hasattr(self, "totals"):
            print("  dimension scores:")
            for dimension in DIMENSIONS:
                print("    {:<12} {}".format(dimension, self.totals[dimension]))
            print("    {:<12} {}".format("TOTAL", sum(self.totals.values())))
        if self.errors:
            print("RESULT: FAIL ({} error(s))".format(len(self.errors)))
            return 1
        print("RESULT: PASS")
        return 0


def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(
        ROOT, "agent", "conversation.json"
    )
    return Validator(path).run().report()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
