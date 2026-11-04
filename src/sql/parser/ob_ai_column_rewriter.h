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

#pragma once

#include "lib/allocator/ob_allocator.h"
#include "lib/ob_define.h"
#include "lib/string/ob_string.h"

namespace oceanbase
{
namespace sql
{

// Accepts the AI column declaration
//
//   create table t (
//     id bigint primary key,
//     content varchar(200),
//     tags json AI COLUMN (AI_COMPLETE('m', content))
//       WITH (sync_mode='async', max_retries=2));
//
// and rewrites it into a plain table whose AI column keeps its declared type
// and is followed by three hidden helper columns.  The generation rule is
// handed to share::schema::ObAiColumnRegistry, which the DDL layer turns into
// rows of __all_ai_column inside the create-table transaction.
class ObAiColumnRewriter
{
public:
  static int maybe_rewrite(ObIAllocator &alloc,
                           const common::ObString &stmt,
                           common::ObString &out,
                           bool &rewritten);
};

} // namespace sql
} // namespace oceanbase
