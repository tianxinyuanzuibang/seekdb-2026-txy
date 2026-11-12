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

#include "sql/engine/basic/ob_file_jsonl_reader.h"

#include <sys/stat.h>

#include "share/ob_errno.h"

namespace oceanbase
{
namespace sql
{

namespace
{
const int64_t JSONL_READ_BUFFER_SIZE = 64 * 1024;

bool is_blank(char c)
{
  return ' ' == c || '\t' == c || '\r' == c || '\n' == c;
}

// Skips the escape sequence that starts at `pos` and returns the character it
// stands for.  `pos` points at the backslash.
bool read_escape(const std::string &text, size_t &pos, std::string &out)
{
  if (pos + 1 >= text.size()) {
    return false;
  }
  const char c = text[++pos];
  switch (c) {
    case '"': out.push_back('"'); break;
    case '\\': out.push_back('\\'); break;
    case '/': out.push_back('/'); break;
    case 'b': out.push_back('\b'); break;
    case 'f': out.push_back('\f'); break;
    case 'n': out.push_back('\n'); break;
    case 'r': out.push_back('\r'); break;
    case 't': out.push_back('\t'); break;
    case 'u': {
      if (pos + 4 >= text.size()) {
        return false;
      }
      unsigned int code = 0;
      for (int i = 0; i < 4; ++i) {
        const char h = text[pos + 1 + i];
        unsigned int digit = 0;
        if (h >= '0' && h <= '9') {
          digit = static_cast<unsigned int>(h - '0');
        } else if (h >= 'a' && h <= 'f') {
          digit = static_cast<unsigned int>(h - 'a' + 10);
        } else if (h >= 'A' && h <= 'F') {
          digit = static_cast<unsigned int>(h - 'A' + 10);
        } else {
          return false;
        }
        code = (code << 4) | digit;
      }
      pos += 4;
      // Encode the code point as UTF-8.
      if (code < 0x80) {
        out.push_back(static_cast<char>(code));
      } else if (code < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (code >> 6)));
        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
      } else {
        out.push_back(static_cast<char>(0xE0 | (code >> 12)));
        out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
      }
      break;
    }
    default:
      return false;
  }
  return true;
}
}  // namespace

ObFileJsonlReader::ObFileJsonlReader()
  : fp_(nullptr),
    buffer_(),
    buf_pos_(0),
    eof_(false),
    file_changed_(false),
    file_size_(0),
    file_mtime_(0),
    options_(),
    column_names_(),
    column_index_()
{
}

ObFileJsonlReader::~ObFileJsonlReader()
{
  close();
}

void ObFileJsonlReader::close()
{
  if (nullptr != fp_) {
    ::fclose(fp_);
    fp_ = nullptr;
  }
}

int ObFileJsonlReader::open(const common::ObString &path,
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

int ObFileJsonlReader::open_for_scan(const common::ObString &path,
                                     const ObFileReadOptions &options,
                                     common::ObIAllocator &allocator)
{
  int ret = OB_SUCCESS;
  UNUSED(allocator);
  options_ = options;
  // The scan needs the mapping of the keys to the columns, so the schema is
  // inferred here as well.  The schema itself is dropped, the operator uses
  // the schema of the plan.
  ObFileSchema schema;
  if (OB_FAIL(open_file(path))) {
    close();
  } else if (OB_FAIL(infer_schema(schema))) {
    close();
  } else if (OB_FAIL(::fseek(fp_, 0, SEEK_SET))) {
    ret = OB_IO_ERROR;
    close();
  } else {
    reset_scan();
  }
  return ret;
}

int ObFileJsonlReader::open_file(const common::ObString &path)
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
  buffer_.reserve(JSONL_READ_BUFFER_SIZE * 2);
  reset_scan();
  return ret;
}

void ObFileJsonlReader::reset_scan()
{
  buf_pos_ = 0;
  buffer_.clear();
  eof_ = false;
}

void ObFileJsonlReader::compact_buffer()
{
  if (buf_pos_ > 0) {
    buffer_.erase(0, buf_pos_);
    buf_pos_ = 0;
  }
}

int ObFileJsonlReader::fill_buffer()
{
  int ret = OB_SUCCESS;
  if (eof_) {
    return ret;
  }
  compact_buffer();
  const size_t old_size = buffer_.size();
  buffer_.resize(old_size + JSONL_READ_BUFFER_SIZE);
  const size_t read_bytes = ::fread(&buffer_[old_size], 1, JSONL_READ_BUFFER_SIZE, fp_);
  if (read_bytes < JSONL_READ_BUFFER_SIZE) {
    eof_ = true;
  }
  buffer_.resize(old_size + read_bytes);
  return ret;
}

int ObFileJsonlReader::read_line(std::string &line)
{
  int ret = OB_SUCCESS;
  line.clear();
  while (true) {
    if (buf_pos_ >= buffer_.size()) {
      if (OB_FAIL(fill_buffer())) {
        return ret;
      }
      if (buf_pos_ >= buffer_.size()) {
        break;
      }
    }
    const size_t nl = buffer_.find('\n', buf_pos_);
    if (std::string::npos == nl) {
      // The line continues in the next buffer.
      line.append(buffer_, buf_pos_, buffer_.size() - buf_pos_);
      buf_pos_ = buffer_.size();
      continue;
    }
    line.append(buffer_, buf_pos_, nl - buf_pos_);
    buf_pos_ = nl + 1;
    break;
  }
  if (!line.empty() && '\r' == line[line.size() - 1]) {
    line.erase(line.size() - 1);
  }
  return ret;
}

bool ObFileJsonlReader::parse_flat_object(const std::string &line,
                                          std::vector<std::string> &keys,
                                          std::vector<std::string> &values)
{
  size_t i = 0;
  const size_t n = line.size();
  while (i < n && is_blank(line[i])) {
    ++i;
  }
  if (i >= n || '{' != line[i]) {
    return false;
  }
  ++i;
  while (i < n) {
    while (i < n && (is_blank(line[i]) || ',' == line[i])) {
      ++i;
    }
    if (i >= n) {
      return false;
    }
    if ('}' == line[i]) {
      return true;
    }
    if ('"' != line[i]) {
      return false;
    }
    std::string key;
    ++i;
    while (i < n && '"' != line[i]) {
      if ('\\' == line[i]) {
        if (!read_escape(line, i, key)) {
          return false;
        }
        continue;
      }
      key.push_back(line[i]);
      ++i;
    }
    if (i >= n) {
      return false;
    }
    ++i;  // the closing quote of the key
    while (i < n && is_blank(line[i])) {
      ++i;
    }
    if (i >= n || ':' != line[i]) {
      return false;
    }
    ++i;
    while (i < n && is_blank(line[i])) {
      ++i;
    }
    if (i >= n) {
      return false;
    }
    std::string value;
    if ('"' == line[i]) {
      // A string value: keep the quotes so the type inference can tell a
      // string from a number.
      value.push_back(line[i]);
      ++i;
      while (i < n && '"' != line[i]) {
        if ('\\' == line[i]) {
          ++i;
          if (i >= n) {
            return false;
          }
          // Keep the escape as it is, the value is handed to the engine as
          // text.
          value.push_back('\\');
          value.push_back(line[i]);
          ++i;
          continue;
        }
        value.push_back(line[i]);
        ++i;
      }
      if (i >= n) {
        return false;
      }
      value.push_back('"');
      ++i;
    } else if ('{' == line[i] || '[' == line[i]) {
      // A nested container is not supported.
      return false;
    } else {
      while (i < n && ',' != line[i] && '}' != line[i]) {
        value.push_back(line[i]);
        ++i;
      }
      // trim the trailing blanks of a bare value
      size_t e = value.size();
      while (e > 0 && is_blank(value[e - 1])) {
        --e;
      }
      value.erase(e);
      size_t b = 0;
      while (b < value.size() && is_blank(value[b])) {
        ++b;
      }
      value = value.substr(b);
    }
    keys.push_back(key);
    values.push_back(value);
  }
  return false;
}

int ObFileJsonlReader::infer_schema(ObFileSchema &schema)
{
  int ret = OB_SUCCESS;
  std::string line;
  std::vector<std::string> keys;
  std::vector<std::string> values;
  int64_t row_count = 0;
  while (true) {
    if (OB_FAIL(read_line(line))) {
      return ret;
    }
    // Skip the blank lines.
    bool only_blanks = true;
    for (size_t i = 0; i < line.size(); ++i) {
      if (!is_blank(line[i])) {
        only_blanks = false;
        break;
      }
    }
    if (only_blanks) {
      if (line.empty() && eof_ && buf_pos_ >= buffer_.size()) {
        break;
      }
      continue;
    }
    keys.clear();
    values.clear();
    if (!parse_flat_object(line, keys, values)) {
      ret = OB_INVALID_DATA;
      LOG_USER_ERROR(OB_INVALID_DATA, "the jsonl file holds a line that is not a flat json object");
      return ret;
    }
    ++row_count;
    // The schema is the union of the keys of all objects.
    for (size_t i = 0; i < keys.size(); ++i) {
      bool found = false;
      for (size_t j = 0; j < column_names_.size(); ++j) {
        if (column_names_[j] == keys[i]) {
          found = true;
          break;
        }
      }
      if (!found) {
        column_index_.push_back(std::make_pair(keys[i], static_cast<int64_t>(column_names_.size())));
        column_names_.push_back(keys[i]);
      }
    }
    if (options_.max_rows > 0 && row_count >= options_.max_rows) {
      break;
    }
  }
  if (column_names_.empty()) {
    ret = OB_INVALID_DATA;
    LOG_USER_ERROR(OB_INVALID_DATA, "the jsonl file is empty");
    return ret;
  }
  schema.columns.clear();
  for (size_t i = 0; i < column_names_.size(); ++i) {
    ObFileColumn column(column_names_[i]);
    column.nullable = true;
    schema.columns.push_back(column);
  }
  // The second pass infers the types.  The file is read again from the
  // start, still without materialising it.
  if (OB_FAIL(::fseek(fp_, 0, SEEK_SET))) {
    ret = OB_IO_ERROR;
    return ret;
  }
  buf_pos_ = 0;
  buffer_.clear();
  eof_ = false;
  row_count = 0;
  while (true) {
    if (OB_FAIL(read_line(line))) {
      return ret;
    }
    bool only_blanks = true;
    for (size_t i = 0; i < line.size(); ++i) {
      if (!is_blank(line[i])) {
        only_blanks = false;
        break;
      }
    }
    if (only_blanks) {
      // An empty line at the end of the file ends the pass.
      if (line.empty() && eof_ && buf_pos_ >= buffer_.size()) {
        break;
      }
      continue;
    }
    keys.clear();
    values.clear();
    if (!parse_flat_object(line, keys, values)) {
      ret = OB_INVALID_DATA;
      return ret;
    }
    ++row_count;
    for (size_t i = 0; i < keys.size(); ++i) {
      for (size_t j = 0; j < column_index_.size(); ++j) {
        if (column_index_[j].first == keys[i]) {
          ObFileColumn &column = schema.columns[static_cast<size_t>(column_index_[j].second)];
          const ObFileInferType type = infer_file_type(values[i], true);
          column.type = merge_file_infer_type(column.type, type);
          if (OB_FILE_TYPE_NULL == type) {
            // The column holds a null in this object.
            column.nullable = true;
          }
          break;
        }
      }
    }
    // A key that is missing in this object is a null as well.
    for (size_t j = 0; j < column_index_.size(); ++j) {
      bool found = false;
      for (size_t i = 0; i < keys.size(); ++i) {
        if (column_index_[j].first == keys[i]) {
          found = true;
          break;
        }
      }
      if (!found) {
        schema.columns[static_cast<size_t>(column_index_[j].second)].nullable = true;
      }
    }
    if (options_.max_rows > 0 && row_count >= options_.max_rows) {
      break;
    }
  }
  // A key that only ever held NULL has no type of its own; it stays text.
  for (size_t i = 0; i < schema.columns.size(); ++i) {
    if (OB_FILE_TYPE_NULL == schema.columns[i].type) {
      schema.columns[i].type = OB_FILE_TYPE_VARCHAR;
    }
  }
  return ret;
}

int ObFileJsonlReader::next_row(std::vector<std::string> &values, std::vector<bool> &nulls)
{
  int ret = OB_SUCCESS;
  if (nullptr == fp_) {
    ret = OB_ITER_END;
    return ret;
  }
  std::string line;
  std::vector<std::string> keys;
  std::vector<std::string> raw_values;
  while (true) {
    if (OB_FAIL(read_line(line))) {
      return ret;
    }
    bool only_blanks = true;
    for (size_t i = 0; i < line.size(); ++i) {
      if (!is_blank(line[i])) {
        only_blanks = false;
        break;
      }
    }
    if (only_blanks) {
      // The end of the file ends the scan.
      if (line.empty() && eof_ && buf_pos_ >= buffer_.size()) {
        ret = OB_ITER_END;
        return ret;
      }
      continue;
    }
    keys.clear();
    raw_values.clear();
    if (!parse_flat_object(line, keys, raw_values)) {
      ret = OB_INVALID_DATA;
      LOG_USER_ERROR(OB_INVALID_DATA, "the jsonl file holds a line that is not a flat json object");
      return ret;
    }
    break;
  }
  values.assign(column_names_.size(), std::string());
  nulls.assign(column_names_.size(), true);
  for (size_t i = 0; i < keys.size(); ++i) {
    for (size_t j = 0; j < column_index_.size(); ++j) {
      if (column_index_[j].first == keys[i]) {
        const int64_t index = column_index_[j].second;
        values[static_cast<size_t>(index)] = raw_values[i];
        nulls[static_cast<size_t>(index)] = ("null" == raw_values[i]);
        break;
      }
    }
  }
  // A JSON string is handed to the engine as text, so the quotes and the
  // escape sequences are removed.
  for (size_t i = 0; i < values.size(); ++i) {
    if (nulls[i]) {
      continue;
    }
    const std::string &value = values[i];
    if (value.length() >= 2 && '"' == value[0] && '"' == value[value.length() - 1]) {
      std::string plain;
      plain.reserve(value.length());
      // Skip the opening quote, the closing quote stays out of the loop.
      size_t pos = 1;
      const size_t end = value.length() - 1;
      while (pos < end) {
        if ('\\' == value[pos] && pos + 1 < end) {
          if (!read_escape(value, pos, plain)) {
            // the escape is not understood, keep the text as it is
            plain.append(value, pos, end - pos);
            break;
          }
          continue;
        }
        plain.push_back(value[pos]);
        ++pos;
      }
      values[i] = plain;
    }
  }
  return ret;
}

}  // namespace sql
}  // namespace oceanbase
