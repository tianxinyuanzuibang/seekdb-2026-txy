# MiniVectorDB — Design and Verification Record

This document is the "简要设计与验证记录" required by the contest
statement: it describes the record layout, the vector index, transaction
handling, commit durability and the recovery flow, then lists what is
implemented, what was verified and what is known to be limited.

---

## 1. Architecture

```
stdin ──► protocol.py ──► parser.py ──► executor.py ──► storage.py
stdout ◄── results.py  ◄──────────────┘                  │
                                                         ├── vector_index.py
                                                         └── /data  (WAL + row logs)
```

| Module | Responsibility |
|---|---|
| `protocol.py` | line / MySQL-wire / JSON framing, statement splitting, ready signal |
| `lexer.py` | tokenizer (keywords, strings, numbers, vector literals) |
| `parser.py` | recursive-descent parser producing the AST in `ast_nodes.py` |
| `executor.py` | statement semantics, expression evaluation, transactions |
| `storage.py` | catalog, write-ahead log, row logs, recovery, caches |
| `vector_index.py` | self-implemented approximate vector index |
| `values.py` | value semantics, float32 handling, distance functions |
| `results.py` | result container and rendering |

Everything above is written from scratch on the Python standard library.
No storage engine, no SQL parser and no vector index library is used.

---

## 2. Record layout

```
/data/
  catalog.json        # table schemas + vector index metadata (atomic replace)
  wal.log             # global commit log for multi-table transactions
  tables/<id>.dat     # append-only row log per table
```

A row log frame is `len:u32 | crc32:u32 | payload`. The payload starts with
an opcode (`INSERT`/`UPDATE`/`DELETE`/`COMMIT_MARKER`) and the primary key,
followed by the column values in declared order.

**Vectors are stored as raw little-endian `float32`** (4 bytes per
dimension). A 128-dimension vector therefore costs 512 bytes on disk
instead of ~2.5 KiB as text, which directly reduces both the I/O volume
and the page cache that the platform counts toward the memory metric.

Updates and deletes **append** a new version or a tombstone; they never
rewrite a row in place. The write path is therefore sequential, and only
the latest version of each key is visible.

`catalog.json` is written with a temporary file + `fsync` +
`os.replace`, so a schema change is either fully visible or not at all.

---

## 3. Transactions and commit durability

* At most one explicit transaction is active (per the statement).
* Writes inside a transaction go to a small in-memory **overlay**. Reads
  inside the transaction merge the overlay with the committed state, so a
  transaction sees its own changes.
* `ROLLBACK` discards the overlay; the committed state was never touched,
  so the pre-transaction state is restored exactly.
* `COMMIT` appends the transaction's records to the row log, appends a
  commit marker, then `flush()`es and `fsync`s **before** the success
  response is produced.
* A statement that fails leaves no partial modification, because a
  statement's writes are only applied to the overlay after the whole
  statement has been validated.

The durability boundary is deliberate: **one `fsync` per commit**, not per
row. The statement requires that an acknowledged commit survives a
process kill; it does not require that an unacknowledged one does. Batching
the `fsync` per commit is what makes the write phase fast (the benchmark
commits every 100 rows).

---

## 4. Recovery

On startup `Storage.open()`:

1. loads `catalog.json`,
2. replays `wal.log` and, for each table, truncates the row log to the
   committed length,
3. rebuilds the primary-key → offset index by scanning the committed
   prefix,
4. rebuilds any vector index from the persisted rows.

Details that make this safe:

* Only the **committed prefix** of a row log is visible. A record that
  was appended but never committed (the process died mid-statement) lies
  beyond the committed length and is dropped, so an uncommitted
  transaction can never become partially visible.
* A torn tail record fails its length/CRC check and is discarded.
* Recovery is **idempotent**: it only ever *truncates* to a committed
  length and rebuilds derived state by scanning, so being killed during
  recovery and restarting again reaches the same state. This is verified
  by killing the process five times during startup.

The database does not report readiness until recovery (including any
vector index rebuild) has finished.

---

## 5. Vector index

`vector_index.py` implements a **bit-packed scalar-quantization screen
followed by an exact rerank**:

1. **Build.** Each vector is scalar-quantized per dimension into 4
   equal-frequency buckets whose thresholds are learned from a sample. The
   bucket indices are encoded as a **thermometer (prefix-sum) code**:
   dimension `j` owns bits `[4j, 4j+4)` and bucket value `b` sets the
   lowest `b` of them. A 128-dimension vector becomes a single Python
   `int` of 512 bits, and
   `popcount(code(q) ^ code(v)) == Σ_j |bucket_j(q) − bucket_j(v)|` —
   exactly the L1 distance of the bucket indices, a good proxy for L2, at
   the cost of one `int.bit_count()` per row (~50 ns) instead of a float
   distance (~440 ns).
2. **Screen.** Keep the rows whose code distance is within a per-query
   threshold, which cuts 10,000 candidates down to a few hundred.
3. **Rerank.** Compute the exact `math.dist` only on the survivors and
   keep the best K in a heap, ties broken by id ascending.

The threshold is **self-calibrated on the real data**: a few sample
queries get their exact answers computed once, which yields the code
distance that the true neighbours actually need; the threshold is then
chosen to cover that with a margin. Two query families are sampled (rows
of the table and generic points from the per-dimension marginals), because
a threshold calibrated only on clustered data collapses if the driver's
queries come from a different distribution.

**Correctness guards** (structural, not statistical):

* the filter is applied **before** the top-K, so a filtered search is
  always computed inside the filtered set,
* deleted ids are removed from the id→slot map and can never be returned,
* the returned distances are exact float distances, and the candidates are
  re-sorted by exact distance, so ANN results have exactly the same
  ordering rules as exact results,
* when the candidate set is small (a selective filter) the search falls
  back to an exact scan — that is both faster and perfectly accurate,
* `exact` mode never consults the index at all.

---

## 6. Query execution

The vector top-K path is arranged so that a repeated query does almost no
I/O:

1. **Filter pass** — only the columns the `WHERE` clause mentions are
   decoded, in one sequential chunked read. The matching primary-key set
   is cached and invalidated by the storage layer's data version.
2. **Vector pass** — the decoded-vector cache of the storage layer
   supplies the vectors; a warm cache costs no disk I/O and no float
   decoding.
3. **Rank** — distances are computed only for the candidate keys, keeping
   the K best.
4. **Fetch** — only the K returned rows are fetched, by primary key.

Filtering happens before the top-K at every step, which is the requirement
the statement emphasises.

Ordinary `ORDER BY` sorts with `(value, primary key)` keys, so ties break
by primary key ascending. Vector results break equal distances by primary
key ascending.

---

## 7. Verified behaviour

| Area | How it was verified | Result |
|---|---|---|
| SQL basics | `tests/t/basic.test` + 180 unit tests | pass |
| Vector exact Top-K | `tests/t/vector.test`, brute-force comparison | pass |
| Filtered Top-K | filtered set computed independently | pass |
| Approximate search | `tests/test_recall.py` vs brute force | recall 0.992 unfiltered / 1.000 filtered |
| Error handling | `tests/t/errors.test` (dup key, bad column, bad type, too long, unknown table, unsupported) | pass, explicit codes |
| Transactions | `tests/t/transaction.test` | read-your-writes, rollback restores exactly |
| Crash recovery | `tests/test_crash.py`, 6 scenarios incl. repeated kills during recovery | pass |
| Persistence | clean restart and SIGKILL restart | pass |
| MySQL wire protocol | stock `mysql` client: DDL, INSERT, SELECT, vector Top-K | pass |
| **Official public cases** | organisers' `mysqltest/cases` (20 cases, 95 SQL, JSON assertions, incl. `--restart` and `--mode ann`) | **20/20 pass** |
| `affected_rows` contract | asserted by the public `.result` files; exposed as a real result field and in the JSON envelope | pass |
| Performance | `tests/bench.py`, 10,000 × 128 dims, 1,000 TopK | see below |

### Measured performance

Measured on an **idle Linux x86_64 host with Python 3.12** (the same
language version as the Ubuntu 24.04 evaluation image), 10,000 rows of
128 dimensions, 1,000 Top-K queries with K=10, half of them filtered:

| Phase | Result |
|---|---|
| Batch write (100 batches, each committed → one fsync per batch) | **2.25 s** |
| Vector Top-K, `exact` mode | **11.95 ms/query** |
| Vector Top-K, `ann` mode | **4.79 ms/query** |
| Peak RSS | **116 MiB** |
| Streaming path (vector cache disabled, 20,000 x 128, Top-K=10) | **0.10 s, 31.8 MiB peak**, identical to brute force |

For reference, a bare `[math.dist(v, q) for v in vectors]` over the same
10,000 vectors costs 11.93 ms on that machine, so the exact-mode query
path is at the physical floor of a pure-Python exact scan: the remaining
time is the distance computation itself, not overhead. Choosing `ann`
mode cuts the query phase by a further 2.4x while keeping Recall@10 at
0.997 (unfiltered) and 1.000 (filtered).

The default search mode is `exact`, as the statement specifies, and a
session may switch to `ann` with `SET @mvd_mode='ann'` or `MVD_MODE=ann`.
The default is deliberately left at `exact`: the statement documents
`exact` as the default, and small tables produce identical results in
both modes anyway (the index falls back to an exact scan when the
candidate set is small).

Absolute numbers still vary with hardware; the evaluation platform
measures wall clock, `io.stat` bytes and `memory.peak` under cgroup v2.

---

## 8. Implemented vs. not implemented

Implemented: `CREATE TABLE`, `DROP TABLE`, `CREATE/DROP VECTOR INDEX`,
`INSERT` (multi-row), `SELECT` (`WHERE`, `ORDER BY`, `LIMIT`/`OFFSET`,
projection, `*`, `COUNT`/`SUM`/`MIN`/`MAX`/`AVG`), `UPDATE`, `DELETE`,
`BEGIN`/`COMMIT`/`ROLLBACK`, `SHOW`, `DESC`, `EXPLAIN`, `USE`,
`SET @mvd_mode`, the L2 / cosine / inner-product distance functions,
exact and approximate Top-K, filtered Top-K, persistence, transactions and
crash recovery.

Deliberately **not** implemented (each returns an explicit error rather
than a silently different behaviour): `JOIN`, subqueries, `ALTER TABLE`,
foreign keys, nested transactions, primary-key updates, and NULL
comparison semantics beyond plain equality.

## 9. Known limitations

* The approximate search is tuned for the stated workload (10k–100k rows,
  128 dims). For much larger tables the fixed 64 MiB vector cache stops
  caching and the code falls back to streaming column scans, which stay
  correct but lose the cached-query advantage.
* Peak memory is dominated by the decoded-vector cache. It is bounded
  (`MVD_VECTOR_CACHE_MB`, default 64) precisely so that a large data set
  cannot push the container over its memory limit; correctness does not
  depend on the cache.
* The `line` protocol's default output is the mysqltest text form; the
  exact framing of the official driver is unpublished, so the server also
  speaks the MySQL wire protocol and a JSON form and selects one
  automatically.
* There is no concurrency: one client, one explicit transaction, as the
  statement specifies.
