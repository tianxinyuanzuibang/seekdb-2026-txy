"""Durable, append-only storage engine for MiniVectorDB.

Design summary
==============

The storage layer keeps three kinds of persistent state under the data
directory (default ``/data``, override with the ``MVD_DATA_DIR`` environment
variable)::

    <data_dir>/
        catalog.json        table schemas (rewritten atomically)
        wal.log             global commit log (commit markers + committed
                            file lengths)
        tables/<id>.dat     per-table append-only row log (the redo records)

Every table owns one append-only log file.  A log is a sequence of framed
records::

    +-----------+-----------+-----------------+
    | u32 len   | u32 crc32 | payload (len B) |
    +-----------+-----------+-----------------+

The CRC32 protects the payload; a torn tail record simply fails the length
or CRC check and is discarded.  Payloads are either

``ROW_INSERT`` / ``ROW_UPDATE`` / ``ROW_DELETE``
    a row version (full column values, vectors stored as raw little-endian
    float32), or

``MARKER``
    a *commit marker* for the table, holding the transaction id and the
    resulting file length.

``wal.log`` uses the same framing and stores commit markers for transactions
that span several tables (it also records the committed length of every
table it touches).

Crash safety
------------

* Rows are appended to the table log and fsync'ed, then a commit marker is
  written and fsync'ed.  Only after the marker is durable is the transaction
  reported as committed.
* A crash before the marker is durable leaves the uncommitted suffix in the
  log; recovery truncates the log back to the last committed length, so an
  uncommitted transaction is never partially applied.
* Recovery is idempotent: it computes a committed length per table and
  truncates, which is idempotent, and then rebuilds the in-memory index by
  scanning the committed prefix.  Killing the process during recovery and
  restarting again yields exactly the same state.
* The catalog is written with the classic temp-file + fsync + ``os.replace``
  dance, so ``CREATE TABLE`` / ``DROP TABLE`` are durable too.

Memory
------

Rows are *never* kept in RAM.  The only in-memory structure is a
``primary key -> file offset`` map per table (see the contract: "A
PRIMARY KEY -> file-offset map in memory is acceptable").  Payloads are
streamed from disk on demand, and vectors are stored as raw little-endian
float32 (4 bytes per dimension) instead of text.

Transactions
------------

At most one explicit transaction is active at a time.  Writes inside a
transaction are buffered in a small in-memory overlay and applied to the
table logs at ``COMMIT`` time; ``ROLLBACK`` simply discards the overlay, so
the pre-transaction state is restored exactly.  Statements outside an
explicit transaction are auto-committed (one commit per statement).

Public API (see ``docs/CONTRACT.md`` section 4)::

    Storage(data_dir=None)
        .open() / .close() / .flush()
        .create_table(name, columns, primary_key, vector_column)
        .drop_table(name)
        .get_table(name) -> Table
        .list_tables() -> list[str]
        .insert_rows(table, rows)
        .update_rows(table, pk, new_values)
        .delete_rows(table, pks)
        .scan(table) -> Iterator[Row]
        .get(table, pk) -> Row | None
        .begin() / .commit() / .rollback()

Additional helpers (not part of the frozen contract but useful for the
executor): :meth:`Storage.table_names`, :meth:`Storage.in_transaction`,
:meth:`Storage.get_row` (alias of :meth:`get`) and the module level
functions :func:`open_storage` and :func:`default_data_dir`.

Documented deviations from the contract
---------------------------------------

* ``Storage.__init__`` accepts an optional ``data_dir``; when omitted the
  ``MVD_DATA_DIR`` environment variable is used, defaulting to ``/data``.
* ``Storage.get(table, pk)`` returns ``None`` when no row with that primary
  key exists (instead of raising).  The executor can therefore use it for
  duplicate-key detection.
* ``Storage.commit()`` / ``Storage.rollback()`` are no-ops when no explicit
  transaction is open (they only flush), so the executor may call them
  unconditionally.
* The durable log of a table lives in ``tables/<id>.dat`` (the numeric table
  id is stored in ``catalog.json``) instead of ``tables/<name>.dat``; the
  numeric id keeps ``DROP TABLE`` + re-create correct.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import zlib

from .errors import (
    ER_BAD_FIELD_ERROR,
    ER_BAD_TABLE,
    ER_DUP_ENTRY,
    ER_NO_DEFAULT_VALUE,
    ER_NOT_SUPPORTED_YET,
    ER_TABLE_EXISTS,
    SQLError,
    bad_field,
    duplicate_key,
    no_such_table,
)

__all__ = [
    "Column",
    "Row",
    "Storage",
    "Table",
    "default_data_dir",
    "open_storage",
]


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

CATALOG_NAME = "catalog.json"
WAL_NAME = "wal.log"
TABLES_DIRNAME = "tables"
CATALOG_VERSION = 1

# row-log opcodes
OP_INSERT = 0
OP_UPDATE = 1
OP_DELETE = 2
OP_MARKER = 3

# value tags
TAG_NULL = 0
TAG_INT = 1
TAG_STR = 2
TAG_VEC = 3

# wal.log record types
WAL_COMMIT = 1

_FRAME_HDR = struct.Struct("<II")
_U64 = struct.Struct("<Q")
_MARKER_PAYLOAD = struct.Struct("<BQ")  # opcode + txn id

# how many rows the streaming scan may buffer while reordering
_REORDER_BUFFER_LIMIT = 4096

# default budget for the decoded vector cache (see Storage.cached_vectors)
_DEFAULT_VECTOR_CACHE_MB = 64

# A decoded vector is a tuple of Python floats, which really costs
# ``8 * dim`` bytes of pointers plus a 24-byte float object per element.
# Accounting only ``dim * 4`` (the on-disk size) would undercount the real
# footprint about 8x and let the cache blow the memory budget.
_VECTOR_ENTRY_OVERHEAD = 56     # tuple header
_VECTOR_ELEMENT_BYTES = 32      # 8-byte slot + 24-byte float object


def _vector_entry_bytes(vector):
    if vector is None:
        return _VECTOR_ENTRY_OVERHEAD
    return _VECTOR_ENTRY_OVERHEAD + _VECTOR_ELEMENT_BYTES * len(vector)


def _vector_cache_limit():
    """Byte budget of the decoded vector cache.

    Override with ``MVD_VECTOR_CACHE_MB`` (0 disables the cache entirely).
    """
    raw = os.environ.get("MVD_VECTOR_CACHE_MB")
    if raw is None:
        return _DEFAULT_VECTOR_CACHE_MB * 1024 * 1024
    try:
        megabytes = float(raw)
    except (TypeError, ValueError):
        megabytes = float(_DEFAULT_VECTOR_CACHE_MB)
    if megabytes <= 0:
        return 0
    return int(megabytes * 1024 * 1024)


class _ReorderOverflow(Exception):
    """Internal signal: the reorder buffer is full."""


def default_data_dir():
    """Return the data directory to use when none is given explicitly."""
    return os.environ.get("MVD_DATA_DIR") or "/data"


def _log(message):
    """Diagnostics go to stderr only (stdout carries results exclusively)."""
    try:
        sys.stderr.write("storage: {}\n".format(message))
        sys.stderr.flush()
    except Exception:  # pragma: no cover - stderr must never break the server
        pass


# --------------------------------------------------------------------------
# value helpers
# --------------------------------------------------------------------------


def _encode_value(column, value):
    """Encode a single column value into its on-disk representation."""
    if value is None:
        return bytes((TAG_NULL,))
    kind = column.type_name
    if kind == "BIGINT":
        return bytes((TAG_INT,)) + struct.pack("<q", int(value))
    if kind == "VARCHAR":
        raw = str(value).encode("utf-8")
        return bytes((TAG_STR,)) + struct.pack("<I", len(raw)) + raw
    if kind == "VECTOR":
        vec = [float(x) for x in value]
        if vec:
            body = struct.pack("<%df" % len(vec), *vec)
        else:
            body = b""
        return bytes((TAG_VEC,)) + struct.pack("<I", len(vec)) + body
    # Unknown declared type: fall back to the python type of the value.
    if isinstance(value, bool):
        return bytes((TAG_INT,)) + struct.pack("<q", int(value))
    if isinstance(value, int):
        return bytes((TAG_INT,)) + struct.pack("<q", value)
    if isinstance(value, float):
        return bytes((TAG_INT,)) + struct.pack("<q", int(value))
    if isinstance(value, (tuple, list)):
        vec = [float(x) for x in value]
        body = struct.pack("<%df" % len(vec), *vec) if vec else b""
        return bytes((TAG_VEC,)) + struct.pack("<I", len(vec)) + body
    raw = str(value).encode("utf-8")
    return bytes((TAG_STR,)) + struct.pack("<I", len(raw)) + raw


def _decode_value(column, buf, pos):
    """Decode one value from ``buf`` starting at ``pos``; return (value, pos)."""
    tag = buf[pos]
    pos += 1
    if tag == TAG_NULL:
        return None, pos
    if tag == TAG_INT:
        value = struct.unpack_from("<q", buf, pos)[0]
        return value, pos + 8
    if tag == TAG_STR:
        length = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        raw = bytes(buf[pos:pos + length])
        pos += length
        return raw.decode("utf-8"), pos
    if tag == TAG_VEC:
        dim = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        body = bytes(buf[pos:pos + 4 * dim])
        pos += 4 * dim
        return struct.unpack("<%df" % dim, body) if dim else (), pos
    raise SQLError(ER_BAD_FIELD_ERROR, "corrupt value in table log", "HY000")


def _value_span(buf, pos):
    """Return the offset just after the value that starts at ``pos``.

    Only the tag and the length prefix are read, so a value can be skipped
    without decoding it.  This is what makes the column selective scan
    cheap: unwanted columns are walked over, not decoded.
    """
    tag = buf[pos]
    pos += 1
    if tag == TAG_NULL:
        return pos
    if tag == TAG_INT:
        return pos + 8
    if tag == TAG_STR:
        length = struct.unpack_from("<I", buf, pos)[0]
        return pos + 4 + length
    if tag == TAG_VEC:
        dim = struct.unpack_from("<I", buf, pos)[0]
        return pos + 4 + 4 * dim
    raise SQLError(ER_BAD_FIELD_ERROR, "corrupt value in table log", "HY000")


# --------------------------------------------------------------------------
# schema objects
# --------------------------------------------------------------------------


class Column:
    """A single column of a table.

    Exposes ``name``, ``type_name``, ``length``, ``vector_dim``,
    ``primary_key`` and ``not_null``.
    """

    __slots__ = (
        "name",
        "type_name",
        "length",
        "vector_dim",
        "primary_key",
        "not_null",
    )

    def __init__(
        self,
        name,
        type_name,
        length=None,
        vector_dim=None,
        primary_key=False,
        not_null=False,
    ):
        self.name = name
        self.type_name = type_name
        self.length = length
        self.vector_dim = vector_dim
        self.primary_key = primary_key
        self.not_null = not_null

    # convenience aliases -------------------------------------------------
    @property
    def type(self):
        return self.type_name

    @property
    def dim(self):
        return self.vector_dim

    def __getitem__(self, key):
        if key in ("name", "type_name", "length", "vector_dim"):
            return getattr(self, key)
        raise KeyError(key)

    def __repr__(self):  # pragma: no cover - debugging helper
        return "Column({!r}, {!r}, length={!r}, vector_dim={!r})".format(
            self.name, self.type_name, self.length, self.vector_dim
        )


def _normalize_type(type_name, length, vector_dim):
    """Return ``(TYPE_NAME, length, vector_dim)`` for a declared type."""
    text = (type_name or "").strip()
    upper = text.upper()
    extra = None
    if "(" in upper and ")" in upper:
        head, _, rest = upper.partition("(")
        upper = head.strip()
        extra = rest.rstrip(")").strip()
    if upper == "VARCHAR":
        if length is None and extra:
            length = int(extra)
        return "VARCHAR", length, None
    if upper == "VECTOR":
        if vector_dim is None and extra:
            vector_dim = int(extra)
        return "VECTOR", None, vector_dim
    if upper in ("BIGINT", "INT", "INTEGER", "LONGLONG"):
        return "BIGINT", None, None
    return upper, length, vector_dim


def _normalize_columns(columns):
    """Build :class:`Column` objects from whatever the executor hands us.

    Accepted shapes: :class:`Column`, AST ``ColumnDef`` objects, mappings
    and ``(name, type[, length[, dim]])`` tuples.
    """
    out = []
    for col in columns or ():
        if isinstance(col, Column):
            kind, length, dim = _normalize_type(
                col.type_name, col.length, col.vector_dim
            )
            out.append(
                Column(
                    col.name,
                    kind,
                    length,
                    dim,
                    col.primary_key,
                    col.not_null,
                )
            )
            continue
        if isinstance(col, dict):
            name = col.get("name")
            type_name = (
                col.get("type_name")
                or col.get("type")
                or col.get("data_type")
                or col.get("column_type")
            )
            length = col.get("length")
            dim = col.get("vector_dim", col.get("dim"))
            pk_flag = bool(col.get("primary_key", False))
            nn_flag = bool(col.get("not_null", False))
        elif isinstance(col, (tuple, list)):
            name = col[0]
            type_name = col[1] if len(col) > 1 else "BIGINT"
            length = col[2] if len(col) > 2 else None
            dim = col[3] if len(col) > 3 else None
            pk_flag = False
            nn_flag = False
        elif hasattr(col, "name") and hasattr(col, "type_name"):
            name = col.name
            type_name = col.type_name
            length = getattr(col, "length", None)
            dim = getattr(col, "vector_dim", None)
            pk_flag = bool(getattr(col, "primary_key", False))
            nn_flag = bool(getattr(col, "not_null", False))
        else:
            raise SQLError(
                ER_BAD_FIELD_ERROR, "cannot interpret column {!r}".format(col)
            )
        kind, length, dim = _normalize_type(type_name, length, dim)
        out.append(Column(name, kind, length, dim, pk_flag, nn_flag))
    return out


class Table:
    """Schema of a table plus the helpers the executor needs."""

    __slots__ = (
        "name",
        "id",
        "columns",
        "column_names",
        "primary_key",
        "primary_key_index",
        "vector_column",
        "vector_dim",
        "_extra",
    )

    def __init__(self, name, table_id, columns, primary_key, vector_column):
        self.name = name
        self.id = table_id
        self.columns = columns
        self.column_names = [c.name for c in columns]
        self.primary_key = primary_key
        self.vector_column = vector_column
        self.primary_key_index = None
        self.vector_dim = None
        for idx, col in enumerate(columns):
            if primary_key is not None and col.name == primary_key:
                self.primary_key_index = idx
            if vector_column is not None and col.name == vector_column:
                self.vector_dim = col.vector_dim
        # dict-like access for convenience
        self._extra = {
            "name": self.name,
            "id": self.id,
            "columns": self.columns,
            "column_names": self.column_names,
            "primary_key": self.primary_key,
            "vector_column": self.vector_column,
            "vector_dim": self.vector_dim,
        }

    # -- helpers ----------------------------------------------------------
    def get_column(self, name):
        for col in self.columns:
            if col.name == name:
                return col
        lowered = name.lower()
        for col in self.columns:
            if col.name.lower() == lowered:
                return col
        return None

    @property
    def primary_key_column(self):
        if self.primary_key_index is None:
            return None
        return self.columns[self.primary_key_index]

    @property
    def vector_column_index(self):
        if self.vector_column is None:
            return None
        for idx, col in enumerate(self.columns):
            if col.name == self.vector_column:
                return idx
        return None

    def __getitem__(self, key):
        return self._extra[key]

    def __contains__(self, key):
        return key in self._extra

    def get(self, key, default=None):
        return self._extra.get(key, default)

    def __repr__(self):  # pragma: no cover - debugging helper
        return "Table({!r}, id={!r}, columns={!r}, pk={!r})".format(
            self.name, self.id, self.column_names, self.primary_key
        )


class Row(list):
    """A row of a table.

    The row behaves both like a list (positional access, ``row[pk_index]``,
    in the table's declared column order) and like a mapping
    (``row["col"]``, ``row.keys()``, ``row.items()``).  It also carries the
    primary key in the ``pk`` attribute.
    """

    def __init__(self, values, pk, data=None, column_names=None):
        super().__init__(values)
        self.pk = pk
        if data is None:
            names = column_names or ()
            data = dict(zip(names, values))
        self._data = dict(data)

    # The mapping view is authoritative for column lookups.
    @property
    def data(self):
        return self._data

    def __getitem__(self, key):
        if isinstance(key, int):
            return list.__getitem__(self, key)
        if isinstance(key, slice):
            return list.__getitem__(self, key)
        return self._data[key]

    def get(self, key, default=None):
        if isinstance(key, int):
            try:
                return list.__getitem__(self, key)
            except IndexError:
                return default
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __contains__(self, key):
        if isinstance(key, str):
            return key in self._data
        return list.__contains__(self, key)

    def __iter__(self):
        # list semantics: iterating a row yields the column values in the
        # table's declared column order.
        return list.__iter__(self)

    def __len__(self):
        return list.__len__(self)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self.pk == other.pk and self._data == other._data
        if isinstance(other, dict):
            return self._data == other
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):  # pragma: no cover - debugging helper
        return "Row(pk={!r}, {!r})".format(self.pk, self._data)

    def to_dict(self):
        return dict(self._data)

    def copy(self):
        return Row(list(self), self.pk, dict(self._data))


# --------------------------------------------------------------------------
# per table log file
# --------------------------------------------------------------------------


class _TableLog:
    """Append-only row log of a single table."""

    __slots__ = ("path", "fd", "size", "index", "_sorted_pks")

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        self.size = os.fstat(self.fd).st_size
        # primary key -> offset of the latest version of that row
        self.index = {}
        self._sorted_pks = None

    def sorted_pks(self):
        """The visible primary keys in ascending order (cached)."""
        if self._sorted_pks is None:
            self._sorted_pks = sorted(self.index)
        return self._sorted_pks

    def monotonic_pks(self):
        """True when the log's row order already equals primary key order.

        That is the common case for an append-only table loaded in key
        order; a scan can then stream straight out of the file without any
        reordering buffer.
        """
        pks = self.sorted_pks()
        index = self.index
        previous = -1
        for pk in pks:
            offset = index[pk]
            if offset <= previous:
                return False
            previous = offset
        return True

    def append(self, frame):
        """Append a framed record; returns the offset it was written at."""
        offset = self.size
        os.write(self.fd, frame)
        self.size += len(frame)
        self._sorted_pks = None
        return offset

    def sync(self):
        os.fsync(self.fd)

    def truncate(self, length):
        if length < self.size:
            os.ftruncate(self.fd, length)
            self.size = length
            self._sorted_pks = None

    def read_frame(self, offset):
        """Return ``(payload, frame_length)`` at ``offset`` or ``None``."""
        # fast path: one syscall is enough for a typical row
        blob = os.pread(self.fd, 4096, offset)
        if len(blob) < 8:
            return None
        length, crc = _FRAME_HDR.unpack_from(blob, 0)
        if length > (1 << 31):
            return None
        if len(blob) >= 8 + length:
            payload = blob[8:8 + length]
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                return None
            return payload, 8 + length
        header = os.pread(self.fd, 8, offset)
        if len(header) < 8:
            return None
        length, crc = _FRAME_HDR.unpack(header)
        payload = os.pread(self.fd, length, offset + 8)
        if len(payload) != length:
            return None
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            return None
        return payload, 8 + length

    def read_frames(self, offsets, window=1 << 20):
        """Return ``{offset: payload}`` for the given frame offsets.

        Frames that live close together are fetched with a single read, so
        a batch of scattered offsets usually costs only one or two syscalls.
        """
        result = {}
        if not offsets:
            return result
        lo = offsets[0]
        hi = offsets[-1]
        if hi - lo <= window:
            blob = os.pread(self.fd, hi - lo + 65536, lo)
            wanted = set(offsets)
            pos = 0
            while pos + 8 <= len(blob):
                length, crc = _FRAME_HDR.unpack_from(blob, pos)
                if length > (1 << 31):
                    break
                if pos + 8 + length > len(blob):
                    break
                frame_offset = lo + pos
                if frame_offset in wanted:
                    payload = blob[pos + 8:pos + 8 + length]
                    if zlib.crc32(payload) & 0xFFFFFFFF == crc:
                        result[frame_offset] = payload
                pos += 8 + length
                if frame_offset >= hi:
                    break
        # anything still missing is fetched individually
        for offset in offsets:
            if offset in result:
                continue
            record = self.read_frame(offset)
            if record is not None:
                result[offset] = record[0]
        return result

    def iter_headers(self):
        """Yield ``(offset, payload_length)`` for every complete frame."""
        offset = 0
        size = self.size
        fd = self.fd
        while offset + 8 <= size:
            header = os.pread(fd, 8, offset)
            if len(header) < 8:
                break
            length, _crc = _FRAME_HDR.unpack(header)
            if offset + 8 + length > size:
                break
            yield offset, length
            offset += 8 + length

    def close(self):
        try:
            os.close(self.fd)
        except OSError:  # pragma: no cover
            pass

    # -- sequential reading ------------------------------------------------
    def iter_payloads(self, limit=None, chunk_size=262144, verify=True):
        """Yield ``(offset, payload)`` for every intact frame, sequentially.

        The file is read in large chunks (256 KiB by default) instead of one
        ``pread`` syscall per row.  ``limit`` is the exclusive end offset of
        the region to read (the committed length of the log).

        ``verify`` controls the CRC32 check of each frame.  Verification is
        what makes a torn record detectable, so it is on by default; it can
        be switched off for pure performance scans of already validated
        data.
        """
        end = self.size if limit is None else min(limit, self.size)
        fd = self.fd
        base = 0          # absolute offset of buf[0]
        buf = bytearray()
        pos = 0           # cursor inside buf
        while True:
            # drop the already consumed prefix to keep the buffer small
            if pos:
                del buf[:pos]
                base += pos
                pos = 0
            # refill
            want = chunk_size
            if base + len(buf) + want > end:
                want = end - (base + len(buf))
            if want > 0:
                chunk = os.pread(fd, want, base + len(buf))
                if not chunk:
                    break
                buf += chunk
            # parse as many complete frames as the buffer holds
            progressed = False
            while pos + 8 <= len(buf):
                length, crc = _FRAME_HDR.unpack_from(buf, pos)
                if length > (1 << 31):
                    return
                if pos + 8 + length > len(buf):
                    break  # frame continues in the next chunk
                payload = bytes(buf[pos + 8:pos + 8 + length])
                if verify and zlib.crc32(payload) & 0xFFFFFFFF != crc:
                    return
                frame_offset = base + pos
                pos += 8 + length
                progressed = True
                yield frame_offset, payload
            if pos >= len(buf) and base + len(buf) >= end:
                return
            if not progressed and base + len(buf) >= end:
                return


def _frame(payload):
    return _FRAME_HDR.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


class Storage:
    """Durable storage engine (see module docstring)."""

    def __init__(self, data_dir=None):
        self.data_dir = os.path.abspath(
            data_dir if data_dir is not None else default_data_dir()
        )
        self._tables_dir = os.path.join(self.data_dir, TABLES_DIRNAME)
        self._catalog_path = os.path.join(self.data_dir, CATALOG_NAME)
        self._wal_path = os.path.join(self.data_dir, WAL_NAME)

        self._tables = {}       # name -> Table
        self._by_id = {}        # table id -> Table
        self._logs = {}         # table id -> _TableLog
        self._next_id = 1

        self._wal_fd = None
        self._wal_size = 0

        # vector index metadata: index name -> {table, column}
        self._index_meta = {}
        # live VectorIndex objects: table name -> index object
        self.vector_indexes = {}

        # decoded vector cache: (table id, column name) -> {pk: vector tuple}
        self._vector_cache = {}
        self._vector_cache_pks = {}     # key -> sorted list of pks
        self._vector_cache_sizes = {}   # key -> estimated bytes
        self._vector_cache_bytes = 0
        self._vector_cache_limit = _vector_cache_limit()

        # per table mutation counter: table id -> int, bumped whenever the
        # visible contents change (see Storage.data_version)
        self._versions = {}
        self._version_counter = 0

        # committed length per table id: (txn_id, length)
        self._committed = {}

        self._txn_active = False
        self._overlay = {}      # table id -> {pk: (kind, payload)}
        self._txn_counter = 0
        self._next_txn_id = 1

        self._opened = False
        self._closed = False

    # -- lifecycle --------------------------------------------------------
    def open(self):
        """Open (or create) the data directory and recover the state."""
        if self._opened:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self._tables_dir, exist_ok=True)
        self._load_catalog()
        self._open_wal()
        self._recover()
        self._opened = True

    def _ensure_open(self):
        if self._closed:
            raise SQLError(
                ER_BAD_TABLE, "the storage has been closed", "HY000"
            )
        if not self._opened:
            self.open()

    def close(self):
        """Flush, fsync and close everything (clean shutdown path)."""
        if self._closed:
            return
        try:
            if self._txn_active:
                self.rollback()
            self.flush()
        finally:
            for log in self._logs.values():
                log.close()
            self._logs.clear()
            if self._wal_fd is not None:
                try:
                    os.close(self._wal_fd)
                except OSError:  # pragma: no cover
                    pass
                self._wal_fd = None
            self._opened = False
            self._closed = True

    def flush(self):
        """fsync the data files and the WAL."""
        if self._closed:
            return
        self._ensure_open()
        for log in self._logs.values():
            log.sync()
        if self._wal_fd is not None:
            os.fsync(self._wal_fd)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- catalog ----------------------------------------------------------
    def _load_catalog(self):
        self._tables = {}
        self._by_id = {}
        self._next_id = 1
        try:
            with open(self._catalog_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            _log("catalog unreadable ({}), starting empty".format(exc))
            return
        tables = data.get("tables") or {}
        self._index_meta = dict(data.get("indexes") or {})
        self._next_id = int(data.get("next_table_id") or 1)
        for name, spec in tables.items():
            columns = _normalize_columns(spec.get("columns") or [])
            table = Table(
                name,
                int(spec.get("id")),
                columns,
                spec.get("primary_key"),
                spec.get("vector_column"),
            )
            self._tables[name] = table
            self._by_id[table.id] = table
            if table.id >= self._next_id:
                self._next_id = table.id + 1

    def _write_catalog(self):
        tables = {}
        for name, table in self._tables.items():
            tables[name] = {
                "id": table.id,
                "columns": [
                    {
                        "name": col.name,
                        "type_name": col.type_name,
                        "length": col.length,
                        "vector_dim": col.vector_dim,
                        "primary_key": col.primary_key,
                        "not_null": col.not_null,
                    }
                    for col in table.columns
                ],
                "primary_key": table.primary_key,
                "vector_column": table.vector_column,
            }
        payload = json.dumps(
            {
                "version": CATALOG_VERSION,
                "next_table_id": self._next_id,
                "tables": tables,
                "indexes": self._index_meta,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        tmp = self._catalog_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._catalog_path)
        self._fsync_dir(self.data_dir)

    @staticmethod
    def _fsync_dir(path):
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:  # pragma: no cover
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(fd)

    # -- WAL --------------------------------------------------------------
    def _open_wal(self):
        fd = os.open(self._wal_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        self._wal_fd = fd
        self._wal_size = os.fstat(fd).st_size

    def _wal_iter_records(self):
        """Yield ``(offset, payload)`` of every intact WAL record."""
        fd = self._wal_fd
        size = self._wal_size
        offset = 0
        while offset + 8 <= size:
            header = os.pread(fd, 8, offset)
            if len(header) < 8:
                break
            length, crc = _FRAME_HDR.unpack(header)
            if length > (1 << 31) or offset + 8 + length > size:
                break
            payload = os.pread(fd, length, offset + 8)
            if len(payload) != length:
                break
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                break
            yield offset, payload
            offset += 8 + length

    def _wal_write(self, payload):
        frame = _frame(payload)
        os.write(self._wal_fd, frame)
        self._wal_size += len(frame)

    # -- recovery ---------------------------------------------------------
    def _recover(self):
        """Replay the WAL and rebuild the in-memory indexes.

        Idempotent: only the committed prefix of every log is used and the
        log is truncated back to that prefix.
        """
        # 1. committed lengths announced by the global WAL
        wal_committed = {}
        max_txn = 0
        valid_end = 0
        for offset, payload in self._wal_iter_records():
            valid_end = offset + 8 + len(payload)
            if not payload:
                continue
            if payload[0] != WAL_COMMIT:
                continue
            try:
                txn_id, count = struct.unpack_from("<QI", payload, 1)
            except struct.error:  # pragma: no cover - corrupt but framed
                continue
            pos = 13
            for _ in range(count):
                if pos + 24 > len(payload):
                    break
                table_id, txn_id, length = struct.unpack_from("<QQQ", payload, pos)
                pos += 24
                current = wal_committed.get(table_id)
                if current is None or txn_id >= current[0]:
                    wal_committed[table_id] = (txn_id, length)
            if txn_id > max_txn:
                max_txn = txn_id

        # 2. per table: find the local commit marker (single table commits)
        for table in list(self._tables.values()):
            log = self._log_for(table)
            local = None
            for offset, length in log.iter_headers():
                if length < 1:
                    continue
                head = os.pread(log.fd, min(length, 9), offset + 8)
                if not head:
                    break
                if head[0] != OP_MARKER:
                    continue
                if length < 9:
                    continue
                txn_id = struct.unpack_from("<Q", head, 1)[0]
                local = (txn_id, offset + 8 + length)
            committed = local
            announced = wal_committed.get(table.id)
            if announced is not None:
                if committed is None or announced[0] >= committed[0]:
                    committed = announced
            if committed is None:
                # No commit marker and no WAL entry: nothing was ever
                # committed for this table, so the whole log is an
                # uncommitted suffix and must be rolled back.
                committed = (0, 0)
                if log.size:
                    _log(
                        "table {!r} has no commit marker; rolling back "
                        "{} uncommitted bytes".format(table.name, log.size)
                    )
            txn_id, length = committed
            if log.size > length:
                log.truncate(length)
            elif log.size < length:
                _log(
                    "table {!r} is shorter than its committed length "
                    "({} < {}); truncating the committed length".format(
                        table.name, log.size, length
                    )
                )
                committed = (txn_id, log.size)
            self._committed[table.id] = committed
            if txn_id > max_txn:
                max_txn = txn_id
            self._rebuild_index(table)

        # 3. drop a torn WAL tail
        if valid_end < self._wal_size:
            os.ftruncate(self._wal_fd, valid_end)
            self._wal_size = valid_end

        self._next_txn_id = max_txn + 1
        self._compact_wal()

    def _rebuild_index(self, table):
        """Scan the committed prefix of a table log and build the pk index."""
        log = self._log_for(table)
        index = log.index
        index.clear()
        for offset, length in log.iter_headers():
            if length < 9:
                continue
            head = os.pread(log.fd, 9, offset + 8)
            if len(head) < 9:
                break
            opcode = head[0]
            if opcode == OP_MARKER:
                continue
            pk = struct.unpack_from("<q", head, 1)[0]
            if opcode == OP_DELETE:
                index.pop(pk, None)
            else:
                index[pk] = offset

    def _compact_wal(self):
        """Rewrite the WAL so that it holds a single checkpoint record."""
        if self._wal_fd is None:
            return
        entries = []
        for table_id, (txn_id, length) in sorted(self._committed.items()):
            if table_id in self._by_id:
                entries.append((table_id, txn_id, length))
        if not entries and self._wal_size == 0:
            return
        payload = bytearray()
        payload.append(WAL_COMMIT)
        max_txn = 0
        payload += struct.pack("<QI", 0, len(entries))
        for table_id, txn_id, length in entries:
            payload += struct.pack("<QQQ", table_id, txn_id, length)
            max_txn = max(max_txn, txn_id)
        payload = bytes(payload)
        # rewrite with the correct txn id
        payload = bytearray(payload)
        struct.pack_into("<Q", payload, 1, max_txn)
        tmp = self._wal_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            frame = _frame(bytes(payload))
            os.write(fd, frame)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._wal_path)
        self._fsync_dir(self.data_dir)
        os.close(self._wal_fd)
        self._open_wal()

    # -- decoded vector cache ---------------------------------------------
    def cached_vectors(self, table, column=None):
        """Return the decoded vector cache of ``table``, or ``None``.

        The mapping is ``{primary key: tuple of float}`` for every visible
        row - exactly the representation :meth:`scan_column` yields for a
        ``VECTOR`` column.  The live dict is returned (no copy), so the
        executor can iterate it directly.

        ``column`` selects the vector column; it defaults to the table's
        declared vector column.  A table with several vector columns needs
        the explicit name.

        ``None`` means "no complete cache": the caller must scan instead.
        The cache is only a fast path - it never changes which rows are
        visible, and it is dropped whenever it could become stale.
        """
        self._ensure_open()
        tbl = self._resolve_table(table)
        if self._txn_active:
            # an open transaction may change the table at any moment
            return None
        name = self._cached_vector_column_name(tbl, column)
        if name is None:
            return None
        return self._vector_cache.get((tbl.id, name))

    def cached_vector_pks(self, table, column=None):
        """The primary keys of :meth:`cached_vectors`, ascending."""
        self._ensure_open()
        tbl = self._resolve_table(table)
        if self._txn_active:
            return None
        name = self._cached_vector_column_name(tbl, column)
        if name is None:
            return None
        key = (tbl.id, name)
        cached = self._vector_cache.get(key)
        if cached is None:
            return None
        pks = self._vector_cache_pks.get(key)
        if pks is None:
            pks = sorted(cached)
            self._vector_cache_pks[key] = pks
        return pks

    @staticmethod
    def _cached_vector_column_name(table, column=None):
        """The vector column the cache of ``table`` refers to."""
        if column is None:
            return table.vector_column
        col = table.get_column(column)
        if col is None or col.type_name != "VECTOR":
            return None
        return col.name

    def _vector_column(self, table):
        """The VECTOR column of ``table`` (or ``None``)."""
        for col in table.columns:
            if col.type_name == "VECTOR":
                return col
        return None

    def _vector_cache_drop(self, table_id, column=None):
        """Drop the cache of one table (or one table/column pair)."""
        if column is None:
            keys = [k for k in self._vector_cache if k[0] == table_id]
        else:
            keys = [(table_id, column)]
        for key in keys:
            self._vector_cache_bytes -= self._vector_cache_sizes.pop(key, 0)
            self._vector_cache.pop(key, None)
            self._vector_cache_pks.pop(key, None)
        if self._vector_cache_bytes < 0:
            self._vector_cache_bytes = 0

    def _vector_cache_store(self, table, vectors, pks, column):
        """Install a complete cache for one column if it fits the budget."""
        if not vectors or self._vector_cache_limit <= 0:
            return
        size = sum(_vector_entry_bytes(v) for v in vectors.values())
        if size > self._vector_cache_limit - self._vector_cache_bytes:
            # too big: rather than a partial (and possibly misleading)
            # cache, keep the column uncached
            self._vector_cache_drop(table.id, column)
            return
        key = (table.id, column)
        self._vector_cache_drop(table.id, column)
        self._vector_cache[key] = vectors
        self._vector_cache_pks[key] = pks
        self._vector_cache_sizes[key] = size
        self._vector_cache_bytes += size

    def _vector_cache_update(self, table, pk, vector, column):
        """Update the cached vector of one row (cache must be complete)."""
        key = (table.id, column)
        cached = self._vector_cache.get(key)
        if cached is None:
            return
        old = cached.get(pk)
        if old is not None:
            delta = _vector_entry_bytes(vector) - _vector_entry_bytes(old)
        else:
            delta = _vector_entry_bytes(vector)
        if self._vector_cache_bytes + delta > self._vector_cache_limit:
            self._vector_cache_drop(table.id, column)
            return
        cached[pk] = vector
        self._vector_cache_bytes += delta
        self._vector_cache_sizes[key] = self._vector_cache_sizes.get(key, 0) + delta
        # invalidate the sorted pk list lazily; re-sorting it per row would
        # make bulk inserts quadratic
        self._vector_cache_pks[key] = None

    def _vector_cache_remove(self, table, pk, column):
        """Drop one row from the cached vectors (cache must be complete)."""
        key = (table.id, column)
        cached = self._vector_cache.get(key)
        if cached is None:
            return
        old = cached.pop(pk, None)
        if old is None:
            return
        delta = _vector_entry_bytes(old)
        self._vector_cache_bytes -= delta
        self._vector_cache_sizes[key] = self._vector_cache_sizes.get(key, 0) - delta
        self._vector_cache_pks[key] = None
        self._vector_cache_bytes = max(self._vector_cache_bytes, 0)

    def _vector_cache_invalidate(self, table):
        """Invalidate the whole cache of ``table``."""
        self._vector_cache_drop(table.id)

    # -- mutation counter -------------------------------------------------
    def data_version(self, table):
        """A counter that changes whenever the visible contents of ``table``
        change.

        Any ``INSERT`` / ``UPDATE`` / ``DELETE`` - committed or merely
        buffered in an open transaction - bumps it, and so do ``COMMIT`` and
        ``ROLLBACK`` for every table the transaction touched, because the
        visible content of those tables differs across the transaction
        boundary.

        It is a plain integer read and never changes on a pure scan, so the
        executor can use it as the invalidation key of its own caches::

            version = storage.data_version(table)
            # ... reuse a cached scan ...
            if storage.data_version(table) != version:
                cache = None

        The counter is not persisted: after :meth:`open` it restarts at a
        fresh value, which is fine because any in-memory cache is gone too.
        """
        self._ensure_open()
        tbl = self._resolve_table(table)
        return self._versions.get(tbl.id, 0)

    def _bump_version(self, table_id):
        """Monotonically bump the mutation counter of one table."""
        self._version_counter += 1
        self._versions[table_id] = self._version_counter

    def _bump_versions(self, table_ids):
        for table_id in table_ids:
            self._bump_version(table_id)

    def _apply_index_changes_to_vector_cache(self, index_changes):
        """Keep the decoded vector cache in step with committed writes."""
        if not self._vector_cache:
            return
        for table_id, changes in index_changes.items():
            table = self._by_id.get(table_id)
            if table is None:
                continue
            keys = [k for k in self._vector_cache if k[0] == table.id]
            if not keys:
                continue
            for pk, (kind, vectors) in changes.items():
                for key in keys:
                    column = key[1]
                    if kind == "del":
                        self._vector_cache_remove(table, pk, column)
                        continue
                    vector = vectors.get(column) if vectors else None
                    if vector is None:
                        # no vector value available: drop the cache to stay
                        # safe
                        self._vector_cache_drop(table.id, column)
                    else:
                        self._vector_cache_update(table, pk, vector, column)

    # -- table helpers ----------------------------------------------------
    def _log_for(self, table):
        log = self._logs.get(table.id)
        if log is None:
            path = os.path.join(self._tables_dir, "{}.dat".format(table.id))
            log = _TableLog(path)
            self._logs[table.id] = log
        return log

    def _resolve_table(self, table):
        self._ensure_open()
        if isinstance(table, Table):
            if table.name not in self._tables:
                # a Table object built by the caller
                return table
            return self._tables[table.name]
        if isinstance(table, str):
            return self.get_table(table)
        raise SQLError(ER_BAD_TABLE, "invalid table reference {!r}".format(table))

    # -- schema API -------------------------------------------------------
    def create_table(self, name, columns, primary_key, vector_column):
        """Create a table and make it durable."""
        self._ensure_open()
        if name in self._tables:
            raise SQLError(
                ER_TABLE_EXISTS, "Table '{}' already exists".format(name)
            )
        cols = _normalize_columns(columns)
        if not cols:
            raise SQLError(
                ER_NOT_SUPPORTED_YET, "a table needs at least one column"
            )
        names = set()
        for col in cols:
            if col.name in names:
                raise SQLError(
                    ER_BAD_FIELD_ERROR, "duplicate column name '{}'".format(col.name)
                )
            names.add(col.name)
        if primary_key is None:
            raise SQLError(
                ER_NOT_SUPPORTED_YET, "a table needs a PRIMARY KEY column"
            )
        pk_col = None
        for col in cols:
            if col.name == primary_key:
                pk_col = col
                break
        if pk_col is None:
            raise SQLError(
                ER_BAD_FIELD_ERROR,
                "Unknown column '{}' in 'primary key'".format(primary_key),
            )
        if pk_col.type_name != "BIGINT":
            raise SQLError(
                ER_NOT_SUPPORTED_YET, "the PRIMARY KEY must be a BIGINT column"
            )
        pk_col.primary_key = True
        if vector_column is not None and vector_column not in names:
            raise SQLError(
                ER_BAD_FIELD_ERROR,
                "Unknown column '{}' in 'vector column'".format(vector_column),
            )

        table_id = self._next_id
        self._next_id += 1
        table = Table(name, table_id, cols, primary_key, vector_column)
        self._tables[name] = table
        self._by_id[table_id] = table
        self._log_for(table)
        self._committed[table_id] = (0, 0)
        self._bump_version(table_id)
        self._write_catalog()
        self.flush()

    def drop_table(self, name):
        """Drop a table and make the change durable."""
        self._ensure_open()
        table = self._tables.get(name)
        if table is None:
            raise no_such_table(name)
        if self._txn_active and table.id in self._overlay:
            self._overlay.pop(table.id, None)
        log = self._logs.pop(table.id, None)
        if log is not None:
            log.close()
        try:
            os.unlink(os.path.join(self._tables_dir, "{}.dat".format(table.id)))
        except OSError:
            pass
        del self._tables[name]
        self._by_id.pop(table.id, None)
        self._committed.pop(table.id, None)
        self.vector_indexes.pop(name, None)
        self._vector_cache_drop(table.id)
        # the id of a dropped table is never reused, and the global counter
        # only moves forward, so a re-created table always gets a version
        # that differs from the dropped one
        self._versions.pop(table.id, None)
        self._version_counter += 1
        for index_name in [
            key for key, meta in self._index_meta.items()
            if meta.get("table") == name
        ]:
            self._index_meta.pop(index_name, None)
        self._write_catalog()
        self.flush()

    def get_table(self, name):
        """Return the :class:`Table` for ``name``."""
        self._ensure_open()
        table = self._tables.get(name)
        if table is not None:
            return table
        lowered = name.lower()
        for key, value in self._tables.items():
            if key.lower() == lowered:
                return value
        raise no_such_table(name)

    def list_tables(self):
        """Return the names of all tables (sorted)."""
        self._ensure_open()
        return sorted(self._tables)

    def table_names(self):
        """Alias of :meth:`list_tables`."""
        return self.list_tables()

    # -- vector index API -------------------------------------------------
    def create_vector_index(self, index_name, table_name, column_name):
        """Record a vector index in the catalog (durable).

        The index itself is derived data: it is (re)built from the persisted
        rows, so it never has to be crash-safe by itself.
        """
        self._ensure_open()
        table = self.get_table(table_name)
        column = table.get_column(column_name)
        if column is None:
            raise bad_field(column_name)
        if column.type_name != "VECTOR":
            raise SQLError(
                ER_NOT_SUPPORTED_YET,
                "a vector index requires a VECTOR column",
            )
        for name, meta in self._index_meta.items():
            if name == index_name:
                raise SQLError(
                    ER_TABLE_EXISTS,
                    "Index '{}' already exists".format(index_name),
                )
        self._index_meta[index_name] = {
            "table": table.name,
            "column": column.name,
        }
        self._write_catalog()
        self.flush()
        # build eagerly so the executor can use it right away
        self._build_vector_index_for(table.name)

    def drop_vector_index(self, index_name):
        """Remove a vector index from the catalog (durable)."""
        self._ensure_open()
        meta = self._index_meta.pop(index_name, None)
        if meta is None:
            raise SQLError(
                ER_BAD_TABLE, "Index '{}' doesn't exist".format(index_name)
            )
        table_name = meta.get("table")
        self.vector_indexes.pop(table_name, None)
        self._write_catalog()
        self.flush()

    def get_vector_index(self, table_name):
        """Return the :class:`VectorIndex` of ``table_name`` or ``None``.

        The index is built lazily from the persisted rows (so it survives a
        restart) and kept up to date by subsequent writes.
        """
        self._ensure_open()
        table = self.get_table(table_name)
        index = self.vector_indexes.get(table.name)
        if index is not None:
            return index
        if not self._table_has_index(table.name):
            return None
        return self._build_vector_index_for(table.name)

    def _table_has_index(self, table_name):
        for meta in self._index_meta.values():
            if meta.get("table") == table_name:
                return True
        return False

    def _index_meta_for_table(self, table_name):
        for meta in self._index_meta.values():
            if meta.get("table") == table_name:
                return meta
        return None

    def _build_vector_index_for(self, table_name):
        """Build (and cache) the vector index of ``table_name``."""
        try:
            from .vector_index import VectorIndex
        except ImportError:  # pragma: no cover - vector_index not written yet
            return None
        table = self._tables.get(table_name)
        if table is None:
            return None
        meta = self._index_meta_for_table(table_name)
        if meta is None:
            return None
        column = meta.get("column")
        col = table.get_column(column)
        if col is None:
            return None
        dim = col.vector_dim or 0
        index = VectorIndex(dim, metric="l2")
        ids = []
        vectors = []
        for pk, vector in self.iter_vectors(table, column):
            ids.append(pk)
            vectors.append(vector)
        index.build(ids, vectors)
        self.vector_indexes[table.name] = index
        return index

    def iter_vectors(self, table, column=None):
        """Yield ``(pk, vector_tuple)`` for every visible row.

        Used to (re)build the vector index.
        """
        self._ensure_open()
        tbl = self._resolve_table(table)
        if column is None:
            column = tbl.vector_column
        col = tbl.get_column(column) if column else None
        if col is None:
            return
        idx = tbl.columns.index(col)
        for row in self._scan(tbl):
            value = list.__getitem__(row, idx)
            if value is None:
                value = ()
            yield row.pk, value

    def _sync_vector_indexes(self, changes):
        """Apply row changes to the live vector indexes.

        ``changes`` maps a table id to ``{pk: (kind, vector_or_None)}``.
        """
        if not self.vector_indexes:
            return
        for table_id, table_changes in changes.items():
            table = self._by_id.get(table_id)
            if table is None:
                continue
            index = self.vector_indexes.get(table.name)
            if index is None:
                continue
            meta = self._index_meta_for_table(table.name)
            if meta is None:
                continue
            col = table.get_column(meta.get("column"))
            if col is None:
                continue
            col_idx = table.columns.index(col)
            upsert_ids = []
            upsert_vectors = []
            delete_ids = []
            for pk, (kind, value) in table_changes.items():
                if kind == "del":
                    delete_ids.append(pk)
                else:
                    upsert_ids.append(pk)
                    upsert_vectors.append(value)
            if delete_ids:
                try:
                    index.delete(delete_ids)
                except Exception:  # pragma: no cover - index is best effort
                    pass
            if upsert_ids:
                try:
                    index.upsert(upsert_ids, upsert_vectors)
                except Exception:  # pragma: no cover
                    pass

    # -- row API ----------------------------------------------------------
    def _pk_of(self, table, values):
        pk = values.get(table.primary_key)
        if pk is None:
            raise SQLError(
                ER_NO_DEFAULT_VALUE,
                "Field '{}' doesn't have a default value".format(table.primary_key),
            )
        try:
            return int(pk)
        except (TypeError, ValueError):
            raise SQLError(
                ER_BAD_FIELD_ERROR,
                "invalid primary key value {!r}".format(pk),
            )

    def _check_columns(self, table, values):
        for name in values:
            if table.get_column(name) is None:
                raise bad_field(name)

    def insert_rows(self, table, rows):
        """Insert ``rows``.

        ``rows`` is a list of mappings ``column name -> value``.  Missing
        columns default to ``NULL``.  A duplicate primary key raises
        ``SQLError(1062)``; this also holds inside an explicit transaction
        and for duplicates inside one multi-row statement.
        """
        self._ensure_open()
        tbl = self._resolve_table(table)
        if isinstance(rows, dict):
            rows = [rows]
        prepared = []
        seen = set()
        for row in rows:
            if isinstance(row, Row):
                data = dict(row.items())
            else:
                data = dict(row)
            self._check_columns(tbl, data)
            pk = self._pk_of(tbl, data)
            if pk in seen:
                raise duplicate_key(pk)
            seen.add(pk)
            prepared.append((pk, data))
        # duplicate check against the visible state (committed rows plus the
        # current transaction overlay)
        for pk, _data in prepared:
            if self._row_exists(tbl, pk):
                raise duplicate_key(pk)
        if self._txn_active:
            overlay = self._overlay.setdefault(tbl.id, {})
            for pk, data in prepared:
                overlay[pk] = ("row", data)
            # uncommitted data must never reach the cache
            self._vector_cache_invalidate(tbl)
            self._bump_version(tbl.id)
            return
        records = []
        for pk, data in prepared:
            records.append(self._encode_row(tbl, OP_INSERT, pk, data))
        self._commit_records({tbl: records})

    def _row_exists(self, table, pk):
        """True when a row with primary key ``pk`` is currently visible."""
        if self._txn_active:
            overlay = self._overlay.get(table.id)
            if overlay is not None and pk in overlay:
                return overlay[pk][0] != "del"
        return pk in self._log_for(table).index

    def update_rows(self, table, pk, new_values):
        """Update the row identified by ``pk`` with ``new_values``."""
        self._ensure_open()
        tbl = self._resolve_table(table)
        if isinstance(new_values, (list, tuple)):
            new_values = dict(new_values)
        new_values = dict(new_values or {})
        self._check_columns(tbl, new_values)
        try:
            pk = int(pk)
        except (TypeError, ValueError):
            raise SQLError(
                ER_BAD_FIELD_ERROR, "invalid primary key value {!r}".format(pk)
            )
        current = self._current_row_dict(tbl, pk)
        if current is None:
            return
        merged = dict(current)
        merged.update(new_values)
        if self._txn_active:
            overlay = self._overlay.setdefault(tbl.id, {})
            overlay[pk] = ("row", merged)
            self._vector_cache_invalidate(tbl)
            self._bump_version(tbl.id)
            return
        record = self._encode_row(tbl, OP_UPDATE, pk, merged)
        self._commit_records({tbl: [record]})

    def delete_rows(self, table, pks):
        """Delete the rows identified by ``pks``."""
        self._ensure_open()
        tbl = self._resolve_table(table)
        if isinstance(pks, (int, str)):
            pks = [pks]
        prepared = []
        for pk in pks:
            try:
                pk = int(pk)
            except (TypeError, ValueError):
                raise SQLError(
                    ER_BAD_FIELD_ERROR, "invalid primary key value {!r}".format(pk)
                )
            prepared.append(pk)
        if self._txn_active:
            overlay = self._overlay.setdefault(tbl.id, {})
            for pk in prepared:
                overlay[pk] = ("del", None)
            self._vector_cache_invalidate(tbl)
            self._bump_version(tbl.id)
            return
        records = []
        for pk in prepared:
            if self._current_row_dict(tbl, pk) is None:
                continue
            records.append(self._encode_row(tbl, OP_DELETE, pk, None))
        if not records:
            return
        self._commit_records({tbl: records})

    def _encode_row(self, table, opcode, pk, values):
        payload = bytearray()
        payload.append(opcode)
        payload += struct.pack("<q", pk)
        if opcode != OP_DELETE:
            for col in table.columns:
                payload += _encode_value(col, values.get(col.name))
        return _frame(bytes(payload))

    def _vector_of_record(self, table, frame):
        """Extract the vector values of an encoded row record.

        Returns ``{column name: vector}`` for every VECTOR column of the
        table, so a record can update the caches of all of them.
        """
        payload = frame[8:]
        pos = 9
        result = {}
        for candidate in table.columns:
            value, pos = _decode_value(candidate, payload, pos)
            if candidate.type_name == "VECTOR":
                result[candidate.name] = value
        return result

    def _current_row_dict(self, table, pk):
        """Return the visible row of ``pk`` as a dict, or ``None``."""
        if self._txn_active:
            overlay = self._overlay.get(table.id)
            if overlay is not None and pk in overlay:
                kind, data = overlay[pk]
                if kind == "del":
                    return None
                result = {c.name: data.get(c.name) for c in table.columns}
                return result
        row = self.get(table, pk)
        if row is None:
            return None
        return dict(row.items())

    def get(self, table, pk):
        """Return the visible :class:`Row` for ``pk`` or ``None``."""
        self._ensure_open()
        tbl = self._resolve_table(table)
        try:
            pk = int(pk)
        except (TypeError, ValueError):
            return None
        if self._txn_active:
            overlay = self._overlay.get(tbl.id)
            if overlay is not None and pk in overlay:
                kind, data = overlay[pk]
                if kind == "del":
                    return None
                return self._row_from_dict(tbl, data, pk)
        log = self._log_for(tbl)
        offset = log.index.get(pk)
        if offset is None:
            return None
        return self._row_at(tbl, log, offset)

    def get_row(self, table, pk):
        """Alias of :meth:`get`."""
        return self.get(table, pk)

    def scan(self, table):
        """Yield the visible rows of ``table`` ordered by primary key."""
        self._ensure_open()
        tbl = self._resolve_table(table)
        return self._scan(tbl)

    # -- sequential (streaming) scans --------------------------------------
    def scan_columns(self, table, column_names, allowed_pks=None,
                     ordered=True, verify=False):
        """Yield ``(pk, {name: value})`` for a subset of columns.

        The row log is read sequentially in large chunks instead of one
        ``pread`` per row, and only the requested columns are decoded -
        the others are walked over using their encoded size.

        * ``allowed_pks`` - when given (a set/mapping of primary keys), only
          those rows are decoded.  Rows outside the set are skipped without
          touching their payload at all.
        * ``ordered`` - when ``True`` (default) the results are yielded in
          primary key ascending order, like :meth:`scan`.  When ``False``
          the rows are streamed in file order with O(1) memory, which is
          what an intermediate filter/distance pass wants.

        Rows written by the current transaction are always included, and the
        overlay version wins over the stored one, so a query inside an
        explicit transaction sees its own changes (as the statement
        requires).
        """
        self._ensure_open()
        tbl = self._resolve_table(table)
        names = list(column_names)
        wanted = []
        for name in names:
            col = tbl.get_column(name)
            if col is None:
                raise bad_field(name)
            wanted.append(tbl.columns.index(col))
        wanted_set = set(wanted)

        if not ordered:
            for pk, values in self._iter_selected(
                tbl, wanted_set, allowed_pks, verify=verify
            ):
                yield pk, values
            for pk, values in self._iter_overlay_rows(
                tbl, wanted_set, allowed_pks
            ):
                yield pk, values
            return

        # ordered: same streaming path as scan(), restricted to the wanted
        # columns
        for pk, values in self._ordered_stream(tbl, wanted_set, verify=verify):
            if allowed_pks is not None and pk not in allowed_pks:
                continue
            yield pk, values

    def scan_column(self, table, column_name, allowed_pks=None,
                    ordered=True, verify=False):
        """Yield ``(pk, value)`` for one column, reading the file sequentially.

        The values are yielded in primary key ascending order, like
        :meth:`scan`.  See :meth:`scan_columns` for the meaning of
        ``allowed_pks``, ``ordered`` and ``verify``.

        A ``VECTOR`` column is special: its decoded values are kept in a
        bounded in-memory cache (see :meth:`cached_vectors`), so the second
        and following scans of the same column cost no disk I/O and no
        float decoding at all.
        """
        tbl = self._resolve_table(table)
        col = tbl.get_column(column_name)
        is_vector = col is not None and col.type_name == "VECTOR"
        if is_vector:
            for pk, value in self._scan_vector_column(
                tbl, column_name, allowed_pks, ordered, verify
            ):
                yield pk, value
            return
        for pk, values in self.scan_columns(
            table, (column_name,), allowed_pks=allowed_pks, ordered=ordered,
            verify=verify,
        ):
            yield pk, values[column_name]

    def _scan_vector_column(self, table, column_name, allowed_pks, ordered,
                            verify):
        """``scan_column`` for a VECTOR column, using the decoded cache."""
        can_cache = (allowed_pks is None and not self._txn_active
                     and self._vector_cache_limit > 0)
        key = (table.id, column_name)
        cached = self._vector_cache.get(key) if can_cache else None
        if cached is not None:
            if ordered:
                pks = self._vector_cache_pks.get(key)
                if pks is None:
                    pks = sorted(cached)
                    if can_cache:
                        self._vector_cache_pks[key] = pks
                for pk in pks:
                    yield pk, cached[pk]
            else:
                for item in cached.items():
                    yield item
            return

        # cache miss: scan once, collecting the decoded vectors on the way.
        # Collection stops as soon as the budget is exceeded, so an oversized
        # table never causes a large temporary allocation.
        budget = self._vector_cache_limit - self._vector_cache_bytes
        collected = {}
        used = 0
        collecting = can_cache
        for pk, values in self.scan_columns(
            table, (column_name,), allowed_pks=allowed_pks, ordered=ordered,
            verify=verify,
        ):
            value = values[column_name]
            if collecting:
                used += _vector_entry_bytes(value)
                if used > budget:
                    collecting = False
                    collected = None
                else:
                    collected[pk] = value
            yield pk, value
        if collecting and collected is not None and len(collected) == len(
            self._log_for(table).index
        ):
            self._vector_cache_store(table, collected, sorted(collected),
                                     column_name)

    def _iter_selected(self, table, wanted_set, allowed_pks, verify=False):
        """Yield ``(pk, {name: value})`` for the *stored* live rows.

        Single sequential pass over the row log; only the columns in
        ``wanted_set`` are decoded.  When a complete decoded vector cache
        exists for the table, the vector column is taken from the cache and
        the encoded bytes are merely skipped - that removes the dominant
        per-row ``struct.unpack`` cost.

        Rows that the current transaction overlay owns (inserted, updated or
        deleted inside the transaction) are skipped here; the overlay version
        is produced by :meth:`_iter_overlay_rows` instead, so callers that
        need a full view must merge both streams.
        """
        log = self._log_for(table)
        limit = self._committed.get(table.id, (0, log.size))[1]
        index = log.index
        overlay = self._overlay.get(table.id) if self._txn_active else None
        # use the decoded vector cache when it is complete and the vector
        # column is wanted
        cached_vectors = None
        vector_index = None
        if self._vector_cache and table.vector_column is not None:
            key = (table.id, table.vector_column)
            cached_vectors = self._vector_cache.get(key)
            if cached_vectors is not None:
                col = table.get_column(table.vector_column)
                if col is not None:
                    vector_index = table.columns.index(col)
                    if vector_index not in wanted_set:
                        cached_vectors = None
                    elif overlay:
                        cached_vectors = None
        for offset, payload in log.iter_payloads(limit=limit, verify=verify):
            opcode = payload[0]
            if opcode == OP_MARKER:
                continue
            pk = struct.unpack_from("<q", payload, 1)[0]
            if overlay is not None and pk in overlay:
                continue  # the overlay version supersedes the stored one
            if index.get(pk) != offset:
                continue  # a newer version of this row exists
            if allowed_pks is not None and pk not in allowed_pks:
                continue
            if opcode == OP_DELETE:
                continue
            values = {}
            pos = 9
            for idx, col in enumerate(table.columns):
                if idx in wanted_set:
                    if idx == vector_index:
                        start = pos
                        pos = _value_span(payload, pos)
                        cached = cached_vectors.get(pk)
                        if cached is None:
                            # defensive: a hole in the cache must never turn
                            # into a wrong value, so decode it after all
                            cached, _ = _decode_value(col, payload, start)
                        values[col.name] = cached
                    else:
                        value, pos = _decode_value(col, payload, pos)
                        values[col.name] = value
                else:
                    pos = _value_span(payload, pos)
            yield pk, values

    def _overlay_values(self, table, data, wanted_set):
        """The wanted columns of one overlay row, as a dict."""
        values = {}
        for idx, col in enumerate(table.columns):
            if idx in wanted_set:
                values[col.name] = data.get(col.name)
        return values

    def _iter_overlay_rows(self, table, wanted_set, allowed_pks=None):
        """Yield ``(pk, {name: value})`` for the rows of the write overlay.

        These are the transaction's own uncommitted changes: rows inserted
        or updated inside the transaction.  Deleted rows are not yielded.
        Together with :meth:`_iter_selected` (which skips the very same
        primary keys) this gives the complete view seen by the transaction.
        """
        overlay = self._overlay.get(table.id) if self._txn_active else None
        if not overlay:
            return
        for pk, (kind, data) in overlay.items():
            if kind != "row":
                continue
            if allowed_pks is not None and pk not in allowed_pks:
                continue
            yield pk, self._overlay_values(table, data, wanted_set)

    def _row_at(self, table, log, offset):
        record = log.read_frame(offset)
        if record is None:
            return None
        payload = record[0]
        opcode = payload[0]
        if opcode == OP_DELETE:
            return None
        pk = struct.unpack_from("<q", payload, 1)[0]
        values = []
        data = {}
        pos = 9
        for col in table.columns:
            value, pos = _decode_value(col, payload, pos)
            values.append(value)
            data[col.name] = value
        return Row(values, pk, data)

    def _row_from_dict(self, table, data, pk):
        values = []
        mapping = {}
        for col in table.columns:
            value = data.get(col.name)
            values.append(value)
            mapping[col.name] = value
        return Row(values, pk, mapping)

    def _scan(self, table):
        """Yield the visible rows of ``table`` ordered by primary key.

        Uses one sequential pass over the row log.  When the file order
        already matches the primary key order (the common case: rows are
        appended in key order) the rows are streamed out with O(1) memory.
        Otherwise the rows are reordered through a bounded buffer, and if
        that buffer overflows the remaining rows are fetched by offset - the
        result is always ordered, never unbounded in memory.
        """
        wanted = set(range(len(table.columns)))
        for pk, values in self._ordered_stream(table, wanted):
            yield self._row_from_dict(table, values, pk)

    def _visible_pks(self, table):
        """The primary keys that are currently visible, as a set."""
        index = self._log_for(table).index
        overlay = self._overlay.get(table.id) if self._txn_active else None
        if not overlay:
            return set(index)
        visible = set()
        for pk in index:
            if pk not in overlay:
                visible.add(pk)
        for pk, (kind, _data) in overlay.items():
            if kind == "row":
                visible.add(pk)
        return visible

    def _ordered_stream(self, table, wanted_set, verify=True):
        """Yield ``(pk, {name: value})`` in primary key ascending order."""
        log = self._log_for(table)
        overlay = self._overlay.get(table.id) if self._txn_active else None

        if not overlay and log.monotonic_pks():
            # Fast path: the file order is already the primary key order, so
            # the sequential scan can be streamed out verbatim.
            for pk, values in self._iter_selected(
                table, wanted_set, None, verify=verify
            ):
                yield pk, values
            return

        visible = self._visible_pks(table)
        if not visible:
            return
        expected = sorted(visible)

        pending = {}
        if overlay:
            for pk, (kind, data) in overlay.items():
                if kind != "row":
                    continue
                values = {}
                for idx, col in enumerate(table.columns):
                    if idx in wanted_set:
                        values[col.name] = data.get(col.name)
                pending[pk] = values

        j = 0
        emitted = 0
        total = len(expected)
        try:
            for pk, values in self._iter_selected(
                table, wanted_set, None, verify=verify
            ):
                pending[pk] = values
                while j < total and expected[j] in pending:
                    key = expected[j]
                    j += 1
                    emitted += 1
                    yield key, pending.pop(key)
                if len(pending) > _REORDER_BUFFER_LIMIT:
                    raise _ReorderOverflow()
        except _ReorderOverflow:
            # too much reordering - finish with random access by offset
            for pk in expected[emitted:]:
                if pk in pending:
                    yield pk, pending.pop(pk)
                    continue
                row = self._row_at(table, self._log_for(table),
                                   self._log_for(table).index.get(pk))
                if row is None:
                    continue
                values = {}
                for idx, col in enumerate(table.columns):
                    if idx in wanted_set:
                        values[col.name] = list.__getitem__(row, idx)
                yield pk, values
            return
        # drain what is left (the disk rows that never showed up are gone)
        while j < total:
            key = expected[j]
            j += 1
            if key in pending:
                yield key, pending.pop(key)

    # -- transaction API --------------------------------------------------
    def begin(self):
        """Start an explicit transaction."""
        self._ensure_open()
        if self._txn_active:
            raise SQLError(
                ER_NOT_SUPPORTED_YET,
                "a transaction is already active; only one is allowed",
            )
        self._txn_active = True
        self._overlay = {}

    def commit(self):
        """Commit the active transaction (no-op when there is none)."""
        self._ensure_open()
        if not self._txn_active:
            self.flush()
            return
        overlay = self._overlay
        self._overlay = {}
        self._txn_active = False
        if not overlay:
            self.flush()
            return
        by_table = {}
        for table_id, changes in overlay.items():
            table = self._by_id.get(table_id)
            if table is None:
                continue
            records = []
            for pk in sorted(changes):
                kind, data = changes[pk]
                if kind == "del":
                    records.append(self._encode_row(table, OP_DELETE, pk, None))
                else:
                    opcode = OP_UPDATE if pk in self._log_for(table).index else OP_INSERT
                    records.append(self._encode_row(table, opcode, pk, data))
            by_table[table] = records
        if by_table:
            self._commit_records(by_table)

    def rollback(self):
        """Discard the active transaction (no-op when there is none)."""
        self._ensure_open()
        touched = list(self._overlay)
        self._overlay = {}
        self._txn_active = False
        # the visible content of those tables changed when the transaction
        # ended, so any cache built inside it must be discarded
        self._bump_versions(touched)

    @property
    def in_transaction(self):
        return self._txn_active

    # -- commit path ------------------------------------------------------
    def _commit_records(self, by_table):
        """Append records, fsync them and make the commit durable."""
        self._ensure_open()
        txn_id = self._next_txn_id
        self._next_txn_id += 1
        self._txn_counter += 1

        written = []
        for table, records in by_table.items():
            log = self._log_for(table)
            for record in records:
                offset = log.append(record)
                written.append((table, log, offset, record))
        # update the in-memory index and keep the vector index in sync
        index_changes = {}
        for table, log, offset, record in written:
            payload = record[8:8 + 9]  # opcode + pk (frame header is 8 bytes)
            opcode = payload[0]
            pk = struct.unpack_from("<q", payload, 1)[0]
            if opcode == OP_DELETE:
                log.index.pop(pk, None)
                index_changes.setdefault(table.id, {})[pk] = ("del", None)
            else:
                log.index[pk] = offset
                index_changes.setdefault(table.id, {})[pk] = (
                    "row",
                    self._vector_of_record(table, record),
                )
        self._sync_vector_indexes(index_changes)
        if index_changes:
            self._apply_index_changes_to_vector_cache(index_changes)
        # every durable write changes the visible contents of these tables
        for table in by_table:
            self._bump_version(table.id)

        if len(by_table) == 1:
            # single table transaction: one fsync is enough, the commit
            # marker lives inside the table log itself.
            table = next(iter(by_table))
            log = self._log_for(table)
            marker_payload = bytes((OP_MARKER,)) + struct.pack("<Q", txn_id)
            marker = _frame(marker_payload)
            log.append(marker)
            log.sync()
            self._committed[table.id] = (txn_id, log.size)
            return

        # multi table transaction: fsync every data file, then record the
        # committed lengths in the global WAL.
        lengths = []
        for table, records in by_table.items():
            log = self._log_for(table)
            log.sync()
            lengths.append((table.id, log.size))
        payload = bytearray()
        payload.append(WAL_COMMIT)
        payload += struct.pack("<QI", txn_id, len(lengths))
        for table_id, length in lengths:
            payload += struct.pack("<QQQ", table_id, txn_id, length)
        self._wal_write(bytes(payload))
        os.fsync(self._wal_fd)
        for table_id, length in lengths:
            self._committed[table_id] = (txn_id, length)

    # -- misc -------------------------------------------------------------
    def table_row_count(self, table):
        """Number of visible rows (cheap: the index size)."""
        self._ensure_open()
        tbl = self._resolve_table(table)
        return len(self._log_for(tbl).index)


def open_storage(data_dir=None):
    """Open a :class:`Storage` (convenience helper for the server)."""
    storage = Storage(data_dir)
    storage.open()
    return storage
