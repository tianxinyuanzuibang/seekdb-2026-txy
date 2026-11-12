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

#include "sql/engine/basic/ob_file_scan_utils.h"
#include "lib/allocator/ob_allocator.h"
#include "lib/string/ob_string.h"
#include "lib/utility/ob_print_utils.h"
#include "lib/utility/serialization.h"

#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{

// The definition of a file table function.  The resolver fills it in, the
// optimizer copies it into the access path and the physical operator reads it
// back at execution time.
//
// The definition is built once at resolve time.  The schema inference reads
// the whole file, but only the resulting column metadata is kept: the rows are
// streamed by the operator, so the file is never materialised.
//
// The definition is serializable so a plan that holds it can be sent to
// another node.
struct ObFileTableDef
{
public:
  ObFileTableDef()
    : format(OB_FILE_FORMAT_CSV),
      has_header(true),
      sample_size(-1),
      delimiter(','),
      quote('"'),
      escape('"'),
      has_null_string(false),
      max_rows(-1),
      null_string(),
      file_path(),
      file_pattern(),
      schema(),
      row_count_estimate(0),
      file_size(0),
      file_mtime(0)
  {}

  ObFileFormat format;
  bool has_header;
  int64_t sample_size;
  char delimiter;
  char quote;
  char escape;
  bool has_null_string;
  int64_t max_rows;
  // The text that marks a NULL value.
  common::ObString null_string;
  // The absolute path of the file to read.
  common::ObString file_path;
  // The glob pattern for the `glob()` table function.
  common::ObString file_pattern;
  // The inferred schema.
  ObFileSchema schema;
  // A rough row count estimate, used by the optimizer only.
  int64_t row_count_estimate;
  // The size and modification time of the file at resolve time.  The operator
  // compares them before scanning so a file that changed in the meantime is
  // reported instead of silently returning different data.
  int64_t file_size;
  int64_t file_mtime;

  int deep_copy(const ObFileTableDef &src, common::ObIAllocator &allocator);

  // The definition is serialized on its own, so a plan that holds it can be
  // sent to another node.
  int serialize(char *buf, const int64_t buf_len, int64_t &pos) const;
  int deserialize(const char *buf, const int64_t data_len, int64_t &pos);
  int64_t get_serialize_size() const;

  // The definition is printed in the explain output.
  int64_t to_string(char *buf, const int64_t buf_len) const
  {
    return snprintf(buf, buf_len,
                     "format:%d header:%d sample_size:%ld delimiter:%c path:%.*s",
                     static_cast<int>(format), has_header ? 1 : 0,
                     sample_size, delimiter,
                     file_path.length(), file_path.ptr() ? file_path.ptr() : "");
  }
};

}  // namespace sql
}  // namespace oceanbase
