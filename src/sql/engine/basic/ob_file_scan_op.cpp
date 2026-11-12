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

#include "sql/engine/basic/ob_file_scan_op.h"

#include <sys/stat.h>

#include <strings.h>

#include "lib/container/ob_array_serialization.h"
#include "lib/utility/alloc_assist.h"
#include "sql/engine/basic/ob_file_csv_reader.h"
#include "sql/engine/basic/ob_file_jsonl_reader.h"
#include "sql/engine/basic/ob_file_parquet_reader.h"
#include "sql/engine/basic/ob_file_path_utils.h"
#include "sql/engine/basic/ob_file_reader.h"
#include "sql/engine/ob_exec_context.h"
#include "sql/engine/expr/ob_datum_cast.h"
#include "sql/session/ob_basic_session_info.h"
#include "share/ob_errno.h"

namespace oceanbase
{
namespace sql
{

OB_DEF_SERIALIZE(ObFileScanSpec)
{
  INIT_SUCC(ret);
  BASE_SER((ObFileScanSpec, ObOpSpec));
  OB_UNIS_ENCODE(column_exprs_);
  // The indexes are a small plain array.
  OB_UNIS_ENCODE(column_index_count_);
  for (int64_t i = 0; OB_SUCC(ret) && i < column_index_count_; ++i) {
    OB_UNIS_ENCODE(column_indexes_[i]);
  }
  OB_UNIS_ENCODE(file_def_);
  return ret;
}

OB_DEF_SERIALIZE_SIZE(ObFileScanSpec)
{
  int64_t len = 0;
  BASE_ADD_LEN((ObFileScanSpec, ObOpSpec));
  OB_UNIS_ADD_LEN(column_exprs_);
  OB_UNIS_ADD_LEN(column_index_count_);
  for (int64_t i = 0; i < column_index_count_; ++i) {
    OB_UNIS_ADD_LEN(column_indexes_[i]);
  }
  len += file_def_.get_serialize_size();
  return len;
}

OB_DEF_DESERIALIZE(ObFileScanSpec)
{
  int ret = OB_SUCCESS;
  int64_t version = 0;
  int64_t len = 0;
  BASE_DESER((ObFileScanSpec, ObOpSpec));
  OB_UNIS_DECODE(column_exprs_);
  OB_UNIS_DECODE(column_index_count_);
  if (OB_FAIL(ret)) {
  } else if (column_index_count_ < 0 || column_index_count_ > MAX_FILE_COLUMNS) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    MEMSET(column_indexes_, 0, sizeof(column_indexes_));
    for (int64_t i = 0; OB_SUCC(ret) && i < column_index_count_; ++i) {
      OB_UNIS_DECODE(column_indexes_[i]);
    }
  }
  OB_UNIS_DECODE(file_def_);
  return ret;
}

ObFileScanOp::ObFileScanOp(ObExecContext &exec_ctx, const ObOpSpec &spec, ObOpInput *input)
  : ObOperator(exec_ctx, spec, input),
    reader_(),
    glob_files_(),
    glob_index_(0),
    row_values_(),
    row_nulls_(),
    row_alloc_(common::ObModIds::OB_MODULE_PAGE_ALLOCATOR, OB_MALLOC_NORMAL_BLOCK_SIZE),
    is_open_(false)
{
}

ObFileScanOp::~ObFileScanOp()
{
  if (nullptr != reader_) {
    reader_->close();
    reader_.reset();
  }
}

int ObFileScanOp::inner_open()
{
  int ret = OB_SUCCESS;
  is_open_ = false;
  glob_index_ = 0;
  glob_files_.clear();
  if (MY_SPEC.column_exprs_.empty()) {
    LOG_WARN("the file scan has no column", K(MY_SPEC.column_exprs_.count()),
             K(MY_SPEC.column_index_count_));
    ret = OB_ERR_UNEXPECTED;
  } else {
    if (OB_FAIL(open_reader())) {
    } else {
      is_open_ = true;
    }
  }
  return ret;
}

int ObFileScanOp::inner_rescan()
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObOperator::inner_rescan())) {
  } else {
    is_open_ = false;
    glob_index_ = 0;
    glob_files_.clear();
    row_alloc_.clear();
    if (nullptr != reader_) {
      reader_->close();
      reader_.reset();
    }
    if (OB_FAIL(open_reader())) {
    } else {
      is_open_ = true;
    }
  }
  return ret;
}

int ObFileScanOp::switch_iterator()
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObOperator::inner_switch_iterator())) {
  } else {
    is_open_ = false;
    glob_index_ = 0;
    glob_files_.clear();
    row_alloc_.clear();
    if (nullptr != reader_) {
      reader_->close();
      reader_.reset();
    }
    if (OB_FAIL(open_reader())) {
    } else {
      is_open_ = true;
    }
  }
  return ret;
}

int ObFileScanOp::open_reader()
{
  int ret = OB_SUCCESS;
  const ObFileTableDef &def = MY_SPEC.file_def_;
  if (OB_FILE_FORMAT_GLOB == def.format) {
    // The glob function only discovers the files, it does not read them.
    if (OB_FAIL(file_glob_list(def.file_pattern.ptr(), glob_files_))) {
      LOG_USER_ERROR(OB_FILE_NOT_EXIST, def.file_pattern.ptr());
    }
    return ret;
  }
  // The readers need the options of the table function.
  ObFileReadOptions options;
  options.format = def.format;
  options.has_header = def.has_header;
  options.sample_size = def.sample_size;
  options.delimiter = def.delimiter;
  options.quote = def.quote;
  options.escape = def.escape;
  options.has_null_string = def.has_null_string;
  options.null_string = def.null_string;
  options.max_rows = def.max_rows;
  switch (def.format) {
    case OB_FILE_FORMAT_CSV: {
      std::unique_ptr<ObFileCsvReader> reader(new ObFileCsvReader());
      if (OB_FAIL(reader->open_for_scan(def.file_path, options, ctx_.get_allocator()))) {
      } else {
        reader_ = std::move(reader);
      }
      break;
    }
    case OB_FILE_FORMAT_JSONL: {
      std::unique_ptr<ObFileJsonlReader> reader(new ObFileJsonlReader());
      if (OB_FAIL(reader->open_for_scan(def.file_path, options, ctx_.get_allocator()))) {
      } else {
        reader_ = std::move(reader);
      }
      break;
    }
    case OB_FILE_FORMAT_PARQUET: {
      std::unique_ptr<ObFileParquetReader> reader(new ObFileParquetReader());
      if (OB_FAIL(reader->open_for_scan(def.file_path, options, ctx_.get_allocator()))) {
      } else {
        reader_ = std::move(reader);
      }
      break;
    }
    default:
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "the file format is not supported");
      break;
  }
  return ret;
}

int ObFileScanOp::check_file_unchanged()
{
  int ret = OB_SUCCESS;
  const ObFileTableDef &def = MY_SPEC.file_def_;
  if (0 == def.file_path.length() || OB_FILE_FORMAT_GLOB == def.format) {
    return ret;
  }
  int64_t file_size = 0;
  int64_t file_mtime = 0;
  if (OB_FAIL(file_stat_info(def.file_path.ptr(), file_size, file_mtime))) {
    // The file is gone, the scan reports the error.
    ret = OB_ERR_UNEXPECTED;
    LOG_USER_ERROR(OB_ERR_UNEXPECTED, "the file of the file table function is gone");
  } else if (file_size != def.file_size || file_mtime != def.file_mtime) {
    // The file changed after the schema was inferred, so the schema may not
    // match the data any more.
    ret = OB_ERR_UNEXPECTED;
    LOG_USER_ERROR(OB_ERR_UNEXPECTED, "the file changed while it was being read");
    LOG_WARN("the file changed while it was being read", K(file_size), K(def.file_size),
              K(file_mtime), K(def.file_mtime));
  }
  return ret;
}

int ObFileScanOp::write_value(ObExpr *expr, const std::string &value, bool is_null)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(expr)) {
    ret = OB_ERR_UNEXPECTED;
    return ret;
  }
  ObDatum &datum = expr->locate_datum_for_write(eval_ctx_);
  if (is_null) {
    datum.set_null();
    expr->set_evaluated_projected(eval_ctx_);
    return ret;
  }
  const ObObjType type = expr->datum_meta_.type_;
  switch (type) {
    case ObIntType:
    case ObTinyIntType:
    case ObSmallIntType:
    case ObMediumIntType:
    case ObInt32Type: {
      int64_t number = 0;
      // A boolean column holds the text true or false.
      if (0 == ::strcasecmp(value.c_str(), "true")) {
        number = 1;
      } else if (0 == ::strcasecmp(value.c_str(), "false")) {
        number = 0;
      } else if (!parse_file_integer(value, number)) {
        LOG_ERROR("the file value does not parse as a number",
                  "value", value.c_str(), K(type), K(ret));
        ret = OB_INVALID_DATA;
      }
      if (OB_SUCC(ret)) {
        datum.set_int(number);
      }
      break;
    }
    case ObDoubleType:
    case ObFloatType: {
      double number = 0;
      // A column that was widened to a floating point number also holds the
      // integers that widened it, so an integer is accepted as well.
      int64_t int_number = 0;
      if (parse_file_double(value, number)) {
        datum.set_double(number);
      } else if (parse_file_integer(value, int_number)) {
        datum.set_double(static_cast<double>(int_number));
      } else {
        LOG_ERROR("the file value does not parse as a number",
                  "value", value.c_str(), K(ret));
        ret = OB_INVALID_DATA;
      }
      break;
    }
    case ObDateType: {
      int32_t date_value = 0;
      if (!parse_file_date(value, date_value)) {
        LOG_ERROR("the file value does not parse as a date",
                  "value", value.c_str(), K(ret));
        ret = OB_INVALID_DATA;
      } else {
        datum.set_date(date_value);
      }
      break;
    }
    case ObDateTimeType:
    case ObTimestampType: {
      int64_t datetime_value = 0;
      int32_t date_value = 0;
      // A column that was widened to a datetime also holds the plain dates
      // that widened it.
      if (parse_file_datetime(value, datetime_value)) {
        datum.set_datetime(datetime_value);
      } else if (parse_file_date(value, date_value)) {
        datum.set_datetime(static_cast<int64_t>(date_value) * 86400 * 1000000);
      } else {
        LOG_ERROR("the file value does not parse as a datetime",
                  "value", value.c_str(), K(ret));
        ret = OB_INVALID_DATA;
      }
      break;
    }
    default: {
      // The string is copied into the result buffer of the expression so it
      // stays valid until the parent consumed the row.
      const int64_t length = static_cast<int64_t>(value.length());
      if (0 == length) {
        datum.set_string("", 0);
      } else {
        ObExprStrResAlloc res_alloc(*expr, eval_ctx_);
        char *buf = static_cast<char *>(res_alloc.alloc(length));
        if (OB_ISNULL(buf)) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        } else {
          MEMCPY(buf, value.data(), length);
          datum.set_string(buf, static_cast<uint32_t>(length));
        }
      }
      break;
    }
  }
  if (OB_SUCC(ret)) {
    expr->set_evaluated_projected(eval_ctx_);
  }
  return ret;
}

int ObFileScanOp::write_row(const std::vector<std::string> &values, const std::vector<bool> &nulls)
{
  int ret = OB_SUCCESS;
  const int64_t column_count = MY_SPEC.column_exprs_.count();
  const int64_t index_count = MY_SPEC.column_index_count_;
  if (column_count != index_count) {
    LOG_WARN("the file scan has a mismatch of the columns", K(column_count), K(index_count));
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < column_count; ++i) {
    // The expression may belong to any column of the file, the index says
    // which value of the row belongs to it.
    int64_t index = i;
    if (i < index_count) {
      index = MY_SPEC.column_indexes_[i];
    }
    const bool is_null = (index >= static_cast<int64_t>(nulls.size()))
                         || nulls[static_cast<size_t>(index)];
    const std::string value = (index >= 0 && index < static_cast<int64_t>(values.size()))
                              ? values[static_cast<size_t>(index)]
                              : std::string();
    if (OB_FAIL(write_value(MY_SPEC.column_exprs_.at(i), value, is_null))) {
      LOG_ERROR("the value of the file does not match the column type",
                K(i), K(index), "value", value.c_str(), K(is_null), K(ret));
      LOG_USER_ERROR(OB_INVALID_DATA, "the value of the file does not match the column type");
    }
  }
  return ret;
}

int ObFileScanOp::inner_get_next_row()
{
  int ret = OB_SUCCESS;
  if (!is_open_) {
    ret = OB_ITER_END;
    return ret;
  }
  if (OB_FAIL(try_check_status())) {
    return ret;
  }
  clear_evaluated_flag();
  const ObFileTableDef &def = MY_SPEC.file_def_;
  if (OB_FILE_FORMAT_GLOB == def.format) {
    // The glob function only reports the files it found.
    if (glob_index_ >= glob_files_.size()) {
      ret = OB_ITER_END;
      return ret;
    }
    const std::string &file = glob_files_[glob_index_++];
    row_values_.clear();
    row_nulls_.clear();
    row_values_.push_back(file);
    row_nulls_.push_back(false);
    if (OB_FAIL(write_row(row_values_, row_nulls_))) {
      return ret;
    }
    return ret;
  }
  if (OB_FAIL(check_file_unchanged())) {
    return ret;
  }
  if (OB_ISNULL(reader_)) {
    ret = OB_ITER_END;
    return ret;
  }
  if (OB_FAIL(reader_->next_row(row_values_, row_nulls_))) {
    if (OB_ITER_END != ret) {
      LOG_WARN("failed to read the next row of the file", K(ret));
    }
    return ret;
  }
  if (OB_FAIL(write_row(row_values_, row_nulls_))) {
    return ret;
  }
  return ret;
}

int ObFileScanOp::inner_close()
{
  int ret = OB_SUCCESS;
  is_open_ = false;
  if (nullptr != reader_) {
    reader_->close();
    reader_.reset();
  }
  glob_files_.clear();
  glob_index_ = 0;
  row_values_.clear();
  row_nulls_.clear();
  row_alloc_.clear();
  return ret;
}

}  // namespace sql
}  // namespace oceanbase
