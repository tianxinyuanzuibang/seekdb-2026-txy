"""Recursive-descent parser for the MiniVectorDB SQL subset.

``parse(sql)`` returns a list of statements; ``parse_one(sql)`` returns
exactly one statement and raises when the input holds more than one.

Only the grammar described in ``docs/CONTRACT.md`` section 3 is accepted.
Anything else raises ``errors.parse_error(...)``.
"""

from typing import Any, List, Optional, Tuple

from .ast_nodes import (
    Show,
    Between,
    BinaryOp,
    ColumnDef,
    ColumnRef,
    Commit,
    CreateTable,
    CreateVectorIndex,
    Delete,
    Desc,
    DropTable,
    DropVectorIndex,
    Explain,
    FunctionCall,
    InList,
    Insert,
    IsNull,
    Like,
    Literal,
    NullLiteral,
    Rollback,
    Select,
    SelectItem,
    SetMode,
    Show,
    UnaryOp,
    Update,
    Use,
    VectorLiteral,
    Begin,
)

Stmt = Any  # ast_nodes has no common Stmt base class


def _make_show(what, table=None):
    """Build a ``Show`` node.

    ``ast_nodes.Show`` only carries ``what``; the table name of
    ``SHOW COLUMNS FROM t`` is attached as an extra ``table`` attribute.
    The shared ``ast_nodes.Show`` should gain ``table: Optional[str] = None``.
    """
    node = Show(what)
    node.table = table
    return node
from .errors import parse_error
from .lexer import Token, tokenize

__all__ = ["parse", "parse_one", "Parser"]


# Distance functions that mark a SELECT as a vector search.
DISTANCE_FUNCTIONS = frozenset(
    {
        "L2_DISTANCE",
        "L2",
        "EUCLIDEAN_DISTANCE",
        "COSINE_DISTANCE",
        "COSINE",
        "INNER_PRODUCT",
        "IP",
    }
)

# Words that clearly look like a search-mode hint.
_MODE_WORDS = {
    "EXACT": "exact",
    "ANN": "ann",
    "APPROX": "ann",
    "APPROXIMATE": "ann",
    "APPROXIMATELY": "ann",
}

# Parameter names that carry the search mode.
_MODE_PARAM_NAMES = frozenset(
    {
        "MODE",
        "SEARCH_MODE",
        "ANN",
        "APPROX",
        "APPROXIMATE",
        "INDEX_TYPE",
        "USING_INDEX",
        "SEARCH",
        "METHOD",
        "ALGORITHM",
    }
)

_TRUE_WORDS = {"TRUE", "1", "ON", "YES"}
_FALSE_WORDS = {"FALSE", "0", "OFF", "NO"}


class Parser:
    """Recursive-descent parser over a token list."""

    def __init__(self, sql: str):
        self.tokens: List[Token] = tokenize(sql)
        self.index = 0
        self.sql = sql

    # --- token helpers -----------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 0) -> Token:
        idx = self.index + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.index]
        if tok.kind != "eof":
            self.index += 1
        return tok

    def at_eof(self) -> bool:
        return self.current.kind == "eof"

    def error(self, message: str) -> "SQLError":
        tok = self.current
        near = tok.value if tok.kind != "eof" else "end of input"
        return parse_error("{} near {!r} at position {}".format(message, near, tok.pos))

    # --- matchers ----------------------------------------------------------

    def match_keyword(self, *words: str) -> Optional[str]:
        tok = self.current
        if tok.kind == "keyword" and tok.value in words:
            self.advance()
            return tok.value
        return None

    def peek_keyword(self, *words: str) -> bool:
        tok = self.current
        return tok.kind == "keyword" and tok.value in words

    def match_punct(self, *chars: str) -> Optional[str]:
        tok = self.current
        if tok.kind == "punct" and tok.value in chars:
            self.advance()
            return tok.value
        return None

    def peek_punct(self, *chars: str) -> bool:
        tok = self.current
        return tok.kind == "punct" and tok.value in chars

    def expect_punct(self, char: str):
        if self.match_punct(char) is None:
            raise self.error("expected {!r}".format(char))

    def expect_keyword(self, word: str):
        if self.match_keyword(word) is None:
            raise self.error("expected {}".format(word))

    def expect_identifier(self) -> str:
        tok = self.current
        if tok.kind == "ident":
            self.advance()
            return tok.value
        raise self.error("expected an identifier")

    # --- entry points ------------------------------------------------------

    def parse_statements(self) -> List[Stmt]:
        stmts: List[Stmt] = []
        while True:
            while self.match_punct(";") is not None:
                pass
            if self.at_eof():
                break
            stmts.append(self.parse_statement())
            if self.match_punct(";") is None:
                if not self.at_eof():
                    raise self.error("expected ';' or end of input")
        return stmts

    def parse_statement(self) -> Stmt:
        tok = self.current
        if tok.kind == "keyword":
            handler = {
                "CREATE": self.parse_create,
                "DROP": self.parse_drop,
                "INSERT": self.parse_insert,
                "UPDATE": self.parse_update,
                "DELETE": self.parse_delete,
                "SELECT": self.parse_select,
                "BEGIN": self.parse_begin,
                "START": self.parse_start,
                "COMMIT": self.parse_commit,
                "ROLLBACK": self.parse_rollback,
                "SHOW": self.parse_show,
                "DESC": self.parse_desc,
                "DESCRIBE": self.parse_desc,
                "EXPLAIN": self.parse_explain,
                "USE": self.parse_use,
                "SET": self.parse_set,
            }.get(tok.value)
            if handler is not None:
                return handler()
        raise self.error("unsupported statement")

    # --- DDL ---------------------------------------------------------------

    def parse_create(self) -> Stmt:
        self.expect_keyword("CREATE")
        if self.match_keyword("VECTOR") is not None:
            self.expect_keyword("INDEX")
            index_name = self.expect_identifier()
            self.expect_keyword("ON")
            table_name = self.expect_identifier()
            self.expect_punct("(")
            column_name = self.expect_identifier()
            self.expect_punct(")")
            return CreateVectorIndex(index_name, table_name, column_name)
        if self.match_keyword("TABLE") is None:
            raise self.error("unsupported CREATE statement")
        if_exists = False
        name = self.expect_identifier()
        if self.peek_keyword("IF") and self.peek(1).kind == "keyword" and self.peek(1).value == "EXISTS":
            self.advance()
            self.advance()
            if_exists = True
            name = self.expect_identifier()
        self.expect_punct("(")
        columns: List[ColumnDef] = []
        primary_key: Optional[str] = None
        while True:
            if self.peek_keyword("PRIMARY") and self.peek(1).kind == "keyword" and self.peek(1).value == "KEY":
                self.advance()
                self.advance()
                self.expect_punct("(")
                pk_name = self.expect_identifier()
                self.expect_punct(")")
                if primary_key is not None:
                    raise parse_error("multiple primary key defined")
                primary_key = pk_name
            elif self.peek_keyword("UNIQUE") or self.peek_keyword("KEY") or self.peek_keyword("INDEX"):
                raise self.error("unsupported table constraint")
            else:
                col = self.parse_column_def()
                if col.primary_key:
                    if primary_key is not None:
                        raise parse_error("multiple primary key defined")
                    primary_key = col.name
                columns.append(col)
            if self.match_punct(",") is None:
                break
        self.expect_punct(")")
        # trailing table options are not part of the subset
        if not self.at_eof() and not self.peek_punct(";"):
            raise self.error("unsupported CREATE TABLE options")
        return CreateTable(name, columns, primary_key, None)

    def parse_column_def(self) -> ColumnDef:
        name = self.expect_identifier()
        type_name = self.current.value
        if self.current.kind != "keyword" or type_name not in ("BIGINT", "VARCHAR", "VECTOR"):
            raise self.error("unsupported column type")
        self.advance()
        length = None
        vector_dim = None
        if self.peek_punct("("):
            self.advance()
            tok = self.current
            if tok.kind != "number" or not isinstance(tok.value, int):
                raise self.error("expected an integer length")
            self.advance()
            if type_name == "VECTOR":
                vector_dim = tok.value
            else:
                length = tok.value
            self.expect_punct(")")
        primary_key = False
        not_null = False
        while True:
            if self.match_keyword("PRIMARY") is not None:
                self.expect_keyword("KEY")
                primary_key = True
                continue
            if self.match_keyword("NOT") is not None:
                self.expect_keyword("NULL")
                not_null = True
                continue
            if self.match_keyword("NULL") is not None:
                continue
            if self.match_keyword("UNIQUE") is not None:
                continue
            if self.match_keyword("DEFAULT") is not None:
                self.parse_expr()
                continue
            break
        if type_name == "VARCHAR" and length is None:
            raise parse_error("VARCHAR requires a length")
        if type_name == "VECTOR" and vector_dim is None:
            raise parse_error("VECTOR requires a dimension")
        return ColumnDef(
            name=name,
            type_name=type_name,
            length=length,
            vector_dim=vector_dim,
            primary_key=primary_key,
            not_null=not_null,
        )

    def parse_drop(self) -> Stmt:
        self.expect_keyword("DROP")
        if self.match_keyword("VECTOR") is not None:
            self.expect_keyword("INDEX")
            return DropVectorIndex(self.expect_identifier())
        if self.match_keyword("TABLE") is None:
            raise self.error("unsupported DROP statement")
        if_exists = False
        if self.peek_keyword("IF") and self.peek(1).kind == "keyword" and self.peek(1).value == "EXISTS":
            self.advance()
            self.advance()
            if_exists = True
        return DropTable(self.expect_identifier(), if_exists)

    # --- DML ---------------------------------------------------------------

    def parse_insert(self) -> Stmt:
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.expect_identifier()
        columns = None
        if self.peek_punct("("):
            self.advance()
            columns = []
            while True:
                columns.append(self.expect_identifier())
                if self.match_punct(",") is None:
                    break
            self.expect_punct(")")
        if self.match_keyword("SET") is not None:
            assignments = []
            while True:
                col = self.expect_identifier()
                self.expect_punct("=")
                assignments.append((col, self.parse_expr()))
                if self.match_punct(",") is None:
                    break
            rows = [[expr for _, expr in assignments]]
            return Insert(table, columns, rows)
        if self.match_keyword("VALUES") is None:
            raise self.error("expected VALUES or SET")
        rows: List[List[Any]] = []
        while True:
            self.expect_punct("(")
            values = []
            while True:
                values.append(self.parse_expr())
                if self.match_punct(",") is None:
                    break
            self.expect_punct(")")
            rows.append(values)
            if self.match_punct(",") is None:
                break
        return Insert(table, columns, rows)

    def parse_update(self) -> Stmt:
        self.expect_keyword("UPDATE")
        table = self.expect_identifier()
        self.expect_keyword("SET")
        assignments = []
        while True:
            col = self.expect_identifier()
            self.expect_punct("=")
            assignments.append((col, self.parse_expr()))
            if self.match_punct(",") is None:
                break
        where = None
        if self.match_keyword("WHERE") is not None:
            where = self.parse_expr()
        return Update(table, assignments, where)

    def parse_delete(self) -> Stmt:
        self.expect_keyword("DELETE")
        table = None
        if self.match_keyword("FROM") is not None:
            table = self.expect_identifier()
        where = None
        if self.match_keyword("WHERE") is not None:
            where = self.parse_expr()
        return Delete(table, where)

    # --- SELECT ------------------------------------------------------------

    def parse_select(self) -> Stmt:
        self.expect_keyword("SELECT")
        items = self.parse_select_items()
        table = None
        if self.match_keyword("FROM") is not None:
            table = self.expect_identifier()
        search_mode = "exact"
        mode_explicit = False
        # table hint:  FROM t WITH (mode='ann')
        if self.peek_keyword("WITH") and self.peek(1).kind == "punct" and self.peek(1).value == "(":
            self.advance()
            hint = self.parse_hint_body()
            if hint is not None:
                search_mode = hint
                mode_explicit = True
        where = None
        if self.match_keyword("WHERE") is not None:
            where = self.parse_expr()
        order_by: List[Tuple[Any, bool]] = []
        if self.match_keyword("ORDER") is not None:
            self.expect_keyword("BY")
            while True:
                expr = self.parse_expr()
                asc = True
                if self.match_keyword("DESC") is not None:
                    asc = False
                elif self.match_keyword("ASC") is not None:
                    asc = True
                order_by.append((expr, asc))
                if self.match_punct(",") is None:
                    break
        limit = None
        offset = None
        if self.match_keyword("LIMIT") is not None:
            first = self.parse_int_literal()
            if self.match_punct(",") is not None:
                # MySQL "LIMIT offset, count"
                second = self.parse_int_literal()
                offset = first
                limit = second
            else:
                limit = first
                if self.match_keyword("OFFSET") is not None:
                    offset = self.parse_int_literal()
        # mode hint after LIMIT
        search_mode, mode_explicit = self.parse_mode_hint(search_mode,
                                                          mode_explicit)
        # a second hint form may appear before the table hint; handled above
        select = Select(
            items=items,
            table=table,
            where=where,
            order_by=order_by,
            limit=limit,
            offset=offset,
            is_vector_search=False,
            vector_expr=None,
            search_mode=search_mode,
            mode_explicit=mode_explicit,
        )
        detect_vector_search(select)
        return select

    def parse_select_items(self) -> List[SelectItem]:
        items = []
        while True:
            if self.peek_punct("*"):
                self.advance()
                items.append(SelectItem(expr=None, alias=None, star=True))
            else:
                expr = self.parse_expr()
                alias = None
                if self.match_keyword("AS") is not None:
                    alias = self.expect_identifier()
                elif self.current.kind == "ident":
                    alias = self.current.value
                    self.advance()
                items.append(SelectItem(expr=expr, alias=alias, star=False))
            if self.match_punct(",") is None:
                break
        if not items:
            raise self.error("empty select list")
        return items

    def parse_int_literal(self) -> int:
        tok = self.current
        if tok.kind != "number" or not isinstance(tok.value, int):
            raise self.error("expected an integer")
        self.advance()
        return tok.value

    # --- mode hints --------------------------------------------------------

    def parse_hint_body(self) -> Optional[str]:
        """Parse ``( name=value, ... )`` returning a mode if one is found."""
        self.expect_punct("(")
        mode = None
        while True:
            tok = self.current
            if tok.kind == "ident":
                name = tok.value.upper()
                self.advance()
            elif tok.kind == "keyword":
                name = tok.value
                self.advance()
            elif tok.kind == "string":
                name = tok.value.upper()
                self.advance()
                if name in _MODE_PARAM_NAMES:
                    mode = "ann" if _is_ann_value(tok.value) else "exact"
                if self.match_punct(",") is None:
                    break
                continue
            else:
                raise self.error("malformed hint")
            if self.match_punct("=") is None:
                # bare flag, e.g. (ann)
                if name in ("ANN", "APPROX", "APPROXIMATE"):
                    mode = "ann"
                elif name == "EXACT":
                    mode = "exact"
            else:
                value_tok = self.current
                if value_tok.kind == "string":
                    self.advance()
                    val = value_tok.value
                elif value_tok.kind == "number":
                    self.advance()
                    val = value_tok.value
                elif value_tok.kind == "keyword":
                    self.advance()
                    val = value_tok.value
                else:
                    raise self.error("malformed hint value")
                if name in _MODE_PARAM_NAMES:
                    mode = "ann" if _is_ann_value(val) else "exact"
            if self.match_punct(",") is None:
                break
        self.expect_punct(")")
        return mode

    def parse_mode_hint(self, current_mode: str, explicit: bool = False):
        """Consume a trailing search-mode hint, if present.

        Returns ``(mode, explicit)``.  Unrecognised trailing tokens raise a
        parse error.
        """
        while True:
            tok = self.current
            if tok.kind == "keyword" and tok.value == "USING":
                # USING ANN / USING EXACT
                nxt = self.peek(1)
                nxt_word = nxt.value.upper() if nxt.kind in ("keyword", "ident") else None
                if nxt_word in _MODE_WORDS:
                    self.advance()
                    self.advance()
                    current_mode = _MODE_WORDS[nxt_word]
                    explicit = True
                    continue
                if nxt.kind == "keyword" and nxt.value == "INDEX":
                    # USING INDEX ... - not a mode hint we understand
                    break
                break
            if tok.kind == "keyword" and tok.value == "PARAMETERS":
                nxt = self.peek(1)
                if nxt.kind == "punct" and nxt.value == "(":
                    self.advance()
                    mode = self.parse_hint_body()
                    if mode is not None:
                        current_mode = mode
                        explicit = True
                    continue
                break
            if tok.kind == "keyword" and tok.value == "WITH":
                nxt = self.peek(1)
                if nxt.kind == "punct" and nxt.value == "(":
                    self.advance()
                    mode = self.parse_hint_body()
                    if mode is not None:
                        current_mode = mode
                        explicit = True
                    continue
                break
            if tok.kind == "ident" and tok.value.upper() in _MODE_WORDS:
                self.advance()
                current_mode = _MODE_WORDS[tok.value.upper()]
                explicit = True
                continue
            if tok.kind == "keyword" and tok.value in _MODE_WORDS:
                self.advance()
                current_mode = _MODE_WORDS[tok.value]
                explicit = True
                continue
            break
        # Anything left over that is not a statement separator is an error.
        if not self.at_eof() and not self.peek_punct(";"):
            raise self.error("unexpected token")
        return current_mode, explicit

    # --- transactions ------------------------------------------------------

    def parse_begin(self) -> Stmt:
        self.expect_keyword("BEGIN")
        return Begin()

    def parse_start(self) -> Stmt:
        self.expect_keyword("START")
        self.expect_keyword("TRANSACTION")
        return Begin()

    def parse_commit(self) -> Stmt:
        self.expect_keyword("COMMIT")
        return Commit()

    def parse_rollback(self) -> Stmt:
        self.expect_keyword("ROLLBACK")
        return Rollback()

    # --- misc --------------------------------------------------------------

    def parse_show(self) -> Stmt:
        self.expect_keyword("SHOW")
        if self.match_keyword("TABLES") is not None:
            return Show("tables")
        if self.match_keyword("DATABASES") is not None:
            return Show("databases")
        if self.match_keyword("COLUMNS") is not None:
            return _make_show("columns", self.parse_from_clause())
        if self.match_keyword("INDEXES") is not None:
            return _make_show("indexes", self.parse_from_clause())
        if self.match_keyword("INDEX") is not None:
            return _make_show("indexes", self.parse_from_clause())
        if self.match_keyword("KEYS") is not None:
            return _make_show("indexes", self.parse_from_clause())
        raise self.error("unsupported SHOW statement")

    def parse_from_clause(self) -> str:
        # ``FROM t`` / ``FROM db.t`` / ``IN t``
        if self.match_keyword("FROM") is None and self.match_keyword("IN") is None:
            raise self.error("expected FROM")
        if self.current.kind != "ident":
            raise self.error("expected a table name")
        name = self.current.value
        self.advance()
        if self.peek_punct("."):
            self.advance()
            return self.expect_identifier()
        return name

    def parse_desc(self) -> Stmt:
        self.advance()  # DESC / DESCRIBE
        table = self.expect_identifier()
        return Desc(table)

    def parse_explain(self) -> Stmt:
        self.expect_keyword("EXPLAIN")
        return Explain(self.parse_statement())

    def parse_use(self) -> Stmt:
        self.expect_keyword("USE")
        return Use(self.expect_identifier())

    def parse_set(self) -> Stmt:
        self.expect_keyword("SET")
        # optional session/global scope
        if self.peek_keyword("SESSION") or self.peek_keyword("GLOBAL") or self.peek_keyword("LOCAL"):
            self.advance()
        name_tok = self.current
        if name_tok.kind not in ("ident", "keyword"):
            raise self.error("expected a variable name")
        self.advance()
        name = name_tok.value
        if self.match_punct(":=") is None and self.match_punct("=") is None:
            raise self.error("expected '='")
        value_tok = self.current
        if value_tok.kind == "string":
            self.advance()
            value = value_tok.value
        elif value_tok.kind == "number":
            self.advance()
            value = value_tok.value
        elif value_tok.kind == "keyword":
            self.advance()
            value = value_tok.value
        else:
            raise self.error("expected a literal value")
        if not self.at_eof() and not self.peek_punct(";"):
            raise self.error("unexpected token")
        return SetMode(name=lstrip_at(name), value=str(value))

    # --- expressions -------------------------------------------------------

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.match_keyword("OR") is not None:
            right = self.parse_and()
            left = BinaryOp("OR", left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.match_keyword("AND") is not None:
            right = self.parse_not()
            left = BinaryOp("AND", left, right)
        return left

    def parse_not(self):
        if self.match_keyword("NOT") is not None:
            return UnaryOp("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_additive()
        while True:
            tok = self.current
            if tok.kind == "punct" and tok.value in ("=", "<", "<=", ">", ">=", "<>", "!="):
                self.advance()
                right = self.parse_additive()
                op = "<>" if tok.value == "<>" else ("!=" if tok.value == "!=" else tok.value)
                left = BinaryOp(op, left, right)
                continue
            if tok.kind == "keyword" and tok.value == "IS":
                self.advance()
                negated = self.match_keyword("NOT") is not None
                if self.match_keyword("NULL") is None:
                    raise self.error("expected NULL")
                left = IsNull(left, negated)
                continue
            if tok.kind == "keyword" and tok.value == "IN":
                self.advance()
                self.expect_punct("(")
                values = []
                if not self.peek_punct(")"):
                    while True:
                        values.append(self.parse_expr())
                        if self.match_punct(",") is None:
                            break
                self.expect_punct(")")
                left = InList(left, values, False)
                continue
            if tok.kind == "keyword" and tok.value == "BETWEEN":
                self.advance()
                low = self.parse_additive()
                self.expect_keyword("AND")
                high = self.parse_additive()
                left = Between(left, low, high, False)
                continue
            if tok.kind == "keyword" and tok.value == "LIKE":
                self.advance()
                pattern = self.parse_additive()
                left = Like(left, pattern, False)
                continue
            if tok.kind == "keyword" and tok.value == "NOT":
                nxt = self.peek(1)
                if nxt.kind == "keyword" and nxt.value == "IN":
                    self.advance()
                    self.advance()
                    self.expect_punct("(")
                    values = []
                    if not self.peek_punct(")"):
                        while True:
                            values.append(self.parse_expr())
                            if self.match_punct(",") is None:
                                break
                    self.expect_punct(")")
                    left = InList(left, values, True)
                    continue
                if nxt.kind == "keyword" and nxt.value == "BETWEEN":
                    self.advance()
                    self.advance()
                    low = self.parse_additive()
                    self.expect_keyword("AND")
                    high = self.parse_additive()
                    left = Between(left, low, high, True)
                    continue
                if nxt.kind == "keyword" and nxt.value == "LIKE":
                    self.advance()
                    self.advance()
                    pattern = self.parse_additive()
                    left = Like(left, pattern, True)
                    continue
            break
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while True:
            tok = self.current
            if tok.kind == "punct" and tok.value in ("+", "-"):
                self.advance()
                right = self.parse_multiplicative()
                left = BinaryOp(tok.value, left, right)
                continue
            break
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while True:
            tok = self.current
            if tok.kind == "punct" and tok.value in ("*", "/"):
                self.advance()
                right = self.parse_unary()
                left = BinaryOp(tok.value, left, right)
                continue
            break
        return left

    def parse_unary(self):
        tok = self.current
        if tok.kind == "punct" and tok.value in ("-", "+"):
            self.advance()
            return UnaryOp(tok.value, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        tok = self.current
        if tok.kind == "number":
            self.advance()
            return Literal(tok.value)
        if tok.kind == "string":
            self.advance()
            return Literal(tok.value)
        if tok.kind == "vector":
            self.advance()
            return VectorLiteral(list(tok.value))
        if tok.kind == "keyword":
            if tok.value == "NULL":
                self.advance()
                return NullLiteral()
            if tok.value in ("TRUE", "FALSE"):
                self.advance()
                return Literal(tok.value == "TRUE")
        if tok.kind == "ident":
            # function call?
            nxt = self.peek(1)
            if nxt.kind == "punct" and nxt.value == "(":
                name = tok.value.upper()
                self.advance()
                self.advance()
                args = []
                if not self.peek_punct(")"):
                    while True:
                        # COUNT(*) - the star is an argument, not a
                        # projection wildcard.
                        if self.peek_punct("*"):
                            self.advance()
                            args.append(UnaryOp("*", None))
                        else:
                            args.append(self.parse_expr())
                        if self.match_punct(",") is None:
                            break
                self.expect_punct(")")
                return FunctionCall(name, args)
            self.advance()
            return ColumnRef(tok.value)
        if tok.kind == "punct" and tok.value == "(":
            self.advance()
            expr = self.parse_expr()
            self.expect_punct(")")
            return expr
        raise self.error("expected an expression")


# --- helpers ---------------------------------------------------------------


def lstrip_at(name: str) -> str:
    """Strip a leading ``@`` from a session variable name."""
    return name[1:] if name.startswith("@") else name


def _is_ann_value(value) -> bool:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("ann", "approx", "approximate", "true", "1", "yes", "on"):
            return True
        if v in ("exact", "false", "0", "no", "off", ""):
            return False
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def detect_vector_search(select: Select) -> None:
    """Mark *select* as a vector search if an ORDER BY term is a distance call."""
    for expr, _asc in select.order_by:
        found = _find_distance_call(expr)
        if found is not None:
            select.is_vector_search = True
            select.vector_expr = found
            return


def _find_distance_call(expr):
    if isinstance(expr, FunctionCall):
        if expr.name.upper() in DISTANCE_FUNCTIONS:
            return expr
        for arg in expr.args:
            found = _find_distance_call(arg)
            if found is not None:
                return found
        return None
    if isinstance(expr, BinaryOp):
        left = _find_distance_call(expr.left)
        if left is not None:
            return left
        return _find_distance_call(expr.right)
    if isinstance(expr, UnaryOp):
        return _find_distance_call(expr.operand)
    return None


# --- public API ------------------------------------------------------------


def parse(sql: str) -> List[Stmt]:
    """Parse *sql* into a list of statements."""
    return Parser(sql).parse_statements()


def parse_one(sql: str) -> Stmt:
    """Parse exactly one statement out of *sql*."""
    stmts = parse(sql)
    if len(stmts) != 1:
        raise parse_error(
            "expected exactly one statement, got {}".format(len(stmts))
        )
    return stmts[0]
