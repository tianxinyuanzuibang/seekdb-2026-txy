#!/usr/bin/env python3
"""Recall validation for the approximate (ANN) vector search.

The graded benchmark requires the average Recall@10 to be at least 0.95
for both the filtered and the unfiltered query groups, where the ground
truth is the exact brute-force result over the *filtered* set.

This script drives the database through the line protocol, computes the
brute-force ground truth in pure Python, and reports the recall.

    python3 tests/test_recall.py --rows 3000 --queries 200
"""

import argparse
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SENTINEL = "--MVD-END--"


class Database:
    def __init__(self, data_dir):
        env = dict(os.environ)
        env["MVD_DATA_DIR"] = data_dir
        env["MVD_PROTOCOL"] = "line"
        env["MVD_ECHO"] = "0"
        env["MVD_RESULT_SENTINEL"] = SENTINEL
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "minivectordb"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=ROOT,
            env=env,
        )

    def query(self, sql):
        self.proc.stdin.write((sql + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        rows = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("database closed the connection")
            text = line.decode("utf-8").rstrip("\n")
            if text == SENTINEL:
                return rows
            if text.startswith("ERROR"):
                raise RuntimeError(text)
            rows.append(text.split("\t"))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


def vector_literal(vector):
    return "[" + ",".join(repr(v) for v in vector) + "]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=3000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--mode", default="ann")
    args = parser.parse_args()

    rng = random.Random(4242)
    buckets = 16
    rows = []
    for i in range(args.rows):
        vector = [rng.gauss(0.0, 1.0) for _ in range(args.dim)]
        rows.append((i + 1, "bucket_%02d" % (i % buckets), vector))

    queries = []
    for i in range(args.queries):
        vector = [rng.gauss(0.0, 1.0) for _ in range(args.dim)]
        queries.append((vector, "bucket_%02d" % (i % buckets), i % 2 == 0))

    data_dir = tempfile.mkdtemp(prefix="mvd-recall-")
    db = Database(data_dir)
    try:
        db.query(
            "create table t (id bigint, bucket varchar(16), vec vector({}), "
            "primary key (id))".format(args.dim)
        )
        for offset in range(0, len(rows), 100):
            batch = rows[offset:offset + 100]
            values = ",".join(
                "({},'{}',{})".format(pk, bucket, vector_literal(vector))
                for pk, bucket, vector in batch
            )
            db.query("insert into t values " + values)
        db.query("create vector index idx on t(vec)")

        # Ground truth: exact brute force over the filtered set.
        # Each query gets its own entry - keyed by the query index, not by
        # the filter, because many queries share the same filter.
        truth = []
        for vector, bucket, filtered in queries:
            pool = [
                (math.dist(row[2], vector), row[0])
                for row in rows
                if (not filtered) or row[1] == bucket
            ]
            pool.sort()
            truth.append([pk for _distance, pk in pool[: args.k]])

        unfiltered_recall = []
        filtered_recall = []
        problems = 0
        for index, (vector, bucket, filtered) in enumerate(queries):
            if filtered:
                sql = (
                    "select id from t where bucket = '{}' "
                    "order by l2_distance(vec, {}) limit {} {}".format(
                        bucket, vector_literal(vector), args.k, args.mode
                    )
                )
            else:
                sql = (
                    "select id from t order by l2_distance(vec, {}) "
                    "limit {} {}".format(
                        vector_literal(vector), args.k, args.mode
                    )
                )
            out = db.query(sql)
            got = [int(line[0]) for line in out[1:]]
            expected = truth[index]

            # Contract: no duplicates, correct count, filter respected.
            if len(got) != len(set(got)):
                print("DUPLICATE ids in result: {}".format(got))
                problems += 1
            if len(got) != min(args.k, len(expected)):
                print("WRONG count: got {} want {}".format(
                    len(got), min(args.k, len(expected))))
                problems += 1
            if filtered:
                allowed = {r[0] for r in rows if r[1] == bucket}
                for pk in got:
                    if pk not in allowed:
                        print("FILTER VIOLATION: id {}".format(pk))
                        problems += 1

            hits = len(set(got) & set(expected))
            denominator = min(args.k, len(expected))
            recall = hits / denominator if denominator else 0.0
            if filtered:
                filtered_recall.append(recall)
            else:
                unfiltered_recall.append(recall)

        def average(values):
            return sum(values) / len(values) if values else 0.0

        print("mode                : {}".format(args.mode))
        print("rows / queries      : {} / {}".format(args.rows, args.queries))
        print("recall@10 unfiltered: {:.4f}".format(
            average(unfiltered_recall)))
        print("recall@10 filtered  : {:.4f}".format(
            average(filtered_recall)))
        print("contract problems   : {}".format(problems))
        ok = (
            average(unfiltered_recall) >= 0.95
            and average(filtered_recall) >= 0.95
            and problems == 0
        )
        print("RESULT: {}".format("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        db.close()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
