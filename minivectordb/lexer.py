"""Tokenizer for the MiniVectorDB SQL subset.

Standard library only.  The tokenizer is intentionally small and fast: it is
run for every statement that reaches the server.

Token kinds (see ``docs/CONTRACT.md`` section 3):

``ident``   identifier (case-insensitive comparison, original spelling kept)
``keyword`` reserved word, ``value`` is the upper-cased word
``number``  integer / float / exponent, ``value`` is ``int`` or ``float``
``string``  quoted literal, ``value`` is the decoded ``str``
``punct``   operator or punctuation, ``value`` is the literal text
``vector``  ``[1,2,3]``, ``value`` is a ``list[float]``
``eof``     end of input, ``value`` is ``None``
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from .errors import parse_error

__all__ = ["Token", "tokenize", "KEYWORDS"]


# Reserved words understood by the parser.  Matching is case-insensitive.
KEYWORDS = frozenset(
    {
        "ADD",
        "ALL",
        "ANN",
        "AND",
        "AS",
        "ASC",
        "BEGIN",
        "BETWEEN",
        "BIGINT",
        "BY",
        "COLUMNS",
        "COMMIT",
        "CREATE",
        "DATABASES",
        "DEFAULT",
        "DELETE",
        "DESC",
        "DESCRIBE",
        "DISTINCT",
        "DROP",
        "END",
        "EXACT",
        "EXISTS",
        "EXPLAIN",
        "FALSE",
        "FROM",
        "FULL",
        "GROUP",
        "HAVING",
        "IF",
        "IGNORE",
        "IN",
        "INDEX",
        "INDEXES",
        "INNER",
        "INSERT",
        "INTO",
        "IS",
        "KEY",
        "LIKE",
        "LIMIT",
        "NOT",
        "NULL",
        "OFFSET",
        "ON",
        "OR",
        "ORDER",
        "OUTER",
        "PARAMETERS",
        "PRIMARY",
        "ROLLBACK",
        "SELECT",
        "SET",
        "SHOW",
        "START",
        "TABLE",
        "TABLES",
        "TRANSACTION",
        "TRUE",
        "UNIQUE",
        "UPDATE",
        "USE",
        "USING",
        "VALUES",
        "VARCHAR",
        "VECTOR",
        "WHERE",
        "WITH",
    }
)

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_IDENT_CHARS = _IDENT_START | set("0123456789")
_DIGITS = set("0123456789")
_WS = " \t\r\n\f\v"

# Multi-character operators, longest first.
_OPERATORS = ("<=>", "<>", "!=", "<=", ">=", "||", "&&", ":=")


@dataclass
class Token:
    """A single lexical token."""

    kind: str
    value: Any
    pos: int

    def __repr__(self):  # pragma: no cover - debugging helper
        return "Token({!r}, {!r}, {})".format(self.kind, self.value, self.pos)


def _is_ident_start(ch: str) -> bool:
    return ch in _IDENT_START or ch.isalpha()


def _is_ident_char(ch: str) -> bool:
    return ch in _IDENT_CHARS or ch.isalnum()


def tokenize(sql: str) -> List[Token]:
    """Turn *sql* into a list of :class:`Token` (always ending with ``eof``)."""

    if sql is None:
        raise parse_error("cannot tokenize None")
    if not isinstance(sql, str):
        raise parse_error("SQL input must be a string")

    tokens: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # --- whitespace ---
        if ch in _WS:
            i += 1
            continue

        # --- comments ---
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise parse_error("unterminated comment at position {}".format(i))
            i = j + 2
            continue

        # --- string literals ---
        if ch == "'" or ch == '"':
            start = i
            value, i = _scan_string(sql, i)
            tokens.append(Token("string", value, start))
            continue

        # --- backquoted identifier ---
        if ch == "`":
            start = i
            value, i = _scan_quoted_ident(sql, i)
            tokens.append(Token("ident", value, start))
            continue

        # --- numbers ---
        if ch in _DIGITS or (
            ch == "." and i + 1 < n and sql[i + 1] in _DIGITS
        ):
            start = i
            value, i = _scan_number(sql, i)
            tokens.append(Token("number", value, start))
            continue

        # --- vector literal ---
        if ch == "[":
            vec, end = _try_scan_vector(sql, i)
            if vec is not None:
                tokens.append(Token("vector", vec, i))
                i = end
                continue

        # --- identifiers / keywords (possibly prefixed with '@') ---
        if ch == "@":
            j = i + 1
            while j < n and _is_ident_char(sql[j]):
                j += 1
            if j == i + 1:
                raise parse_error("expected a variable name after '@'")
            tokens.append(Token("ident", sql[i:j], i))
            i = j
            continue
        if _is_ident_start(ch):
            j = i + 1
            while j < n and _is_ident_char(sql[j]):
                j += 1
            word = sql[i:j]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token("keyword", upper, i))
            else:
                tokens.append(Token("ident", word, i))
            i = j
            continue

        # --- punctuation / operators ---
        matched = None
        for op in _OPERATORS:
            if sql.startswith(op, i):
                matched = op
                break
        if matched is None:
            matched = ch
        tokens.append(Token("punct", matched, i))
        i += len(matched)

    tokens.append(Token("eof", None, n))
    return tokens


def _scan_string(sql: str, i: int):
    quote = sql[i]
    i += 1
    out = []
    n = len(sql)
    while True:
        if i >= n:
            raise parse_error(
                "unterminated string literal at position {}".format(i)
            )
        ch = sql[i]
        if ch == "\\" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt == quote or nxt == "\\":
                out.append(nxt)
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "0":
                out.append("\0")
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                out.append(quote)
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1


def _scan_quoted_ident(sql: str, i: int):
    i += 1
    j = sql.find("`", i)
    if j < 0:
        raise parse_error("unterminated quoted identifier at position {}".format(i))
    return sql[i:j], j + 1


def _scan_number(sql: str, i: int):
    n = len(sql)
    start = i
    is_float = False
    while i < n and sql[i] in _DIGITS:
        i += 1
    if i < n and sql[i] == ".":
        is_float = True
        i += 1
        while i < n and sql[i] in _DIGITS:
            i += 1
    if i < n and sql[i] in "eE":
        j = i + 1
        if j < n and sql[j] in "+-":
            j += 1
        if j < n and sql[j] in _DIGITS:
            is_float = True
            i = j
            while i < n and sql[i] in _DIGITS:
                i += 1
    text = sql[start:i]
    if is_float:
        try:
            value = float(text)
        except ValueError:  # pragma: no cover - defensive
            raise parse_error("invalid numeric literal {!r}".format(text))
    else:
        value = int(text)
    return value, i


def _try_scan_vector(sql: str, i: int):
    """Try to lex ``[ ... ]`` as a vector literal.

    Returns ``(values, end_index)`` or ``(None, i)`` when the bracket does
    not contain a well formed list of numbers.
    """
    n = len(sql)
    j = i + 1
    values = []
    while True:
        while j < n and sql[j] in _WS:
            j += 1
        if j < n and sql[j] == "]":
            return values, j + 1
        sign = 1.0
        if j < n and sql[j] in "+-":
            if sql[j] == "-":
                sign = -1.0
            j += 1
        if j >= n or not (sql[j] in _DIGITS or (sql[j] == "." and j + 1 < n and sql[j + 1] in _DIGITS)):
            return None, i
        value, j = _scan_number(sql, j)
        if not isinstance(value, float):
            value = float(value)
        values.append(sign * value)
        while j < n and sql[j] in _WS:
            j += 1
        if j < n and sql[j] == ",":
            j += 1
            continue
        if j < n and sql[j] == "]":
            return values, j + 1
        return None, i
