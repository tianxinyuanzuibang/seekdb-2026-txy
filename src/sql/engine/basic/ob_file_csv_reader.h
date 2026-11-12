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

// A streaming CSV reader.
//
// The reader keeps one read buffer plus the row it is currently building, so
// a file of any size can be scanned without materialising it.  The schema is
// inferred by a first pass over the file; the rows are then produced one by
// one.
//
// The parser understands the usual CSV dialect:
//   * a header row is optional,
//   * fields may be quoted, and a doubled quote inside a quoted field is a
//     literal quote,
//   * a quoted field may span several lines,
//   * both LF and CRLF end a record,
//   * an empty field is NULL unless a null string is configured.
class ObFileCsvReader : public ObFileReader
{
public:
  ObFileCsvReader();
  virtual ~ObFileCsvReader();

  // Opens the file and infers the schema.  `options.has_header` tells
  // whether the first record holds the column names.
  virtual int open(const common::ObString &path,
                   const ObFileReadOptions &options,
                   common::ObIAllocator &allocator,
                   ObFileSchema &schema,
                   const std::vector<int64_t> &projected);

  // Produces the next row.  `fields` holds one entry per column and `nulls`
  // tells which of them are NULL; the strings stay valid until the next call.
  // Returns OB_ITER_END at the end of the file.
  virtual int next_row(std::vector<std::string> &fields, std::vector<bool> &nulls);

  // Opens the file for the scan, without inferring the schema again.
  virtual int open_for_scan(const common::ObString &path,
                             const ObFileReadOptions &options,
                             common::ObIAllocator &allocator);

  virtual void close();

  // Reports whether the file changed since it was opened.
  virtual bool file_changed() const { return file_changed_; }

  const std::vector<std::string> &header() const { return header_; }

private:
  // Opens the file and records its size and modification time.
  int open_file(const common::ObString &path);
  // Resets the state of the scan.
  void reset_scan();
  // Refills the buffer when it runs low.  Returns false at end of file.
  int fill_buffer();
  // Compacts the buffer so the unparsed tail starts at offset 0.
  void compact_buffer();
  // Parses one record.  Returns OB_ITER_END at the end of the file.
  int parse_record(std::vector<std::string> &fields, bool &is_eof);
  // Adds the field that is currently being assembled to the record.
  void push_field();
  // Computes which fields of the current record are NULL.
  int compute_nulls(std::vector<bool> &nulls);
  // Splits the header record into unique column names.
  void build_header(std::vector<std::string> &fields);
  int infer_schema(ObFileSchema &schema);

  FILE *fp_;
  std::string buffer_;
  size_t buf_pos_;
  bool eof_;
  bool header_consumed_;
  bool file_changed_;
  int64_t file_size_;
  int64_t file_mtime_;
  ObFileReadOptions options_;
  std::vector<std::string> header_;
  // The field that is currently being assembled.  It survives a buffer
  // refill, which is what lets a quoted field span a buffer boundary.
  std::string field_;
  // The fields of the record that is currently being assembled.
  std::vector<std::string> record_;
  // Whether the field that is currently being assembled was quoted.  A quoted
  // field keeps its blanks, an unquoted one is trimmed.
  std::vector<bool> record_quoted_;
  bool in_quotes_;
  bool field_started_;
  bool quoted_field_;
};

}  // namespace sql
}  // namespace oceanbase
