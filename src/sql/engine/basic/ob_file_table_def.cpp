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

#include "sql/engine/basic/ob_file_table_def.h"

#include "lib/utility/serialization.h"

namespace oceanbase
{
namespace sql
{

namespace
{

// Writes a text with a length prefix.
int encode_text(char *buf, const int64_t buf_len, int64_t &pos, const std::string &text)
{
  int ret = OB_SUCCESS;
  const int64_t length = static_cast<int64_t>(text.size());
  if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, length))) {
  } else if (length > 0) {
    if (buf_len - pos < length) {
      ret = OB_SIZE_OVERFLOW;
    } else {
      MEMCPY(buf + pos, text.data(), length);
      pos += length;
    }
  }
  return ret;
}

// Reads a text with a length prefix.
int decode_text(const char *buf, const int64_t data_len, int64_t &pos, std::string &text)
{
  int ret = OB_SUCCESS;
  int64_t length = 0;
  text.clear();
  if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &length))) {
  } else if (length < 0) {
    ret = OB_ERR_UNEXPECTED;
  } else if (length > 0) {
    if (data_len - pos < length) {
      ret = OB_SIZE_OVERFLOW;
    } else {
      text.assign(buf + pos, static_cast<size_t>(length));
      pos += length;
    }
  }
  return ret;
}

int64_t text_encoded_length(const std::string &text)
{
  return common::serialization::encoded_length_vi64(static_cast<int64_t>(text.size()))
         + static_cast<int64_t>(text.size());
}

}  // namespace

int ObFileTableDef::serialize(char *buf, const int64_t buf_len, int64_t &pos) const
{
  int ret = OB_SUCCESS;
  const ObFileTableDef &def = *this;
  const int32_t format = static_cast<int32_t>(def.format);
  const int32_t has_header = def.has_header ? 1 : 0;
  const int32_t has_null_string = def.has_null_string ? 1 : 0;
  const int32_t delimiter = def.delimiter;
  const int32_t quote = def.quote;
  const int32_t escape = def.escape;
  if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, format))) {
  } else if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, has_header))) {
  } else if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, def.sample_size))) {
  } else if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, delimiter))) {
  } else if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, quote))) {
  } else if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, escape))) {
  } else if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, has_null_string))) {
  } else if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, def.max_rows))) {
  } else if (OB_FAIL(encode_text(buf, buf_len, pos, def.file_path.ptr() ? std::string(def.file_path.ptr(), def.file_path.length()) : std::string()))) {
  } else if (OB_FAIL(encode_text(buf, buf_len, pos, def.file_pattern.ptr() ? std::string(def.file_pattern.ptr(), def.file_pattern.length()) : std::string()))) {
  } else if (OB_FAIL(encode_text(buf, buf_len, pos, def.has_null_string && def.null_string.ptr() ? std::string(def.null_string.ptr(), def.null_string.length()) : std::string()))) {
  } else if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, def.row_count_estimate))) {
  } else if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, def.file_size))) {
  } else if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, def.file_mtime))) {
  } else {
    // The schema: the name and the type of every column.
    const int64_t column_count = static_cast<int64_t>(def.schema.columns.size());
    if (OB_FAIL(common::serialization::encode_vi64(buf, buf_len, pos, column_count))) {
    } else {
      for (int64_t i = 0; OB_SUCC(ret) && i < column_count; ++i) {
        const ObFileColumn &column = def.schema.columns[static_cast<size_t>(i)];
        const int32_t type = static_cast<int32_t>(column.type);
        const int32_t nullable = column.nullable ? 1 : 0;
        if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, type))) {
        } else if (OB_FAIL(common::serialization::encode_vi32(buf, buf_len, pos, nullable))) {
        } else if (OB_FAIL(encode_text(buf, buf_len, pos, column.name))) {
        }
      }
    }
  }
  return ret;
}

int ObFileTableDef::deserialize(const char *buf, const int64_t data_len, int64_t &pos)
{
  int ret = OB_SUCCESS;
  ObFileTableDef &def = *this;
  int32_t format = 0;
  int32_t has_header = 0;
  int32_t has_null_string = 0;
  int32_t delimiter = ',';
  int32_t quote = '"';
  int32_t escape = '"';
  int64_t sample_size = -1;
  int64_t max_rows = -1;
  int64_t row_count_estimate = 0;
  int64_t file_size = 0;
  int64_t file_mtime = 0;
  std::string file_path;
  std::string file_pattern;
  std::string null_string;
  if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &format))) {
  } else if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &has_header))) {
  } else if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &sample_size))) {
  } else if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &delimiter))) {
  } else if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &quote))) {
  } else if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &escape))) {
  } else if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &has_null_string))) {
  } else if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &max_rows))) {
  } else if (OB_FAIL(decode_text(buf, data_len, pos, file_path))) {
  } else if (OB_FAIL(decode_text(buf, data_len, pos, file_pattern))) {
  } else if (OB_FAIL(decode_text(buf, data_len, pos, null_string))) {
  } else if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &row_count_estimate))) {
  } else if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &file_size))) {
  } else if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &file_mtime))) {
  } else {
    def.format = static_cast<ObFileFormat>(format);
    def.has_header = (0 != has_header);
    def.sample_size = sample_size;
    def.delimiter = static_cast<char>(delimiter);
    def.quote = static_cast<char>(quote);
    def.escape = static_cast<char>(escape);
    def.has_null_string = (0 != has_null_string);
    def.max_rows = max_rows;
    def.file_path.assign_ptr(file_path.c_str(), static_cast<int32_t>(file_path.size()));
    def.file_pattern.assign_ptr(file_pattern.c_str(), static_cast<int32_t>(file_pattern.size()));
    if (def.has_null_string) {
      def.null_string.assign_ptr(null_string.c_str(), static_cast<int32_t>(null_string.size()));
    }
    int64_t column_count = 0;
    if (OB_FAIL(common::serialization::decode_vi64(buf, data_len, pos, &column_count))) {
    } else if (column_count < 0) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      def.schema.columns.clear();
      for (int64_t i = 0; OB_SUCC(ret) && i < column_count; ++i) {
        int32_t type = 0;
        int32_t nullable = 0;
        std::string name;
        if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &type))) {
        } else if (OB_FAIL(common::serialization::decode_vi32(buf, data_len, pos, &nullable))) {
        } else if (OB_FAIL(decode_text(buf, data_len, pos, name))) {
        } else {
          ObFileColumn column;
          column.name = name;
          column.type = static_cast<ObFileInferType>(type);
          column.nullable = (0 != nullable);
          def.schema.columns.push_back(column);
        }
      }
    }
  }
  return ret;
}

int64_t ObFileTableDef::get_serialize_size() const
{
  int64_t len = 0;
  const ObFileTableDef &def = *this;
  len += common::serialization::encoded_length_vi32(static_cast<int32_t>(def.format));
  len += common::serialization::encoded_length_vi32(def.has_header ? 1 : 0);
  len += common::serialization::encoded_length_vi64(def.sample_size);
  len += common::serialization::encoded_length_vi32(static_cast<int32_t>(def.delimiter));
  len += common::serialization::encoded_length_vi32(static_cast<int32_t>(def.quote));
  len += common::serialization::encoded_length_vi32(static_cast<int32_t>(def.escape));
  len += common::serialization::encoded_length_vi32(def.has_null_string ? 1 : 0);
  len += common::serialization::encoded_length_vi64(def.max_rows);
  len += text_encoded_length(def.file_path.ptr() ? std::string(def.file_path.ptr(), def.file_path.length()) : std::string());
  len += text_encoded_length(def.file_pattern.ptr() ? std::string(def.file_pattern.ptr(), def.file_pattern.length()) : std::string());
  len += text_encoded_length(def.has_null_string && def.null_string.ptr() ? std::string(def.null_string.ptr(), def.null_string.length()) : std::string());
  len += common::serialization::encoded_length_vi64(def.row_count_estimate);
  len += common::serialization::encoded_length_vi64(def.file_size);
  len += common::serialization::encoded_length_vi64(def.file_mtime);
  len += common::serialization::encoded_length_vi64(static_cast<int64_t>(def.schema.columns.size()));
  for (size_t i = 0; i < def.schema.columns.size(); ++i) {
    const ObFileColumn &column = def.schema.columns[i];
    len += common::serialization::encoded_length_vi32(static_cast<int32_t>(column.type));
    len += common::serialization::encoded_length_vi32(column.nullable ? 1 : 0);
    len += text_encoded_length(column.name);
  }
  return len;
}

int ObFileTableDef::deep_copy(const ObFileTableDef &src, common::ObIAllocator &allocator)
{
  int ret = OB_SUCCESS;
  format = src.format;
  has_header = src.has_header;
  sample_size = src.sample_size;
  delimiter = src.delimiter;
  quote = src.quote;
  escape = src.escape;
  has_null_string = src.has_null_string;
  max_rows = src.max_rows;
  row_count_estimate = src.row_count_estimate;
  file_size = src.file_size;
  file_mtime = src.file_mtime;
  if (OB_FAIL(ob_write_string(allocator, src.null_string, null_string))) {
  } else if (OB_FAIL(ob_write_string(allocator, src.file_path, file_path, true))) {
  } else if (OB_FAIL(ob_write_string(allocator, src.file_pattern, file_pattern, true))) {
  } else {
    schema.columns = src.schema.columns;
  }
  return ret;
}

}  // namespace sql
}  // namespace oceanbase
