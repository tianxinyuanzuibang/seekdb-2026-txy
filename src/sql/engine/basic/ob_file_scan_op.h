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

#include "sql/engine/expr/ob_expr.h"
#include "sql/engine/basic/ob_file_reader.h"
#include "sql/engine/basic/ob_file_table_def.h"
#include "query/engine/ob_operator.h"

#include <memory>
#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{

class ObFileScanSpec : public ObOpSpec
{
  OB_UNIS_VERSION_V(1);
public:
  ObFileScanSpec(common::ObIAllocator &alloc, const ObPhyOperatorType type)
    : ObOpSpec(alloc, type),
      column_exprs_(alloc),
      column_index_count_(0),
      file_def_()
  {
    MEMSET(column_indexes_, 0, sizeof(column_indexes_));
  }

  INHERIT_TO_STRING_KV("op_spec", ObOpSpec, K(file_def_));

public:
  // The columns the operator produces, one per column of the file.
  common::ObFixedArray<ObExpr *, common::ObIAllocator> column_exprs_;
  // The position of every produced column in the schema of the file.  The
  // query may only use a part of the columns, so the operator needs to know
  // which value of the row belongs to which expression.  A plain array is
  // used because the count of the columns of a file is small.
  static const int64_t MAX_FILE_COLUMNS = 128;
  int64_t column_indexes_[MAX_FILE_COLUMNS];
  int64_t column_index_count_;
  // The definition of the file table function.
  ObFileTableDef file_def_;

private:
  DISALLOW_COPY_AND_ASSIGN(ObFileScanSpec);
};

class ObFileScanOp : public ObOperator
{
public:
  ObFileScanOp(ObExecContext &exec_ctx, const ObOpSpec &spec, ObOpInput *input);
  virtual ~ObFileScanOp();

  virtual int inner_open() override;
  virtual int inner_rescan() override;
  virtual int switch_iterator() override;
  virtual int inner_get_next_row() override;
  virtual int inner_close() override;
  virtual void destroy() override { ObOperator::destroy(); }

private:
  // Opens the reader of the file, or the next file of a glob.
  int open_reader();
  // Writes one row into the output expressions.
  int write_row(const std::vector<std::string> &values, const std::vector<bool> &nulls);
  // Writes one value into one output expression.
  int write_value(ObExpr *expr, const std::string &value, bool is_null);
  // Checks that the file did not change since the schema was inferred.
  int check_file_unchanged();

private:
  // The reader that produces the rows.
  std::unique_ptr<ObFileReader> reader_;
  // The files the `glob()` function found.
  std::vector<std::string> glob_files_;
  size_t glob_index_;
  // The row that is currently produced.
  std::vector<std::string> row_values_;
  std::vector<bool> row_nulls_;
  // The buffer the strings of the current row live in.  It is reset for every
  // row so the memory does not grow with the number of rows.
  common::ObArenaAllocator row_alloc_;
  bool is_open_;
};

}  // namespace sql
}  // namespace oceanbase
