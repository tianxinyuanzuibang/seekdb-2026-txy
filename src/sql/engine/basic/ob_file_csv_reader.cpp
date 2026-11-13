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

#define USING_LOG_PREFIX SQL_ENG

#include "sql/engine/basic/ob_file_csv_reader.h"

#include <sys/stat.h>

#include "share/ob_errno.h"
#include "lib/utility/utility.h"

namespace oceanbase
{
namespace sql
{

namespace
{
// The read buffer size.  A record longer than this still works: the parser
// keeps the incomplete tail and asks for more data.
const int64_t CSV_READ_BUFFER_SIZE = 64 * 1024;
}  // namespace

ObFileCsvReader::ObFileCsvReader()
  : fp_(nullptr),
    buffer_(),
    buf_pos_(0),
    eof_(false),
    header_consumed_(false),
    file_changed_(false),
    file_size_(0),
    file_mtime_(0),
    options_(),
    header_(),
    field_(),
    record_(),
    in_quotes_(false),
    field_started_(false),
    quoted_field_(false)
{
}

ObFileCsvReader::~ObFileCsvReader()
{
  close();
}

void ObFileCsvReader::close()
{
  if (nullptr != fp_) {
    ::fclose(fp_);
    fp_ = nullptr;
  }
}

int ObFileCsvReader::open(const common::ObString &path,
                           const ObFileReadOptions &options,
                           common::ObIAllocator &allocator,
                           ObFileSchema &schema,
                           const std::vector<int64_t> &projected)
{
  int ret = OB_SUCCESS;
  UNUSED(allocator);
  UNUSED(projected);
  options_ = options;
  if (OB_FAIL(open_file(path))) {
  } else if (OB_FAIL(infer_schema(schema))) {
  } else {
    // restart the scan after the inference pass
    if (OB_FAIL(::fseek(fp_, 0, SEEK_SET))) {
      ret = OB_IO_ERROR;
    } else {
      reset_scan();
    }
  }
  if (OB_FAIL(ret)) {
    close();
  }
  return ret;
}

int ObFileCsvReader::open_for_scan(const common::ObString &path,
                                   const ObFileReadOptions &options,
                                   common::ObIAllocator &allocator)
{
  int ret = OB_SUCCESS;
  UNUSED(allocator);
  options_ = options;
  if (OB_FAIL(open_file(path))) {
    close();
  }
  return ret;
}

int ObFileCsvReader::open_file(const common::ObString &path)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(path.ptr()) || 0 == path.length()) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "the file table function needs a path");
    return ret;
  }
  fp_ = ::fopen(path.ptr(), "rb");
  if (nullptr == fp_) {
    ret = OB_FILE_NOT_EXIST;
    LOG_USER_ERROR(OB_FILE_NOT_EXIST, path.ptr());
    return ret;
  }
  struct stat stat_buf;
  if (0 == ::stat(path.ptr(), &stat_buf)) {
    file_size_ = stat_buf.st_size;
    file_mtime_ = stat_buf.st_mtime;
  }
  buffer_.reserve(CSV_READ_BUFFER_SIZE * 2);
  reset_scan();
  return ret;
}

void ObFileCsvReader::reset_scan()
{
  buf_pos_ = 0;
  buffer_.clear();
  eof_ = false;
  header_consumed_ = false;
  in_quotes_ = false;
  field_started_ = false;
  quoted_field_ = false;
  field_.clear();
  record_.clear();
  record_quoted_.clear();
}

void ObFileCsvReader::compact_buffer()
{
  if (buf_pos_ > 0) {
    buffer_.erase(0, buf_pos_);
    buf_pos_ = 0;
  }
}

int ObFileCsvReader::fill_buffer()
{
  int ret = OB_SUCCESS;
  if (eof_) {
    return OB_SUCCESS;
  }
  // Keep the unparsed tail and read more data after it.
  compact_buffer();
  const size_t old_size = buffer_.size();
  buffer_.resize(old_size + CSV_READ_BUFFER_SIZE);
  const size_t read_bytes = ::fread(&buffer_[old_size], 1, CSV_READ_BUFFER_SIZE, fp_);
  if (read_bytes < CSV_READ_BUFFER_SIZE) {
    eof_ = true;
  }
  buffer_.resize(old_size + read_bytes);
  if (0 == read_bytes && old_size == buf_pos_) {
    return OB_SUCCESS;
  }
  return ret;
}

int ObFileCsvReader::parse_record(std::vector<std::string> &fields, bool &is_eof)
{
  int ret = OB_SUCCESS;
  is_eof = false;
  record_.clear();
  record_quoted_.clear();
  field_.clear();
  in_quotes_ = false;
  field_started_ = false;
  quoted_field_ = false;
  bool record_done = false;
  while (!record_done) {
    if (buf_pos_ >= buffer_.size()) {
      if (OB_FAIL(fill_buffer())) {
        LOG_ERROR("the csv file could not be read", K(ret));
        return ret;
      }
      if (buf_pos_ >= buffer_.size()) {
        // End of file.  A trailing record without a newline is still a
        // record, an empty file has no record at all.
        if (record_.empty() && field_.empty() && !field_started_) {
          is_eof = true;
          // The record is empty, the fields of the caller are cleared as
          // well so it does not work with the values of the last record.
          fields = record_;
          return OB_SUCCESS;
        }
        push_field();
        record_done = true;
        break;
      }
    }
    const char c = buffer_[buf_pos_];
    if (in_quotes_) {
      if (c == options_.quote) {
        // A doubled quote inside a quoted field is a literal quote.
        if (buf_pos_ + 1 < buffer_.size() && buffer_[buf_pos_ + 1] == options_.quote) {
          field_.push_back(options_.quote);
          buf_pos_ += 2;
          continue;
        }
        in_quotes_ = false;
        ++buf_pos_;
        continue;
      }
      field_.push_back(c);
      ++buf_pos_;
      continue;
    }
    if (c == options_.quote && !field_started_) {
      in_quotes_ = true;
      field_started_ = true;
      quoted_field_ = true;
      ++buf_pos_;
      continue;
    }
    if (c == options_.delimiter) {
      push_field();
      ++buf_pos_;
      continue;
    }
    if (c == '\n') {
      push_field();
      ++buf_pos_;
      record_done = true;
      break;
    }
    if (c == '\r') {
      // CRLF and a lone CR both end the record.
      push_field();
      ++buf_pos_;
      if (buf_pos_ < buffer_.size() && buffer_[buf_pos_] == '\n') {
        ++buf_pos_;
      }
      record_done = true;
      break;
    }
    field_.push_back(c);
    field_started_ = true;
    ++buf_pos_;
  }
  fields = record_;
  return ret;
}

void ObFileCsvReader::push_field()
{
  // An unquoted field is trimmed, a quoted one keeps its blanks.
  if (!quoted_field_) {
    size_t b = 0;
    size_t e = field_.size();
    while (b < e && (field_[b] == ' ' || field_[b] == '\t')) {
      ++b;
    }
    while (e > b && (field_[e - 1] == ' ' || field_[e - 1] == '\t')) {
      --e;
    }
    field_ = field_.substr(b, e - b);
  }
  record_.push_back(field_);
  record_quoted_.push_back(quoted_field_);
  field_.clear();
  field_started_ = false;
  quoted_field_ = false;
}

void ObFileCsvReader::build_header(std::vector<std::string> &fields)
{
  header_.clear();
  for (size_t i = 0; i < fields.size(); ++i) {
    std::string name = fields[i];
    // Trim the surrounding blanks of an unquoted header field.
    const bool was_quoted = (i < record_quoted_.size()) && record_quoted_[i];
    if (!was_quoted) {
      size_t b = 0;
      size_t e = name.size();
      while (b < e && (name[b] == ' ' || name[b] == '\t')) {
        ++b;
      }
      while (e > b && (name[e - 1] == ' ' || name[e - 1] == '\t')) {
        --e;
      }
      name = name.substr(b, e - b);
    }
    if (name.empty()) {
      name = "c" + std::to_string(i + 1);
    }
    header_.push_back(name);
  }
  // A duplicated header name gets a numeric suffix so the columns stay
  // addressable.
  for (size_t i = 0; i < header_.size(); ++i) {
    int suffix = 2;
    for (size_t j = 0; j < i; ++j) {
      if (header_[j] == header_[i]) {
        header_[i] = header_[i] + "__" + std::to_string(suffix);
        ++suffix;
      }
    }
  }
}

int ObFileCsvReader::infer_schema(ObFileSchema &schema)
{
  int ret = OB_SUCCESS;
  std::vector<std::string> fields;
  bool is_eof = false;
  if (OB_FAIL(parse_record(fields, is_eof))) {
    return ret;
  }
  if (is_eof) {
    // An empty file has no schema at all.
    LOG_ERROR("the csv file is empty", K(record_.size()), K(field_.size()));
    ret = OB_INVALID_DATA;
    LOG_USER_ERROR(OB_INVALID_DATA, "the csv file is empty");
    return ret;
  }
  const size_t column_count = fields.size();
  if (options_.has_header) {
    build_header(fields);
    header_consumed_ = true;
    // Read the first data record so the inference sees at least one row.
    if (OB_FAIL(parse_record(fields, is_eof))) {
      return ret;
    }
    if (is_eof) {
      // The file holds only the header, there is no row to infer from.
      fields.clear();
    }
  } else {
    header_.clear();
    for (size_t i = 0; i < column_count; ++i) {
      header_.push_back("c" + std::to_string(i + 1));
    }
  }
  schema.columns.clear();
  for (size_t i = 0; i < header_.size(); ++i) {
    ObFileColumn column(header_[i]);
    // A column that never holds a null is not nullable.
    column.nullable = false;
    schema.columns.push_back(column);
  }
  // The inference looks at the whole file so the result does not depend on
  // how many rows happened to be sampled.
  int64_t row_count = 0;
  std::vector<bool> nulls;
  // `fields` holds the record that was read before the loop: the first data
  // record of a file with a header, or the first record of a file without
  // one.  It is part of the inference.
  bool have_record = true;
  while (true) {
    if (!have_record) {
      if (OB_FAIL(parse_record(fields, is_eof))) {
        return ret;
      }
      if (is_eof) {
        break;
      }
    }
    have_record = false;
    ++row_count;
    if (OB_FAIL(compute_nulls(nulls))) {
      return ret;
    }
    for (size_t i = 0; i < schema.columns.size() && i < fields.size() && i < nulls.size(); ++i) {
      if (nulls[i]) {
        // A NULL in any row keeps the column nullable.
        schema.columns[i].nullable = true;
        continue;
      }
      const ObFileInferType type = infer_file_type(fields[i], false);
      schema.columns[i].type = merge_file_infer_type(schema.columns[i].type, type);
    }
    if (options_.max_rows > 0 && row_count >= options_.max_rows) {
      break;
    }
  }
  // A column that only ever held NULL has no type of its own; it stays text.
  for (size_t i = 0; i < schema.columns.size(); ++i) {
    if (OB_FILE_TYPE_NULL == schema.columns[i].type) {
      schema.columns[i].type = OB_FILE_TYPE_VARCHAR;
    }
  }
  return ret;
}

int ObFileCsvReader::compute_nulls(std::vector<bool> &nulls)
{
  int ret = OB_SUCCESS;
  // The fields were trimmed when the record was parsed, so an unquoted field
  // that holds only blanks is empty and therefore NULL.
  nulls.clear();
  nulls.reserve(record_.size());
  for (size_t i = 0; i < record_.size(); ++i) {
    const bool was_quoted = (i < record_quoted_.size()) && record_quoted_[i];
    bool is_null = (!was_quoted && record_[i].empty());
    if (!is_null && options_.has_null_string && options_.null_string.length() > 0
        && record_[i].length() == static_cast<size_t>(options_.null_string.length())
        && 0 == ::memcmp(record_[i].data(), options_.null_string.ptr(),
                         static_cast<size_t>(options_.null_string.length()))) {
      is_null = true;
    }
    nulls.push_back(is_null);
  }
  return ret;
}

int ObFileCsvReader::next_row(std::vector<std::string> &fields, std::vector<bool> &nulls)
{
  int ret = OB_SUCCESS;
  if (nullptr == fp_) {
    ret = OB_ITER_END;
    return ret;
  }
  bool is_eof = false;
  while (true) {
    if (OB_FAIL(parse_record(fields, is_eof))) {
      return ret;
    }
    if (is_eof) {
      break;
    }
    // The header row is not data, it only names the columns.  A file without
    // a header starts with the first row of data.
    if (options_.has_header && !header_consumed_) {
      header_consumed_ = true;
      continue;
    }
    break;
  }
  if (is_eof) {
    ret = OB_ITER_END;
    return ret;
  }
  if (OB_FAIL(compute_nulls(nulls))) {
    return ret;
  }
  fields = record_;
  return ret;
}

}  // namespace sql
}  // namespace oceanbase
