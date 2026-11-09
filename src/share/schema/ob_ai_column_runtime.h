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

#ifndef OCEANBASE_SHARE_SCHEMA_OB_AI_COLUMN_RUNTIME_H_
#define OCEANBASE_SHARE_SCHEMA_OB_AI_COLUMN_RUNTIME_H_

#include "lib/allocator/page_arena.h"
#include "lib/hash_func/murmur_hash.h"
#include "object/ob_object.h"
#include "share/schema/ob_ai_column_spec.h"
#include "share/schema/ob_table_schema.h"

namespace oceanbase
{
namespace share
{
namespace schema
{

// Runtime support for AI columns.
//
// A table with AI columns carries three hidden helper columns per AI column:
//   __ai_state_<col>  the state of the last request of that column
//   __ai_req_<col>    the request id that is allowed to write the result back
//   __ai_spec_<col>   the generation rule, encoded in the column comment
//
// The helper columns make the rule available wherever the table schema is
// available, which is what the DML layer and the background worker need.
class ObAiColumnRuntime
{
public:
  // The state of an AI column, stored in __ai_state_<col>.
  enum State
  {
    STATE_PENDING = 0,   // a request is in flight
    STATE_READY = 1,     // the column holds a result
    STATE_FAILED = 2,    // the request failed and will not be retried
  };

  // The state of a task row in __all_ai_column_task.
  // The state of a row of oceanbase.__all_ai_column_task.  The values are part
  // of the contract of the feature, a client can select them.
  enum TaskState
  {
    TASK_PENDING = 0,    // waiting to be picked up
    TASK_DONE = 1,       // finished
    TASK_RUNNING = 2,    // claimed by a worker
    TASK_RETRY = 3,      // failed, will be retried while retries are left
  };

  // Collects the AI columns of a table.  The rules are read back from the
  // comment of the __ai_spec_<col> helper columns.
  static int collect_ai_columns(const ObTableSchema &table_schema,
                                ObIAllocator &allocator,
                                ObIArray<ObAiColumnSpec> &specs);

  // True when the table has at least one AI column.
  static bool has_ai_column(const ObTableSchema &table_schema);

  // A stable hash of the row key, used as part of the primary key of a task
  // row so that two rows of the same table do not collide.
  static uint32_t rowkey_hash(const common::ObString &predicate)
  {
    return (OB_NOT_NULL(predicate.ptr()) && predicate.length() > 0)
               ? static_cast<uint32_t>(common::murmurhash(predicate.ptr(),
                                                          predicate.length(), 0))
               : 0;
  }
};

} // namespace schema
} // namespace share
} // namespace oceanbase

#endif // OCEANBASE_SHARE_SCHEMA_OB_AI_COLUMN_RUNTIME_H_
