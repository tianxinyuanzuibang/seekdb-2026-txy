"""Error codes and exceptions for MiniVectorDB.

The contest statement requires that unsupported statements must return an
explicit error instead of silently executing with a different meaning.  We
model MySQL-style error codes so the mysqltest-style driver can render them
as ``ERROR <code>: <message>``.
"""


class SQLError(Exception):
    """A SQL-level error that is reported back to the client."""

    def __init__(self, code, message, sqlstate="HY000"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.sqlstate = sqlstate

    def format(self):
        return "ERROR {}: {}".format(self.code, self.message)

    def __repr__(self):  # pragma: no cover - debugging helper
        return "SQLError({!r}, {!r})".format(self.code, self.message)


# --- error codes -----------------------------------------------------------
ER_PARSE_ERROR = 1064
ER_NO_SUCH_TABLE = 1146
ER_BAD_FIELD_ERROR = 1054
ER_DUP_ENTRY = 1062
ER_BAD_TABLE = 1051
ER_BAD_FIELD_ERROR_2 = 1054
ER_WRONG_VALUE_COUNT_ON_ROW = 1136
ER_DATA_TOO_LONG = 1406
ER_TRUNCATED_WRONG_VALUE = 1292
ER_NO_DEFAULT_VALUE = 1364
ER_UNKNOWN_TABLE = 1109
ER_TABLE_EXISTS = 1050
ER_NOT_SUPPORTED_YET = 1235
ER_BAD_NULL_ERROR = 1048
ER_CANT_REMOVE_ALL_FIELDS = 1091
ER_MULTIPLE_PRI_KEY = 1068
ER_DUP_FIELDNAME = 1060
ER_KEY_COLUMN_DOES_NOT_EXITS = 1072
ER_WRONG_VALUE_COUNT = 1058
ER_LOCK_WAIT_TIMEOUT = 1205
ER_GET_ERRNO = 1030
ER_UNKNOWN_ERROR = 1105
ER_INVALID_VECTOR = 1367
ER_SP_DOES_NOT_EXIST = 1305


def parse_error(message):
    return SQLError(ER_PARSE_ERROR, message, "42000")


def no_such_table(name):
    return SQLError(ER_NO_SUCH_TABLE, "Table 'test.{}' doesn't exist".format(name))


def bad_field(name):
    return SQLError(
        ER_BAD_FIELD_ERROR, "Unknown column '{}' in 'field list'".format(name)
    )


def duplicate_key(name):
    return SQLError(
        ER_DUP_ENTRY, "Duplicate entry '{}' for key 'PRIMARY'".format(name)
    )


def not_supported(message):
    return SQLError(ER_NOT_SUPPORTED_YET, message, "42000")


def invalid_value(message):
    return SQLError(ER_TRUNCATED_WRONG_VALUE, message, "22007")
