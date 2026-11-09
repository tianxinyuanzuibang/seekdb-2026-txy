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

#define USING_LOG_PREFIX SHARE_SCHEMA

#include "share/schema/ob_ai_column_runtime.h"

namespace oceanbase
{
namespace share
{
namespace schema
{

int ObAiColumnRuntime::collect_ai_columns(const ObTableSchema &table_schema,
                                          ObIAllocator &allocator,
                                          ObIArray<ObAiColumnSpec> &specs)
{
  int ret = OB_SUCCESS;
  UNUSED(allocator);
  const std::string spec_prefix(ObAiColumnSpec::SPEC_PREFIX());
  for (ObTableSchema::const_column_iterator iter = table_schema.column_begin();
       OB_SUCC(ret) && iter != table_schema.column_end(); ++iter) {
    if (OB_ISNULL(iter) || OB_ISNULL(*iter)) {
      continue;
    }
    const ObColumnSchemaV2 &column = **iter;
    const char *name = column.get_column_name();
    if (OB_ISNULL(name)) {
      continue;
    }
    const std::string column_name(name);
    if (0 != column_name.compare(0, spec_prefix.size(), spec_prefix)) {
      continue;
    }
    const char *comment = column.get_comment();
    if (OB_ISNULL(comment)) {
      continue;
    }
    ObAiColumnSpec spec;
    if (!ObAiColumnSpec::decode(std::string(comment), spec)) {
      LOG_WARN("undecodable ai column spec", K(ret));
      continue;
    }
    spec.column_name_ = column_name.substr(spec_prefix.size());
    if (OB_FAIL(specs.push_back(spec))) {
    }
  }
  return ret;
}

bool ObAiColumnRuntime::has_ai_column(const ObTableSchema &table_schema)
{
  const std::string spec_prefix(ObAiColumnSpec::SPEC_PREFIX());
  for (ObTableSchema::const_column_iterator iter = table_schema.column_begin();
       iter != table_schema.column_end(); ++iter) {
    if (OB_ISNULL(iter) || OB_ISNULL(*iter)) {
      continue;
    }
    const char *name = (*iter)->get_column_name();
    if (OB_ISNULL(name)) {
      continue;
    }
    if (0 == std::string(name).compare(0, spec_prefix.size(), spec_prefix)) {
      return true;
    }
  }
  return false;
}

} // namespace schema
} // namespace share
} // namespace oceanbase
