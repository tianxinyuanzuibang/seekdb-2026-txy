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

#define USING_LOG_PREFIX SERVER_OMT
#include "ob_ai_service.h"
#include "observer/ai_service/ob_ai_service_executor.h"
#include "sql/privilege_check/ob_ai_model_priv_util.h"
#include "sql/session/ob_sql_session_info.h"
#include "sql/engine/ob_exec_context.h"
#include "lib/allocator/page_arena.h"
#include "lib/string/ob_sql_string.h"
#include "common/mysqlclient/ob_mysql_proxy.h"
#include "common/mysqlclient/ob_mysql_result.h"
#include "common/mysqlclient/ob_mysql_transaction.h"
#include "share/inner_table/ob_inner_table_schema_constants.h"
#include "share/schema/ob_ai_column_spec.h"
#include "share/schema/ob_schema_getter_guard.h"
#include "share/schema/ob_schema_struct.h"
#include "share/schema/ob_table_schema.h"
#include "sql/engine/dml/ob_ai_column_dml.h"
#include "share/rc/ob_server_runtime.h"

using namespace oceanbase::share;
using namespace oceanbase::common;
namespace oceanbase
{
namespace omt
{
namespace
{

// State values of oceanbase.__all_ai_column_task, see
// share::schema::ObAiColumnRuntime::TaskState.
const int64_t AI_TASK_PENDING = 0;
const int64_t AI_TASK_RUNNING = 2;
const int64_t AI_TASK_RETRY = 3;

const int64_t AI_TASK_BATCH_SIZE = 128;
// A task that was claimed by a worker that died stays in the running state.  It
// is taken over after this many seconds, which is much longer than the longest
// model request the worker waits for.
const int64_t AI_TASK_RUNNING_TIMEOUT_SECOND = 30;
const int64_t AI_TASK_RETRY_BACKOFF_US = 200L * 1000L;
const int64_t AI_TASK_IDLE_SLEEP_US = 20L * 1000L;

// Where the row of a task lives.
struct AiColumnTarget
{
  common::ObString database_name_;
  common::ObString table_name_;
  common::ObString predicate_;
};

// The generation rule of one AI column, read back from the comment of its
// `__ai_spec_` helper column.  The comment is part of the table schema, so the
// rule is available together with the definition of the column.
struct AiColumnRule
{
  common::ObString result_column_;
  common::ObString state_column_;
  common::ObString prompt_expr_;
  common::ObString system_prompt_;
  common::ObString model_name_;
  int64_t ai_func_type_;
  int64_t declared_dim_;
  int64_t max_retries_;

  AiColumnRule() : ai_func_type_(1), declared_dim_(0), max_retries_(0) {}
};

// One pending task of one AI column of one row.
struct AiTaskColumn
{
  uint64_t column_id_;
  int64_t rowkey_hash_;
  int64_t retry_count_;
  int64_t max_retries_;
  common::ObString definition_;

  AiTaskColumn() : column_id_(OB_INVALID_ID), rowkey_hash_(0),
                   retry_count_(0), max_retries_(0) {}
};

// All tasks of one write of one row: the AI columns of the row that the write
// invalidated.  Their results are published together.
struct AiTaskGroup
{
  uint64_t table_id_;
  common::ObString request_id_;
  common::ObString predicate_;
  common::ObSEArray<AiTaskColumn, 4> columns_;

  AiTaskGroup() : table_id_(OB_INVALID_ID) {}
};

// Appends `text` as a single quoted sql literal.
int append_sql_literal(common::ObSqlString &sql, const char *text, const int64_t length)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(text) || length < 0) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(sql.append("'"))) {
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < length; ++i) {
    const char c = text[i];
    if ('\'' == c) {
      ret = sql.append("''");
    } else if ('\\' == c) {
      ret = sql.append("\\\\");
    } else {
      ret = sql.append(&c, 1);
    }
  }
  if (OB_SUCC(ret)) {
    ret = sql.append("'");
  }
  return ret;
}

int append_sql_literal(common::ObSqlString &sql, const common::ObString &text)
{
  return append_sql_literal(sql, text.ptr(), text.length());
}

// Appends the body of the json string that carries the system message of
// AI_COMPLETE, escaped for json first and for sql second.  The caller adds the
// surrounding quotes of both levels.
int append_json_string_body(common::ObSqlString &sql, const common::ObString &text)
{
  int ret = OB_SUCCESS;
  for (int64_t i = 0; OB_SUCC(ret) && i < text.length(); ++i) {
    const char c = text[i];
    if ('\\' == c) {
      ret = sql.append("\\\\");
    } else if ('"' == c) {
      ret = sql.append("\\\"");
    } else if ('\'' == c) {
      // a single quote is escaped for sql on the outer level
      ret = sql.append("''");
    } else {
      ret = sql.append(&c, 1);
    }
  }
  return ret;
}

int copy_string(common::ObIAllocator &allocator,
                const common::ObString &in,
                common::ObString &out)
{
  int ret = OB_SUCCESS;
  if (in.empty()) {
    out.reset();
  } else {
    char *buffer = static_cast<char *>(allocator.alloc(in.length()));
    if (OB_ISNULL(buffer)) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      MEMCPY(buffer, in.ptr(), in.length());
      out.assign_ptr(buffer, in.length());
    }
  }
  return ret;
}

int fetch_string(common::sqlclient::ObMySQLResult &result,
                 const char *column,
                 common::ObIAllocator &allocator,
                 common::ObString &out)
{
  int ret = OB_SUCCESS;
  common::ObString value;
  if (OB_FAIL(result.get_varchar(column, value))) {
  } else if (OB_FAIL(copy_string(allocator, value, out))) {
  }
  return ret;
}

// Reads the pending tasks and groups them by the write that created them.
int read_tasks(common::ObIAllocator &allocator,
               common::ObIArray<AiTaskGroup *> &groups)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  if (OB_FAIL(sql.assign_fmt(
          "SELECT task.table_id, task.column_id, task.rowkey_hash, task.request_id, "
          "task.rowkey_predicate, task.retry_count, meta.max_retries "
          "FROM oceanbase.%s task JOIN oceanbase.%s meta "
          "ON meta.table_id = task.table_id AND meta.column_id = task.column_id "
          "WHERE ((task.state IN (%ld, %ld)) "
          "       OR (task.state = %ld AND task.eligible_ts <= "
          "           DATE_SUB(NOW(6), INTERVAL %ld SECOND))) "
          "  AND task.eligible_ts <= NOW(6) "
          "ORDER BY task.request_id, task.table_id, task.column_id LIMIT %ld",
          OB_ALL_AI_COLUMN_TASK_TNAME, OB_ALL_AI_COLUMN_TNAME,
          AI_TASK_PENDING, AI_TASK_RETRY, AI_TASK_RUNNING,
          AI_TASK_RUNNING_TIMEOUT_SECOND, AI_TASK_BATCH_SIZE))) {
  } else {
    ObMySQLProxy::MySQLResult result_holder;
    if (OB_FAIL(GCTX.sql_proxy_->read(result_holder, sql.ptr()))) {
    } else if (OB_ISNULL(result_holder.get_result())) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      sqlclient::ObMySQLResult *result = result_holder.get_result();
      int next_ret = OB_SUCCESS;
      while (OB_SUCC(ret) && OB_SUCCESS == (next_ret = result->next())) {
        int64_t table_id = 0;
        int64_t column_id = 0;
        int64_t rowkey_hash = 0;
        int64_t retry_count = 0;
        int64_t max_retries = 0;
        common::ObString request_id;
        common::ObString predicate;
        AiTaskGroup *group = nullptr;
        if (OB_FAIL(result->get_int("table_id", table_id))) {
        } else if (OB_FAIL(result->get_int("column_id", column_id))) {
        } else if (OB_FAIL(result->get_int("rowkey_hash", rowkey_hash))) {
        } else if (OB_FAIL(fetch_string(*result, "request_id", allocator, request_id))) {
        } else if (OB_FAIL(fetch_string(*result, "rowkey_predicate", allocator, predicate))) {
        } else if (OB_FAIL(result->get_int("retry_count", retry_count))) {
        } else if (OB_FAIL(result->get_int("max_retries", max_retries))) {
        } else {
          for (int64_t i = 0; OB_ISNULL(group) && i < groups.count(); ++i) {
            AiTaskGroup *candidate = groups.at(i);
            if (OB_NOT_NULL(candidate)
                && static_cast<uint64_t>(table_id) == candidate->table_id_
                && candidate->request_id_.length() == request_id.length()
                && 0 == MEMCMP(candidate->request_id_.ptr(), request_id.ptr(),
                               request_id.length())
                && candidate->predicate_.length() == predicate.length()
                && 0 == MEMCMP(candidate->predicate_.ptr(), predicate.ptr(),
                               predicate.length())) {
              group = candidate;
            }
          }
          if (OB_ISNULL(group)) {
            void *mem = allocator.alloc(sizeof(AiTaskGroup));
            if (OB_ISNULL(mem)) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              group = new (mem) AiTaskGroup();
              group->table_id_ = static_cast<uint64_t>(table_id);
              if (OB_FAIL(copy_string(allocator, request_id, group->request_id_))) {
              } else if (OB_FAIL(copy_string(allocator, predicate, group->predicate_))) {
              } else if (OB_FAIL(groups.push_back(group))) {
              }
            }
          }
          if (OB_SUCC(ret)) {
            AiTaskColumn column;
            column.column_id_ = static_cast<uint64_t>(column_id);
            column.rowkey_hash_ = rowkey_hash;
            column.retry_count_ = retry_count;
            column.max_retries_ = max_retries;
            if (OB_FAIL(group->columns_.push_back(column))) {
            }
          }
        }
      }
      if (OB_ITER_END == next_ret && OB_SUCC(ret)) {
        // no more tasks
      } else if (OB_SUCCESS != next_ret && OB_SUCC(ret)) {
        ret = next_ret;
      }
    }
  }
  return ret;
}

// Finds the generation rule of the AI column `column_id` of `table`.  Returns
// OB_ENTRY_NOT_EXIST when the column carries no rule, which is the case when
// the definition of the table changed after the task was written.
int find_ai_column_rule(const share::schema::ObTableSchema &table,
                        const uint64_t column_id,
                        common::ObIAllocator &allocator,
                        AiColumnRule &rule)
{
  int ret = OB_SUCCESS;
  const share::schema::ObColumnSchemaV2 *result_column = table.get_column_schema(column_id);
  if (OB_ISNULL(result_column)) {
    ret = OB_ENTRY_NOT_EXIST;
  } else {
    const common::ObString column_name = result_column->get_column_name();
    const std::string name(column_name.ptr(), column_name.length());
    const std::string spec_name = share::schema::ObAiColumnSpec::spec_column_name(name);
    const std::string state_name = share::schema::ObAiColumnSpec::state_column_name(name);
    const share::schema::ObColumnSchemaV2 *spec_column =
        table.get_column_schema(spec_name.c_str());
    if (OB_ISNULL(spec_column)) {
      ret = OB_ENTRY_NOT_EXIST;
    } else {
      const common::ObString comment = spec_column->get_comment_str();
      share::schema::ObAiColumnSpec spec;
      if (!share::schema::ObAiColumnSpec::decode(
              std::string(comment.ptr(), comment.length()), spec)) {
        ret = OB_ENTRY_NOT_EXIST;
      } else {
        const common::ObString model(spec.model_name_.size(), spec.model_name_.c_str());
        const common::ObString prompt(spec.prompt_expr_.size(), spec.prompt_expr_.c_str());
        const common::ObString system(spec.system_prompt_.size(),
                                      spec.system_prompt_.c_str());
        const common::ObString state(state_name.size(), state_name.c_str());
        rule.ai_func_type_ = spec.ai_func_type_;
        rule.declared_dim_ = spec.declared_dim_;
        rule.max_retries_ = spec.max_retries_;
        if (OB_FAIL(copy_string(allocator, column_name, rule.result_column_))) {
        } else if (OB_FAIL(copy_string(allocator, state, rule.state_column_))) {
        } else if (OB_FAIL(copy_string(allocator, model, rule.model_name_))) {
        } else if (OB_FAIL(copy_string(allocator, prompt, rule.prompt_expr_))) {
        } else if (OB_FAIL(copy_string(allocator, system, rule.system_prompt_))) {
        } else if (rule.prompt_expr_.empty() || rule.model_name_.empty()) {
          ret = OB_ENTRY_NOT_EXIST;
        }
      }
    }
  }
  return ret;
}

// The SQL text that asks the model for the value of one AI column.
int build_ai_expression(const AiColumnRule &rule, common::ObSqlString &expression)
{
  int ret = OB_SUCCESS;
  if (2 == rule.ai_func_type_) {
    if (OB_FAIL(expression.append("AI_EMBED("))) {
    } else if (OB_FAIL(append_sql_literal(expression, rule.model_name_))) {
    } else if (OB_FAIL(expression.append(", "))) {
    } else if (OB_FAIL(expression.append(rule.prompt_expr_.ptr(),
                                        rule.prompt_expr_.length()))) {
    } else if (rule.declared_dim_ > 0
               && OB_FAIL(expression.append_fmt(", %ld", rule.declared_dim_))) {
    } else if (OB_FAIL(expression.append(")"))) {
    }
  } else {
    if (OB_FAIL(expression.append("AI_COMPLETE("))) {
    } else if (OB_FAIL(append_sql_literal(expression, rule.model_name_))) {
    } else if (OB_FAIL(expression.append(", "))) {
    } else if (OB_FAIL(expression.append(rule.prompt_expr_.ptr(),
                                        rule.prompt_expr_.length()))) {
    } else if (!rule.system_prompt_.empty()
               && (OB_FAIL(expression.append(", '{\"system\":\""))
                   || OB_FAIL(append_json_string_body(expression, rule.system_prompt_))
                   || OB_FAIL(expression.append("\"}'")))) {
    } else if (OB_FAIL(expression.append(")"))) {
    }
  }
  return ret;
}

int key_condition(common::ObSqlString &sql,
                  const uint64_t table_id,
                  const uint64_t column_id,
                  const int64_t rowkey_hash,
                  const common::ObString &request_id)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(sql.append_fmt("table_id = %lu AND column_id = %lu AND "
                             "rowkey_hash = %ld AND request_id = ",
                             static_cast<unsigned long>(table_id),
                             static_cast<unsigned long>(column_id),
                             rowkey_hash))) {
  } else if (OB_FAIL(append_sql_literal(sql, request_id))) {
  }
  return ret;
}

// Takes the ownership of one task.  Only one worker can claim a task, so a task
// is never executed twice unless the worker that claimed it died.
int claim_task(const AiTaskColumn &column,
               const uint64_t table_id,
               const common::ObString &request_id,
               bool &claimed)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  int64_t affected_rows = 0;
  claimed = false;
  if (OB_FAIL(sql.assign_fmt("UPDATE oceanbase.%s SET state = %ld WHERE ",
                             OB_ALL_AI_COLUMN_TASK_TNAME, AI_TASK_RUNNING))) {
  } else if (OB_FAIL(key_condition(sql, table_id, column.column_id_,
                                   column.rowkey_hash_, request_id))) {
  } else if (OB_FAIL(sql.append_fmt(
                 " AND (state IN (%ld, %ld) OR (state = %ld AND eligible_ts <= "
                 "DATE_SUB(NOW(6), INTERVAL %ld SECOND)))",
                 AI_TASK_PENDING, AI_TASK_RETRY, AI_TASK_RUNNING,
                 AI_TASK_RUNNING_TIMEOUT_SECOND))) {
  } else if (OB_FAIL(GCTX.sql_proxy_->write(sql.ptr(), affected_rows))) {
  } else {
    claimed = (affected_rows > 0);
  }
  return ret;
}

int delete_task(common::ObISQLClient &client,
                const uint64_t table_id,
                const uint64_t column_id,
                const int64_t rowkey_hash,
                const common::ObString &request_id)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  int64_t affected_rows = 0;
  if (OB_FAIL(sql.assign_fmt("DELETE FROM oceanbase.%s WHERE ",
                             OB_ALL_AI_COLUMN_TASK_TNAME))) {
  } else if (OB_FAIL(key_condition(sql, table_id, column_id, rowkey_hash, request_id))) {
  } else if (OB_FAIL(client.write(sql.ptr(), affected_rows))) {
  }
  return ret;
}

// Puts the task back into the retry state with a longer eligibility delay, or
// records the final failure of the column when the attempts are exhausted.  The
// result of a finally failed column stays null.
int retry_or_fail_task(const AiColumnTarget &target,
                       const AiColumnRule &rule,
                       const AiTaskColumn &column,
                       const uint64_t table_id,
                       const common::ObString &request_id,
                       const int error_code)
{
  int ret = OB_SUCCESS;
  const int64_t next_retry = column.retry_count_ + 1;
  UNUSED(rule);
  if (next_retry > column.max_retries_) {
    // The result of the column stays null and the task is removed, so the
    // column reports a final failure: no task is pending for the row.
    if (OB_FAIL(delete_task(*GCTX.sql_proxy_, table_id, column.column_id_,
                            column.rowkey_hash_, request_id))) {
    }
  } else {
    ObSqlString sql;
    int64_t affected_rows = 0;
    if (OB_FAIL(sql.assign_fmt(
            "UPDATE oceanbase.%s SET state = %ld, retry_count = %ld, "
            "eligible_ts = NOW(6) + INTERVAL %ld MICROSECOND, "
            "last_error = 'ai column generation failed: %d' WHERE ",
            OB_ALL_AI_COLUMN_TASK_TNAME, AI_TASK_RETRY, next_retry,
            AI_TASK_RETRY_BACKOFF_US, error_code))) {
    } else if (OB_FAIL(key_condition(sql, table_id, column.column_id_,
                                     column.rowkey_hash_, request_id))) {
    } else if (OB_FAIL(GCTX.sql_proxy_->write(sql.ptr(), affected_rows))) {
    }
  }
  return ret;
}

// Evaluates the generation rule of one AI column on the row of the task.  The
// guard is the digest of the input of the model: a result is only published
// when the input it was produced from is still the content of the row, so a
// late result can never overwrite the result of a newer write.
int evaluate_ai_column(const AiColumnTarget &target,
                       const AiColumnRule &rule,
                       common::ObIAllocator &allocator,
                       common::ObString &value,
                       common::ObString &guard,
                       bool &found,
                       int &error_code)
{
  int ret = OB_SUCCESS;
  found = false;
  error_code = OB_SUCCESS;
  ObSqlString expression;
  ObSqlString sql;
  if (OB_FAIL(build_ai_expression(rule, expression))) {
  } else if (OB_FAIL(sql.assign_fmt("SELECT %s AS ai_value, MD5(%s) AS ai_guard "
                                    "FROM `%.*s`.`%.*s` WHERE %.*s LIMIT 1",
                                    expression.ptr(),
                                    rule.prompt_expr_.ptr(),
                                    target.database_name_.length(),
                                    target.database_name_.ptr(),
                                    target.table_name_.length(),
                                    target.table_name_.ptr(),
                                    target.predicate_.length(),
                                    target.predicate_.ptr()))) {
  } else {
    ObMySQLProxy::MySQLResult result_holder;
    if (OB_FAIL(GCTX.sql_proxy_->read(result_holder, sql.ptr()))) {
      // The request failed: a timeout, an error of the service or a value the
      // type of the column cannot hold.  It is a failure of this attempt, not
      // of the worker, so the column is retried or finally declared failed.
      error_code = ret;
      ret = OB_SUCCESS;
    } else if (OB_ISNULL(result_holder.get_result())) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      sqlclient::ObMySQLResult *result = result_holder.get_result();
      const int next_ret = result->next();
      if (OB_ITER_END == next_ret) {
        // The row does not match the request anymore, the request is stale.
        found = false;
      } else if (OB_SUCCESS != next_ret) {
        error_code = next_ret;
        ret = OB_SUCCESS;
      } else if (OB_FAIL(fetch_string(*result, "ai_value", allocator, value))) {
      } else if (OB_FAIL(fetch_string(*result, "ai_guard", allocator, guard))) {
      } else {
        found = true;
      }
    }
  }
  return ret;
}

// Publishes the results of one change.  All AI columns of the change are
// published in one transaction, so a reader never sees a part of the change.
int publish_group(const AiTaskGroup &group,
                  const AiColumnTarget &target,
                  common::ObIArray<AiColumnRule> &rules,
                  common::ObIArray<common::ObString> &values,
                  common::ObIArray<common::ObString> &guards)
{
  int ret = OB_SUCCESS;
  ObMySQLTransaction trans;
  // The write below publishes results; it must not be read as a change of the
  // row that would invalidate them again.
  sql::ObAiColumnDml::PublishGuard publish_guard;
  if (OB_FAIL(trans.start(GCTX.sql_proxy_, false))) {
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < rules.count(); ++i) {
      ObSqlString sql;
      int64_t affected_rows = 0;
      if (OB_FAIL(sql.assign_fmt("UPDATE `%.*s`.`%.*s` SET `%.*s` = ",
                                 target.database_name_.length(),
                                 target.database_name_.ptr(),
                                 target.table_name_.length(),
                                 target.table_name_.ptr(),
                                 rules.at(i).result_column_.length(),
                                 rules.at(i).result_column_.ptr()))) {
      } else if (OB_FAIL(append_sql_literal(sql, values.at(i)))) {
      } else if (OB_FAIL(sql.append(" WHERE "))) {
      } else if (OB_FAIL(sql.append(target.predicate_.ptr(),
                                    target.predicate_.length()))) {
      } else if (OB_FAIL(sql.append_fmt(" AND MD5(%s) = ",
                                        rules.at(i).prompt_expr_.ptr()))) {
      } else if (OB_FAIL(append_sql_literal(sql, guards.at(i)))) {
      } else if (OB_FAIL(trans.write(sql.ptr(), affected_rows))) {
        LOG_WARN("the publish write failed", K(ret), K(sql));
      } else if (0 >= affected_rows) {
        LOG_WARN("the ai column result was not published, the request is stale",
                 K(affected_rows));
      }
    }
    // The tasks of the change are consumed together with its results.
    for (int64_t i = 0; OB_SUCC(ret) && i < rules.count(); ++i) {
      const AiTaskColumn &column = group.columns_.at(i);
      if (OB_FAIL(delete_task(trans, group.table_id_, column.column_id_,
                              column.rowkey_hash_, group.request_id_))) {
      }
    }
    const int end_ret = trans.end(OB_SUCCESS == ret);
    if (OB_SUCC(ret) && OB_SUCCESS != end_ret) {
      ret = end_ret;
    }
  }
  return ret;
}

// Handles every task of one change of one row.
int process_group(const AiTaskGroup &group)
{
  int ret = OB_SUCCESS;
  ObArenaAllocator allocator(ObMemAttr("AiColumnWorker", ObCtxIds::DEFAULT_CTX_ID));
  share::schema::ObSchemaGetterGuard guard;
  const share::schema::ObTableSchema *table_schema = nullptr;
  const share::schema::ObDatabaseSchema *database_schema = nullptr;
  bool table_gone = false;
  if (OB_FAIL(GCTX.schema_service_->get_runtime_schema_guard(guard))) {
  } else if (OB_FAIL(guard.get_table_schema(group.table_id_, table_schema))) {
  } else if (OB_ISNULL(table_schema)) {
    table_gone = true;
  } else if (OB_FAIL(guard.get_database_schema(table_schema->get_database_id(),
                                               database_schema))) {
  } else if (OB_ISNULL(database_schema)) {
    table_gone = true;
  }

  if (OB_SUCC(ret) && table_gone) {
    // The definition of the table is gone, the tasks cannot be served.
    for (int64_t i = 0; OB_SUCC(ret) && i < group.columns_.count(); ++i) {
      if (OB_FAIL(delete_task(*GCTX.sql_proxy_, group.table_id_,
                              group.columns_.at(i).column_id_,
                              group.columns_.at(i).rowkey_hash_,
                              group.request_id_))) {
      }
    }
  } else if (OB_SUCC(ret)) {
    AiColumnTarget target;
    target.database_name_ = database_schema->get_database_name_str();
    target.table_name_ = table_schema->get_table_name_str();
    target.predicate_ = group.predicate_;
    common::ObSEArray<AiColumnRule, 4> rules;
    common::ObSEArray<common::ObString, 4> values;
    common::ObSEArray<common::ObString, 4> guards;
    common::ObSEArray<AiTaskColumn, 4> published_columns;
    for (int64_t i = 0; OB_SUCC(ret) && i < group.columns_.count(); ++i) {
      const AiTaskColumn &column = group.columns_.at(i);
      AiColumnRule rule;
      int rule_ret = find_ai_column_rule(*table_schema, column.column_id_, allocator, rule);
      if (OB_ENTRY_NOT_EXIST == rule_ret) {
        // The definition of the column changed after the task was written.
        if (OB_FAIL(delete_task(*GCTX.sql_proxy_, group.table_id_, column.column_id_,
                                column.rowkey_hash_, group.request_id_))) {
        }
        continue;
      } else if (OB_FAIL(rule_ret)) {
        ret = rule_ret;
        break;
      }
      bool claimed = false;
      if (OB_FAIL(claim_task(column, group.table_id_, group.request_id_, claimed))) {
        LOG_WARN("the ai column claim failed", K(ret));
      } else if (!claimed) {
        // Another worker owns the task, or it is already done.
        continue;
      } else {
        common::ObString value;
        common::ObString guard_text;
        bool found = false;
        int error_code = OB_SUCCESS;
        if (OB_FAIL(evaluate_ai_column(target, rule, allocator, value, guard_text,
                                       found, error_code))) {
        } else if (found) {
          if (OB_FAIL(rules.push_back(rule))
              || OB_FAIL(values.push_back(value))
              || OB_FAIL(guards.push_back(guard_text))
              || OB_FAIL(published_columns.push_back(column))) {
          }
        } else if (OB_SUCCESS != error_code) {
          if (OB_FAIL(retry_or_fail_task(target, rule, column, group.table_id_,
                                         group.request_id_, error_code))) {
          }
        } else {
          if (OB_FAIL(delete_task(*GCTX.sql_proxy_, group.table_id_,
                                  column.column_id_, column.rowkey_hash_,
                                  group.request_id_))) {
          }
        }
      }
    }

    if (OB_SUCC(ret) && rules.count() > 0) {
      int publish_ret = publish_group(group, target, rules, values, guards);
      if (OB_SUCCESS == publish_ret) {
        // the results and the tasks were published together
      } else {
        // The results could not be published, for example because the model
        // returned a value that the type of the column cannot hold.  The
        // columns are retried, or declared failed when no attempt is left.
        LOG_WARN("publish the ai column results failed", K(publish_ret));
        for (int64_t i = 0; i < rules.count(); ++i) {
          const int ignored = retry_or_fail_task(target, rules.at(i),
                                                 published_columns.at(i),
                                                 group.table_id_,
                                                 group.request_id_, publish_ret);
          UNUSED(ignored);
        }
      }
    }
  }
  return ret;
}

} // namespace

ObAiService::ObAiService()
  : is_inited_(false)
{
}

ObAiService::~ObAiService()
{
  destroy();
}

int ObAiService::server_module_init(ObAiService* &ai_service)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(ai_service)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ai_service->init())) {
  }
  return ret;
}

int ObAiService::init()
{
  int ret = OB_SUCCESS;
  if (IS_INIT) {
    ret = OB_INIT_TWICE;
  } else if (OB_FAIL(ai_column_worker_.init())) {
    LOG_WARN("init the ai column worker failed", K(ret));
  } else {
    is_inited_ = true;
  }
  return ret;
}

int ObAiService::start()
{
  int ret = OB_SUCCESS;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else if (OB_FAIL(ai_column_worker_.start())) {
    LOG_WARN("start the ai column worker failed", K(ret));
  } else {
    LOG_INFO("the ai column worker is started");
  }
  return ret;
}

void ObAiService::stop()
{
  ai_column_worker_.stop();
}

void ObAiService::wait()
{
  ai_column_worker_.wait();
}

void ObAiService::destroy()
{
  ai_column_worker_.destroy();
  is_inited_ = false;
}

int ObAiService::get_ai_service_guard(ObAiServiceGuard &ai_service_guard)
{
  // TODO: implement after cache support
  int ret = OB_SUCCESS;
  return ret;
}

ObAiServiceGuard::ObAiServiceGuard()
  : local_allocator_(ObMemAttr("AiServiceGuard", ObCtxIds::DEFAULT_CTX_ID))
{
}

ObAiServiceGuard::~ObAiServiceGuard()
{
}

int ObAiServiceGuard::check_access_privilege()
{
  int ret = OB_SUCCESS;
  bool has_priv = false;

  sql::ObSQLSessionInfo *session = nullptr;
  if (OB_ISNULL(session = THIS_WORKER.get_session())) {
    // system background task thread do not have session, skip check
  } else {
    ObArenaAllocator tmp_allocator;
    share::schema::ObSchemaGetterGuard *schema_guard = session->get_cur_exec_ctx()->get_sql_ctx()->schema_guard_;
    if (OB_ISNULL(schema_guard)) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      sql::ObAIServiceEndpointPrivUtil priv_util(*schema_guard);
      share::schema::ObSessionPrivInfo session_priv;
      uint64_t user_id = session->get_priv_user_id();
      if (user_id == OB_INVALID_ID) {
        user_id = OB_SYS_USER_ID;
      }
      if (OB_FAIL(schema_guard->get_session_priv_info(
                                                    user_id,
                                                    session->get_database_name(),
                                                    session_priv))) {
      } else if (OB_FAIL(priv_util.check_access_ai_model_priv(tmp_allocator, session_priv, has_priv))) {
      } else if (!has_priv) {
        ret = OB_ERR_NO_PRIVILEGE;
        LOG_WARN("no privilege for ai model access", K(ret));
        LOG_USER_ERROR(OB_ERR_NO_PRIVILEGE, "access ai model");
      }
    }
  }

  return ret;
}

int ObAiServiceGuard::get_ai_endpoint(const common::ObString &name, const share::ObAiModelEndpointInfo *&endpoint_info)
{
  int ret = OB_SUCCESS;
  ObAiModelEndpointInfo *tmp_endpoint_info = nullptr;

  if (OB_FAIL(check_access_privilege())) {
  } else if (OB_ISNULL(tmp_endpoint_info = OB_NEWx(ObAiModelEndpointInfo, &local_allocator_))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else if (OB_FAIL(ObAiServiceExecutor::read_ai_endpoint(local_allocator_, name, *tmp_endpoint_info))) {
  } else {
    endpoint_info = tmp_endpoint_info;
  }
  return ret;
}

int ObAiServiceGuard::get_ai_endpoint_by_ai_model_name(const common::ObString &ai_model_name, const share::ObAiModelEndpointInfo *&endpoint_info, bool need_check)
{
  int ret = OB_SUCCESS;
  ObAiModelEndpointInfo *tmp_endpoint_info = nullptr;

  if (need_check && OB_FAIL(check_access_privilege())) {
    LOG_WARN("failed to check access privilege", K(ret));
  } else if (OB_ISNULL(tmp_endpoint_info = OB_NEWx(ObAiModelEndpointInfo, &local_allocator_))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else if (OB_FAIL(ObAiServiceExecutor::read_ai_endpoint_by_ai_model_name(local_allocator_, ai_model_name, *tmp_endpoint_info))) {
  } else {
    endpoint_info = tmp_endpoint_info;
  }
  return ret;
}

ObAiColumnWorker::ObAiColumnWorker()
  : is_inited_(false)
{
}

ObAiColumnWorker::~ObAiColumnWorker()
{
  destroy();
}

int ObAiColumnWorker::init()
{
  int ret = OB_SUCCESS;
  if (is_inited_) {
    ret = OB_INIT_TWICE;
  } else if (OB_ISNULL(share::server_runtime())) {
    ret = OB_NOT_INIT;
  } else {
    // The worker runs in the runtime context of the server, so that it can use
    // the same internal sql connection pool as every other background task.
    share::ObThreadPool::set_run_wrapper(share::server_runtime());
    if (OB_FAIL(share::ObThreadPool::init())) {
      LOG_WARN("init the thread of the ai column worker failed", K(ret));
    } else {
      is_inited_ = true;
    }
  }
  return ret;
}

void ObAiColumnWorker::destroy()
{
  if (is_inited_) {
    stop();
    wait();
    is_inited_ = false;
  }
}

void ObAiColumnWorker::run1()
{
  while (!has_set_stop()) {
    int ret = OB_SUCCESS;
    if (OB_SUCCESS == share::check_server_runtime_ready()) {
      if (OB_FAIL(run_one_round())) {
        LOG_WARN("run one round of the ai column worker failed", K(ret));
      }
    }
    ob_usleep(AI_TASK_IDLE_SLEEP_US);
  }
}

int ObAiColumnWorker::run_one_round()
{
  int ret = OB_SUCCESS;
  ObArenaAllocator allocator(ObMemAttr("AiColumnWorker", ObCtxIds::DEFAULT_CTX_ID));
  common::ObSEArray<AiTaskGroup *, 8> groups;
  if (OB_ISNULL(GCTX.sql_proxy_) || OB_ISNULL(GCTX.schema_service_)) {
    ret = OB_NOT_INIT;
  } else if (OB_FAIL(read_tasks(allocator, groups))) {
    LOG_WARN("the ai column worker cannot read the tasks", K(ret));
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < groups.count(); ++i) {
      if (OB_FAIL(process_group(*groups.at(i)))) {
        LOG_WARN("process the tasks of one write failed", K(ret));
        ret = OB_SUCCESS;
      }
    }
  }
  return ret;
}

} // namespace omt
} // namespace oceanbase
