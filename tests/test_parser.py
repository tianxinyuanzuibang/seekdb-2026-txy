"""Unit tests for the MiniVectorDB lexer and parser (stdlib unittest)."""

import unittest

from minivectordb.ast_nodes import (
    Between,
    BinaryOp,
    ColumnRef,
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
    SetMode,
    Show,
    UnaryOp,
    Update,
    Use,
    VectorLiteral,
    Begin,
    Commit,
)
from minivectordb.errors import SQLError
from minivectordb.lexer import tokenize
from minivectordb.parser import parse, parse_one


# --- lexer -----------------------------------------------------------------


class LexerTests(unittest.TestCase):
    def test_kinds(self):
        toks = tokenize("SELECT a FROM t")
        self.assertEqual([t.kind for t in toks], ["keyword", "ident", "keyword", "ident", "eof"])
        self.assertEqual(toks[0].value, "SELECT")
        self.assertEqual(toks[1].value, "a")
        self.assertEqual(toks[-1].kind, "eof")

    def test_pos(self):
        toks = tokenize("SELECT a")
        self.assertEqual(toks[0].pos, 0)
        self.assertEqual(toks[1].pos, 7)

    def test_case_insensitive_keywords(self):
        toks = tokenize("select From")
        self.assertEqual(toks[0].kind, "keyword")
        self.assertEqual(toks[0].value, "SELECT")
        self.assertEqual(toks[1].value, "FROM")

    def test_identifiers_keep_spelling(self):
        toks = tokenize("MyTable")
        self.assertEqual(toks[0].kind, "ident")
        self.assertEqual(toks[0].value, "MyTable")

    def test_numbers(self):
        self.assertEqual(tokenize("42")[0].value, 42)
        self.assertEqual(tokenize("42")[0].kind, "number")
        self.assertEqual(tokenize("3.5")[0].value, 3.5)
        self.assertEqual(tokenize("1e3")[0].value, 1000.0)
        self.assertEqual(tokenize("1.5e-2")[0].value, 0.015)
        self.assertEqual(tokenize("2E+3")[0].value, 2000.0)

    def test_strings(self):
        self.assertEqual(tokenize("'abc'")[0].value, "abc")
        self.assertEqual(tokenize('"abc"')[0].value, "abc")
        self.assertEqual(tokenize("'a\\'b'")[0].value, "a'b")
        self.assertEqual(tokenize("'a''b'")[0].value, "a'b")
        self.assertEqual(tokenize("'a''b'")[0].kind, "string")

    def test_unterminated_string(self):
        with self.assertRaises(SQLError):
            tokenize("'abc")

    def test_vector_token(self):
        toks = tokenize("[1,2,3]")
        self.assertEqual(len(toks), 2)
        self.assertEqual(toks[0].kind, "vector")
        self.assertEqual(toks[0].value, [1.0, 2.0, 3.0])

    def test_vector_negative_and_exponent(self):
        self.assertEqual(tokenize("[-1, 2.5e1]")[0].value, [-1.0, 25.0])

    def test_empty_vector(self):
        toks = tokenize("[]")
        self.assertEqual(toks[0].kind, "vector")
        self.assertEqual(toks[0].value, [])

    def test_punct(self):
        toks = tokenize("(a,b)")
        self.assertEqual([t.value for t in toks[:-1]], ["(", "a", ",", "b", ")"])
        self.assertTrue(all(t.kind == "punct" for t in toks if t.value is not None and t.value in "(),"))


# --- DDL -------------------------------------------------------------------


class DDlTests(unittest.TestCase):
    def test_create_table(self):
        st = parse_one(
            "CREATE TABLE t (a BIGINT, b VARCHAR(10), c VECTOR(3), PRIMARY KEY (a))"
        )
        self.assertIsInstance(st, CreateTable)
        self.assertEqual(st.name, "t")
        self.assertEqual(st.primary_key, "a")
        self.assertEqual([c.name for c in st.columns], ["a", "b", "c"])
        self.assertEqual([c.type_name for c in st.columns], ["BIGINT", "VARCHAR", "VECTOR"])
        self.assertEqual(st.columns[1].length, 10)
        self.assertEqual(st.columns[2].vector_dim, 3)

    def test_create_table_inline_primary_key(self):
        st = parse_one("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(4))")
        self.assertEqual(st.primary_key, "id")
        self.assertTrue(st.columns[0].primary_key)

    def test_create_table_requires_varchar_length(self):
        with self.assertRaises(SQLError):
            parse_one("CREATE TABLE t (a VARCHAR)")

    def test_create_table_requires_vector_dim(self):
        with self.assertRaises(SQLError):
            parse_one("CREATE TABLE t (a VECTOR)")

    def test_drop_table(self):
        st = parse_one("DROP TABLE IF EXISTS t")
        self.assertIsInstance(st, DropTable)
        self.assertEqual(st.name, "t")
        self.assertTrue(st.if_exists)
        self.assertFalse(parse_one("DROP TABLE t").if_exists)

    def test_create_vector_index(self):
        st = parse_one("CREATE VECTOR INDEX idx ON t(c)")
        self.assertIsInstance(st, CreateVectorIndex)
        self.assertEqual(st.index_name, "idx")
        self.assertEqual(st.table_name, "t")
        self.assertEqual(st.column_name, "c")

    def test_drop_vector_index(self):
        st = parse_one("DROP VECTOR INDEX idx")
        self.assertIsInstance(st, DropVectorIndex)
        self.assertEqual(st.index_name, "idx")

    def test_unsupported_create(self):
        with self.assertRaises(SQLError):
            parse_one("CREATE DATABASE d")


# --- DML -------------------------------------------------------------------


class DmlTests(unittest.TestCase):
    def test_insert_single_row(self):
        st = parse_one("INSERT INTO t (a, b) VALUES (1, 'x')")
        self.assertIsInstance(st, Insert)
        self.assertEqual(st.table, "t")
        self.assertEqual(st.columns, ["a", "b"])
        self.assertEqual(len(st.rows), 1)
        self.assertEqual(st.rows[0][0], Literal(1))
        self.assertEqual(st.rows[0][1], Literal("x"))

    def test_insert_multi_row(self):
        st = parse_one("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y'), (3, 'z')")
        self.assertEqual(len(st.rows), 3)
        self.assertEqual(st.rows[2][0], Literal(3))

    def test_insert_without_columns(self):
        st = parse_one("INSERT INTO t VALUES (1)")
        self.assertIsNone(st.columns)

    def test_insert_set_form(self):
        st = parse_one("INSERT INTO t SET a=1, b='x'")
        self.assertIsInstance(st, Insert)
        self.assertEqual(len(st.rows), 1)
        self.assertEqual(st.rows[0][0], Literal(1))
        self.assertEqual(st.rows[0][1], Literal("x"))

    def test_insert_vector_value(self):
        st = parse_one("INSERT INTO t (v) VALUES ([1,2,3])")
        self.assertEqual(st.rows[0][0], VectorLiteral([1.0, 2.0, 3.0]))

    def test_update(self):
        st = parse_one("UPDATE t SET a=1, b='x' WHERE id > 3")
        self.assertIsInstance(st, Update)
        self.assertEqual(st.table, "t")
        self.assertEqual(len(st.assignments), 2)
        self.assertEqual(st.assignments[0][0], "a")
        self.assertIsInstance(st.where, BinaryOp)
        self.assertEqual(st.where.op, ">")

    def test_update_no_where(self):
        self.assertIsNone(parse_one("UPDATE t SET a=1").where)

    def test_delete(self):
        st = parse_one("DELETE FROM t WHERE id = 1")
        self.assertIsInstance(st, Delete)
        self.assertEqual(st.table, "t")
        self.assertIsNotNone(st.where)

    def test_delete_all(self):
        st = parse_one("DELETE FROM t")
        self.assertIsNone(st.where)


# --- SELECT ----------------------------------------------------------------


class SelectTests(unittest.TestCase):
    def test_select_star(self):
        st = parse_one("SELECT * FROM t")
        self.assertIsInstance(st, Select)
        self.assertTrue(st.items[0].star)

    def test_select_columns_and_alias(self):
        st = parse_one("SELECT a, b AS bb, c cc FROM t")
        self.assertEqual([i.alias for i in st.items], [None, "bb", "cc"])
        self.assertIsInstance(st.items[0].expr, ColumnRef)

    def test_select_where_order_limit(self):
        st = parse_one(
            "SELECT a FROM t WHERE a >= 1 AND b < 2 ORDER BY a DESC LIMIT 10 OFFSET 5"
        )
        self.assertEqual(st.limit, 10)
        self.assertEqual(st.offset, 5)
        self.assertEqual(len(st.order_by), 1)
        self.assertFalse(st.order_by[0][1])

    def test_mysql_limit_offset_comma(self):
        st = parse_one("SELECT a FROM t LIMIT 5, 10")
        self.assertEqual(st.offset, 5)
        self.assertEqual(st.limit, 10)

    def test_where_operators(self):
        st = parse_one("SELECT a FROM t WHERE a = 1 OR (b <> 2 AND c >= 3)")
        self.assertIsInstance(st.where, BinaryOp)
        self.assertEqual(st.where.op, "OR")

    def test_where_not(self):
        st = parse_one("SELECT a FROM t WHERE NOT a = 1")
        self.assertIsInstance(st.where, UnaryOp)
        self.assertEqual(st.where.op, "NOT")

    def test_where_in(self):
        st = parse_one("SELECT a FROM t WHERE a IN (1, 2, 3)")
        self.assertIsInstance(st.where, InList)
        self.assertFalse(st.where.negated)
        self.assertEqual(len(st.where.values), 3)

    def test_where_not_in(self):
        st = parse_one("SELECT a FROM t WHERE a NOT IN (1, 2)")
        self.assertIsInstance(st.where, InList)
        self.assertTrue(st.where.negated)

    def test_where_between(self):
        st = parse_one("SELECT a FROM t WHERE a BETWEEN 1 AND 5")
        self.assertIsInstance(st.where, Between)
        self.assertFalse(st.where.negated)

    def test_where_not_between(self):
        st = parse_one("SELECT a FROM t WHERE a NOT BETWEEN 1 AND 5")
        self.assertIsInstance(st.where, Between)
        self.assertTrue(st.where.negated)

    def test_where_like(self):
        st = parse_one("SELECT a FROM t WHERE name LIKE 'a%b_c'")
        self.assertIsInstance(st.where, Like)
        self.assertEqual(st.where.pattern, Literal("a%b_c"))

    def test_where_is_null(self):
        st = parse_one("SELECT a FROM t WHERE a IS NULL")
        self.assertIsInstance(st.where, IsNull)
        self.assertFalse(st.where.negated)

    def test_where_is_not_null(self):
        st = parse_one("SELECT a FROM t WHERE a IS NOT NULL")
        self.assertIsInstance(st.where, IsNull)
        self.assertTrue(st.where.negated)

    def test_where_null_literal(self):
        st = parse_one("SELECT a FROM t WHERE a = NULL")
        self.assertEqual(st.where.right, NullLiteral())


# --- vector search ---------------------------------------------------------


class VectorSearchTests(unittest.TestCase):
    def _search(self, sql):
        return parse_one(sql)

    def test_l2_distance(self):
        st = self._search("SELECT id FROM t ORDER BY L2_DISTANCE(v, [1,2,3]) LIMIT 10")
        self.assertTrue(st.is_vector_search)
        self.assertEqual(st.limit, 10)
        self.assertIsInstance(st.vector_expr, FunctionCall)
        self.assertEqual(st.vector_expr.name, "L2_DISTANCE")
        self.assertEqual(len(st.vector_expr.args), 2)
        self.assertEqual(st.vector_expr.args[1], VectorLiteral([1.0, 2.0, 3.0]))

    def test_all_distance_names(self):
        for name in (
            "L2_DISTANCE",
            "L2",
            "EUCLIDEAN_DISTANCE",
            "COSINE_DISTANCE",
            "COSINE",
            "INNER_PRODUCT",
            "IP",
        ):
            st = self._search(
                "SELECT id FROM t ORDER BY %s(v, [1,2]) LIMIT 5" % name
            )
            self.assertTrue(st.is_vector_search, name)
            self.assertEqual(st.vector_expr.name, name)

    def test_case_insensitive_distance_names(self):
        st = self._search("SELECT id FROM t ORDER BY l2_distance(v, [1,2]) LIMIT 5")
        self.assertTrue(st.is_vector_search)
        self.assertEqual(st.vector_expr.name, "L2_DISTANCE")

    def test_not_a_vector_search(self):
        st = self._search("SELECT id FROM t ORDER BY id LIMIT 5")
        self.assertFalse(st.is_vector_search)
        self.assertIsNone(st.vector_expr)

    def test_vector_search_with_filter(self):
        st = self._search(
            "SELECT id FROM t WHERE c = 1 ORDER BY COSINE(v, [1,2]) LIMIT 10"
        )
        self.assertTrue(st.is_vector_search)
        self.assertIsNotNone(st.where)


# --- mode hints ------------------------------------------------------------


class ModeHintTests(unittest.TestCase):
    def assert_mode(self, sql, mode, limit=10):
        st = parse_one(sql)
        self.assertEqual(st.search_mode, mode, sql)
        self.assertEqual(st.limit, limit, sql)

    def test_default_exact(self):
        self.assert_mode("SELECT * FROM t LIMIT 10", "exact")

    def test_trailing_ann(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 ANN", "ann")

    def test_trailing_exact(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 EXACT", "exact")

    def test_using_ann(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 USING ANN", "ann")

    def test_using_exact(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 USING EXACT", "exact")

    def test_parameters_mode(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 PARAMETERS (mode='ann')", "ann")

    def test_parameters_mode_double_quoted(self):
        self.assert_mode('SELECT * FROM t LIMIT 10 PARAMETERS (mode = "ann")', "ann")

    def test_parameters_search_mode(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 PARAMETERS (search_mode='ann')", "ann")

    def test_parameters_ann_flag(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 PARAMETERS (ann=1)", "ann")

    def test_parameters_exact(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 PARAMETERS (mode='exact')", "exact")

    def test_with_after_limit(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 WITH (mode='ann')", "ann")

    def test_with_table_hint(self):
        self.assert_mode("SELECT * FROM t WITH (mode='ann') LIMIT 10", "ann")

    def test_approx(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 APPROX", "ann")

    def test_approximate(self):
        self.assert_mode("SELECT * FROM t LIMIT 10 APPROXIMATE", "ann")

    def test_mode_hint_on_vector_search(self):
        st = parse_one(
            "SELECT id FROM t ORDER BY L2_DISTANCE(v,[1,2]) LIMIT 10 USING ANN"
        )
        self.assertTrue(st.is_vector_search)
        self.assertEqual(st.search_mode, "ann")

    def test_junk_after_limit_is_error(self):
        with self.assertRaises(SQLError):
            parse_one("SELECT * FROM t LIMIT 10 GARBAGE")
        with self.assertRaises(SQLError):
            parse_one("SELECT * FROM t LIMIT 10 20")


# --- SET / session mode ----------------------------------------------------


class SetModeTests(unittest.TestCase):
    def test_set_at_variable(self):
        st = parse_one("SET @mvd_mode = 'ann'")
        self.assertIsInstance(st, SetMode)
        self.assertEqual(st.name, "mvd_mode")
        self.assertEqual(st.value, "ann")

    def test_set_bare_variable(self):
        st = parse_one("SET mvd_mode='exact'")
        self.assertEqual(st.name, "mvd_mode")
        self.assertEqual(st.value, "exact")

    def test_set_double_quoted(self):
        st = parse_one('SET @mvd_mode="ann"')
        self.assertEqual(st.value, "ann")


# --- statements / misc -----------------------------------------------------


class MiscTests(unittest.TestCase):
    def test_transaction_statements(self):
        self.assertIsInstance(parse_one("BEGIN"), Begin)
        self.assertIsInstance(parse_one("START TRANSACTION"), Begin)
        self.assertIsInstance(parse_one("COMMIT"), Commit)
        self.assertIsInstance(parse_one("ROLLBACK"), Rollback)

    def test_show(self):
        self.assertEqual(parse_one("SHOW TABLES").what, "tables")
        self.assertEqual(parse_one("SHOW COLUMNS FROM t").what, "columns")
        self.assertEqual(parse_one("SHOW INDEXES FROM t").what, "indexes")
        self.assertEqual(parse_one("SHOW COLUMNS FROM t").table, "t")

    def test_desc(self):
        st = parse_one("DESC t")
        self.assertIsInstance(st, Desc)
        self.assertEqual(st.table, "t")

    def test_explain(self):
        st = parse_one("EXPLAIN SELECT * FROM t")
        self.assertIsInstance(st, Explain)
        self.assertIsInstance(st.statement, Select)

    def test_use(self):
        st = parse_one("USE mydb")
        self.assertIsInstance(st, Use)
        self.assertEqual(st.name, "mydb")

    def test_multiple_statements(self):
        stmts = parse("SELECT 1; SELECT 2;")
        self.assertEqual(len(stmts), 2)

    def test_trailing_semicolon(self):
        self.assertEqual(len(parse("SELECT 1;")), 1)

    def test_empty_statements(self):
        self.assertEqual(parse(";;;"), [])
        self.assertEqual(len(parse(";; SELECT 1 ;;")), 1)

    def test_parse_one_rejects_multiple(self):
        with self.assertRaises(SQLError):
            parse_one("SELECT 1; SELECT 2;")

    def test_unsupported_statement(self):
        with self.assertRaises(SQLError):
            parse_one("TRUNCATE TABLE t")
        with self.assertRaises(SQLError):
            parse_one("GRANT ALL ON *.* TO x")

    def test_unsupported_select_join(self):
        with self.assertRaises(SQLError):
            parse_one("SELECT * FROM a JOIN b ON a.id = b.id")

    def test_error_code_is_parse_error(self):
        with self.assertRaises(SQLError) as ctx:
            parse_one("SELECT * FROM")
        self.assertEqual(ctx.exception.code, 1064)

    def test_case_insensitive_sql(self):
        st = parse_one("select a from t where b = 1 limit 3")
        self.assertEqual(st.table, "t")
        self.assertEqual(st.limit, 3)


if __name__ == "__main__":
    unittest.main()
