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
#include "lib/string/ob_string.h"
#include "share/ob_errno.h"
#include "share/schema/ob_ai_column_spec.h"
#include "share/schema/ob_table_schema.h"

namespace oceanbase
{
namespace sql
{

// Validates the AI column declarations of a table.
//
// The check runs in the execution stage on purpose.  The user message of an
// error is carried by the warning buffer, which is thread local, while the
// error packet of a DDL is sent by the thread that executes the statement.
// A check performed while the statement is parsed would therefore report the
// default message instead of the expected one.
class ObAiColumnValidator
{
public:
  // Checks every AI column of `table_schema`.
  //
  // `schema` is the table schema of the statement that is about to run.  It
  // carries the helper columns together with the comment that holds the
  // generation rule.
  static int validate(const share::schema::ObTableSchema &schema)
  {
    int ret = OB_SUCCESS;
    for (ObTableSchema::const_column_iterator iter = schema.column_begin();
         OB_SUCC(ret) && iter != schema.column_end(); ++iter) {
      if (OB_ISNULL(iter) || OB_ISNULL(*iter)) {
        continue;
      }
      const share::schema::ObColumnSchemaV2 &column = **iter;
      const common::ObString name = column.get_column_name();
      const std::string name_str(name.ptr(), name.length());
      const std::string prefix(share::schema::ObAiColumnSpec::SPEC_PREFIX());
      if (0 != name_str.compare(0, prefix.size(), prefix)) {
        continue;
      }
      const common::ObString comment = column.get_comment_str();
      share::schema::ObAiColumnSpec spec;
      if (!share::schema::ObAiColumnSpec::decode(std::string(comment.ptr(), comment.length()),
                                                  spec)) {
        continue;
      }
      if (OB_FAIL(validate_one(spec))) {
        break;
      }
    }
    return ret;
  }

private:
  static int validate_one(const share::schema::ObAiColumnSpec &spec)
  {
    int ret = OB_SUCCESS;
    if (!spec.ai_dep_column_.empty()) {
      // An AI column cannot read another AI column.
      //
      // The message of OB_NOT_SUPPORTED is built from a "%s not supported"
      // template, so the expected text is supplied directly here.
      ret = OB_NOT_SUPPORTED;
      FORWARD_USER_ERROR_MSG(OB_NOT_SUPPORTED, "Not supported feature or function");
    } else if (spec.dim_mismatch_) {
      // The user message of OB_ERR_INVALID_VECTOR_DIM is
      // "inconsistent dimension: expected %u got %u", the arguments are the
      // declared dimension and the requested one.
      ret = OB_ERR_INVALID_VECTOR_DIM;
      LOG_USER_ERROR(OB_ERR_INVALID_VECTOR_DIM,
                     static_cast<uint32_t>(spec.declared_dim_),
                     static_cast<uint32_t>(spec.actual_dim_));
    }
    return ret;
  }
};

} // namespace sql
} // namespace oceanbase
