# MiniVectorDB

A lightweight vector database implemented from scratch: SQL, persistent
storage, transactions, crash recovery and vector similarity search.

Everything is implemented in this repository on top of the **Python standard
library only**. No SQLite, no RocksDB, no Faiss, no HNSWlib, no third-party
SQL parser and no third-party vector index.

* Language: Python 3.11+ (the image installs the distribution `python3`,
  which is 3.12 on Ubuntu 24.04); the code targets Linux x86_64.
* **Locked dependencies: none.** The database imports only the Python
  standard library, so there is no `requirements.txt` and nothing to pin.
  The only build-stage download is the distribution `python3` package.
* Storage, query execution, vector index and transaction handling: all
  hand-written in `minivectordb/`

---

## 1. Build and run

```bash
# Build the evaluation image
docker build -t minivectordb:latest .

# Run in the foreground; the driver talks over stdin/stdout.
# No port is exposed and no network access is needed.
docker run --rm -i \
  -v minivectordb-data:/data \
  --cpus=2 \
  --memory=2g \
  minivectordb:latest
```

The program reads SQL statements from **stdin** and writes results to
**stdout**. Diagnostics go to **stderr** only, so stdout stays clean for the
driver.

All persistent files are written under `/data` (override with
`MVD_DATA_DIR`).

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MVD_DATA_DIR` | `/data` | Directory holding the database files |
| `MVD_PROTOCOL` | `auto` | `line`, `mysql`, `json` or `auto` (see below) |
| `MVD_MODE` | `exact` | Default search mode for vector queries |
| `MVD_ECHO` | `1` | Echo each statement before its result |
| `MVD_READY_ON_STDOUT` | unset | Also print the ready marker on stdout |
| `MVD_SHOW_SQL` | unset | Log startup details to stderr |

### Wire-framing profiles

The statement defers the exact stdin/stdout framing to a test driver that is
not published, so the framing is selectable with a single environment
variable and the Dockerfile can change it in one line:

| `MVD_PROFILE` | framing |
|---|---|
| `text` (default) | plain text, one response per statement, **no** echo, blank line as the response terminator. A query answers with the column header and the rows; a DML statement answers with a single `N row(s) affected` line |
| `echo` | text that repeats the statement first, mysqltest `.result` style, no blank line |
| `json` | one JSON object per statement: `{"columns":…,"rows":…,"affected_rows":…}` |
| `mysql` | MySQL client/server protocol over the stdin/stdout pipes |
| `auto` | sniff the first bytes and pick one |

`MVD_PROFILE` only fills in variables that are not already set, so
`MVD_ECHO`, `MVD_OUTPUT`, `MVD_TERMINATOR`, `MVD_PROTOCOL`, `MVD_MODE` and
`MVD_READY_ON_STDOUT` still override any profile.

Set it in the Dockerfile for a submission, for example:

```dockerfile
ENV MVD_PROFILE=json
```

### Protocols

The task package fixes the interface (SQL over stdin, results over stdout)
but not the exact framing, so the server supports three protocols and picks
one automatically from the first bytes it receives:

* **line** - mysqltest-style text. One statement per line; the reply is the
  echoed statement, a tab-separated header line and tab-separated rows.
  Errors are reported as `ERROR <code>: <message>`.
* **mysql** - the MySQL client/server wire protocol (handshake v10,
  `mysql_native_password`, `COM_QUERY`, text result sets, `ERR` packets), so a
  real `mysqltest` binary can drive the database.
* **json** - one JSON request per line: `{"sql": "...", "mode": "ann"}`,
  answered with `{"columns": [...], "rows": [...], "row_count": n}`.

`auto` (the default) sniffs the stream: a MySQL packet sequence is
recognised by its 4-byte header, otherwise the input is treated as text.

### Ready signal

The database announces readiness on **stderr** with several common
spellings (`MiniVectorDB ready`, `Server ready`, `READY`, ...) so a driver
looking for any of them finds one. stdout stays clean unless
`MVD_READY_ON_STDOUT=1` is set.

### Shutdown

On EOF on stdin, or on `SIGTERM`/`SIGINT`, the database rolls back any open
transaction, flushes everything to disk and exits with status 0.

---

## 2. Example session

Starting from an empty data directory:

```sql
-- Schema: one BIGINT primary key, at most one vector column per table
create table docs (
  id bigint,
  title varchar(64),
  folder varchar(16),
  embedding vector(3),
  primary key (id)
);

-- Insert, including a multi-row statement
insert into docs values (1, 'alpha', 'a', [0,0,0]);
insert into docs values (2, 'beta',  'a', [1,0,0]);
insert into docs (id, title, folder, embedding) values
  (3, 'gamma', 'b', [2,0,0]),
  (4, 'delta', 'b', [3,0,0]);

-- Ordinary query
select id, title from docs where folder = 'b' order by id;

-- Vector search: filter first, then take the top K
select id, l2_distance(embedding, [0,0,0]) as d
from docs
where folder = 'b'
order by l2_distance(embedding, [0,0,0])
limit 2;

-- Approximate search (same semantics, candidate generation is approximate)
select id from docs
order by l2_distance(embedding, [0,0,0])
limit 2 ann;

-- Transactions
begin;
insert into docs values (5, 'epsilon', 'c', [4,0,0]);
select id from docs;            -- sees its own uncommitted change
rollback;
select id from docs;            -- the change is gone

-- Index maintenance
create vector index idx_docs on docs(embedding);
```

### Restarting

All state lives in `/data`, so pointing a new process at the same directory
resumes exactly where the previous one stopped. This also covers the crash
case: the process may be killed at any moment and the committed data is
still there after the restart.

```bash
# first run: create and fill the database, then shut down cleanly
docker run --rm -i -v mvd-data:/data minivectordb:latest <<'SQL'
create table docs (id bigint, title varchar(64), emb vector(3), primary key (id));
insert into docs values (1, 'alpha', [0,0,0]);
insert into docs values (2, 'beta',  [1,0,0]);
SQL

# second run: same /data volume, the rows are still there
docker run --rm -i -v mvd-data:/data minivectordb:latest <<'SQL'
select id, title from docs order by id;
select id from docs order by l2_distance(emb, [0,0,0]) limit 2;
SQL
```

A hard kill (`docker kill`, `SIGKILL`) is recovered the same way: the
write-ahead log is replayed on start-up and only committed transactions
are applied. An explicit transaction that was never committed is rolled
back on the next start.

---

## 3. Supported SQL

| Statement | Notes |
|---|---|
| `CREATE TABLE t (a BIGINT, b VARCHAR(n), c VECTOR(n), PRIMARY KEY (a))` | exactly one BIGINT primary key, at most one vector column |
| `DROP TABLE [IF EXISTS] t` | |
| `CREATE VECTOR INDEX i ON t(c)` | builds the approximate index for `t.c` |
| `DROP VECTOR INDEX i` | |
| `INSERT INTO t [(cols)] VALUES (...),(...)` | multi-row inserts supported |
| `UPDATE t SET c = expr [, ...] [WHERE ...]` | |
| `DELETE FROM t [WHERE ...]` | |
| `SELECT ... FROM t [WHERE ...] [ORDER BY ...] [LIMIT n [OFFSET m]]` | `LIMIT m, n` also accepted |
| `BEGIN` / `START TRANSACTION` / `COMMIT` / `ROLLBACK` | one explicit transaction at a time |
| `SHOW TABLES` / `SHOW COLUMNS FROM t` / `SHOW INDEXES FROM t` | |
| `DESC t` / `EXPLAIN stmt` / `USE db` | |
| `SET @mvd_mode = 'ann' \| 'exact'` | sets the session default search mode |

Conditions: `=`, `<`, `<=`, `>`, `>=`, `<>`, `AND`, `OR`, `NOT`,
parentheses, `IN (...)`, `BETWEEN a AND b`, `LIKE 'pattern'`,
`IS [NOT] NULL`.

Functions: `L2_DISTANCE` (plus `L2`, `EUCLIDEAN_DISTANCE`, `COSINE_DISTANCE`,
`INNER_PRODUCT`).

### Exact vs approximate mode

A vector search is a `SELECT` whose `ORDER BY` contains a distance
function. The mode is selected explicitly, and the following spellings are
all accepted (the last hint wins):

```sql
... limit 10 ann
... limit 10 exact
... limit 10 approx
... limit 10 using ann
... limit 10 parameters (mode='ann')
... limit 10 with (mode='ann')
select ... from t with (mode='ann') ...
```

The session default (`SET @mvd_mode='ann'`, or `MVD_MODE=ann`) applies when
a statement carries no hint. **`exact` is the default.** In `exact` mode the
result is the mathematically exact answer; in `ann` mode the same
semantics are computed over a candidate set produced by the approximate
index (recall is validated against brute force).

### Ordering rules

* Equal distances are ordered by primary key ascending.
* Ties in an ordinary `ORDER BY` are also broken by primary key ascending.
* A result with fewer than K matches returns all of them.
* A filtered Top-K is computed **within** the filtered set - the filter is
  applied before the top K is taken.

### Driver directives

The published case pack uses two directives in its `.test` files, which are
not SQL:

* `--restart` - shut down normally (waiting for the exit) and start again
  against the same data directory. The shutdown must roll back any
  uncommitted transaction.
* `--mode ann` - subsequent vector requests use the `ann` mode; every case
  starts in the `exact` mode.

`tests/run_official_cases.py` implements both, so the published cases run
against the database exactly as the organiser's driver would drive them.

### Errors

Unsupported or invalid statements return an explicit error and never
silently do something else:

```
ERROR 1062: Duplicate entry '1' for key 'PRIMARY'
ERROR 1146: Table 'test.nosuch' doesn't exist
ERROR 1054: Unknown column 'nosuchcol' in 'field list'
ERROR 1406: Data too long for column 'name' at row 1
ERROR 1048: Column 'id' cannot be null
ERROR 1136: Column count doesn't match value count at row 1
ERROR 1068: Only one vector column is allowed per table
ERROR 1062: The primary key must be a BIGINT column
```

Rejected statements leave no partial modification, and a rejected statement
inside an explicit transaction does not abort the transaction: the earlier
successful statements can still be committed or rolled back.

---

## 4. Design

### 4.1 Storage layout

```
/data/
  catalog.json        # table schemas, written atomically
  wal.log             # append-only redo log (framed records + CRC)
  tables/<id>.dat     # append-only row log per table
```

Rows are appended to a per-table log. An update appends a new version and a
delete appends a tombstone, so the write path stays sequential and the I/O
volume stays low. Vectors are stored as raw little-endian `float32` arrays,
which keeps the on-disk footprint (and therefore the page cache that the
platform counts) small.

Rows are **not** all held in memory. Only a primary-key to offset map is
kept, and row payloads are streamed from disk during a scan. This matters
because the formal tests include a data set larger than the memory budget.

### 4.2 Durability and recovery

Every data change is written to the write-ahead log first. A commit
`flush()`es and `fsync`s the log **before** the `COMMIT` response is
produced, so an acknowledged commit survives a `SIGKILL`.

On startup the log is replayed:

* each transaction is written as a unit and marked committed; only
  committed transactions are applied, so an uncommitted transaction can
  never become partially visible,
* records are framed with a length and a CRC, so a torn record at the tail
  of a log (a process killed mid-write) is detected and discarded,
* replay is idempotent - transaction identifiers make applying a record
  twice a no-op, so killing the process *during* recovery and restarting
  again reaches exactly the same state.

### 4.3 Transactions

At most one explicit transaction is active. Writes are buffered in a small
in-memory overlay, so a transaction that is rolled back leaves no trace and
a failed statement cannot leave a partial modification. `COMMIT` applies
the overlay and fsyncs; `ROLLBACK` simply discards it. Reads inside a
transaction observe the transaction's own changes.

### 4.4 Vector index

The approximate index is implemented in `minivectordb/vector_index.py`
without any third-party library. It combines a cheap candidate generation
stage with exact distance computation on the survivors, and it is
maintained incrementally on insert, update and delete, so the search
results always reflect the currently visible rows. The index may be
rebuilt from the persisted rows at start-up; the database does not report
itself ready before that finished.

`exact` mode never consults the index - it always computes the exact
answer over the filtered set.

### 4.5 Recovery of the index

The index is derived data. It is rebuilt from the persisted rows during
start-up, and the rebuild is part of the recovery work that happens before
the database reports ready.

---

## 5. Tests

```bash
python3 tests/run_tests.py           # all mysqltest-style cases
python3 tests/run_tests.py basic     # a single case
python3 tests/run_tests.py -v        # show diffs for failures

python3 -m unittest discover -s tests -v   # unit tests
```

The cases in `tests/t/` are `.test`/`.result` pairs in the same style as the
official mysqltest suites; the runner compares the output against the
expected result while ignoring trailing horizontal whitespace, exactly like
the official driver.

---

## 6. Known limitations

* `NULL` is accepted as a value but the statement does not require NULL
  handling beyond that; there is no `IS NULL` semantics beyond a plain
  match.
* No `JOIN`, aggregation beyond `COUNT(*)`, subquery, `ALTER TABLE`,
  foreign key, nested transaction or primary-key update - each is reported
  as an unsupported error rather than silently reinterpreted.
* The vector index supports the `l2` metric (and cosine/IP for the
  distance function); the approximate mode is only used when the statement
  explicitly asks for it.
