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

#include "sql/optimizer/ob_logical_operator.h"
#include "sql/engine/basic/ob_file_table_def.h"

namespace oceanbase
{
namespace sql
{

// The logical operator of a file table function.  It produces the columns of
// the file, the definition of the file is kept in `file_def_`.
class ObLogFileScan : public ObLogicalOperator
{
public:
  ObLogFileScan(ObLogPlan &plan)
    : ObLogicalOperator(plan),
      table_id_(OB_INVALID_ID),
      table_name_(),
      column_exprs_(),
      file_def_(nullptr)
  {}

  virtual ~ObLogFileScan() {}

  virtual int compute_equal_set() override;
  virtual int compute_table_set() override;
  virtual int est_cost() override;
  virtual int do_re_est_cost(EstimateCostInfo &param, double &card, double &op_cost, double &cost) override;
  virtual int get_op_exprs(ObIArray<ObRawExpr*> &all_exprs) override;
  virtual int allocate_expr_post(ObAllocExprContext &ctx) override;
  virtual int get_plan_item_info(PlanText &plan_text, ObSqlPlanItem &plan_item) override;
  virtual int is_my_fixed_expr(const ObRawExpr *expr, bool &is_fixed) override;
  virtual int inner_replace_op_exprs(ObRawExprReplacer &replacer) override;
  int allocate_dummy_output();

  void set_table_id(uint64_t table_id) { table_id_ = table_id; }
  uint64_t get_table_id() const { return table_id_; }
  inline common::ObString &get_table_name() { return table_name_; }
  inline const common::ObString &get_table_name() const { return table_name_; }
  inline void set_table_name(const common::ObString &table_name) { table_name_ = table_name; }
  inline ObFileTableDef *get_file_def() { return file_def_; }
  inline void set_file_def(ObFileTableDef *file_def) { file_def_ = file_def; }
  common::ObIArray<ObRawExpr *> &get_column_exprs() { return column_exprs_; }
  const common::ObIArray<ObRawExpr *> &get_column_exprs() const { return column_exprs_; }
  // The position of every produced column in the schema of the file.  The
  // query may only use a part of the columns, so the operator needs to know
  // which value of the row belongs to which expression.
  common::ObIArray<int64_t> &get_schema_indexes() { return schema_indexes_; }
  const common::ObIArray<int64_t> &get_schema_indexes() const { return schema_indexes_; }
  int set_column_exprs(const common::ObIArray<ObColumnRefRawExpr *> &column_exprs)
  {
    int ret = OB_SUCCESS;
    column_exprs_.reset();
    for (int64_t i = 0; OB_SUCC(ret) && i < column_exprs.count(); ++i) {
      if (OB_FAIL(column_exprs_.push_back(column_exprs.at(i)))) {
      }
    }
    return ret;
  }

private:
  uint64_t table_id_;
  common::ObString table_name_;
  common::ObSEArray<ObRawExpr *, 4, common::ModulePageAllocator, true> column_exprs_;
  common::ObSEArray<int64_t, 4, common::ModulePageAllocator, true> schema_indexes_;
  ObFileTableDef *file_def_;

  DISALLOW_COPY_AND_ASSIGN(ObLogFileScan);
};

} // namespace sql
} // namespace oceanbase
