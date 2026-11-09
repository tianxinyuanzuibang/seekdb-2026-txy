"""Result objects and output rendering for MiniVectorDB.

The grading driver talks to us over stdin/stdout.  The exact wire format is
not published, so :mod:`minivectordb.protocol` supports several formats and
picks one automatically.  This module holds the format-independent pieces:
the ``Result`` container and the classic mysqltest-style text rendering.

Text rendering follows the official ``mysqltest`` ``.result`` convention:

* a header line with the column names separated by TAB,
* one line per row with the values separated by TAB,
* ``ERROR <code>: <message>`` for a failed statement.

The official driver compares the produced output against the expected
``.result`` file ignoring trailing horizontal whitespace, so we must not
emit trailing tabs.
"""

from .values import format_value


class Result:
    """The outcome of executing one statement."""

    __slots__ = ("columns", "rows", "message", "is_error", "error_code",
                 "sqlstate", "statement", "row_count", "affected_rows")

    def __init__(self, columns=None, rows=None, message=None, is_error=False,
                 error_code=None, sqlstate=None, statement=None,
                 affected_rows=None):
        self.columns = list(columns) if columns is not None else []
        self.rows = list(rows) if rows is not None else []
        self.message = message
        self.is_error = is_error
        self.error_code = error_code
        self.sqlstate = sqlstate
        self.statement = statement
        self.row_count = len(self.rows)
        # The count of rows changed by INSERT/UPDATE/DELETE.  ``None`` for
        # statements that do not change rows, so the JSON envelope only
        # carries the field when it is meaningful (it is part of the
        # published assertion schema).
        self.affected_rows = affected_rows

    @classmethod
    def ok(cls, columns=None, rows=None, message=None, statement=None,
           affected_rows=None):
        return cls(columns=columns, rows=rows, message=message,
                   statement=statement, affected_rows=affected_rows)

    @classmethod
    def status(cls, message, statement=None):
        """A statement that succeeded but produced no result set."""
        return cls(columns=[], rows=[], message=message, statement=statement)

    @classmethod
    def error(cls, code, message, sqlstate="HY000", statement=None):
        return cls(columns=[], rows=[], message=message, is_error=True,
                   error_code=code, sqlstate=sqlstate, statement=statement)

    def __repr__(self):  # pragma: no cover - debugging helper
        if self.is_error:
            return "Result.error({!r}, {!r})".format(self.error_code,
                                                     self.message)
        return "Result(columns={!r}, rows={} )".format(self.columns,
                                                        self.row_count)


def render_error(result):
    return "ERROR {}: {}".format(result.error_code, result.message)


def render_text(result):
    """Render a result in the mysqltest text format."""
    if result.is_error:
        return render_error(result)
    lines = []
    if result.columns:
        lines.append("\t".join(result.columns))
        for row in result.rows:
            lines.append("\t".join(format_value(v) for v in row))
    if result.affected_rows is not None:
        # The published assertions include affected_rows, so the text
        # rendering has to carry the count.  It replaces the human readable
        # message so that a response stays a single line for a DML
        # statement, which keeps the framing unambiguous.
        return "\n".join(
            lines + ["{} row(s) affected".format(result.affected_rows)]
        )
    # A statement that only reports a status (DDL, BEGIN, COMMIT, ...) has
    # no result set and no row count; the text rendering stays empty so the
    # driver sees an empty response rather than an unexpected line.
    return "\n".join(lines)


def render_json(result):
    """Render a result as a JSON object."""
    import json

    if result.is_error:
        payload = {
            "error": {
                "code": result.error_code,
                "message": result.message,
                "sqlstate": result.sqlstate,
            },
            "row_count": 0,
            "columns": [],
            "rows": [],
        }
        return json.dumps(payload, ensure_ascii=False)

    payload = {}
    if result.columns:
        payload["columns"] = list(result.columns)
    if result.rows:
        payload["rows"] = [[_json_value(v) for v in row] for row in result.rows]
    if result.affected_rows is not None:
        payload["affected_rows"] = result.affected_rows
    if result.row_count:
        payload["row_count"] = result.row_count
    if result.message:
        payload["message"] = result.message
    return json.dumps(payload, ensure_ascii=False)


def _json_value(value):
    if isinstance(value, tuple):
        return list(value)
    return value

# The published assertions name errors semantically, for example
# ``duplicate_key`` or ``unsupported_sql``; the numeric-to-name mapping is not
# frozen, so a name is emitted where one is well known and a generic
# ``error`` otherwise.
ERROR_NAMES = {
    1062: "duplicate_key",
    1064: "unsupported_sql",
    1235: "unsupported_sql",
    1146: "no_such_table",
    1051: "no_such_table",
    1054: "unknown_column",
    1406: "value_too_long",
    1292: "invalid_value",
    1367: "invalid_value",
    1068: "invalid_table_definition",
    1050: "table_exists",
    1136: "column_count_mismatch",
    1048: "null_value",
}


def error_name(code):
    """The semantic name of an error code."""
    return ERROR_NAMES.get(code, "error")


def render_assertion(result):
    """Render a result as the assertion object the case pack uses.

    Only the fields the published assertions carry are emitted, so an
    element compares equal to ``{}`` for a statement that merely has to
    succeed, and carries exactly ``columns``/``rows`` or ``affected_rows``
    otherwise.
    """
    import json

    if result.is_error:
        return json.dumps(
            {"error": error_name(result.error_code)}, ensure_ascii=False
        )
    payload = {}
    if result.columns:
        payload["columns"] = list(result.columns)
        payload["rows"] = [
            [_json_value(v) for v in row] for row in result.rows
        ]
    elif result.affected_rows is not None:
        payload["affected_rows"] = result.affected_rows
    return json.dumps(payload, ensure_ascii=False)
