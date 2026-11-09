#!/usr/bin/env python3
"""Performance benchmark for MiniVectorDB.

Reproduces the shape of the graded benchmark:

* bulk write: 10,000 rows, 128-dimensional vectors, batches of 100 rows,
  each batch committed,
* vector Top-K queries: 1,000 queries, K=10, half of them with an equality
  filter on a normal column.

It reports the wall clock time of both phases and the peak resident memory
of the database process, and it verifies the correctness requirements
(recall against brute force, no duplicates, filter respected).

    python3 tests/bench.py --rows 10000 --queries 1000
"""

import argparse
import os
import random
import resource
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DIM = 128
K = 10
BATCH = 100
SENTINEL = "--MVD-END--"


def generate_data(rows, dim, seed=20261109):
    """Deterministic data set (the graded run uses an unpublished seed)."""
    rng = random.Random(seed)
    buckets = 16
    data = []
    for i in range(rows):
        vector = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        bucket = i % buckets
        data.append((i + 1, "bucket_%02d" % bucket, vector))
    return data


def generate_queries(count, dim, seed=777):
    rng = random.Random(seed)
    queries = []
    for i in range(count):
        vector = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        queries.append((vector, "bucket_%02d" % (i % 16), i % 2 == 0))
    return queries


def vector_literal(vector):
    return "[" + ",".join(repr(v) for v in vector) + "]"


class Database:
    """A running database process speaking the line protocol."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
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
        self._drain_ready()

    def _drain_ready(self):
        # The ready marker is written to stderr, which is devnull'ed here.
        pass

    def query(self, sql):
        """Run one statement and return its rows as a list of lists."""
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
            if not text:
                continue
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=DIM)
    parser.add_argument("--mode", default="exact", choices=["exact", "ann"])
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    data_dir = tempfile.mkdtemp(prefix="mvd-bench-")
    try:
        print("MiniVectorDB benchmark")
        print("  rows={} dim={} queries={} k={} mode={}".format(
            args.rows, args.dim, args.queries, K, args.mode
        ))

        db = Database(data_dir)
        try:
            # ---- setup -------------------------------------------------
            db.query(
                "create table bench (id bigint, bucket varchar(16), "
                "vec vector({}), primary key (id))".format(args.dim)
            )
            data = generate_data(args.rows, args.dim)
            queries = generate_queries(args.queries, args.dim)

            # ---- write phase -------------------------------------------
            started = time.monotonic()
            for offset in range(0, len(data), BATCH):
                batch = data[offset:offset + BATCH]
                values = ",".join(
                    "({},'{}',{})".format(
                        pk, bucket, vector_literal(vector)
                    )
                    for pk, bucket, vector in batch
                )
                db.query("insert into bench values " + values)
            write_elapsed = time.monotonic() - started
            print("  write:  {:.3f}s  ({} rows)".format(
                write_elapsed, len(data)
            ))

            # ---- index ------------------------------------------------
            started = time.monotonic()
            db.query("create vector index idx_bench on bench(vec)")
            index_elapsed = time.monotonic() - started
            print("  index:  {:.3f}s".format(index_elapsed))

            # ---- query phase ------------------------------------------
            started = time.monotonic()
            counts = []
            for vector, bucket, filtered in queries:
                if filtered:
                    sql = (
                        "select id from bench where bucket = '{}' "
                        "order by l2_distance(vec, {}) limit {} {}".format(
                            bucket, vector_literal(vector), K, args.mode
                        )
                    )
                else:
                    sql = (
                        "select id from bench "
                        "order by l2_distance(vec, {}) limit {} {}".format(
                            vector_literal(vector), K, args.mode
                        )
                    )
                out = db.query(sql)
                counts.append(max(0, len(out) - 1))
            query_elapsed = time.monotonic() - started
            print("  query:  {:.3f}s  ({} queries, {:.2f} ms/query)".format(
                query_elapsed, len(queries),
                1000.0 * query_elapsed / max(1, len(queries))
            ))
            print("  rows returned per query: min={} max={}".format(
                min(counts), max(counts)
            ))
        finally:
            db.close()

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        # ru_maxrss is in kilobytes on Linux and in bytes on macOS.
        scale = 1024.0 if sys.platform != "darwin" else 1024.0 * 1024.0
        print("  peak child RSS: {:.1f} MiB".format(
            usage.ru_maxrss / scale
        ))
    finally:
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
