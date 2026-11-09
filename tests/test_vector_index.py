"""Unit tests for ``minivectordb.vector_index`` (stdlib unittest only).

Run with::

    python3 -m unittest tests.test_vector_index -v

The tests keep a brute-force reference implementation in this file and check
the two things the contest grades:

* exact results are compared against brute force (must be identical, including
  the "ties broken by id ascending" rule);
* approximate results are compared against brute force with Recall@10,
  computed over the *filtered* set, exactly like the graded metric::

      recall@k = mean over queries of |ann_top_k & exact_top_k| / k
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minivectordb import vector_index as vi  # noqa: E402
from minivectordb.vector_index import VectorIndex  # noqa: E402
from minivectordb.values import (  # noqa: E402
    cosine_distance,
    inner_product_distance,
    l2_distance,
)

DISTANCE = {
    "l2": l2_distance,
    "cosine": cosine_distance,
    "ip": inner_product_distance,
}


# --- brute-force reference -------------------------------------------------


def brute_force(ids, vectors, query, k, metric="l2", allowed_ids=None):
    """Exact top-k as ``[(id, distance), ...]`` sorted by (distance, id)."""
    dist = DISTANCE[metric]
    if allowed_ids is not None:
        allowed_ids = set(allowed_ids)
    scored = []
    for row_id, vec in zip(ids, vectors):
        if allowed_ids is not None and row_id not in allowed_ids:
            continue
        scored.append((dist(query, vec), row_id))
    scored.sort()
    return [(row_id, d) for d, row_id in scored[:k]]


def make_random_vectors(n, dim, rng, scale=1.0):
    return [tuple(rng.gauss(0.0, scale) for _ in range(dim)) for _ in range(n)]


def make_clustered_vectors(n, dim, rng, n_centres=12, jitter=0.12):
    centres = make_random_vectors(n_centres, dim, rng)
    out = []
    for _ in range(n):
        c = centres[rng.randrange(n_centres)]
        out.append(tuple(x + jitter * rng.gauss(0.0, 1.0) for x in c))
    return out


def ids_ascending(n):
    return list(range(1, n + 1))


class Base(unittest.TestCase):
    """Shared helpers."""

    def measure_recall(self, index, ids, vectors, k=10, metric="l2",
                       filter_fraction=0.0, n_queries=40, seed=5):
        """Independent recall@k implementation (same formula as the grader)."""
        rng = random.Random(seed)
        pairs = list(zip(ids, vectors))
        hits = 0
        slots = 0
        for _ in range(n_queries):
            q = tuple(rng.gauss(0.0, 1.0) for _ in range(index.dim))
            allowed = None
            if filter_fraction > 0.0:
                allowed = {rid for rid, _ in pairs if rng.random() < filter_fraction}
                if len(allowed) < k:
                    continue
            truth = {rid for rid, _ in brute_force(ids, vectors, q, k, metric, allowed)}
            got = {rid for rid, _ in index.search(q, k, allowed)}
            hits += len(truth & got)
            slots += len(truth)
        self.assertGreater(slots, 0)
        return hits / slots

    def assert_matches_brute_force(self, index, ids, vectors, queries, k,
                                   metric="l2", allowed_lists=None):
        """Exact-path check: identical ids, identical order, identical values."""
        for qi, q in enumerate(queries):
            allowed = None if allowed_lists is None else allowed_lists[qi]
            got = index.search(q, k, allowed)
            want = brute_force(ids, vectors, q, k, metric, allowed)
            self.assertEqual(
                [row_id for row_id, _ in got],
                [row_id for row_id, _ in want],
                "id/order mismatch for query %d" % qi,
            )
            for (_, dgot), (_, dwant) in zip(got, want):
                self.assertAlmostEqual(dgot, dwant, places=9)


# --- exact-path correctness ------------------------------------------------


class TestExactPath(Base):
    def setUp(self):
        self.rng = random.Random(1234)
        self.dim = 12
        self.vectors = make_random_vectors(300, self.dim, self.rng)
        self.ids = ids_ascending(300)
        self.index = VectorIndex(self.dim)
        self.index.build(self.ids, self.vectors)
        self.queries = make_random_vectors(10, self.dim, self.rng)

    def test_matches_brute_force(self):
        self.assert_matches_brute_force(
            self.index, self.ids, self.vectors, self.queries, 10
        )

    def test_matches_brute_force_filtered(self):
        rng = random.Random(7)
        allowed_lists = [
            {rid for rid in self.ids if rng.random() < 0.3} for _ in self.queries
        ]
        self.assert_matches_brute_force(
            self.index, self.ids, self.vectors, self.queries, 10,
            allowed_lists=allowed_lists,
        )

    def test_distances_are_contract_distances(self):
        q = self.queries[0]
        for row_id, d in self.index.search(q, 10):
            self.assertAlmostEqual(
                d, l2_distance(q, self.vectors[row_id - 1]), places=9
            )

    def test_filter_is_applied_before_top_k(self):
        # Allowed set excludes the 10 global nearest rows; the answer must be
        # the nearest rows *of the allowed set*, not the filtered leftovers of
        # the global top-k.
        q = self.queries[0]
        nearest = [row_id for row_id, _ in brute_force(
            self.ids, self.vectors, q, 40)]
        allowed = set(self.ids) - set(nearest[:10])
        got = [row_id for row_id, _ in self.index.search(q, 10, allowed)]
        self.assertFalse(set(got) & set(nearest[:10]))
        want = [row_id for row_id, _ in brute_force(
            self.ids, self.vectors, q, 10, allowed_ids=allowed)]
        self.assertEqual(got, want)

    def test_ordering_ties_broken_by_id(self):
        # 6 identical vectors -> identical distances -> id order decides.
        vec = tuple(1.0 for _ in range(self.dim))
        ids = [50, 10, 40, 30, 20]
        vectors = [vec] * len(ids)
        index = VectorIndex(self.dim)
        index.build(ids, vectors)
        # 5 rows only, so the quantizer is not trained -> exact path
        got = index.search(vec, 5)
        self.assertEqual([row_id for row_id, _ in got], [10, 20, 30, 40, 50])
        for row_id, d in got:
            self.assertEqual(d, 0.0)

    def test_ordering_ties_broken_by_id_large(self):
        # Same, but large enough to use the quantized path: 6 rows share the
        # vector of row 0, so they all tie at distance 0 and the id decides.
        rng = random.Random(3)
        base = make_random_vectors(400, 8, rng)
        vecs = base + [base[0]] * 6
        ids = list(range(1000, 1000 + len(vecs)))
        index = VectorIndex(8)
        index.build(ids, vecs)
        got = index.search(base[0], 5)
        # ids 1000 (original) and 1400..1405 are all at distance 0
        self.assertEqual([row_id for row_id, _ in got],
                         [1000, 1400, 1401, 1402, 1403])
        # all distances are (essentially) equal: must be increasing
        for a, b in zip(got, got[1:]):
            self.assertLessEqual((a[1], a[0]), (b[1], b[0]))


# --- result cardinality ----------------------------------------------------


class TestResultCount(Base):
    def setUp(self):
        self.rng = random.Random(11)
        self.dim = 8
        self.vectors = make_random_vectors(60, self.dim, self.rng)
        self.ids = ids_ascending(60)
        self.index = VectorIndex(self.dim)
        self.index.build(self.ids, self.vectors)
        self.q = self.vectors[3]

    def test_exactly_min_k_n(self):
        for k in (1, 2, 5, 9, 59, 60, 61, 1000):
            got = self.index.search(self.q, k)
            self.assertEqual(len(got), min(k, 60), "k=%d" % k)

    def test_exactly_min_k_allowed(self):
        allowed = set(self.ids[:4])
        for k in (1, 4, 5, 100):
            got = self.index.search(self.q, k, allowed)
            self.assertEqual(len(got), min(k, 4), "k=%d" % k)
            self.assertTrue({rid for rid, _ in got} <= allowed)

    def test_allowed_ids_accepts_a_list_with_duplicates(self):
        got = self.index.search(self.q, 5, [1, 1, 2, 2, 3])
        self.assertEqual(len(got), 3)
        self.assertEqual(len({rid for rid, _ in got}), 3)
        self.assertTrue({rid for rid, _ in got} <= {1, 2, 3})

    def test_allowed_ids_accepts_a_generator(self):
        got = self.index.search(self.q, 5, (r for r in (4, 5, 6)))
        self.assertEqual(len(got), 3)
        self.assertTrue({rid for rid, _ in got} <= {4, 5, 6})

    def test_nonpositive_k(self):
        self.assertEqual(self.index.search(self.q, 0), [])
        self.assertEqual(self.index.search(self.q, -3), [])
        self.assertEqual(self.index.search(self.q, 5, set()), [])

    def test_empty_index(self):
        index = VectorIndex(4)
        self.assertEqual(index.search((1.0, 2.0, 3.0, 4.0), 5), [])
        self.assertEqual(len(index), 0)
        index.build([], [])
        self.assertEqual(index.search((1.0, 2.0, 3.0, 4.0), 5), [])


# --- approximate mode recall ----------------------------------------------


class TestRecall(Base):
    """The graded property: Recall@10 >= 0.95 against filtered ground truth."""

    def build_index(self, vectors, dim, metric="l2"):
        ids = ids_ascending(len(vectors))
        index = VectorIndex(dim, metric)
        index.build(ids, vectors)
        return index, ids

    def test_recall_random_data(self):
        rng = random.Random(2024)
        dim = 32
        vectors = make_random_vectors(2000, dim, rng)
        index, ids = self.build_index(vectors, dim)
        recall = self.measure_recall(index, ids, vectors, k=10, n_queries=40)
        self.assertGreaterEqual(recall, 0.95, "recall=%r calib=%r"
                                % (recall, index.calibration))

    def test_recall_filtered_random_data(self):
        rng = random.Random(2025)
        dim = 32
        vectors = make_random_vectors(2000, dim, rng)
        index, ids = self.build_index(vectors, dim)
        recall = self.measure_recall(index, ids, vectors, k=10,
                                     filter_fraction=0.5, n_queries=40)
        self.assertGreaterEqual(recall, 0.95, "recall=%r calib=%r"
                                % (recall, index.calibration))

    def test_recall_clustered_data(self):
        rng = random.Random(2026)
        dim = 32
        vectors = make_clustered_vectors(2000, dim, rng)
        index, ids = self.build_index(vectors, dim)
        recall = self.measure_recall(index, ids, vectors, k=10, n_queries=40)
        self.assertGreaterEqual(recall, 0.95, "recall=%r calib=%r"
                                % (recall, index.calibration))

    def test_recall_in_ann_mode_with_large_filter(self):
        """A filter big enough to take the ANN path must still hit 0.95."""
        rng = random.Random(2027)
        dim = 48
        n = 4000
        vectors = make_random_vectors(n, dim, rng)
        index, ids = self.build_index(vectors, dim)
        calib = index.calibrate()  # calibration normally happens lazily
        self.assertEqual(calib.get("mode"), "ann",
                         "expected the approximate path to be used: %r" % calib)
        allowed = set(ids[:3200])  # > EXACT_FILTER_LIMIT -> approximates
        self.assertGreater(len(allowed), vi.EXACT_FILTER_LIMIT)
        hits = 0
        slots = 0
        for q in make_random_vectors(25, dim, rng):
            truth = {rid for rid, _ in brute_force(ids, vectors, q, 10,
                                                   allowed_ids=allowed)}
            got = {rid for rid, _ in index.search(q, 10, allowed)}
            self.assertTrue(got <= allowed)
            hits += len(got & truth)
            slots += len(truth)
        self.assertGreaterEqual(hits / slots, 0.95)

    def test_ann_path_actually_used(self):
        """Sanity: with enough data the index really screens, not scans."""
        rng = random.Random(31)
        dim = 64
        n = 4000
        vectors = make_random_vectors(n, dim, rng)
        index, ids = self.build_index(vectors, dim)
        calib = index.calibrate()  # calibration normally happens lazily
        self.assertEqual(calib["mode"], "ann", calib)
        self.assertGreater(calib["threshold"], 0)
        self.assertLess(calib["avg_pool"], n * vi.CALIB_MAX_POOL_FRACTION)

    def test_recall_k_larger_than_10(self):
        rng = random.Random(77)
        dim = 32
        vectors = make_random_vectors(2000, dim, rng)
        index, ids = self.build_index(vectors, dim)
        recall = self.measure_recall(index, ids, vectors, k=20, n_queries=20,
                                     seed=8)
        self.assertGreaterEqual(recall, 0.90)


# --- delete / upsert semantics --------------------------------------------


class TestDelete(Base):
    def setUp(self):
        self.rng = random.Random(5)
        self.dim = 16
        self.vectors = make_random_vectors(500, self.dim, self.rng)
        self.ids = ids_ascending(500)
        self.index = VectorIndex(self.dim)
        self.index.build(self.ids, self.vectors)
        self.q = self.vectors[0]

    def test_deleted_ids_are_never_returned(self):
        nearest = [row_id for row_id, _ in self.index.search(self.q, 20)]
        self.index.delete(nearest[:10])
        got = [row_id for row_id, _ in self.index.search(self.q, 20)]
        self.assertFalse(set(got) & set(nearest[:10]))
        self.assertEqual(len(got), 20)

    def test_deleted_ids_excluded_from_filtered_result(self):
        allowed = set(self.ids[:100])
        self.index.delete(list(allowed)[:50])
        got = self.index.search(self.q, 10, allowed)
        self.assertTrue({rid for rid, _ in got} <= allowed)
        self.assertFalse({rid for rid, _ in got} & set(list(allowed)[:50]))

    def test_delete_keeps_index_exact(self):
        self.index.delete(self.ids[::3])
        remaining_ids = [rid for rid in self.ids if rid not in set(self.ids[::3])]
        remaining_vecs = [v for rid, v in zip(self.ids, self.vectors)
                          if rid not in set(self.ids[::3])]
        self.assertEqual(len(self.index), len(remaining_ids))
        for q in make_random_vectors(5, self.dim, self.rng):
            got = self.index.search(q, 10)
            want = brute_force(remaining_ids, remaining_vecs, q, 10)
            self.assertEqual([r for r, _ in got], [r for r, _ in want])

    def test_delete_everything(self):
        self.index.delete(self.ids)
        self.assertEqual(len(self.index), 0)
        self.assertEqual(self.index.search(self.q, 5), [])

    def test_delete_unknown_is_ignored(self):
        self.index.delete([10 ** 9, -5])
        self.assertEqual(len(self.index), 500)
        self.assertEqual(len(self.index.search(self.q, 5)), 5)

    def test_double_delete_is_harmless(self):
        self.index.delete([1, 2, 3])
        self.index.delete([1, 2, 3])
        self.assertEqual(len(self.index), 497)
        self.assertNotIn(1, [rid for rid, _ in self.index.search(self.q, 497)])

    def test_delete_triggers_compaction_and_stays_correct(self):
        # delete > 3/4 of the rows in small steps: compaction must kick in
        for chunk in (list(range(1, 200)), list(range(200, 400))):
            self.index.delete(chunk)
        self.assertEqual(len(self.index), 101)
        remaining = list(range(400, 501))
        remaining_vecs = self.vectors[399:]
        q = self.vectors[450]
        got = self.index.search(q, 10)
        want = brute_force(remaining, remaining_vecs, q, 10)
        self.assertEqual([r for r, _ in got], [r for r, _ in want])


class TestUpsert(Base):
    def setUp(self):
        self.rng = random.Random(6)
        self.dim = 16
        self.vectors = make_random_vectors(400, self.dim, self.rng)
        self.ids = ids_ascending(400)
        self.index = VectorIndex(self.dim)
        self.index.build(self.ids, self.vectors)

    def test_upsert_new_ids_are_searchable(self):
        extra = make_random_vectors(5, self.dim, self.rng)
        self.index.upsert([9001, 9002, 9003, 9004, 9005], extra)
        self.assertEqual(len(self.index), 405)
        got = self.index.search(extra[3], 1)
        self.assertEqual(got[0][0], 9004)
        self.assertAlmostEqual(got[0][1], 0.0, places=9)

    def test_upsert_replaces_vector_of_existing_id(self):
        q = self.vectors[0]
        self.assertEqual(self.index.search(q, 1)[0][0], 1)
        replacement = tuple(2.5 + x for x in self.vectors[0])
        self.index.upsert([1], [replacement])
        self.assertNotEqual(self.index.search(q, 1)[0][0], 1)
        got = self.index.search(replacement, 1)
        self.assertEqual(got[0][0], 1)
        self.assertAlmostEqual(got[0][1], 0.0, places=9)
        self.assertEqual(len(self.index), 400)

    def test_upsert_keeps_results_exact(self):
        extra_ids = list(range(5000, 5050))
        extra = make_random_vectors(50, self.dim, self.rng)
        self.index.upsert(extra_ids, extra)
        all_ids = self.ids + extra_ids
        all_vecs = self.vectors + extra
        for q in make_random_vectors(5, self.dim, self.rng):
            got = self.index.search(q, 10)
            want = brute_force(all_ids, all_vecs, q, 10)
            self.assertEqual([r for r, _ in got], [r for r, _ in want])

    def test_upsert_idempotent(self):
        self.index.upsert([1, 2], [self.vectors[0], self.vectors[1]])
        self.index.upsert([1, 2], [self.vectors[0], self.vectors[1]])
        self.assertEqual(len(self.index), 400)
        got = self.index.search(self.vectors[5], 10)
        want = brute_force(self.ids, self.vectors, self.vectors[5], 10)
        self.assertEqual([r for r, _ in got], [r for r, _ in want])

    def test_incremental_build_matches_bulk_build(self):
        """Rows inserted in small batches behave like a single build()."""
        rng = random.Random(9)
        vectors = make_random_vectors(1200, 24, rng)
        ids = ids_ascending(1200)
        bulk = VectorIndex(24)
        bulk.build(ids, vectors)
        incremental = VectorIndex(24)
        for start in range(0, 1200, 100):
            incremental.upsert(ids[start:start + 100], vectors[start:start + 100])
        self.assertEqual(len(incremental), 1200)
        hits = 0
        for q in make_random_vectors(20, 24, rng):
            want = [r for r, _ in brute_force(ids, vectors, q, 10)]
            got = [r for r, _ in incremental.search(q, 10)]
            want_bulk = [r for r, _ in bulk.search(q, 10)]
            self.assertGreaterEqual(len(set(got) & set(want)), 9)
            self.assertEqual(len(want_bulk), len(got))
            hits += len(set(got) & set(want))
        self.assertGreaterEqual(hits / 200.0, 0.95)


# --- contract invariants ---------------------------------------------------


class TestInvariants(Base):
    def test_unknown_ids_never_returned(self):
        rng = random.Random(12)
        vectors = make_random_vectors(200, 8, rng)
        ids = ids_ascending(200)
        index = VectorIndex(8)
        index.build(ids, vectors)
        got = index.search(vectors[0], 5, allowed_ids={7777, 8888})
        self.assertEqual(got, [])
        got = index.search(vectors[0], 50, allowed_ids={7777, 1, 2})
        self.assertEqual({rid for rid, _ in got}, {1, 2})

    def test_allowed_ids_respected_large_set(self):
        rng = random.Random(13)
        dim = 24
        vectors = make_random_vectors(3000, dim, rng)
        ids = ids_ascending(3000)
        index = VectorIndex(dim)
        index.build(ids, vectors)
        allowed = set(rng.sample(ids, 2800))
        for q in make_random_vectors(5, dim, rng):
            got = index.search(q, 10, allowed)
            self.assertTrue({rid for rid, _ in got} <= allowed)

    def test_sorted_ascending_with_ties_by_id(self):
        rng = random.Random(14)
        dim = 16
        base = make_random_vectors(600, dim, rng)
        # duplicate a few vectors to force exact ties on the distance
        vectors = base + [base[0], base[0], base[1], base[2]]
        ids = [i * 7 + 3 for i in range(len(vectors))]
        index = VectorIndex(dim)
        index.build(ids, vectors)
        for q in make_random_vectors(10, dim, rng):
            got = index.search(q, 20)
            keys = [(d, rid) for rid, d in got]
            self.assertEqual(keys, sorted(keys))
            for a, b in zip(got, got[1:]):
                self.assertLessEqual((a[1], a[0]), (b[1], b[0]))

    def test_dimension_mismatch_raises(self):
        index = VectorIndex(4)
        with self.assertRaises(Exception):
            index.build([1], [(1.0, 2.0, 3.0)])
        with self.assertRaises(Exception):
            index.search((1.0, 2.0), 1)
        index.build([1], [(1.0, 2.0, 3.0, 4.0)])
        with self.assertRaises(Exception):
            index.upsert([2], [(1.0,)])
        with self.assertRaises(Exception):
            index.upsert([2], [])
        with self.assertRaises(Exception):
            index.upsert([1, 2], [(1.0, 1.0, 1.0, 1.0)])

    def test_metric_and_dim_properties(self):
        self.assertEqual(VectorIndex(8).metric, "l2")
        self.assertEqual(VectorIndex(8, "cosine").metric, "cosine")
        self.assertEqual(VectorIndex(8, "L2").metric, "l2")
        self.assertEqual(VectorIndex(8, "inner_product").metric, "ip")
        self.assertEqual(VectorIndex(8).dim, 8)
        with self.assertRaises(Exception):
            VectorIndex(8, "hamming")

    def test_clear_and_remove_all(self):
        rng = random.Random(15)
        vectors = make_random_vectors(300, 8, rng)
        index = VectorIndex(8)
        index.build(ids_ascending(300), vectors)
        index.clear()
        self.assertEqual(len(index), 0)
        self.assertEqual(index.search(vectors[0], 5), [])
        index.build(ids_ascending(300), vectors)
        self.assertEqual(len(index), 300)
        index.remove_all()
        self.assertEqual(len(index), 0)

    def test_rebuild_from_records(self):
        rng = random.Random(16)
        vectors = make_random_vectors(120, 6, rng)
        ids = ids_ascending(120)
        index = VectorIndex(6)
        index.rebuild_from(zip(ids, vectors))
        self.assertEqual(len(index), 120)
        got = index.search(vectors[7], 3)
        want = brute_force(ids, vectors, vectors[7], 3)
        self.assertEqual([r for r, _ in got], [r for r, _ in want])

    def test_does_not_mutate_input(self):
        rng = random.Random(17)
        vectors = make_random_vectors(100, 5, rng)
        ids = ids_ascending(100)
        snapshot = [tuple(v) for v in vectors]
        index = VectorIndex(5)
        index.build(ids, vectors)
        index.search(vectors[0], 5)
        index.upsert([501], [vectors[0]])
        index.delete([1])
        self.assertEqual([tuple(v) for v in vectors], snapshot)
        self.assertEqual(ids, ids_ascending(100))

    def test_search_is_pure(self):
        rng = random.Random(18)
        vectors = make_random_vectors(300, 10, rng)
        index = VectorIndex(10)
        index.build(ids_ascending(300), vectors)
        first = index.search(vectors[3], 10)
        for _ in range(3):
            self.assertEqual(index.search(vectors[3], 10), first)
        self.assertEqual(len(index), 300)


# --- cosine metric --------------------------------------------------------


class TestCosine(Base):
    def test_cosine_distances_and_order(self):
        rng = random.Random(21)
        dim = 16
        vectors = make_random_vectors(400, dim, rng)
        ids = ids_ascending(400)
        index = VectorIndex(dim, "cosine")
        index.build(ids, vectors)
        self.assertEqual(index.metric, "cosine")
        for q in make_random_vectors(5, dim, rng):
            got = index.search(q, 10)
            want = brute_force(ids, vectors, q, 10, metric="cosine")
            self.assertEqual([r for r, _ in got], [r for r, _ in want])
            for (_, dgot), (_, dwant) in zip(got, want):
                self.assertAlmostEqual(dgot, dwant, places=6)

    def test_cosine_zero_vector(self):
        dim = 4
        ids = [1, 2]
        vectors = [(0.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)]
        index = VectorIndex(dim, "cosine")
        index.build(ids, vectors)
        got = index.search((1.0, 0.0, 0.0, 0.0), 2)
        self.assertEqual([r for r, _ in got], [2, 1])
        self.assertAlmostEqual(got[1][1], 1.0, places=9)

    def test_cosine_recall(self):
        rng = random.Random(22)
        dim = 32
        vectors = make_random_vectors(2000, dim, rng)
        ids = ids_ascending(2000)
        index = VectorIndex(dim, "cosine")
        index.build(ids, vectors)
        recall = self.measure_recall(index, ids, vectors, k=10, metric="cosine",
                                     n_queries=30)
        self.assertGreaterEqual(recall, 0.95,
                                "recall=%r calib=%r" % (recall, index.calibration))


class TestInnerProduct(Base):
    def test_ip_is_exact(self):
        rng = random.Random(51)
        dim = 12
        vectors = make_random_vectors(400, dim, rng)
        ids = ids_ascending(400)
        index = VectorIndex(dim, "ip")
        index.build(ids, vectors)
        self.assertEqual(index.metric, "ip")
        for q in make_random_vectors(5, dim, rng):
            got = index.search(q, 10)
            want = brute_force(ids, vectors, q, 10, metric="ip")
            self.assertEqual([r for r, _ in got], [r for r, _ in want])
            for (_, dgot), (_, dwant) in zip(got, want):
                self.assertAlmostEqual(dgot, dwant, places=9)

    def test_ip_filtered(self):
        rng = random.Random(52)
        dim = 8
        vectors = make_random_vectors(300, dim, rng)
        ids = ids_ascending(300)
        index = VectorIndex(dim, "ip")
        index.build(ids, vectors)
        allowed = set(ids[100:200])
        q = make_random_vectors(1, dim, rng)[0]
        got = index.search(q, 5, allowed)
        want = brute_force(ids, vectors, q, 5, metric="ip", allowed_ids=allowed)
        self.assertEqual([r for r, _ in got], [r for r, _ in want])


# --- lifecycle / robustness ------------------------------------------------


class TestLifecycle(Base):
    def test_small_mutations_do_not_recalibrate_every_query(self):
        """Calibration is expensive: a few inserts/deletes must not redo it."""
        rng = random.Random(41)
        vectors = make_random_vectors(800, 16, rng)
        ids = ids_ascending(800)
        index = VectorIndex(16)
        index.build(ids, vectors)
        index.search(vectors[0], 10)          # triggers the lazy calibration
        self.assertEqual(index._calib_runs, 1)
        self.assertTrue(index.calibration["plans"])
        for step in range(5):
            index.upsert([9000 + step], [make_random_vectors(1, 16, rng)[0]])
            index.search(vectors[step], 10)
        self.assertEqual(index._calib_runs, 1)
        index.delete([1, 2, 3])
        index.search(vectors[5], 10)
        self.assertEqual(index._calib_runs, 1)

    def test_after_mutation_the_approximate_path_is_still_used(self):
        rng = random.Random(42)
        vectors = make_random_vectors(3000, 32, rng)
        ids = ids_ascending(3000)
        index = VectorIndex(32)
        index.build(ids, vectors)
        index.search(vectors[0], 10)
        self.assertTrue(index.calibration["plans"])
        index.upsert([99001], [vectors[7]])
        got = index.search(vectors[7], 3)
        # id 8 (the original) and 99001 now hold the same vector: both are at
        # distance 0 and the tie is broken by id.
        self.assertEqual([row_id for row_id, _ in got], [8, 99001, got[2][0]])
        self.assertAlmostEqual(got[0][1], 0.0, places=9)
        self.assertAlmostEqual(got[1][1], 0.0, places=9)

    def test_index_survives_repeated_rebuilds(self):
        rng = random.Random(43)
        vectors = make_random_vectors(600, 12, rng)
        ids = ids_ascending(600)
        index = VectorIndex(12)
        for _ in range(8):
            index.build(ids, vectors)
            index.search(vectors[0], 5)
        self.assertEqual(len(index), 600)
        self.assertTrue(index.calibration.get("plans"))


class TestGrowWithTombstones(Base):
    def test_growth_after_deletes_retrains_cleanly(self):
        """A big arrival after deletes must retrain codes and stay correct."""
        rng = random.Random(47)
        dim = 8
        index = VectorIndex(dim)
        first = [tuple(rng.gauss(0.0, 1.0) for _ in range(dim)) for _ in range(300)]
        index.upsert(list(range(300)), first)
        index.delete(list(range(50)))
        second = [tuple(rng.gauss(0.0, 1.0) for _ in range(dim)) for _ in range(4000)]
        index.upsert(list(range(5000, 9000)), second)
        self.assertEqual(len(index), 4250)
        ids = [i for i in range(50, 300)] + list(range(5000, 9000))
        vecs = first[50:] + second
        for q in second[:5]:
            got = index.search(q, 10)
            want = brute_force(ids, vecs, q, 10)
            self.assertEqual(len(got), 10)
            self.assertTrue({rid for rid, _ in got} <= set(ids))
            self.assertGreaterEqual(len({rid for rid, _ in got} &
                                       {rid for rid, _ in want}), 9)


class TestQueryDistributionMismatch(Base):
    """Queries from a *different* distribution than the data.

    The driver's query generator is not published, so a threshold calibrated
    only on rows of a clustered table would collapse here.  The calibration
    covers a generic query family for exactly this case.
    """

    def test_recall_when_queries_are_generic(self):
        rng = random.Random(44)
        dim = 64
        n = 3000
        vectors = make_clustered_vectors(n, dim, rng, n_centres=30, jitter=0.12)
        ids = ids_ascending(n)
        index = VectorIndex(dim)
        index.build(ids, vectors)
        qrng = random.Random(45)
        hits = 0
        slots = 0
        for _ in range(40):
            q = tuple(qrng.gauss(0.0, 1.0) for _ in range(dim))
            truth = {rid for rid, _ in brute_force(ids, vectors, q, 10)}
            got = {rid for rid, _ in index.search(q, 10)}
            hits += len(truth & got)
            slots += len(truth)
        self.assertGreaterEqual(hits / slots, 0.95,
                                "calib=%r" % (index.calibration,))

    def test_recall_when_queries_are_noisy_data_points(self):
        rng = random.Random(46)
        dim = 32
        n = 2000
        vectors = make_random_vectors(n, dim, rng)
        ids = ids_ascending(n)
        index = VectorIndex(dim)
        index.build(ids, vectors)
        hits = 0
        slots = 0
        for i in range(40):
            base = vectors[rng.randrange(n)]
            q = tuple(x + 0.25 * rng.gauss(0.0, 1.0) for x in base)
            truth = {rid for rid, _ in brute_force(ids, vectors, q, 10)}
            got = {rid for rid, _ in index.search(q, 10)}
            hits += len(truth & got)
            slots += len(truth)
        self.assertGreaterEqual(hits / slots, 0.95,
                                "calib=%r" % (index.calibration,))


# --- calibration bookkeeping ----------------------------------------------


class TestCalibration(Base):
    def test_calibration_reports_recall(self):
        rng = random.Random(31)
        dim = 32
        vectors = make_random_vectors(2000, dim, rng)
        index = VectorIndex(dim)
        index.build(ids_ascending(2000), vectors)
        info = index.calibrate()
        self.assertIn("recall10", info)
        self.assertIn("avg_pool", info)
        self.assertIn(info["mode"], ("ann", "exact"))
        self.assertEqual(info["rows"], 2000)
        self.assertGreater(info["avg_pool"], 0)

    def test_calibration_is_lazy_and_cached(self):
        rng = random.Random(32)
        vectors = make_random_vectors(600, 12, rng)
        index = VectorIndex(12)
        index.build(ids_ascending(600), vectors)
        self.assertEqual(index.calibration, {})
        index.search(vectors[0], 5)
        info = index.calibration
        self.assertTrue(info)
        index.search(vectors[1], 5)
        self.assertEqual(index.calibration, info)  # no recalibration per query

    def test_tiny_index_is_exact(self):
        index = VectorIndex(3)
        index.build([1, 2], [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)])
        self.assertEqual(index.search((0.0, 0.0, 0.0), 2),
                         [(1, 0.0), (2, math.sqrt(3.0))])


if __name__ == "__main__":
    unittest.main(verbosity=2)
