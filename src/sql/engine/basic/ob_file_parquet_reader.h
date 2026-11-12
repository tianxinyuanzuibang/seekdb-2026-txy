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

// A streaming Parquet reader.
//
// The footer holds the schema and the row group metadata.  The reader decodes
// one row group at a time and only reads the pages of the columns the query
// projects, so neither the whole file nor the whole result is held in memory.
//
// The supported subset:
//   * the flat schema of the footer, with the basic physical types,
//   * several row groups,
//   * nullable columns, encoded with the definition levels,
//   * the PLAIN encoding,
//   * the UNCOMPRESSED and SNAPPY codecs.
class ObFileParquetReader : public ObFileReader
{
public:
  ObFileParquetReader();
  virtual ~ObFileParquetReader();

  // Opens the file and reads the footer.  `projected` lists the indexes of
  // the columns the query needs; an empty list reads every column.
  virtual int open(const common::ObString &path,
                   const ObFileReadOptions &options,
                   common::ObIAllocator &allocator,
                   ObFileSchema &schema,
                   const std::vector<int64_t> &projected);

  // Produces the next row.  `values` holds one entry per column and `nulls`
  // tells which of them are NULL.  Returns OB_ITER_END at the end of the
  // file.
  virtual int next_row(std::vector<std::string> &values, std::vector<bool> &nulls);

  // Opens the file for the scan, without inferring the schema again.
  virtual int open_for_scan(const common::ObString &path,
                             const ObFileReadOptions &options,
                             common::ObIAllocator &allocator);

  virtual void close();

  virtual bool file_changed() const { return file_changed_; }

  int64_t row_count() const { return total_rows_; }

private:
  struct ParquetColumnMeta
  {
    std::string name;
    int type;          // the parquet physical type
    bool optional;
    int64_t num_values;
    int codec;
    int64_t data_page_offset;
    int64_t total_compressed_size;
    ParquetColumnMeta()
      : type(-1), optional(false), num_values(0), codec(0),
        data_page_offset(-1), total_compressed_size(0)
    {}
  };

  struct ParquetRowGroup
  {
    int64_t num_rows;
    std::vector<ParquetColumnMeta> columns;
    ParquetRowGroup() : num_rows(0) {}
  };

  int read_footer(ObFileSchema &schema);
  int read_row_groups();
  // Decodes the next row group into `group_values_` / `group_nulls_`.
  int decode_row_group();
  // Decodes the pages of one column chunk.
  int decode_column_chunk(const ParquetColumnMeta &meta,
                           std::vector<std::string> &values,
                           std::vector<bool> &nulls);
  // Reads `len` bytes at `offset` into `buf`.
  int read_at(int64_t offset, int64_t len, std::string &buf);

  FILE *fp_;
  int64_t file_size_;
  int64_t file_mtime_;
  bool file_changed_;
  ObFileReadOptions options_;
  std::vector<ParquetColumnMeta> schema_columns_;
  std::vector<ParquetRowGroup> row_groups_;
  std::vector<int64_t> projected_;
  int64_t total_rows_;
  // The row group that is currently decoded.
  size_t current_group_;
  int64_t current_row_;
  std::vector<std::vector<std::string> > group_values_;
  std::vector<std::vector<bool> > group_nulls_;
};

}  // namespace sql
}  // namespace oceanbase
