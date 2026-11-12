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

#include "sql/engine/basic/ob_file_reader.h"
#include "sql/engine/basic/ob_file_scan_utils.h"
#include "lib/allocator/ob_allocator.h"
#include "lib/string/ob_string.h"

#include <cstdio>
#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{

// A streaming reader for newline delimited JSON.
//
// Every line holds one flat JSON object.  The schema is the union of the keys
// of all objects, in the order the keys are first seen; a key that is missing
// from an object is NULL there.
//
// Nested objects and arrays are rejected: the reader only supports flat
// documents.
class ObFileJsonlReader : public ObFileReader
{
public:
  ObFileJsonlReader();
  virtual ~ObFileJsonlReader();

  // Opens the file and infers the schema.
  virtual int open(const common::ObString &path,
                   const ObFileReadOptions &options,
                   common::ObIAllocator &allocator,
                   ObFileSchema &schema,
                   const std::vector<int64_t> &projected);

  // Produces the next row.  `values` holds one raw JSON value per column and
  // `nulls` tells which of them are NULL.  Returns OB_ITER_END at the end of
  // the file.
  virtual int next_row(std::vector<std::string> &values, std::vector<bool> &nulls);

  // Opens the file for the scan, without inferring the schema again.
  virtual int open_for_scan(const common::ObString &path,
                             const ObFileReadOptions &options,
                             common::ObIAllocator &allocator);

  virtual void close();

  virtual bool file_changed() const { return file_changed_; }

private:
  // Opens the file and records its size and modification time.
  int open_file(const common::ObString &path);
  // Resets the state of the scan.
  void reset_scan();
  int fill_buffer();
  void compact_buffer();
  // Reads one line into `line`.  Returns false at the end of the file.
  int read_line(std::string &line);
  int infer_schema(ObFileSchema &schema);
  // Splits a flat JSON object into its key/value pairs.  Returns false when
  // the line is not a flat JSON object.
  static bool parse_flat_object(const std::string &line,
                                 std::vector<std::string> &keys,
                                 std::vector<std::string> &values);

  FILE *fp_;
  std::string buffer_;
  size_t buf_pos_;
  bool eof_;
  bool file_changed_;
  int64_t file_size_;
  int64_t file_mtime_;
  ObFileReadOptions options_;
  // The column order of the schema.
  std::vector<std::string> column_names_;
  // The index of every key of the schema.
  std::vector<std::pair<std::string, int64_t> > column_index_;
};

}  // namespace sql
}  // namespace oceanbase
