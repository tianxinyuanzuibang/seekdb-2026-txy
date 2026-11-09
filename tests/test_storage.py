"""Unit tests for the MiniVectorDB storage engine.

Run with::

    python3 -m unittest tests.test_storage -v
"""

import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minivectordb.errors import SQLError  # noqa: E402
from minivectordb.storage import Storage, Row  # noqa: E402


COLUMNS = [
    {"name": "id", "type_name": "BIGINT"},
    {"name": "name", "type_name": "VARCHAR", "length": 64},
    {"name": "emb", "type_name": "VECTOR", "vector_dim": 3},
]


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mvd-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, **kwargs):
        storage = Storage(self.dir, **kwargs)
        storage.open()
        return storage

    @staticmethod
    def create(storage, name="t", vector_column="emb"):
        storage.create_table(name, COLUMNS, "id", vector_column)


class TestBasics(StorageTestCase):
    def test_create_insert_scan_roundtrip(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [
            {"id": 1, "name": "alpha", "emb": (1.0, 2.0, 3.0)},
            {"id": 2, "name": "beta", "emb": (4.0, 5.0, 6.0)},
        ])
        rows = list(st.scan("t"))
        self.assertEqual([r.pk for r in rows], [1, 2])
        self.assertEqual(rows[0]["name"], "alpha")
        self.assertEqual(rows[0]["emb"], (1.0, 2.0, 3.0))
        self.assertIsInstance(rows[0], Row)
        # positional access
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][1], "alpha")
        self.assertEqual(rows[0][2], (1.0, 2.0, 3.0))
        st.close()

    def test_row_mapping_protocol(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 7, "name": "x", "emb": (0.0, 0.0, 0.0)}])
        row = st.get("t", 7)
        self.assertEqual(row.pk, 7)
        self.assertEqual(row.get("name"), "x")
        self.assertEqual(sorted(row.keys()), ["emb", "id", "name"])
        self.assertIn("id", row)
        self.assertEqual(len(row), 3)
        self.assertEqual(dict(row.items())["name"], "x")
        self.assertEqual(list(row), [7, "x", (0.0, 0.0, 0.0)])
        st.close()

    def test_list_tables_and_get_table(self):
        st = self.make()
        self.create(st, "alpha")
        self.create(st, "beta")
        self.assertEqual(st.list_tables(), ["alpha", "beta"])
        table = st.get_table("alpha")
        self.assertEqual(table.name, "alpha")
        self.assertEqual(table.primary_key, "id")
        self.assertEqual(table.vector_column, "emb")
        self.assertEqual(
            [c.name for c in table.columns], ["id", "name", "emb"]
        )
        col = table.columns[2]
        self.assertEqual(col.type_name, "VECTOR")
        self.assertEqual(col.vector_dim, 3)
        self.assertEqual(table.columns[0].type_name, "BIGINT")
        self.assertEqual(table.columns[1].length, 64)
        self.assertTrue(table.columns[0].primary_key)
        st.close()

    def test_duplicate_primary_key(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        with self.assertRaises(SQLError) as ctx:
            st.insert_rows("t", [{"id": 1, "name": "b", "emb": (1.0, 1.0, 1.0)}])
        self.assertEqual(ctx.exception.code, 1062)
        # the failed statement must not have modified anything
        self.assertEqual(len(list(st.scan("t"))), 1)
        self.assertEqual(st.get("t", 1)["name"], "a")
        st.close()

    def test_duplicate_within_one_statement(self):
        st = self.make()
        self.create(st)
        with self.assertRaises(SQLError) as ctx:
            st.insert_rows("t", [
                {"id": 5, "name": "a", "emb": (1.0, 1.0, 1.0)},
                {"id": 5, "name": "b", "emb": (2.0, 2.0, 2.0)},
            ])
        self.assertEqual(ctx.exception.code, 1062)
        self.assertEqual(list(st.scan("t")), [])
        st.close()

    def test_duplicate_inside_transaction(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        st.begin()
        with self.assertRaises(SQLError) as ctx:
            st.insert_rows("t", [{"id": 1, "name": "x", "emb": (1.0, 1.0, 1.0)}])
        self.assertEqual(ctx.exception.code, 1062)
        st.rollback()
        st.close()

    def test_duplicate_against_own_uncommitted_insert(self):
        st = self.make()
        self.create(st)
        st.begin()
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        with self.assertRaises(SQLError):
            st.insert_rows("t", [{"id": 1, "name": "b", "emb": (1.0, 1.0, 1.0)}])
        st.rollback()
        st.close()


class TestTransactions(StorageTestCase):
    def test_rollback_restores_pre_transaction_state(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        st.begin()
        st.insert_rows("t", [{"id": 2, "name": "b", "emb": (2.0, 2.0, 2.0)}])
        st.update_rows("t", 1, {"name": "changed"})
        st.delete_rows("t", [1])
        # visible inside the transaction
        self.assertIsNone(st.get("t", 1))
        self.assertIsNotNone(st.get("t", 2))
        st.rollback()
        rows = list(st.scan("t"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pk, 1)
        self.assertEqual(rows[0]["name"], "a")
        st.close()

    def test_commit_persists_transaction(self):
        st = self.make()
        self.create(st)
        st.begin()
        st.insert_rows("t", [{"id": 2, "name": "b", "emb": (2.0, 2.0, 2.0)}])
        st.commit()
        self.assertEqual(len(list(st.scan("t"))), 1)
        st.close()
        st2 = self.make()
        self.assertEqual(len(list(st2.scan("t"))), 1)
        st2.close()

    def test_update_is_partial_patch(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [
            {"id": 1, "name": "a", "emb": (1.0, 2.0, 3.0)},
        ])
        st.update_rows("t", 1, {"name": "z"})
        row = st.get("t", 1)
        self.assertEqual(row["name"], "z")
        # untouched columns keep their old value
        self.assertEqual(row["emb"], (1.0, 2.0, 3.0))
        st.close()

    def test_failed_statement_inside_transaction_keeps_transaction_usable(self):
        st = self.make()
        self.create(st)
        st.begin()
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        with self.assertRaises(SQLError):
            st.insert_rows("t", [{"id": 1, "name": "dup", "emb": (1.0, 1.0, 1.0)}])
        st.commit()
        rows = list(st.scan("t"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "a")
        st.close()

    def test_only_one_transaction_at_a_time(self):
        st = self.make()
        self.create(st)
        st.begin()
        with self.assertRaises(SQLError):
            st.begin()
        st.rollback()
        st.close()


class TestVisibility(StorageTestCase):
    def test_delete_and_update_visibility(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [
            {"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)},
            {"id": 2, "name": "b", "emb": (2.0, 2.0, 2.0)},
        ])
        st.delete_rows("t", [1])
        self.assertIsNone(st.get("t", 1))
        rows = list(st.scan("t"))
        self.assertEqual([r.pk for r in rows], [2])
        st.update_rows("t", 2, {"name": "b2"})
        self.assertEqual(st.get("t", 2)["name"], "b2")
        st.close()

    def test_scan_is_ordered_by_primary_key(self):
        st = self.make()
        self.create(st)
        pks = [9, 3, 7, 1, 100, -5, 0]
        st.insert_rows("t", [
            {"id": pk, "name": str(pk), "emb": (0.0, 0.0, 0.0)} for pk in pks
        ])
        got = [r.pk for r in st.scan("t")]
        self.assertEqual(got, sorted(pks))
        st.close()

    def test_scan_ordered_with_transaction_overlay(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [
            {"id": 1, "name": "a", "emb": (0.0, 0.0, 0.0)},
            {"id": 2, "name": "b", "emb": (0.0, 0.0, 0.0)},
        ])
        st.begin()
        st.insert_rows("t", [{"id": 3, "name": "c", "emb": (0.0, 0.0, 0.0)}])
        st.delete_rows("t", [1])
        st.update_rows("t", 2, {"name": "b2"})
        got = [r.pk for r in st.scan("t")]
        self.assertEqual(got, [2, 3])
        st.rollback()
        self.assertEqual([r.pk for r in st.scan("t")], [1, 2])
        st.close()

    def test_unicode_roundtrip(self):
        st = self.make()
        self.create(st)
        text = "héllo 世界 🌍 é́"
        st.insert_rows("t", [{"id": 1, "name": text, "emb": (0.0, 0.0, 0.0)}])
        self.assertEqual(st.get("t", 1)["name"], text)
        st.close()
        st2 = self.make()
        self.assertEqual(st2.get("t", 1)["name"], text)
        st2.close()

    def test_null_values(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": None, "emb": None}])
        row = st.get("t", 1)
        self.assertIsNone(row["name"])
        self.assertIsNone(row["emb"])
        st.close()


class TestDurability(StorageTestCase):
    def test_persistence_across_reopen(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 2.0, 3.0)}])
        st.close()
        st2 = self.make()
        rows = list(st2.scan("t"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "a")
        self.assertEqual(rows[0]["emb"], (1.0, 2.0, 3.0))
        st2.close()

    def test_table_schema_survives_reopen(self):
        st = self.make()
        self.create(st, "t")
        st.close()
        st2 = self.make()
        self.assertEqual(st2.list_tables(), ["t"])
        table = st2.get_table("t")
        self.assertEqual(table.primary_key, "id")
        self.assertEqual(table.vector_column, "emb")
        st2.close()

    def test_drop_table_survives_reopen(self):
        st = self.make()
        self.create(st, "gone")
        st.create_table("kept", COLUMNS, "id", "emb")
        st.drop_table("gone")
        st.close()
        st2 = self.make()
        self.assertEqual(st2.list_tables(), ["kept"])
        st2.close()

    def test_uncommitted_transaction_is_not_recovered(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "committed", "emb": (1.0, 1.0, 1.0)}])
        st.begin()
        st.insert_rows("t", [{"id": 2, "name": "uncommitted", "emb": (2.0, 2.0, 2.0)}])
        # simulate a crash: do not commit, do not close cleanly
        st._tables  # noqa: B018
        st2 = self.make()
        rows = list(st2.scan("t"))
        self.assertEqual([r.pk for r in rows], [1])
        st2.close()

    def test_recovery_is_idempotent(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [
            {"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)},
            {"id": 2, "name": "b", "emb": (2.0, 2.0, 2.0)},
        ])
        st.close()
        # reopen several times; the state must be identical every time
        expected = None
        for _ in range(3):
            st2 = self.make()
            rows = [(r.pk, r["name"], r["emb"]) for r in st2.scan("t")]
            if expected is None:
                expected = rows
            self.assertEqual(rows, expected)
            st2.close()

    def test_torn_tail_record_is_discarded(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        st.close()
        # append a garbage tail to the table log
        path = os.path.join(self.dir, "tables", "1.dat")
        with open(path, "ab") as handle:
            handle.write(b"\x10\x00\x00\x00" + b"\x00\x00\x00\x00" + b"garbage")
        st2 = self.make()
        rows = list(st2.scan("t"))
        self.assertEqual([r.pk for r in rows], [1])
        st2.close()

    def test_torn_committed_suffix_is_rolled_back(self):
        """A crash between the row append and the commit marker."""
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        st.close()
        path = os.path.join(self.dir, "tables", "1.dat")
        # append a valid row frame without a commit marker
        payload = bytearray()
        payload.append(0)  # OP_INSERT
        payload += struct.pack("<q", 2)
        payload += bytes((1,)) + struct.pack("<q", 2)          # id
        payload += bytes((0,))                               # name NULL
        payload += bytes((3,)) + struct.pack("<I", 3) + struct.pack("<3f", 1.0, 2.0, 3.0)
        frame = struct.pack("<II", len(payload), zlib.crc32(bytes(payload)) & 0xFFFFFFFF)
        with open(path, "ab") as handle:
            handle.write(frame + bytes(payload))
        st2 = self.make()
        rows = list(st2.scan("t"))
        self.assertEqual([r.pk for r in rows], [1])
        st2.close()

    def test_commit_survives_close_and_reopen(self):
        st = self.make()
        self.create(st)
        st.begin()
        st.insert_rows("t", [{"id": 42, "name": "x", "emb": (0.0, 0.0, 0.0)}])
        st.commit()
        st.close()
        st2 = self.make()
        self.assertIsNotNone(st2.get("t", 42))
        st2.close()


class TestDiskLayout(StorageTestCase):
    def test_vectors_are_stored_as_raw_float32(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 2.0, 3.0)}])
        st.close()
        path = os.path.join(self.dir, "tables", "1.dat")
        with open(path, "rb") as handle:
            blob = handle.read()
        self.assertIn(struct.pack("<3f", 1.0, 2.0, 3.0), blob)
        # 3 floats = 12 bytes, not a JSON/text representation
        self.assertNotIn(b"[1.0", blob)
        self.assertLess(len(blob), 128)

    def test_data_dir_env_override(self):
        old = os.environ.get("MVD_DATA_DIR")
        os.environ["MVD_DATA_DIR"] = self.dir
        try:
            storage = Storage()
            storage.open()
            storage.create_table("t", COLUMNS, "id", "emb")
            storage.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
            storage.close()
            self.assertTrue(os.path.exists(os.path.join(self.dir, "catalog.json")))
        finally:
            if old is None:
                os.environ.pop("MVD_DATA_DIR", None)
            else:
                os.environ["MVD_DATA_DIR"] = old


class TestVectorCache(StorageTestCase):
    """The decoded vector cache must be fast, bounded and correctness-neutral."""

    def _fill(self, st, n=50):
        st.insert_rows("t", [
            {"id": i, "name": "n%d" % i, "emb": (float(i), 0.5, -1.0)}
            for i in range(1, n + 1)
        ])

    def test_cache_never_returns_a_deleted_row(self):
        st = self.make()
        self.create(st)
        self._fill(st, 20)
        list(st.scan_column("t", "emb"))
        self.assertIsNotNone(st.cached_vectors("t"))

        st.delete_rows("t", [5, 6, 7])
        cached = st.cached_vectors("t")
        for gone in (5, 6, 7):
            self.assertNotIn(gone, cached)
            self.assertIsNone(st.get("t", gone))
        self.assertNotIn(5, dict(st.scan_column("t", "emb")))
        self.assertNotIn(5, {r.pk for r in st.scan("t")})
        # and the size accounting released their memory
        self.assertLess(len(cached), 20)
        st.close()

    def test_deleted_row_after_cache_disable(self):
        """Deleting while the cache is disabled must still be correct."""
        st = self.make()
        self.create(st)
        self._fill(st, 5)
        st._vector_cache_limit = 0
        list(st.scan_column("t", "emb"))
        self.assertIsNone(st.cached_vectors("t"))
        st.delete_rows("t", [2])
        self.assertEqual(sorted(pk for pk, _ in st.scan_column("t", "emb")),
                         [1, 3, 4, 5])
        st.close()

    def test_cache_matches_scan(self):
        st = self.make()
        self.create(st)
        self._fill(st)
        # cold: no cache yet
        self.assertIsNone(st.cached_vectors("t"))
        cold = dict(st.scan_column("t", "emb"))
        # now warm
        cached = st.cached_vectors("t")
        self.assertIsNotNone(cached)
        self.assertEqual(dict(cached), cold)
        self.assertEqual(st.cached_vector_pks("t"), sorted(cold))
        # a warm scan returns exactly the same values
        self.assertEqual(dict(st.scan_column("t", "emb")), cold)
        # scan() also agrees (it may take the vector from the cache)
        self.assertEqual(
            {r.pk: r["emb"] for r in st.scan("t")}, cold
        )
        st.close()

    def test_cache_after_insert_update_delete(self):
        st = self.make()
        self.create(st)
        self._fill(st, 10)
        list(st.scan_column("t", "emb"))          # populate
        self.assertIsNotNone(st.cached_vectors("t"))

        st.insert_rows("t", [{"id": 99, "name": "x", "emb": (9.0, 9.0, 9.0)}])
        st.update_rows("t", 3, {"emb": (7.0, 7.0, 7.0)})
        st.delete_rows("t", [4])

        # the incrementally maintained cache must equal a freshly built one
        incremental = dict(st.cached_vectors("t"))
        st._vector_cache_drop(st.get_table("t").id)
        fresh = dict(st.scan_column("t", "emb"))
        self.assertEqual(incremental, fresh)
        self.assertEqual(fresh[99], (9.0, 9.0, 9.0))
        self.assertEqual(fresh[3], (7.0, 7.0, 7.0))
        self.assertNotIn(4, fresh)
        self.assertEqual(len(fresh), 10)
        # and both agree with a plain scan()
        self.assertEqual({r.pk: r["emb"] for r in st.scan("t")}, fresh)
        st.close()

    def test_cache_survives_reopen(self):
        st = self.make()
        self.create(st)
        self._fill(st, 5)
        list(st.scan_column("t", "emb"))
        expected = dict(st.cached_vectors("t"))
        st.close()
        st2 = self.make()
        # a fresh process has no cache but must return the same data
        self.assertIsNone(st2.cached_vectors("t"))
        self.assertEqual(dict(st2.scan_column("t", "emb")), expected)
        self.assertEqual(dict(st2.cached_vectors("t")), expected)
        st2.close()

    def test_cache_not_polluted_by_uncommitted_transaction(self):
        st = self.make()
        self.create(st)
        self._fill(st, 5)
        list(st.scan_column("t", "emb"))
        before = dict(st.cached_vectors("t"))
        self.assertEqual(len(before), 5)

        st.begin()
        # no cache is exposed while a transaction is open
        self.assertIsNone(st.cached_vectors("t"))
        st.insert_rows("t", [{"id": 100, "name": "t", "emb": (1.0, 1.0, 1.0)}])
        st.update_rows("t", 2, {"emb": (8.0, 8.0, 8.0)})
        st.delete_rows("t", [1])
        # the uncommitted rows must not appear in any cache
        self.assertIsNone(st.cached_vectors("t"))
        st.rollback()

        # after a rollback the pre-transaction state is intact; the cache is
        # rebuilt lazily by the next scan
        self.assertIsNone(st.cached_vectors("t"))
        after = dict(st.scan_column("t", "emb"))
        self.assertEqual(after, before)
        self.assertNotIn(100, after)
        self.assertEqual(after[2], before[2])
        self.assertIn(1, after)
        self.assertEqual(dict(st.cached_vectors("t")), before)
        st.close()

    def test_cache_after_commit_reflects_new_rows(self):
        st = self.make()
        self.create(st)
        self._fill(st, 5)
        list(st.scan_column("t", "emb"))
        st.begin()
        st.insert_rows("t", [{"id": 6, "name": "f", "emb": (6.0, 6.0, 6.0)}])
        st.commit()
        # the cache is rebuilt lazily from the committed state
        scanned = dict(st.scan_column("t", "emb"))
        cached = st.cached_vectors("t")
        self.assertIsNotNone(cached)
        self.assertEqual(cached[6], (6.0, 6.0, 6.0))
        self.assertEqual(len(cached), 6)
        self.assertEqual(dict(cached), scanned)
        st.close()

    def test_cache_dropped_on_drop_table(self):
        st = self.make()
        self.create(st)
        self._fill(st, 3)
        list(st.scan_column("t", "emb"))
        self.assertIsNotNone(st.cached_vectors("t"))
        table_id = st.get_table("t").id
        st.drop_table("t")
        # the table is gone, so cached_vectors() now reports "no such table"
        with self.assertRaises(SQLError) as ctx:
            st.cached_vectors("t")
        self.assertEqual(ctx.exception.code, 1146)
        # and no memory is retained for it
        self.assertNotIn(table_id, st._vector_cache)
        self.assertEqual(st._vector_cache_bytes, 0)
        st.close()

    def test_cache_respects_the_budget(self):
        st = self.make()
        # a tiny budget: the cache must simply not be used
        st._vector_cache_limit = 1024
        self.create(st)
        self._fill(st, 50)
        list(st.scan_column("t", "emb"))
        self.assertIsNone(st.cached_vectors("t"))
        # correctness is unaffected
        self.assertEqual(len(dict(st.scan_column("t", "emb"))), 50)
        st.close()

    def test_cache_accounting_is_realistic(self):
        st = self.make()
        self.create(st)
        self._fill(st, 20)
        list(st.scan_column("t", "emb"))
        # a tuple of 3 floats is far bigger than 3 * 4 bytes
        self.assertGreater(
            st._vector_cache_bytes, 20 * 3 * 4
        )
        st.close()

    def test_cached_vectors_returns_the_live_dict(self):
        st = self.make()
        self.create(st)
        self._fill(st, 3)
        list(st.scan_column("t", "emb"))
        cached = st.cached_vectors("t")
        self.assertIs(cached, st.cached_vectors("t"))
        st.close()

    def test_null_vectors_are_cached_correctly(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [
            {"id": 1, "name": "a", "emb": None},
            {"id": 2, "name": "b", "emb": (1.0, 2.0, 3.0)},
        ])
        first = dict(st.scan_column("t", "emb"))
        self.assertIsNone(first[1])
        cached = st.cached_vectors("t")
        self.assertIsNotNone(cached)
        self.assertIsNone(cached[1])
        self.assertEqual(cached[2], (1.0, 2.0, 3.0))
        self.assertEqual(dict(st.scan_column("t", "emb")), first)
        st.close()


class TestDataVersion(StorageTestCase):
    """The mutation counter used by the executor to invalidate its caches."""

    def test_reads_do_not_change_the_version(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        before = st.data_version("t")
        list(st.scan("t"))
        list(st.scan_column("t", "name"))
        list(st.scan_column("t", "emb"))
        st.get("t", 1)
        st.list_tables()
        st.get_table("t")
        st.flush()
        self.assertEqual(st.data_version("t"), before)
        st.close()

    def test_every_mutation_bumps(self):
        st = self.make()
        self.create(st)
        versions = []
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        versions.append(st.data_version("t"))
        st.update_rows("t", 1, {"name": "b"})
        versions.append(st.data_version("t"))
        st.delete_rows("t", [1])
        versions.append(st.data_version("t"))
        st.insert_rows("t", [{"id": 2, "name": "c", "emb": (2.0, 2.0, 2.0)}])
        versions.append(st.data_version("t"))
        self.assertEqual(versions, sorted(set(versions)))
        self.assertTrue(all(v > 0 for v in versions))
        st.close()

    def test_transaction_boundaries_bump(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])

        before = st.data_version("t")
        st.begin()
        st.insert_rows("t", [{"id": 2, "name": "b", "emb": (2.0, 2.0, 2.0)}])
        # a write inside the transaction changes the visible content
        self.assertNotEqual(st.data_version("t"), before)
        after_write = st.data_version("t")

        st.rollback()
        self.assertNotEqual(st.data_version("t"), after_write)

        before = st.data_version("t")
        st.begin()
        st.insert_rows("t", [{"id": 3, "name": "c", "emb": (3.0, 3.0, 3.0)}])
        st.commit()
        self.assertNotEqual(st.data_version("t"), before)
        st.close()

    def test_version_is_per_table(self):
        st = self.make()
        self.create(st, "one")
        self.create(st, "two")
        v1 = st.data_version("one")
        v2 = st.data_version("two")
        self.assertNotEqual(v1, v2)
        before = st.data_version("two")
        st.insert_rows("one", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        self.assertNotEqual(st.data_version("one"), v1)
        self.assertEqual(st.data_version("two"), before)
        st.close()

    def test_recreated_table_gets_a_new_version(self):
        st = self.make()
        self.create(st, "t")
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        old = st.data_version("t")
        st.drop_table("t")
        self.create(st, "t")
        self.assertNotEqual(st.data_version("t"), old)
        st.close()

    def test_version_restarts_after_reopen(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (1.0, 1.0, 1.0)}])
        st.close()
        st2 = self.make()
        # the counter is in-memory only; any cache is gone too
        self.assertEqual(st2.data_version("t"), 0)
        st2.insert_rows("t", [{"id": 2, "name": "b", "emb": (2.0, 2.0, 2.0)}])
        self.assertGreater(st2.data_version("t"), 0)
        st2.close()


class TestVectorIndexWiring(StorageTestCase):
    def test_create_and_get_vector_index(self):
        st = self.make()
        self.create(st)
        st.insert_rows("t", [{"id": 1, "name": "a", "emb": (0.0, 0.0, 0.0)}])
        st.create_vector_index("idx", "t", "emb")
        index = st.get_vector_index("t")
        # the vector_index module may not exist yet in this repo state
        if index is not None:
            self.assertTrue(hasattr(index, "search"))
        # the catalog must record the index durably
        st.close()
        st2 = self.make()
        self.assertIn("idx", st2._index_meta)
        self.assertEqual(st2._index_meta["idx"]["table"], "t")
        st2.close()

    def test_drop_vector_index(self):
        st = self.make()
        self.create(st)
        st.create_vector_index("idx", "t", "emb")
        st.drop_vector_index("idx")
        st.close()
        st2 = self.make()
        self.assertEqual(st2._index_meta, {})
        st2.close()


if __name__ == "__main__":
    unittest.main()
