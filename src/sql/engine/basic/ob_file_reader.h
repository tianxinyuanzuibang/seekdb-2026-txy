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

#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{

// The interface every file reader implements.
//
// A reader opens a file, infers the schema and then produces the rows one at
// a time.  The rows are never collected in memory: the reader holds one row
// plus its own read buffer.
class ObFileReader
{
public:
  ObFileReader() {}
  virtual ~ObFileReader() {}

  // Opens the file and infers the schema.  `projected` lists the indexes of
  // the columns the query needs; it may be empty, which reads every column.
  virtual int open(const common::ObString &path,
                   const ObFileReadOptions &options,
                   common::ObIAllocator &allocator,
                   ObFileSchema &schema,
                   const std::vector<int64_t> &projected) = 0;

  // Opens the file for the scan.  The schema was already inferred when the
  // statement was resolved, so the file is not read again here.
  virtual int open_for_scan(const common::ObString &path,
                             const ObFileReadOptions &options,
                             common::ObIAllocator &allocator) = 0;

  // Produces the next row.  `values` holds one entry per column and `nulls`
  // tells which of them are NULL.  Returns OB_ITER_END at the end of the
  // file.
  virtual int next_row(std::vector<std::string> &values, std::vector<bool> &nulls) = 0;

  // Closes the file and releases the resources.
  virtual void close() = 0;

  // Reports whether the file changed since it was opened.
  virtual bool file_changed() const { return false; }
};

}  // namespace sql
}  // namespace oceanbase
