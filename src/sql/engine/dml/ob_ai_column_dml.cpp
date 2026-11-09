/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#define USING_LOG_PREFIX SQL_ENG

#include "sql/engine/dml/ob_ai_column_dml.h"

#include <string>

#include "share/rc/ob_server_runtime.h"
#include "share/schema/ob_ai_column_runtime.h"
#include "share/schema/ob_schema_getter_guard.h"
#include "share/schema/ob_schema_struct.h"
#include "share/ob_time_utility2.h"
#include "sql/engine/dml/ob_dml_ctx_define.h"
#include "sql/engine/dml/ob_table_modify_op.h"
#include "sql/engine/ob_exec_context.h"
#include "sql/session/ob_sql_session_info.h"

using namespace oceanbase::common;
using namespace oceanbase::share;
using namespace oceanbase::share::schema;

namespace oceanbase
{
namespace sql
{

thread_local int64_t ObAiColumnDml::publish_depth_ = 0;

namespace
{

// The id of the base table of the statement.  The DAS compile definition only
// carries the id of the index that is accessed, which is the base table for a
// primary index write.
inline uint64_t base_table_id(const ObDMLBaseCtDef &dml_ctdef)
{
  const uint64_t index_tid = dml_ctdef.das_base_ctdef_.index_tid_;
  return (0 == index_tid || OB_INVALID_ID == index_tid)
             ? dml_ctdef.das_base_ctdef_.table_id_
             : index_tid;
}

// Reads the table schema of the statement together with the name of its
// database.  Both stay valid as long as `guard` is alive.
int get_table_context(const ObDMLBaseCtDef &dml_ctdef,
                      ObSchemaGetterGuard &guard,
                      const ObTableSchema *&table_schema,
                      const ObDatabaseSchema *&database_schema)
{
  int ret = OB_SUCCESS;
  table_schema = nullptr;
  database_schema = nullptr;
  if (OB_ISNULL(GCTX.schema_service_)) {
    ret = OB_NOT_INIT;
  } else if (OB_FAIL(GCTX.schema_service_->get_runtime_schema_guard(guard))) {
  } else if (OB_FAIL(guard.get_table_schema(base_table_id(dml_ctdef), table_schema))) {
    // A table that is not visible from the schema guard simply has no AI
    // column; the statement is left untouched.
    ret = OB_SUCCESS;
    table_schema = nullptr;
  } else if (OB_ISNULL(table_schema)) {
  } else if (OB_FAIL(guard.get_database_schema(table_schema->get_database_id(),
                                               database_schema))) {
    ret = OB_SUCCESS;
    database_schema = nullptr;
  }
  return ret;
}

// The index of a column in the row arrays of the statement, or -1.
int64_t projector_of(const ObDMLBaseCtDef &dml_ctdef,
                     const ObTableSchema &table_schema,
                     const std::string &column_name)
{
  int64_t projector = -1;
  for (int64_t i = 0; i < dml_ctdef.column_ids_.count(); ++i) {
    const uint64_t column_id = dml_ctdef.column_ids_.at(i);
    const ObColumnSchemaV2 *column_schema = table_schema.get_column_schema(column_id);
    if (OB_NOT_NULL(column_schema)
        && nullptr != column_schema->get_column_name()
        && column_name == column_schema->get_column_name()) {
      projector = i;
      break;
    }
  }
  return projector;
}

uint64_t column_id_of(const ObTableSchema &table_schema, const std::string &column_name)
{
  const ObColumnSchemaV2 *column_schema = table_schema.get_column_schema(
      common::ObString(static_cast<int32_t>(column_name.size()), column_name.c_str()));
  return OB_ISNULL(column_schema) ? OB_INVALID_ID : column_schema->get_column_id();
}

// True when the value of the column differs between the old and the new row.
bool datum_changed(const ObDMLBaseCtDef &dml_ctdef,
                   const int64_t projector,
                   ObEvalCtx &eval_ctx)
{
  bool changed = false;
  if (projector < 0
      || projector >= dml_ctdef.old_row_.count()
      || projector >= dml_ctdef.new_row_.count()) {
    return changed;
  }
  ObDatum *old_datum = nullptr;
  ObDatum *new_datum = nullptr;
  if (OB_SUCCESS == dml_ctdef.old_row_.at(projector)->eval(eval_ctx, old_datum)
      && OB_SUCCESS == dml_ctdef.new_row_.at(projector)->eval(eval_ctx, new_datum)
      && OB_NOT_NULL(old_datum) && OB_NOT_NULL(new_datum)) {
    if (old_datum->is_null() != new_datum->is_null()) {
      changed = true;
    } else if (!old_datum->is_null() && !ObDatum::binary_equal(*old_datum, *new_datum)) {
      changed = true;
    }
  }
  return changed;
}

// True when a column the AI column reads has really changed.
bool dependency_changed(const ObDMLBaseCtDef &dml_ctdef,
                        const ObTableSchema &table_schema,
                        const ObAiColumnSpec &spec,
                        ObEvalCtx &eval_ctx)
{
  bool changed = false;
  std::string text = spec.dep_columns_;
  size_t start = 0;
  while (!changed && start <= text.size()) {
    size_t comma = text.find(',', start);
    if (std::string::npos == comma) {
      comma = text.size();
    }
    if (comma > start) {
      const std::string dep = text.substr(start, comma - start);
      changed = datum_changed(dml_ctdef, projector_of(dml_ctdef, table_schema, dep), eval_ctx);
    }
    start = comma + 1;
  }
  return changed;
}

// True when the update moves the row to a new primary key.  Such a row is a
// new row, so every AI column of it has to be produced again.
bool rowkey_changed(const ObDMLBaseCtDef &dml_ctdef, ObEvalCtx &eval_ctx)
{
  bool changed = false;
  const int64_t rowkey_cnt = dml_ctdef.das_base_ctdef_.rowkey_cnt_;
  for (int64_t i = 0; !changed && i < rowkey_cnt; ++i) {
    changed = datum_changed(dml_ctdef, i, eval_ctx);
  }
  return changed;
}

bool need_invalidate(const ObDMLBaseCtDef &dml_ctdef,
                     const ObTableSchema &table_schema,
                     const ObAiColumnSpec &spec,
                     ObEvalCtx &eval_ctx)
{
  // A new row has no result yet, so every AI column of it has to be produced.
  // The old row of an insert is empty, which would otherwise make the change
  // detection below report that nothing changed.
  return (DAS_OP_TABLE_INSERT == dml_ctdef.dml_type_)
         || rowkey_changed(dml_ctdef, eval_ctx)
         || dependency_changed(dml_ctdef, table_schema, spec, eval_ctx);
}

// The predicate that selects one row of the table, for example `id` = 2.
int build_rowkey_predicate(const ObDMLBaseCtDef &dml_ctdef,
                           const ObTableSchema &table_schema,
                           ObEvalCtx &eval_ctx,
                           ObIAllocator &allocator,
                           ObString &predicate)
{
  int ret = OB_SUCCESS;
  const int64_t rowkey_cnt = dml_ctdef.das_base_ctdef_.rowkey_cnt_;
  const bool is_delete = (DAS_OP_TABLE_DELETE == dml_ctdef.dml_type_);
  const ExprFixedArray &row = is_delete ? dml_ctdef.old_row_ : dml_ctdef.new_row_;
  const ObRowkeyInfo &rowkey_info = table_schema.get_rowkey_info();
  ObSqlString text;
  if (rowkey_cnt <= 0 || row.count() < rowkey_cnt) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("table with ai columns has no rowkey", K(ret), K(rowkey_cnt), K(row.count()));
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < rowkey_cnt; ++i) {
    uint64_t column_id = OB_INVALID_ID;
    const ObColumnSchemaV2 *column_schema = nullptr;
    ObDatum *datum = nullptr;
    if (OB_FAIL(rowkey_info.get_column_id(i, column_id))) {
    } else if (OB_ISNULL(column_schema = table_schema.get_column_schema(column_id))) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(row.at(i)->eval(eval_ctx, datum))) {
    } else if (OB_ISNULL(datum)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (i > 0 && OB_FAIL(text.append(" AND "))) {
    } else if (OB_FAIL(text.append_fmt("`%s` = ", column_schema->get_column_name()))) {
    } else if (datum->is_null()) {
      if (OB_FAIL(text.append("NULL"))) {
      }
    } else {
      ObObj value;
      ObObjDatumMapType map_type =
          ObDatum::get_obj_datum_map_type(row.at(i)->obj_meta_.get_type());
      if (OB_FAIL(datum->to_obj(value, row.at(i)->obj_meta_, map_type))) {
      } else if (value.is_string_type()) {
        if (OB_FAIL(text.append_fmt("'%.*s'", value.get_string().length(),
                                    value.get_string().ptr()))) {
        }
      } else {
        char literal[128];
        int64_t pos = 0;
        if (OB_FAIL(value.print_sql_literal(literal, sizeof(literal), pos, nullptr))) {
        } else if (OB_FAIL(text.append(literal, pos))) {
        }
      }
    }
  }
  if (OB_SUCC(ret) && OB_FAIL(ob_write_string(allocator, text.string(), predicate))) {
  }
  return ret;
}

// A request id that identifies one write of one row.  All AI columns of that
// row share it, so the worker publishes their results at the same time.
int build_request_id(ObIAllocator &allocator, ObString &request_id)
{
  int ret = OB_SUCCESS;
  static int64_t seq = 0;
  char buffer[80];
  const int32_t length = snprintf(buffer, sizeof(buffer), "req-%ld-%ld",
                                  static_cast<long>(ObTimeUtility::current_time()),
                                  static_cast<long>(++seq));
  if (length <= 0) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ob_write_string(allocator, ObString(length, buffer), request_id))) {
  }
  return ret;
}

// Escapes a text so that it can be placed inside a single quoted sql literal.
std::string escape_literal(const ObString &text)
{
  std::string out;
  out.reserve(static_cast<size_t>(text.length()) * 2);
  for (int64_t i = 0; i < text.length(); ++i) {
    const char c = text.ptr()[i];
    if ('\'' == c || '\\' == c) {
      out.push_back('\\');
    }
    out.push_back(c);
  }
  return out;
}

// The hash of the primary key predicate.  It is computed by the server, so
// that the statement that creates a task and the statements that remove it
// agree on the key of the task without sharing any in process state.  The
// modulo keeps the value inside the range of the int column of the system
// table.
std::string predicate_hash_expression(const ObString &predicate)
{
  return std::string("(CRC32('") + escape_literal(predicate) + "') % 1000000007)";
}


// Runs one statement inside the transaction of the outer statement.
//
// The statement runs on the inner connection of the session, which shares the
// transaction of the outer statement.  Running it advances the nesting counter
// and the savepoint of that session, both of which belong to the outer
// statement, so both are restored when the write is done.  The transaction
// descriptor is not touched: the write has to land in the same transaction as
// the row it belongs to.
int execute_nested_write(ObTableModifyOp &dml_op, const char *sql)
{
  int ret = OB_SUCCESS;
  ObExecContext &exec_ctx = dml_op.dml_rtctx_.get_exec_ctx();
  ObDASCtx &das_ctx = DAS_CTX(exec_ctx);
  // The nested statement runs on the session of the outer statement, so it
  // owns the transaction of the outer statement: the task it writes is
  // committed, or rolled back, together with the row it belongs to.
  //
  // A nested statement must not end that transaction, and it must not reset
  // the nesting state of the session.  The flag below marks the statement as
  // nested SQL, which is the same mechanism the foreign key cascade uses; the
  // savepoint is saved and restored for the same reason.
  const transaction::ObTxSEQ saved_savepoint = das_ctx.get_savepoint();
  const uint64_t saved_cascading = das_ctx.is_fk_cascading_;
  das_ctx.is_fk_cascading_ = true;
  if (OB_FAIL(dml_op.execute_write(sql))) {
    LOG_WARN("the nested ai column write failed", K(ret), K(sql));
  }
  das_ctx.set_savepoint(saved_savepoint);
  das_ctx.is_fk_cascading_ = saved_cascading;
  return ret;
}

// Sets the result of the given AI columns of one row to null, so that the row
// does not expose a result that no longer matches its content.  The statement
// is executed inside the transaction of the write, so a rollback restores the
// old results and the old index entries.
int invalidate_results(const ObTableSchema &table_schema,
                       const ObDatabaseSchema &database_schema,
                       const ObIArray<ObAiColumnSpec> &specs,
                       const ObString &predicate,
                       ObTableModifyOp &dml_op)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  if (specs.count() <= 0) {
  } else if (OB_FAIL(sql.append_fmt("UPDATE `%.*s`.`%.*s` SET ",
                                    database_schema.get_database_name_str().length(),
                                    database_schema.get_database_name_str().ptr(),
                                    table_schema.get_table_name_str().length(),
                                    table_schema.get_table_name_str().ptr()))) {
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < specs.count(); ++i) {
    if (OB_FAIL(sql.append_fmt("%s`%s` = NULL", 0 == i ? "" : ", ",
                               specs.at(i).column_name_.c_str()))) {
    }
  }
  if (OB_SUCC(ret) && OB_FAIL(sql.append_fmt(" WHERE %.*s", predicate.length(),
                                             predicate.ptr()))) {
  } else if (OB_FAIL(ret)) {
  } else if (OB_FAIL(execute_nested_write(dml_op, sql.ptr()))) {
  }
  return ret;
}

// Records one task per AI column inside the transaction of the statement.  The
// rows belong to one request, so the worker publishes all results of them at
// the same time.
int create_tasks(const ObDMLBaseCtDef &dml_ctdef,
                 const ObTableSchema &table_schema,
                 const ObIArray<ObAiColumnSpec> &specs,
                 const ObString &predicate,
                 const ObString &request_id,
                 ObTableModifyOp &dml_op)
{
  int ret = OB_SUCCESS;
  const uint64_t table_id = base_table_id(dml_ctdef);
  const std::string hash_expression = predicate_hash_expression(predicate);
  const std::string escaped_predicate = escape_literal(predicate);
  const std::string escaped_request = escape_literal(request_id);
  char eligible_ts[64];
  int64_t pos = 0;
  if (OB_FAIL(ObTimeUtility2::usec_format_to_str(
          ObTimeUtility::current_time(),
          ObString(ObTimeUtility2::STD_TS_FORMAT_WITH_USEC),
          eligible_ts, sizeof(eligible_ts), pos))) {
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < specs.count(); ++i) {
    const ObAiColumnSpec &spec = specs.at(i);
    const uint64_t column_id = column_id_of(table_schema, spec.column_name_);
    ObSqlString insert_sql;
    if (OB_INVALID_ID == column_id) {
      LOG_WARN("ai column not found in the table schema", K(ret), K(table_id));
      continue;
    } else if (OB_FAIL(insert_sql.assign_fmt(
                   "REPLACE INTO oceanbase.%s (table_id, column_id, rowkey_hash, request_id, "
                   "rowkey_predicate, state, retry_count, eligible_ts, last_error) "
                   "VALUES (%lu, %lu, %s, '%.*s', '%.*s', %ld, 0, '%.*s', '')",
                   OB_ALL_AI_COLUMN_TASK_TNAME,
                   static_cast<unsigned long>(table_id),
                   static_cast<unsigned long>(column_id),
                   hash_expression.c_str(),
                   static_cast<int32_t>(escaped_request.size()), escaped_request.c_str(),
                   static_cast<int32_t>(escaped_predicate.size()), escaped_predicate.c_str(),
                   static_cast<long>(ObAiColumnRuntime::TASK_PENDING),
                   static_cast<int32_t>(pos), eligible_ts))) {
    } else if (OB_FAIL(execute_nested_write(dml_op, insert_sql.ptr()))) {
    }
  }
  return ret;
}

// Removes the tasks of a row that no longer exists.
int delete_tasks(const ObDMLBaseCtDef &dml_ctdef,
                 const ObString &predicate,
                 ObTableModifyOp &dml_op)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  const std::string hash_expression = predicate_hash_expression(predicate);
  if (OB_FAIL(sql.assign_fmt("DELETE FROM oceanbase.%s WHERE table_id = %lu AND rowkey_hash = %s",
                             OB_ALL_AI_COLUMN_TASK_TNAME,
                             static_cast<unsigned long>(base_table_id(dml_ctdef)),
                             hash_expression.c_str()))) {
  } else if (OB_FAIL(execute_nested_write(dml_op, sql.ptr()))) {
  }
  return ret;
}

// The AI columns of the table that the write invalidates.
int invalidated_columns(const ObDMLBaseCtDef &dml_ctdef,
                        const ObTableSchema &table_schema,
                        ObEvalCtx &eval_ctx,
                        ObIAllocator &allocator,
                        ObIArray<ObAiColumnSpec> &all,
                        ObIArray<ObAiColumnSpec> &invalidated)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObAiColumnRuntime::collect_ai_columns(table_schema, allocator, all))) {
    // The rule of an AI column cannot be read back, so the row is written as
    // it is.  The worker will not be able to serve it either.
    LOG_WARN("cannot read the ai column rules", K(ret));
    ret = OB_SUCCESS;
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < all.count(); ++i) {
    if (need_invalidate(dml_ctdef, table_schema, all.at(i), eval_ctx)) {
      if (OB_FAIL(invalidated.push_back(all.at(i)))) {
      }
    }
  }
  return ret;
}

// The invalidation of the result columns is itself a statement of the table,
// which would handle the AI columns again.  The nesting is bounded, but a
// depth counter keeps it cheap and makes the recursion impossible.
thread_local int g_ai_column_depth = 0;

class ObAiColumnDepthGuard
{
public:
  ObAiColumnDepthGuard() { ++g_ai_column_depth; }
  ~ObAiColumnDepthGuard() { --g_ai_column_depth; }
};

// Invalidates the AI columns of a row and records their tasks.  Both happen in
// the transaction of the statement: a rollback discards them and no request is
// sent for a row that never existed.
int handle_row_change(const ObDMLBaseCtDef &dml_ctdef,
                      ObTableModifyOp &dml_op)
{
  int ret = OB_SUCCESS;
  ObArenaAllocator allocator(ObModIds::OB_SQL_EXECUTOR);
  ObSchemaGetterGuard guard;
  const ObTableSchema *table_schema = nullptr;
  const ObDatabaseSchema *database_schema = nullptr;
  ObSEArray<ObAiColumnSpec, 4> all_specs;
  ObSEArray<ObAiColumnSpec, 4> invalidated;
  ObSEArray<ObAiColumnSpec, 4> unwritten;
  ObString predicate;
  ObString request_id;
  if (g_ai_column_depth > 2) {
    // A nested invalidation statement: the write it belongs to has already
    // recorded the tasks of the row.
  } else if (ObAiColumnDml::is_publishing_results()) {
    // The write publishes an AI column result.  It is not a change of the
    // content the column reads, so it must neither invalidate the result nor
    // record a task for it.
  } else if (!dml_ctdef.is_primary_index_ || !ObAiColumnDml::has_ai_column(dml_ctdef)) {
  } else if (OB_FAIL(get_table_context(dml_ctdef, guard, table_schema, database_schema))) {
    LOG_WARN("cannot read the table schema of the ai column write", K(ret));
    ret = OB_SUCCESS;
  } else if (OB_ISNULL(table_schema) || OB_ISNULL(database_schema)) {
    LOG_WARN("the table schema of the ai column write is null",
             "table_id", base_table_id(dml_ctdef));
  } else if (OB_FAIL(invalidated_columns(dml_ctdef, *table_schema,
                                         dml_op.get_eval_ctx(), allocator,
                                         all_specs, invalidated))) {
  } else if (invalidated.count() <= 0) {
    // The write does not touch the content the AI columns read.
  } else if (OB_FAIL(build_rowkey_predicate(dml_ctdef, *table_schema,
                                            dml_op.get_eval_ctx(), allocator, predicate))) {
  } else if (OB_FAIL(build_request_id(allocator, request_id))) {
  } else {
    ObAiColumnDepthGuard depth_guard;
    // An insert writes the whole row, so the null that `reset_ai_columns`
    // placed in the row is what the row stores, and no further statement is
    // needed.  An update only writes the columns it assigns, so the result of
    // an AI column it does not assign would keep its old value; every
    // invalidated column is therefore reset by a statement of its own.
    if (DAS_OP_TABLE_UPDATE == dml_ctdef.dml_type_) {
      if (OB_FAIL(unwritten.assign(invalidated))) {
      }
    } else {
      for (int64_t i = 0; OB_SUCC(ret) && i < invalidated.count(); ++i) {
        if (projector_of(dml_ctdef, *table_schema, invalidated.at(i).column_name_) < 0) {
          if (OB_FAIL(unwritten.push_back(invalidated.at(i)))) {
          }
        }
      }
    }
    if (OB_SUCC(ret) && unwritten.count() > 0) {
      if (OB_FAIL(invalidate_results(*table_schema, *database_schema, unwritten,
                                     predicate, dml_op))) {
      }
    }
    if (OB_SUCC(ret)) {
      if (OB_FAIL(create_tasks(dml_ctdef, *table_schema, invalidated,
                               predicate, request_id, dml_op))) {
      }
    }
  }
  return ret;
}

} // namespace

bool ObAiColumnDml::has_ai_column(const ObDMLBaseCtDef &dml_ctdef)
{
  // The flag is computed by the code generator, which is the only stage that
  // has the table schema at hand.  Reading the flag keeps the per row path of
  // an ordinary table free of any schema lookup.
  return dml_ctdef.das_base_ctdef_.has_ai_column_;
}

int ObAiColumnDml::reset_ai_columns(const ObDMLBaseCtDef &dml_ctdef, ObEvalCtx &eval_ctx)
{
  int ret = OB_SUCCESS;
  ObArenaAllocator allocator(ObModIds::OB_SQL_EXECUTOR);
  ObSEArray<ObAiColumnSpec, 4> specs;
  ObSchemaGetterGuard guard;
  const ObTableSchema *table_schema = nullptr;
  const ObDatabaseSchema *database_schema = nullptr;
  if (ObAiColumnDml::is_publishing_results()) {
    // The write publishes an AI column result and must keep the value it sets.
  } else if (!dml_ctdef.is_primary_index_ || !has_ai_column(dml_ctdef)) {
  } else if (OB_FAIL(get_table_context(dml_ctdef, guard, table_schema, database_schema))) {
    LOG_WARN("cannot read the table schema of the ai column write", K(ret));
    ret = OB_SUCCESS;
  } else if (OB_ISNULL(table_schema)) {
  } else if (OB_FAIL(ObAiColumnRuntime::collect_ai_columns(*table_schema, allocator, specs))) {
    LOG_WARN("cannot read the ai column rules", K(ret));
    ret = OB_SUCCESS;
  } else {
    const bool is_update = (DAS_OP_TABLE_UPDATE == dml_ctdef.dml_type_);
    for (int64_t i = 0; OB_SUCC(ret) && i < specs.count(); ++i) {
      const ObAiColumnSpec &spec = specs.at(i);
      // An update keeps the result of an AI column whose dependency columns are
      // untouched.  A value written by the statement is still discarded,
      // because only the worker may produce the final value.
      if (is_update && !need_invalidate(dml_ctdef, *table_schema, spec, eval_ctx)) {
        continue;
      }
      const int64_t projector = projector_of(dml_ctdef, *table_schema, spec.column_name_);
      ObExpr *expr = nullptr;
      if (projector < 0 || projector >= dml_ctdef.new_row_.count()) {
        continue;
      } else if (OB_ISNULL(expr = dml_ctdef.new_row_.at(projector))) {
        LOG_WARN("the expression of the ai column is null", K(projector));
        continue;
      } else {
        ObDatum &datum = expr->locate_datum_for_write(eval_ctx);
        datum.set_null();
        expr->get_eval_info(eval_ctx).evaluated_ = true;
      }
    }
  }
  return ret;
}

int ObAiColumnDml::handle_inserted_row(const ObDMLBaseCtDef &dml_ctdef,
                                       ObDMLBaseRtDef &dml_rtdef,
                                       ObTableModifyOp &dml_op)
{
  UNUSED(dml_rtdef);
  return handle_row_change(dml_ctdef, dml_op);
}

int ObAiColumnDml::handle_updated_row(const ObDMLBaseCtDef &dml_ctdef,
                                      ObDMLBaseRtDef &dml_rtdef,
                                      ObTableModifyOp &dml_op)
{
  UNUSED(dml_rtdef);
  return handle_row_change(dml_ctdef, dml_op);
}

int ObAiColumnDml::handle_deleted_row(const ObDMLBaseCtDef &dml_ctdef,
                                      ObDMLBaseRtDef &dml_rtdef,
                                      ObTableModifyOp &dml_op)
{
  int ret = OB_SUCCESS;
  ObArenaAllocator allocator(ObModIds::OB_SQL_EXECUTOR);
  ObSchemaGetterGuard guard;
  const ObTableSchema *table_schema = nullptr;
  const ObDatabaseSchema *database_schema = nullptr;
  ObString predicate;
  UNUSED(dml_rtdef);
  if (ObAiColumnDml::is_publishing_results()) {
    // The write publishes an AI column result and must keep the value it sets.
  } else if (!dml_ctdef.is_primary_index_ || !has_ai_column(dml_ctdef)) {
  } else if (OB_FAIL(get_table_context(dml_ctdef, guard, table_schema, database_schema))) {
    LOG_WARN("cannot read the table schema of the ai column write", K(ret));
    ret = OB_SUCCESS;
  } else if (OB_ISNULL(table_schema)) {
  } else if (OB_FAIL(build_rowkey_predicate(dml_ctdef, *table_schema,
                                            dml_op.get_eval_ctx(), allocator, predicate))) {
  } else if (OB_FAIL(delete_tasks(dml_ctdef, predicate, dml_op))) {
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
