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

#ifndef OCEANBASE_SQL_ENGINE_DML_OB_AI_COLUMN_DML_H_
#define OCEANBASE_SQL_ENGINE_DML_OB_AI_COLUMN_DML_H_

#include "lib/allocator/page_arena.h"
#include "lib/utility/ob_macro_utils.h"
#include "share/schema/ob_ai_column_spec.h"
#include "share/schema/ob_table_schema.h"

namespace oceanbase
{
namespace sql
{

class ObDMLBaseCtDef;
class ObDMLBaseRtDef;
class ObTableModifyOp;
class ObEvalCtx;
class ObExecContext;

// The DML side of the AI column feature.
//
// When a row of a table with AI columns is written, the AI columns are reset
// to null and a task is recorded in __all_ai_column_task.  The background
// worker picks the task up, calls the model and writes the result back.
//
// The task row is written inside the transaction of the user statement, so a
// rollback of the statement also discards the task and no request is sent for
// a row that never existed.
class ObAiColumnDml
{
public:
  // True when the table of the statement carries at least one AI column.
  // The answer is precomputed by the code generator, so this costs nothing on
  // the per row path of a table without AI columns.
  static bool has_ai_column(const ObDMLBaseCtDef &dml_ctdef);

  // Sets the AI columns of the row to null before the row is written.  An AI
  // column is never taken from the statement: its value is produced by the
  // worker, so a value written by the user would be overwritten anyway.
  //
  // For an update only the AI columns whose dependency column has changed are
  // reset, so that an update of an unrelated column keeps the result.
  static int reset_ai_columns(const ObDMLBaseCtDef &dml_ctdef, ObEvalCtx &eval_ctx);

  // Handles a row that has just been inserted: one task per AI column.
  static int handle_inserted_row(const ObDMLBaseCtDef &dml_ctdef,
                                 ObDMLBaseRtDef &dml_rtdef,
                                 ObTableModifyOp &dml_op);

  // Handles a row that has just been updated: a task only for the AI columns
  // that were invalidated.
  static int handle_updated_row(const ObDMLBaseCtDef &dml_ctdef,
                                ObDMLBaseRtDef &dml_rtdef,
                                ObTableModifyOp &dml_op);

  // Handles a row that has just been deleted: its tasks are removed.
  static int handle_deleted_row(const ObDMLBaseCtDef &dml_ctdef,
                                ObDMLBaseRtDef &dml_rtdef,
                                ObTableModifyOp &dml_op);

  // True while the caller is itself writing AI column results.  The worker
  // publishes a result with a write of the row, and that write must not be
  // treated as a change of the row: it would invalidate the very result it is
  // publishing and record a new task for it.
  static bool is_publishing_results()
  {
    return 0 < publish_depth_;
  }

  // Marks the current thread as writing AI column results for the lifetime of
  // the object.
  class PublishGuard
  {
  public:
    PublishGuard() { ++publish_depth_; }
    ~PublishGuard() { --publish_depth_; }
  private:
    DISALLOW_COPY_AND_ASSIGN(PublishGuard);
  };

private:
  static thread_local int64_t publish_depth_;
};

} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_DML_OB_AI_COLUMN_DML_H_
