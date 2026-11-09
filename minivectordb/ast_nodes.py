"""AST node definitions for the MiniVectorDB SQL subset.

These dataclasses are the shared contract between the parser and the
executor.  They are intentionally plain and immutable-by-convention so that
the executor can pattern match on them cheaply.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional


# --- expressions -----------------------------------------------------------


@dataclass
class Literal:
    """A constant scalar: int, float, str, or vector."""

    value: Any


@dataclass
class NullLiteral:
    pass


@dataclass
class ColumnRef:
    name: str


@dataclass
class UnaryOp:
    op: str  # '-', '+'
    operand: "Expr"


@dataclass
class BinaryOp:
    op: str  # '=', '<', '<=', '>', '>=', 'AND', 'OR', '+', '-', '*', '/'
    left: "Expr"
    right: "Expr"


@dataclass
class FunctionCall:
    name: str  # upper-cased function name, e.g. 'L2_DISTANCE'
    args: List["Expr"]


@dataclass
class InList:
    expr: "Expr"
    values: List["Expr"]
    negated: bool = False


@dataclass
class Between:
    expr: "Expr"
    low: "Expr"
    high: "Expr"
    negated: bool = False


@dataclass
class Like:
    expr: "Expr"
    pattern: "Expr"
    negated: bool = False


@dataclass
class IsNull:
    expr: "Expr"
    negated: bool = False


@dataclass
class VectorLiteral:
    """``[1, 2, 3]`` - a fixed dimension float32 vector."""

    values: List[float]


# --- statements ------------------------------------------------------------


@dataclass
class CreateTable:
    name: str
    columns: List["ColumnDef"]
    primary_key: Optional[str] = None
    vector_column: Optional[str] = None


@dataclass
class ColumnDef:
    name: str
    type_name: str  # 'BIGINT', 'VARCHAR', 'VECTOR'
    length: Optional[int] = None
    vector_dim: Optional[int] = None
    primary_key: bool = False
    not_null: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class CreateVectorIndex:
    index_name: str
    table_name: str
    column_name: str


@dataclass
class DropVectorIndex:
    index_name: str


@dataclass
class Insert:
    table: str
    columns: Optional[List[str]]
    rows: List[List["Expr"]]


@dataclass
class Update:
    table: str
    assignments: List[tuple]  # list of (column_name, Expr)
    where: Optional["Expr"]


@dataclass
class Delete:
    table: str
    where: Optional["Expr"]


@dataclass
class SelectItem:
    expr: "Expr"
    alias: Optional[str] = None
    star: bool = False


@dataclass
class Select:
    items: List[SelectItem]
    table: Optional[str] = None
    where: Optional["Expr"] = None
    order_by: List[tuple] = field(default_factory=list)  # (Expr, asc: bool)
    limit: Optional[int] = None
    offset: Optional[int] = None
    # Vector top-k search: ORDER BY <vector expr> LIMIT k
    is_vector_search: bool = False
    vector_expr: Optional["Expr"] = None
    search_mode: str = "exact"  # 'exact' or 'ann'
    # True when the statement explicitly carried a mode hint.  This lets the
    # executor tell "the user asked for exact" apart from "nobody said
    # anything", so a session default (SET @mvd_mode) can still apply.
    mode_explicit: bool = False


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


@dataclass
class Show:
    what: str  # 'tables', 'columns', 'indexes', 'databases'
    table: Optional[str] = None


@dataclass
class Desc:
    table: str


@dataclass
class Explain:
    statement: Any


@dataclass
class Use:
    name: str


@dataclass
class SetMode:
    """``SET @mvd_mode = 'ann'`` style configuration."""

    name: str
    value: str
