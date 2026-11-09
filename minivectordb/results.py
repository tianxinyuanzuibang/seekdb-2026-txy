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
    if result.message:
        if lines:
            lines.append(result.message)
        else:
            lines.append(result.message)
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

    payload = {
        "columns": list(result.columns),
        "rows": [[_json_value(v) for v in row] for row in result.rows],
        "row_count": result.row_count,
    }
    if result.affected_rows is not None:
        payload["affected_rows"] = result.affected_rows
    if result.message:
        payload["message"] = result.message
    return json.dumps(payload, ensure_ascii=False)


def _json_value(value):
    if isinstance(value, tuple):
        return list(value)
    return value
