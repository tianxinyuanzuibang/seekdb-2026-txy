"""Statement execution for MiniVectorDB.

The executor turns the AST into storage operations and result sets.  It owns
the *semantics* the contest grades:

* schema validation (one BIGINT primary key, at most one vector column),
* value coercion and validation (VARCHAR length in code points, finite
  float32 vectors of the declared dimension),
* the visibility rules of an explicit transaction,
* vector search: filter first, then take the top K of the filtered set,
* deterministic ordering (ties broken by primary key ascending).
"""

import heapq
import math
import os

from . import values as V
from .errors import (SQLError, bad_field, duplicate_key, duplicate_key,
                      invalid_value, no_such_table, not_supported)
from .ast_nodes import (Begin, Between, BinaryOp, ColumnRef, Commit,
                        CreateTable, CreateVectorIndex, Delete, Desc,
                        DropTable, DropVectorIndex, Explain, FunctionCall,
                        InList, Insert, IsNull, Like, Literal, NullLiteral,
                        Rollback, Select, SelectItem, SetMode, Show, UnaryOp,
                        Update, Use, VectorLiteral)
from .results import Result


class Executor:
    """Executes statements against a :class:`minivectordb.storage.Storage`."""

    def __init__(self, storage, index_factory=None):
        self.storage = storage
        self.index_factory = index_factory
        self.session_mode = os.environ.get("MVD_MODE", "exact").strip().lower()
        if self.session_mode not in ("exact", "ann"):
            self.session_mode = "exact"
        self.in_transaction = False
        self._indexes = {}
        # Cache of column name -> position, keyed by table identity.
        self._column_cache = {}
        # Cache of filter results: (table, columns, filter shape) ->
        # (data version, matching primary keys).
        self._filter_cache = {}
        # The K of the search currently being executed.
        self._search_limit = None

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def execute(self, stmt, mode=None):
        handler = getattr(self, "_exec_" + type(stmt).__name__, None)
        if handler is None:
            raise not_supported(
                "statement {} is not supported".format(type(stmt).__name__)
            )
        return handler(stmt, mode)

    def execute_script(self, statements, mode=None):
        results = []
        for stmt in statements:
            results.append(self.execute(stmt, mode))
        return results

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _table(self, name):
        """Look up a table, raising a "no such table" error when absent.

        The storage layer may either return ``None`` or raise for a missing
        table, so both conventions are accepted here.
        """
        table = self._get_table(name)
        if table is None:
            raise no_such_table(name)
        return table

    def _get_table(self, name):
        try:
            return self.storage.get_table(name)
        except SQLError:
            return None

    def _column_index(self, table, name):
        """Resolve a column name to its position.

        The lookup is cached per table because the query loop resolves the
        same handful of column names for every row, and the cache turns a
        string normalisation plus a linear scan into a dict hit.
        """
        cache = self._column_cache.get(id(table))
        if cache is None:
            cache = {}
            self._column_cache[id(table)] = cache
        key = name.lower()
        index = cache.get(key)
        if index is None:
            for position, column in enumerate(table.columns):
                if column.name.lower() == key:
                    index = position
                    break
            else:
                raise bad_field(name)
            cache[key] = index
        return index

    def _check_varchar_length(self, column, value):
        # VARCHAR length is counted in Unicode code points.
        if len(value) > column.length:
            raise SQLError(
                1406,
                "Data too long for column '{}' at row 1".format(column.name),
                "22001",
            )

    def _coerce(self, table, column, raw):
        """Validate and convert a raw value for ``column``."""
        if raw is None:
            if column.not_null:
                raise SQLError(
                    1048, "Column '{}' cannot be null".format(column.name)
                )
            return None
        kind = column.type_name
        if kind == "BIGINT":
            if isinstance(raw, bool):
                return int(raw)
            if isinstance(raw, int):
                return raw
            if isinstance(raw, float):
                if not math.isfinite(raw):
                    raise invalid_value("BIGINT cannot hold a non-finite value")
                if raw != int(raw):
                    raise invalid_value(
                        "value {} is not an integer".format(raw)
                    )
                return int(raw)
            if isinstance(raw, str):
                try:
                    return int(raw.strip())
                except ValueError:
                    raise invalid_value(
                        "Truncated incorrect INTEGER value: '{}'".format(raw)
                    )
            raise invalid_value("cannot store {!r} in BIGINT".format(raw))
        if kind == "VARCHAR":
            if isinstance(raw, str):
                text = raw
            elif isinstance(raw, (int, float)):
                text = V.format_value(raw)
            else:
                raise invalid_value(
                    "Truncated incorrect VARCHAR value: {!r}".format(raw)
                )
            self._check_varchar_length(column, text)
            return text
        if kind == "VECTOR":
            if isinstance(raw, str):
                raise invalid_value("vector value must be a list of numbers")
            if not isinstance(raw, (tuple, list)):
                raise invalid_value("vector value must be a list of numbers")
            if column.vector_dim is not None and len(raw) != column.vector_dim:
                raise invalid_value(
                    "vector dimension mismatch: expected {}, got {}".format(
                        column.vector_dim, len(raw)
                    )
                )
            if isinstance(raw, tuple):
                # Tuples only ever come from an already-validated source:
                # a vector literal just converted to float32, or a vector
                # read back from storage.  Re-validating them would double
                # the conversion cost of every insert.
                return raw
            try:
                return V.to_float32_values(raw)
            except OverflowError:
                raise invalid_value("vector element overflows float32 range")
        raise not_supported("unsupported column type {}".format(kind))

    # ------------------------------------------------------------------
    # expression evaluation
    # ------------------------------------------------------------------
    def _eval(self, expr, row, table):
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, NullLiteral):
            return None
        if isinstance(expr, VectorLiteral):
            try:
                return V.to_float32_values(expr.values)
            except OverflowError:
                raise invalid_value("vector element overflows float32 range")
        if isinstance(expr, ColumnRef):
            if table is None:
                raise bad_field(expr.name)
            index = self._column_index(table, expr.name)
            return row[index]
        if isinstance(expr, UnaryOp):
            if expr.op == "*":
                # The star argument of COUNT(*).
                return "*"
            if expr.op == "NOT":
                value = self._eval(expr.operand, row, table)
                if value is None:
                    return None
                return not self._truthy(value)
            value = self._eval(expr.operand, row, table)
            if value is None:
                return None
            if expr.op == "-":
                if isinstance(value, tuple):
                    return tuple(-v for v in value)
                return -value
            return value
        if isinstance(expr, BinaryOp):
            return self._eval_binary(expr, row, table)
        if isinstance(expr, FunctionCall):
            return self._eval_function(expr, row, table)
        if isinstance(expr, InList):
            value = self._eval(expr.expr, row, table)
            if value is None:
                return None
            found = False
            for candidate in expr.values:
                other = self._eval(candidate, row, table)
                if V.values_equal(value, other):
                    found = True
                    break
            return (not found) if expr.negated else found
        if isinstance(expr, Between):
            value = self._eval(expr.expr, row, table)
            low = self._eval(expr.low, row, table)
            high = self._eval(expr.high, row, table)
            if value is None or low is None or high is None:
                return None
            inside = V.compare_values(value, low) >= 0 and (
                V.compare_values(value, high) <= 0
            )
            return (not inside) if expr.negated else inside
        if isinstance(expr, Like):
            value = self._eval(expr.expr, row, table)
            pattern = self._eval(expr.pattern, row, table)
            if value is None or pattern is None:
                return None
            matched = _like_match(str(value), str(pattern))
            return (not matched) if expr.negated else matched
        if isinstance(expr, IsNull):
            value = self._eval(expr.expr, row, table)
            is_null = value is None
            return (not is_null) if expr.negated else is_null
        raise not_supported("unsupported expression {!r}".format(expr))

    def _eval_binary(self, expr, row, table):
        op = expr.op
        if op == "AND":
            left = self._eval(expr.left, row, table)
            if left is False:
                return False
            right = self._eval(expr.right, row, table)
            if left is None or right is None:
                return None
            return bool(left) and bool(right)
        if op == "OR":
            left = self._eval(expr.left, row, table)
            if left is True:
                return True
            right = self._eval(expr.right, row, table)
            if left is None or right is None:
                return None
            return bool(left) or bool(right)
        left = self._eval(expr.left, row, table)
        right = self._eval(expr.right, row, table)
        if op in ("=", "<", "<=", ">", ">="):
            if left is None or right is None:
                return None
            comparison = V.compare_values(left, right)
            if op == "=":
                return comparison == 0
            if op == "<":
                return comparison < 0
            if op == "<=":
                return comparison <= 0
            if op == ">":
                return comparison > 0
            return comparison >= 0
        if op in ("+", "-", "*", "/"):
            if left is None or right is None:
                return None
            if isinstance(left, tuple) or isinstance(right, tuple):
                raise not_supported("vector arithmetic is not supported")
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if right == 0:
                return None
            return left / right
        if op in ("<>", "!="):
            if left is None or right is None:
                return None
            return not V.values_equal(left, right)
        raise not_supported("unsupported operator {}".format(op))

    def _eval_function(self, expr, row, table):
        name = expr.name.upper()
        if name in V.DISTANCE_FUNCTIONS:
            canonical, function = V.get_distance_function(name)
            args = [self._eval(a, row, table) for a in expr.args]
            if len(args) != 2:
                raise SQLError(
                    1582, "Incorrect parameter count in call to '{}'".format(
                        expr.name.lower()
                    ), "42000"
                )
            left, right = args
            if left is None or right is None:
                return None
            if not isinstance(left, tuple) or not isinstance(right, tuple):
                raise invalid_value(
                    "distance functions require vector arguments"
                )
            if len(left) != len(right):
                raise invalid_value("vector dimension mismatch")
            return function(left, right)
        raise SQLError(
            1305, "FUNCTION {} does not exist".format(expr.name.lower()),
            "42000",
        )

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------
    def _exec_CreateTable(self, stmt, mode=None):
        existing = self._get_table(stmt.name)
        if existing is not None:
            raise SQLError(
                1050, "Table 'test.{}' already exists".format(stmt.name)
            )
        primary_keys = [c.name for c in stmt.columns if c.primary_key]
        if stmt.primary_key:
            primary_keys.append(stmt.primary_key)
        if not primary_keys:
            raise SQLError(
                1062, "A table must have a BIGINT primary key", "42000"
            )
        if len(set(k.lower() for k in primary_keys)) > 1:
            raise SQLError(1068, "Multiple primary key defined", "42000")
        primary_key = primary_keys[0]
        vector_columns = [
            c for c in stmt.columns if c.type_name.upper() == "VECTOR"
        ]
        if len(vector_columns) > 1:
            raise SQLError(
                1068, "Only one vector column is allowed per table", "42000"
            )
        columns = []
        seen = set()
        for column in stmt.columns:
            key = column.name.lower()
            if key in seen:
                raise SQLError(
                    1060, "Duplicate column name '{}'".format(column.name)
                )
            seen.add(key)
            kind = column.type_name.upper()
            if kind == "VARCHAR" and column.length is None:
                raise SQLError(
                    1067, "VARCHAR requires a length", "42000"
                )
            if kind == "VECTOR" and column.vector_dim is None:
                raise SQLError(
                    1067, "VECTOR requires a dimension", "42000"
                )
            columns.append(column)
        pk_column = next(
            (c for c in columns if c.name.lower() == primary_key.lower()), None
        )
        if pk_column is None:
            raise SQLError(
                1072, "Key column '{}' doesn't exist in table".format(
                    primary_key
                )
            )
        if pk_column.type_name.upper() != "BIGINT":
            raise SQLError(
                1062, "The primary key must be a BIGINT column", "42000"
            )
        self.storage.create_table(
            stmt.name, columns, primary_key,
            vector_columns[0].name if vector_columns else None,
        )
        return Result.status("Table '{}' created".format(stmt.name))

    def _exec_DropTable(self, stmt, mode=None):
        table = self._get_table(stmt.name)
        if table is None:
            if stmt.if_exists:
                return Result.status(
                    "Table '{}' doesn't exist".format(stmt.name)
                )
            raise SQLError(
                1051, "Unknown table 'test.{}'".format(stmt.name)
            )
        self.storage.drop_table(stmt.name)
        return Result.status("Table '{}' dropped".format(stmt.name))

    def _exec_CreateVectorIndex(self, stmt, mode=None):
        table = self._table(stmt.table_name)
        if table.vector_column is None:
            raise not_supported(
                "table '{}' has no vector column".format(stmt.table_name)
            )
        if table.vector_column.lower() != stmt.column_name.lower():
            raise SQLError(
                1072, "Key column '{}' doesn't exist in table".format(
                    stmt.column_name
                )
            )
        self.storage.create_vector_index(
            stmt.index_name, stmt.table_name, stmt.column_name
        )
        return Result.status(
            "Vector index '{}' created on '{}.{}'".format(
                stmt.index_name, stmt.table_name, stmt.column_name
            )
        )

    def _exec_DropVectorIndex(self, stmt, mode=None):
        self.storage.drop_vector_index(stmt.index_name)
        return Result.status("Vector index '{}' dropped".format(stmt.index_name))

    # ------------------------------------------------------------------
    # DML
    # ------------------------------------------------------------------
    def _exec_Insert(self, stmt, mode=None):
        table = self._table(stmt.table)
        columns = table.columns
        if stmt.columns:
            names = stmt.columns
        else:
            names = [c.name for c in columns]
        lowered = [n.lower() for n in names]
        for name in lowered:
            self._column_index(table, name)
        rows = []
        for raw_row in stmt.rows:
            if len(raw_row) != len(names):
                raise SQLError(
                    1136,
                    "Column count doesn't match value count at row {}"
                    .format(len(rows) + 1),
                )
            row = {}
            for name, expr in zip(names, raw_row):
                index = self._column_index(table, name)
                column = columns[index]
                row[column.name] = self._coerce(
                    table, column, self._eval(expr, None, table)
                )
            pk_index = self._column_index(table, table.primary_key)
            if row.get(table.primary_key) is None:
                raise SQLError(
                    1048,
                    "Column '{}' cannot be null".format(table.primary_key),
                )
            rows.append(row)
        self.storage.insert_rows(table, rows)
        return Result.ok(
            message="{} row(s) affected".format(len(rows)),
        )

    def _exec_Update(self, stmt, mode=None):
        table = self._table(stmt.table)
        columns = table.columns
        pk_index = self._column_index(table, table.primary_key)
        pk_name = table.primary_key
        targets = []
        for row in self.storage.scan(table):
            if stmt.where is not None:
                if not self._truthy(self._eval(stmt.where, row, table)):
                    continue
            new_values = {}
            for name, expr in stmt.assignments:
                index = self._column_index(table, name)
                column = columns[index]
                new_values[column.name] = self._coerce(
                    table, column, self._eval(expr, row, table)
                )
            if not new_values:
                continue
            self.storage.update_rows(table, row[pk_index], new_values)
            targets.append(row[pk_index])
        return Result.ok(message="{} row(s) affected".format(len(targets)))

    def _exec_Delete(self, stmt, mode=None):
        table = self._table(stmt.table)
        pk_index = self._column_index(table, table.primary_key)
        targets = []
        for row in self.storage.scan(table):
            if stmt.where is not None:
                if not self._truthy(self._eval(stmt.where, row, table)):
                    continue
            targets.append(row[pk_index])
        self.storage.delete_rows(table, targets)
        return Result.ok(message="{} row(s) affected".format(len(targets)))

    def _truthy(self, value):
        return bool(value) if value is not None else False

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    def _exec_Begin(self, stmt, mode=None):
        if self.in_transaction:
            raise SQLError(
                1568, "Transaction already started", "25000"
            )
        self.storage.begin()
        self.in_transaction = True
        return Result.status("Transaction started")

    def _exec_Commit(self, stmt, mode=None):
        if not self.in_transaction:
            return Result.status("No transaction in progress")
        self.storage.commit()
        self.in_transaction = False
        return Result.status("Transaction committed")

    def _exec_Rollback(self, stmt, mode=None):
        if not self.in_transaction:
            return Result.status("No transaction in progress")
        self.storage.rollback()
        self.in_transaction = False
        return Result.status("Transaction rolled back")

    # ------------------------------------------------------------------
    # SELECT
    # ------------------------------------------------------------------
    def _exec_Select(self, stmt, mode=None):
        if stmt.table is None:
            # SELECT of literals only.
            return self._select_without_table(stmt)
        table = self._table(stmt.table)
        columns = table.columns

        if stmt.is_vector_search and stmt.vector_expr is not None:
            return self._exec_vector_select(stmt, table, mode)

        rows = list(self.storage.scan(table))
        if stmt.where is not None:
            rows = [
                row for row in rows
                if self._truthy(self._eval(stmt.where, row, table))
            ]

        rows = self._order_rows(table, rows, stmt.order_by)

        offset = stmt.offset or 0
        if offset:
            rows = rows[offset:]
        if stmt.limit is not None:
            rows = rows[: stmt.limit]

        items = self._expand_items(stmt.items, columns)
        if self._has_aggregate(items):
            return self._exec_aggregate(stmt, table, rows, items, columns)
        result_rows = []
        for row in rows:
            result_rows.append(
                [self._eval(item.expr, row, table) for item in items]
            )
        names = [item.alias or self._item_name(item, columns) for item in items]
        return Result.ok(columns=names, rows=result_rows)

    def _has_aggregate(self, items):
        """True when the projection contains an aggregate function."""
        for item in items:
            expr = item.expr
            if isinstance(expr, FunctionCall) and expr.name.upper() in (
                "COUNT", "SUM", "AVG", "MIN", "MAX",
            ):
                return True
        return False

    def _is_star(self, expr):
        """True for the ``*`` argument of ``COUNT(*)``."""
        return (
            isinstance(expr, UnaryOp)
            and expr.op == "*"
        ) or (
            isinstance(expr, Literal) and expr.value == "*"
        )

    def _exec_aggregate(self, stmt, table, rows, items, columns):
        """Evaluate an aggregate projection over the whole (filtered) set.

        Only the simple, ungrouped form is supported; that is all the
        statement requires.
        """
        values = []
        for item in items:
            expr = item.expr
            if not isinstance(expr, FunctionCall):
                values.append(self._eval(expr, None, table))
                continue
            name = expr.name.upper()
            if name == "COUNT":
                if not expr.args or self._is_star(expr.args[0]):
                    values.append(len(rows))
                    continue
                counted = 0
                for row in rows:
                    value = self._eval(expr.args[0], row, table)
                    if value is not None:
                        counted += 1
                values.append(counted)
                continue
            collected = []
            for row in rows:
                value = self._eval(expr.args[0], row, table)
                if value is not None:
                    collected.append(value)
            if not collected:
                values.append(None)
                continue
            if name == "SUM":
                values.append(sum(collected))
            elif name == "AVG":
                values.append(sum(collected) / len(collected))
            elif name == "MIN":
                values.append(min(collected))
            elif name == "MAX":
                values.append(max(collected))
            else:                                   # pragma: no cover
                values.append(None)
        names = [item.alias or self._item_name(item, columns) for item in items]
        return Result.ok(columns=names, rows=[values])

    def _data_version(self, table):
        """The storage layer's version counter for ``table``.

        It changes whenever the visible contents of the table change, so a
        cached filter result can be invalidated.  ``None`` means the
        storage layer does not provide one, and caching is then disabled.
        """
        getter = getattr(self.storage, "data_version", None)
        if getter is None:
            return None
        try:
            return getter(table)
        except Exception:                          # pragma: no cover
            return None

    def _collect_candidates(self, table, stmt, column_index):
        """Collect the rows that satisfy the WHERE clause.

        Returns ``(allowed_pks, vectors)`` where ``allowed_pks`` is the set
        of matching primary keys and ``vectors`` maps a primary key to its
        vector value.

        The work is split so that the row log is read as cheaply as
        possible:

        * the filter pass only decodes the columns the WHERE clause needs,
        * the vector column comes from the storage layer's vector cache
          when it is available,
        * the rows that are finally returned are fetched by key.
        """
        pk_index = self._column_index(table, table.primary_key)
        vector_name = table.columns[column_index].name
        cache_getter = getattr(self.storage, "cached_vectors", None)
        cached = None
        if cache_getter is not None:
            cached = cache_getter(table)

        if stmt.where is None:
            # No filter: every visible row is a candidate.  When the vector
            # cache is complete, ``None`` means "all rows" and lets the
            # ranking skip the per-row membership test.
            if cached is not None:
                return None, dict(cached)
            keys_getter = getattr(self.storage, "cached_vector_pks", None)
            allowed = None
            if keys_getter is not None:
                keys = keys_getter(table)
                if keys is not None:
                    allowed = set(keys)
            if allowed is None:
                keys = getattr(self.storage, "scan_column", None)
                if keys is not None and hasattr(
                    self.storage, "cached_vectors"
                ):
                    allowed = {
                        pk for pk, _value in keys(
                            table, table.primary_key, ordered=False
                        )
                    }
                else:
                    allowed = {row[pk_index] for row in self.storage.scan(table)}
        else:
            scanner = getattr(self.storage, "scan_columns", None)
            if scanner is None:
                rows = [
                    row for row in self.storage.scan(table)
                    if self._truthy(self._eval(stmt.where, row, table))
                ]
                allowed = {row[pk_index] for row in rows}
                vectors = {
                    row[pk_index]: row[column_index] for row in rows
                }
                return allowed, vectors
            allowed = self._filtered_keys(table, stmt, vector_name, scanner)

        vectors = {}
        if cached is not None:
            for identifier in allowed:
                vector = cached.get(identifier)
                if vector is not None:
                    vectors[identifier] = vector
            if len(vectors) == len(allowed):
                return allowed, vectors
            # The cache does not cover every candidate (a table may have
            # more than one vector column, or the cache may be partial),
            # so fall through and read the missing ones.
            for identifier in list(allowed):
                if identifier not in vectors:
                    allowed.discard(identifier)
            if not vectors:
                return allowed, vectors

        column_scanner = getattr(self.storage, "scan_column", None)
        missing = allowed - set(vectors)
        if missing:
            if column_scanner is not None:
                # A scan without a key restriction is what populates the
                # storage layer's decoded-vector cache, so prefer that when
                # most of the table is wanted anyway.
                restricted = missing
                if len(missing) * 2 >= len(allowed):
                    restricted = None
                for pk, value in column_scanner(
                    table, vector_name, allowed_pks=restricted, ordered=False
                ):
                    if value is None:
                        continue
                    if pk in allowed:
                        vectors[pk] = value
                after = getattr(self.storage, "cached_vectors", None)
                if after is not None:
                    fresh = after(table)
                    if fresh is not None:
                        for identifier in allowed:
                            vector = fresh.get(identifier)
                            if vector is not None:
                                vectors[identifier] = vector
            else:
                scanner = getattr(self.storage, "scan_columns", None)
                for pk, values in scanner(table, vector_name,
                                         allowed_pks=missing, ordered=False):
                    value = values.get(vector_name)
                    if value is not None:
                        vectors[pk] = value
        return allowed, vectors

    def _filtered_keys(self, table, stmt, vector_name, scanner):
        """The primary keys that satisfy the WHERE clause.

        The result is cached per (table, filter shape) and invalidated by
        the storage layer's data version, so a repeated query over an
        unchanged table does not have to re-read the filter column.
        """
        needed = self._columns_used_by(stmt.where, table)
        needed = [name for name in needed if name != vector_name]
        if not needed:
            needed = [table.primary_key]

        cache_key = (id(table), tuple(needed), _filter_shape(stmt.where))
        version = self._data_version(table)
        cached = self._filter_cache.get(cache_key)
        if cached is not None and version is not None and cached[0] == version:
            return set(cached[1])

        allowed = set()
        for pk, values in scanner(table, needed, ordered=False):
            row = self._row_from_values(table, values, pk)
            if self._truthy(self._eval(stmt.where, row, table)):
                allowed.add(pk)
        if version is not None:
            self._filter_cache[cache_key] = (version, allowed)
        return allowed

    def _rows_for(self, table, pks):
        """Fetch the full rows of the given primary keys.

        Only the rows that are actually returned are materialised; the
        rest of the candidate set never has to be decoded.  Each row is
        fetched directly by its key, so this costs one lookup per
        returned row rather than a pass over the whole table.
        """
        if not pks:
            return []
        rows = []
        getter = getattr(self.storage, "get", None)
        if getter is not None:
            for pk in pks:
                try:
                    row = getter(table, pk)
                except Exception:                  # pragma: no cover
                    row = None
                if row is None:
                    continue
                rows.append(list(row))
            if len(rows) == len(pks):
                return rows
            rows = []
        wanted = set(pks)
        scanner = getattr(self.storage, "scan_columns", None)
        if scanner is not None:
            names = [c.name for c in table.columns]
            for pk, values in scanner(table, names, allowed_pks=wanted,
                                     ordered=True):
                rows.append(self._row_from_values(table, values, pk))
            return rows
        for row in self.storage.scan(table):
            if row[0] in wanted:
                rows.append(row)
        return rows

    def _row_from_values(self, table, values, pk):
        """Build a row object from a ``{column: value}`` mapping.

        The primary key column is filled in from ``pk`` as well, so the row
        is complete even when the mapping did not contain it.
        """
        row = [None] * len(table.columns)
        for index, column in enumerate(table.columns):
            row[index] = values.get(column.name)
        try:
            pk_index = self._column_index(table, table.primary_key)
            if row[pk_index] is None:
                row[pk_index] = pk
        except Exception:                          # pragma: no cover
            pass
        return row

    def _columns_used_by(self, expr, table):
        """Collect the column names an expression refers to."""
        if expr is None:
            return []
        found = []
        stack = [expr]
        while stack:
            node = stack.pop()
            if isinstance(node, ColumnRef):
                if node.name not in found:
                    found.append(node.name)
                continue
            if isinstance(node, BinaryOp):
                stack.append(node.left)
                stack.append(node.right)
                continue
            if isinstance(node, UnaryOp):
                stack.append(node.operand)
                continue
            if isinstance(node, FunctionCall):
                stack.extend(node.args)
                continue
            if isinstance(node, InList):
                stack.append(node.expr)
                stack.extend(node.values)
                continue
            if isinstance(node, Between):
                stack.extend((node.expr, node.low, node.high))
                continue
            if isinstance(node, Like):
                stack.extend((node.expr, node.pattern))
                continue
            if isinstance(node, IsNull):
                stack.append(node.expr)
                continue
        return found

    def _exec_vector_select(self, stmt, table, mode):
        """Execute a vector Top-K search.

        The filtered set is computed first, then the K nearest rows are
        taken from it - never the other way round.
        """
        columns = table.columns
        pk_index = self._column_index(table, table.primary_key)
        vector_expr = stmt.vector_expr
        if not isinstance(vector_expr, FunctionCall):
            raise not_supported(
                "ORDER BY must reference a distance function for vector search"
            )
        canonical, function = V.get_distance_function(vector_expr.name)
        if len(vector_expr.args) != 2:
            raise SQLError(
                1582, "Incorrect parameter count in call to '{}'".format(
                    vector_expr.name.lower()
                ), "42000"
            )
        if not isinstance(vector_expr.args[0], ColumnRef):
            raise not_supported(
                "distance functions must be applied to a vector column"
            )
        column_index = self._column_index(table, vector_expr.args[0].name)
        column = columns[column_index]
        if column.type_name.upper() != "VECTOR":
            raise not_supported("distance functions require a vector column")
        query = self._eval(vector_expr.args[1], None, table)
        if query is None:
            raise invalid_value("query vector must not be NULL")
        if not isinstance(query, tuple):
            raise invalid_value("query vector must be a list of numbers")
        if column.vector_dim is not None and len(query) != column.vector_dim:
            raise invalid_value(
                "vector dimension mismatch: expected {}, got {}".format(
                    column.vector_dim, len(query)
                )
            )

        # --- step 1: the filtered candidate set ------------------------
        # The filter and the distance are evaluated over separate column
        # passes, so the row log is read sequentially and only the columns
        # that are actually needed are decoded.
        allowed, vectors = self._collect_candidates(
            table, stmt, column_index
        )

        # --- step 2: rank ----------------------------------------------
        search_mode = self._resolve_mode(stmt, mode)
        limit = stmt.limit
        offset = stmt.offset or 0

        # The ranking only has to keep the K best candidates.
        self._search_limit = limit
        try:
            if search_mode == "ann":
                ordered = self._ann_order(
                    table, allowed, vectors, function, query, limit,
                )
            else:
                ordered = self._exact_order(
                    table, allowed, vectors, function, query,
                )
        finally:
            self._search_limit = None

        if offset:
            ordered = ordered[offset:]
        if limit is not None:
            ordered = ordered[:limit]

        # Only the rows that are actually returned are materialised.
        rows = self._rows_for(table, ordered)

        # --- step 3: project -------------------------------------------
        by_pk = {row[0]: row for row in rows}
        items = self._expand_items(stmt.items, columns)
        result_rows = []
        for pk in ordered:
            row = by_pk.get(pk)
            if row is None:
                continue
            result_rows.append(
                [self._eval(item.expr, row, table) for item in items]
            )
        names = [item.alias or self._item_name(item, columns) for item in items]
        return Result.ok(columns=names, rows=result_rows)

    def _select_without_table(self, stmt):
        rows = []
        for _ in range(max(1, stmt.limit or 1)):
            rows.append(
                [self._eval(item.expr, None, None) for item in stmt.items]
            )
        names = [item.alias or _expr_name(item.expr) for item in stmt.items]
        return Result.ok(columns=names, rows=rows)

    def _expand_items(self, items, columns):
        expanded = []
        for item in items:
            if item.star:
                for column in columns:
                    expanded.append(
                        SelectItem(ColumnRef(column.name), column.name)
                    )
            else:
                expanded.append(item)
        return expanded

    def _item_name(self, item, columns):
        if isinstance(item.expr, ColumnRef):
            return item.expr.name
        if isinstance(item.expr, FunctionCall):
            name = item.expr.name.upper()
            if name == "COUNT" and item.expr.args and self._is_star(
                item.expr.args[0]
            ):
                return "COUNT(*)"
        return _expr_name(item.expr)

    def _order_rows(self, table, rows, order_by):
        if not order_by:
            return rows
        pk_index = self._column_index(table, table.primary_key)
        decorated = []
        for position, row in enumerate(rows):
            key = []
            for expr, ascending in order_by:
                value = self._eval(expr, row, table)
                key.append(_SortKey(value, ascending))
            key.append(_SortKey(row[pk_index], True))
            decorated.append((key, position, row))
        decorated.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in decorated]

    def _ann_order(self, table, allowed, vectors, function, query, limit=None):
        """Approximate ordering using the table's vector index.

        The index produces a candidate set; the candidates are then ordered
        by their exact distance so the returned rows are consistent with
        the exact mode (the approximation only affects *which* rows are
        considered, not how they are ranked).
        """
        if allowed is not None and not allowed:
            return []
        index = self._vector_index_for(table)
        if index is None:
            return self._exact_order(table, allowed, vectors, function, query)
        # Asking for more than K candidates is wasted work, but asking for
        # fewer would break the "fewer than K matches returns all of them"
        # rule, so the whole allowed set is used when it is smaller than K.
        total = len(vectors) if allowed is None else len(allowed)
        wanted = min(total, limit) if limit else total
        candidates = index.search(query, wanted, allowed)
        chosen = []
        seen = set()
        for entry in candidates:
            identifier = entry[0] if isinstance(entry, tuple) else entry
            if identifier in seen:
                continue
            if allowed is not None and identifier not in allowed:
                continue
            seen.add(identifier)
            chosen.append(identifier)
        if len(chosen) < min(total, limit or total):
            # The index could not produce enough candidates (for example a
            # very small table); fall back to the exact ordering so the
            # result is never short.
            return self._exact_order(table, allowed, vectors, function, query)
        # Rank the candidates by their exact distance so the ANN result is
        # ordered exactly like the exact mode.
        scored = []
        for identifier in chosen:
            vector = vectors.get(identifier)
            if vector is None:
                continue
            scored.append((function(vector, query), identifier))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[1] for item in scored]

    def _exact_order(self, table, allowed, vectors, function, query):
        """Rank the candidate rows by their exact distance.

        Ties are broken by primary key ascending, as the statement
        requires.  Only the K best candidates are kept, so the cost is
        O(n log k) instead of sorting the whole candidate set.

        ``allowed`` may be ``None``, meaning "every row in ``vectors``" -
        that lets the unfiltered case skip the per-row membership test.
        """
        limit = self._search_limit
        # Call math.dist directly rather than through the ``l2_distance``
        # wrapper: the wrapper adds a try/except frame to every one of the
        # thousands of distance computations a query performs.
        distance = math.dist if function is V.l2_distance else function
        if allowed is None:
            scored = [
                (distance(vector, query), identifier)
                for identifier, vector in vectors.items()
                if vector is not None
            ]
        else:
            scored = [
                (distance(vector, query), identifier)
                for identifier, vector in vectors.items()
                if vector is not None and identifier in allowed
            ]
        if limit is None or limit >= len(scored):
            scored.sort()
            return [item[1] for item in scored]
        # Keep only the K best.  The pairs are (distance, id), so plain
        # tuple ordering already breaks ties by ascending id.
        best = heapq.nsmallest(limit, scored)
        return [item[1] for item in best]

    def _resolve_mode(self, stmt, mode=None):
        """Decide between exact and approximate search.

        Precedence, highest first:

        1. an explicit mode given with the statement (``stmt.search_mode``),
        2. the mode supplied by the driver for this request (``mode``),
        3. the session default set with ``SET @mvd_mode`` / ``MVD_MODE``.

        The parser sets ``search_mode`` to ``'exact'`` by default, so a
        session default of ``'ann'`` must still win over that placeholder.
        To tell "the user asked for exact" apart from "nobody said
        anything", the parser marks an explicit choice with
        ``stmt.mode_explicit``.
        """
        explicit = getattr(stmt, "mode_explicit", False)
        if explicit and stmt.search_mode in ("exact", "ann"):
            return stmt.search_mode
        if mode in ("exact", "ann"):
            return mode
        if not explicit and self.session_mode in ("exact", "ann"):
            return self.session_mode
        return stmt.search_mode if stmt.search_mode in ("exact", "ann") \
            else "exact"

    def _vector_index_for(self, table):
        getter = getattr(self.storage, "get_vector_index", None)
        if getter is None:
            return None
        try:
            return getter(table.name)
        except Exception:                          # pragma: no cover
            return None

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def _exec_Show(self, stmt, mode=None):
        what = stmt.what.lower()
        if what == "tables":
            names = sorted(self.storage.list_tables())
            return Result.ok(
                columns=["Tables_in_test"], rows=[[n] for n in names]
            )
        if what == "columns":
            table = self._table(stmt.table)
            rows = []
            for column in table.columns:
                rows.append([
                    column.name,
                    column.type_name.lower(),
                    "YES" if not column.not_null else "NO",
                    column.length if column.type_name.upper() == "VARCHAR"
                    else (column.vector_dim
                          if column.type_name.upper() == "VECTOR" else None),
                    column.name if column.primary_key else "",
                ])
            return Result.ok(
                columns=["Field", "Type", "Null", "Key", "Extra"], rows=rows
            )
        if what == "indexes":
            rows = []
            table = self._table(stmt.table)
            rows.append([table.primary_key, "PRIMARY", 1, table.primary_key])
            return Result.ok(
                columns=["Table", "Non_unique", "Key_name", "Column_name"],
                rows=[[table.name, 0, "PRIMARY", table.primary_key]],
            )
        raise not_supported("unsupported SHOW variant")

    def _exec_Desc(self, stmt, mode=None):
        table = self._table(stmt.table)
        rows = []
        for column in table.columns:
            rows.append([
                column.name,
                column.type_name.lower(),
                "YES" if not column.not_null else "NO",
                "PRI" if column.primary_key else "",
            ])
        return Result.ok(
            columns=["Field", "Type", "Null", "Key"], rows=rows
        )

    def _exec_Explain(self, stmt, mode=None):
        return Result.ok(
            columns=["plan"],
            rows=[["single-table scan"], ["vector-search"]],
        )

    def _exec_Use(self, stmt, mode=None):
        return Result.status("Database changed")

    def _exec_SetMode(self, stmt, mode=None):
        value = (stmt.value or "").strip().lower()
        if value not in ("exact", "ann"):
            raise invalid_value(
                "mode must be 'exact' or 'ann', got {!r}".format(stmt.value)
            )
        self.session_mode = value
        return Result.status("Mode set to {}".format(value))


class _SortKey:
    """Wraps a value so that ORDER BY can mix directions and NULLs."""

    __slots__ = ("value", "ascending")

    def __init__(self, value, ascending):
        self.value = value
        self.ascending = ascending

    def __lt__(self, other):
        left, right = self.value, other.value
        if left is None or right is None:
            if left is None and right is None:
                result = 0
            else:
                result = -1 if left is None else 1
        else:
            result = V.compare_values(left, right)
        if not self.ascending:
            result = -result
        return result < 0

    def __eq__(self, other):
        left, right = self.value, other.value
        if left is None or right is None:
            return left is None and right is None
        return V.compare_values(left, right) == 0


def _filter_shape(expr):
    """A hashable canonical form of a filter expression.

    The literal *values* are part of the key, so ``bucket = 'a'`` and
    ``bucket = 'b'`` are cached separately.  Only expressions built from
    literals can be cached safely - anything referring to a column in a
    non-trivial way yields a key that is never reused.
    """
    if expr is None:
        return ("none",)
    if isinstance(expr, Literal):
        return ("lit", repr(expr.value))
    if isinstance(expr, NullLiteral):
        return ("null",)
    if isinstance(expr, ColumnRef):
        return ("col", expr.name.lower())
    if isinstance(expr, UnaryOp):
        return ("unary", expr.op, _filter_shape(expr.operand))
    if isinstance(expr, BinaryOp):
        return (
            "binary",
            expr.op,
            _filter_shape(expr.left),
            _filter_shape(expr.right),
        )
    if isinstance(expr, FunctionCall):
        return ("func", expr.name.upper())
    if isinstance(expr, InList):
        return (
            "in",
            _filter_shape(expr.expr),
            tuple(_filter_shape(v) for v in expr.values),
            expr.negated,
        )
    if isinstance(expr, Between):
        return (
            "between",
            _filter_shape(expr.expr),
            _filter_shape(expr.low),
            _filter_shape(expr.high),
            expr.negated,
        )
    if isinstance(expr, Like):
        return ("like", _filter_shape(expr.expr),
                _filter_shape(expr.pattern), expr.negated)
    if isinstance(expr, IsNull):
        return ("isnull", _filter_shape(expr.expr), expr.negated)
    return ("opaque", id(expr))
    if isinstance(expr, ColumnRef):
        return expr.name
    if isinstance(expr, Literal):
        return V.format_value(expr.value)
    if isinstance(expr, FunctionCall):
        return "{}({})".format(
            expr.name.lower(),
            ",".join(_expr_name(a) for a in expr.args),
        )
    if isinstance(expr, VectorLiteral):
        return "[" + ",".join(V.format_float(v) for v in expr.values) + "]"
    if isinstance(expr, UnaryOp):
        return "{}{}".format(expr.op, _expr_name(expr.operand))
    if isinstance(expr, BinaryOp):
        return "{} {} {}".format(
            _expr_name(expr.left), expr.op, _expr_name(expr.right)
        )
    return "expr"


def _like_match(value, pattern):
    """SQL LIKE with % and _ wildcards, matched on the whole string."""
    return _like_inner(value, pattern, 0, 0)


def _like_inner(value, pattern, vi, pi):
    n, m = len(value), len(pattern)
    while pi < m:
        ch = pattern[pi]
        if ch == "%":
            if pi + 1 == m:
                return True
            for skip in range(vi, n + 1):
                if _like_inner(value, pattern, skip, pi + 1):
                    return True
            return False
        if vi >= n:
            return False
        if ch == "_" or ch == value[vi]:
            vi += 1
            pi += 1
            continue
        return False
    return vi == n
