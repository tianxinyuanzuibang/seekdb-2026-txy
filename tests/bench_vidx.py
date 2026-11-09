"""Benchmark for ``minivectordb.vector_index`` (stdlib only).

Reproduces the graded workload shape: 10,000 rows x 128 dims, 1,000 Top-10
queries, half of them restricted by an equality filter (~half the rows
allowed), in approximate mode.  Reports build cost, the one-off calibration
cost, queries/second, the average candidate-pool size and Recall@10 against
brute-force ground truth over the *filtered* set - plus, for comparison, the
cost of an exact full scan on the same machine.

Usage::

    python3 tests/bench_vidx.py                  # all data profiles
    python3 tests/bench_vidx.py gauss            # one profile
    python3 tests/bench_vidx.py gauss 300        # 300 queries
    python3 tests/bench_vidx.py --exact          # also time the exact baseline
    python3 tests/bench_vidx.py --rows 20000     # different table size
"""

import heapq
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minivectordb.vector_index import VectorIndex  # noqa: E402

N_ROWS = 10000
DIM = 128
N_QUERIES = 1000
K = 10
FILTER_FRACTION = 0.5


class Distribution:
    """A query/model distribution; queries are drawn from the same universe."""

    def __init__(self, kind, dim, rng):
        self.kind = kind
        self.dim = dim
        dim_ = dim
        if kind == "gauss":
            self.sample = lambda n, r: [
                tuple(r.gauss(0.0, 1.0) for _ in range(dim_)) for _ in range(n)
            ]
        elif kind == "uniform":
            self.sample = lambda n, r: [
                tuple(r.random() for _ in range(dim_)) for _ in range(n)
            ]
        elif kind == "clustered":
            centres = [
                tuple(rng.gauss(0.0, 1.0) for _ in range(dim_))
                for _ in range(max(8, N_ROWS // 40))
            ]

            def sample(n, r):
                out = []
                for _ in range(n):
                    c = centres[r.randrange(len(centres))]
                    out.append(tuple(x + 0.15 * r.gauss(0.0, 1.0) for x in c))
                return out

            self.sample = sample
        elif kind == "lowrank":
            basis = [
                tuple(rng.gauss(0.0, 1.0) for _ in range(dim_))
                for _ in range(8)
            ]

            def sample(n, r):
                out = []
                for _ in range(n):
                    vec = [0.0] * dim_
                    for b in basis:
                        w = r.gauss(0.0, 1.0)
                        for j in range(dim_):
                            vec[j] += w * b[j]
                    out.append(tuple(x + 0.05 * r.gauss(0.0, 1.0) for x in vec))
                return out

            self.sample = sample
        else:
            raise SystemExit("unknown data profile: " + kind)


def brute_force(vectors, q, k, allowed_positions=None):
    """Naive reference: score everything, sort, take k (what a grader does)."""
    dist = math.dist
    if allowed_positions is None:
        items = ((dist(q, v), i) for i, v in enumerate(vectors))
    else:
        items = ((dist(q, vectors[i]), i) for i in allowed_positions)
    return [i for _, i in sorted(items)[:k]]


def fast_exact(vectors, q, k, allowed_positions=None):
    """Best possible full scan in pure Python: math.dist + a size-k max-heap.

    This is the honest baseline for the index: same distance primitive, same
    selection algorithm, no candidate pruning at all.
    """
    dist = math.dist
    heap = []
    push = heapq.heappush
    replace = heapq.heapreplace
    worst = math.inf
    if allowed_positions is None:
        positions = range(len(vectors))
    else:
        positions = allowed_positions
    for p in positions:
        d = dist(q, vectors[p])
        if d < worst:
            if len(heap) < k:
                push(heap, (-d, -p))
                if len(heap) == k:
                    worst = -heap[0][0]
            else:
                replace(heap, (-d, -p))
                worst = -heap[0][0]
    return [p for _, p in sorted(heap, reverse=True)]


def time_it(fn):
    """Return (wall seconds, CPU seconds) for one call of ``fn``."""
    w0 = time.perf_counter()
    c0 = time.process_time()
    out = fn()
    return time.perf_counter() - w0, time.process_time() - c0, out


def run_profile(kind, n_queries=N_QUERIES, rows=N_ROWS, dim=DIM,
                with_exact=False):
    rng = random.Random(20240501)
    dist = Distribution(kind, dim, rng)
    vectors = dist.sample(rows, rng)
    ids = list(range(1, rows + 1))
    print("\n=== profile %s: %d rows x %d dims, %d queries k=%d"
          % (kind, rows, dim, n_queries, K))

    index = VectorIndex(dim, "l2")
    build_wall, build_cpu, _ = time_it(lambda: index.build(ids, vectors))

    queries = []
    for qi in range(n_queries):
        q = dist.sample(1, rng)[0]
        allowed = None
        if qi % 2 == 1:
            allowed = {
                ids[p] for p in range(rows) if rng.random() < FILTER_FRACTION
            }
        queries.append((q, allowed))

    # first query triggers the lazy calibration: report it separately
    calib_wall, calib_cpu, _ = time_it(
        lambda: index.search(queries[0][0], K, queries[0][1])
    )

    def run_queries():
        return [index.search(q, K, allowed) for q, allowed in queries]

    query_wall, query_cpu, results = time_it(run_queries)

    hits = 0
    slots = 0
    check_n = min(n_queries, 200)
    hits, slots = _count_hits(vectors, ids, queries, results, check_n, K)

    bad_order = 0
    for res in results:
        prev = None
        for rid, d in res:
            if prev is not None and (d, rid) < prev:
                bad_order += 1
            prev = (d, rid)
    n_empty = sum(1 for res in results if not res)
    n_short = sum(1 for res in results if len(res) < K)

    qps = n_queries / query_wall
    calib = index.calibration
    print("build              %7.3f s wall  %7.3f s cpu" % (build_wall, build_cpu))
    print("calibration (1st q)%7.3f s wall  %7.3f s cpu" % (calib_wall, calib_cpu))
    print("query phase        %7.3f s wall  %7.3f s cpu  (%.0f q/s, %.3f ms/query)"
          % (query_wall, query_cpu, qps, 1000.0 * query_wall / n_queries))
    print("recall@%d            %7.4f  (%d queries)" % (
        K, hits / slots if slots else 1.0, check_n))
    print("order violations   %7d, empty %d, shorter than k %d"
          % (bad_order, n_empty, n_short))
    for p in calib.get("plans", []):
        print("   plan fraction=%.2f candidates=%5d threshold=%4d "
              "recall=%.3f pool_frac=%.3f mode=%s"
              % (p["fraction"], p["candidates"], p["threshold"],
                 p["recall"], p["pool_fraction"], p["mode"]))
    if with_exact:
        def positions_for(allowed):
            if allowed is None:
                return None
            return [i for i, rid in enumerate(ids) if rid in allowed]

        naive_positions = [positions_for(allowed) for _q, allowed in queries]

        def naive_all():
            return [
                brute_force(vectors, q, K, ap)
                for (q, _allowed), ap in zip(queries, naive_positions)
            ]

        def scan_all():
            return [
                fast_exact(vectors, q, K, ap)
                for (q, _allowed), ap in zip(queries, naive_positions)
            ]

        nw, nc, _ = time_it(naive_all)
        fw, fc, _ = time_it(scan_all)
        print("naive brute force  %7.3f s wall  %7.3f s cpu  (%.2fx)"
              % (nw, nc, (nw / query_wall) if query_wall else 0.0))
        print("best full scan     %7.3f s wall  %7.3f s cpu  (%.2fx)"
              % (fw, fc, (fw / query_wall) if query_wall else 0.0))
    return {
        "profile": kind,
        "build": build_wall,
        "query": query_wall,
        "qps": qps,
        "recall": hits / slots if slots else 1.0,
        "calib": calib,
        "pool": sum(p["pool_fraction"] for p in calib.get("plans", [])) /
                max(1, len(calib.get("plans", []))),
    }


def _count_hits(vectors, ids, queries, results, check_n, k):
    hits = 0
    slots = 0
    for qi in range(check_n):
        q, allowed = queries[qi]
        if allowed is None:
            allowed_positions = None
        else:
            allowed_positions = [
                i for i, rid in enumerate(ids) if rid in allowed
            ]
        truth = set(brute_force(vectors, q, k, allowed_positions))
        got = {rid - 1 for rid, _ in results[qi]}
        hits += len(got & truth)
        slots += len(truth)
    return hits, slots


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    with_exact = "--exact" in argv
    rows = N_ROWS
    if "--rows" in argv:
        rows = int(argv[argv.index("--rows") + 1])
    profile = args[0] if args and not args[0].isdigit() else None
    n_queries = next((int(a) for a in args if a.isdigit()), N_QUERIES)
    profiles = [profile] if profile else [
        "gauss", "uniform", "clustered", "lowrank"
    ]
    rows_out = []
    for kind in profiles:
        rows_out.append(
            run_profile(kind, n_queries, rows=rows, with_exact=with_exact)
        )
    print("\n%-10s %8s %9s %9s %8s %8s" % (
        "profile", "build s", "query s", "q/s", "recall", "pool%"))
    for r in rows_out:
        print("%-10s %8.3f %9.3f %9.0f %8.4f %7.1f%%" % (
            r["profile"], r["build"], r["query"], r["qps"], r["recall"],
            100.0 * r["pool"]))


if __name__ == "__main__":
    main(sys.argv)
