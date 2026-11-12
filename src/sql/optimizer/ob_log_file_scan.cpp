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

#define USING_LOG_PREFIX SQL_OPT

#include "sql/optimizer/ob_log_file_scan.h"

#include "src/sql/optimizer/ob_join_order.h"
#include "sql/optimizer/ob_optimizer_util.h"
#include "sql/optimizer/ob_opt_est_cost.h"
#include "sql/resolver/expr/ob_raw_expr_util.h"

using namespace oceanbase::common;

namespace oceanbase
{
namespace sql
{

int ObLogFileScan::compute_equal_set()
{
  int ret = OB_SUCCESS;
  set_output_equal_sets(&empty_expr_sets_);
  return ret;
}

int ObLogFileScan::compute_table_set()
{
  int ret = OB_SUCCESS;
  set_table_set(&empty_table_set_);
  return ret;
}

int ObLogFileScan::est_cost()
{
  int ret = OB_SUCCESS;
  double card = 0.0;
  double op_cost = 0.0;
  double cost = 0.0;
  EstimateCostInfo param;
  param.need_parallel_ = get_parallel();
  if (OB_FAIL(do_re_est_cost(param, card, op_cost, cost))) {
  } else {
    set_card(card);
    set_op_cost(op_cost);
    set_cost(cost);
  }
  return ret;
}

int ObLogFileScan::do_re_est_cost(EstimateCostInfo &param, double &card, double &op_cost, double &cost)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(get_plan()) || OB_ISNULL(file_def_)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    ObOptimizerContext &opt_ctx = get_plan()->get_optimizer_context();
    // The row count of the file is only an estimate, it is taken from the
    // size of the file.
    double read_rows = static_cast<double>(file_def_->row_count_estimate);
    card = read_rows;
    if (param.need_row_count_ >= 0 && param.need_row_count_ < card) {
      read_rows = read_rows * param.need_row_count_ / card;
      card = param.need_row_count_;
    }
    op_cost = ObOptEstCost::cost_values_table(read_rows, filter_exprs_, opt_ctx);
    cost = op_cost;
  }
  return ret;
}

int ObLogFileScan::get_op_exprs(ObIArray<ObRawExpr*> &all_exprs)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObLogicalOperator::get_op_exprs(all_exprs))) {
  } else if (OB_FAIL(append(all_exprs, column_exprs_))) {
  } else { /* do nothing */ }
  return ret;
}

int ObLogFileScan::allocate_expr_post(ObAllocExprContext &ctx)
{
  int ret = OB_SUCCESS;
  for (int64_t i = 0; OB_SUCC(ret) && i < column_exprs_.count(); ++i) {
    ObRawExpr *value_col = column_exprs_.at(i);
    if (OB_FAIL(mark_expr_produced(value_col, branch_id_, id_, ctx))) {
    } else if (!is_plan_root() && OB_FAIL(output_exprs_.push_back(value_col))) {
    } else { /* do nothing */ }
  }
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(ObLogicalOperator::allocate_expr_post(ctx))) {
  } else if (get_output_exprs().empty() && OB_FAIL(allocate_dummy_output())) {
  } else { /* do nothing */ }
  return ret;
}

int ObLogFileScan::get_plan_item_info(PlanText &plan_text, ObSqlPlanItem &plan_item)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObLogicalOperator::get_plan_item_info(plan_text, plan_item))) {
  } else {
    if (OB_SUCC(ret)) {
      const ObString &name = get_table_name();
      BUF_PRINT_OB_STR(name.ptr(),
                       name.length(),
                       plan_item.object_alias_,
                       plan_item.object_alias_len_);
      BUF_PRINT_STR("FILE SCAN", plan_item.object_type_, plan_item.object_type_len_);
    }
  }
  return ret;
}

int ObLogFileScan::is_my_fixed_expr(const ObRawExpr *expr, bool &is_fixed)
{
  is_fixed = ObOptimizerUtil::find_item(column_exprs_, expr);
  return OB_SUCCESS;
}
int ObLogFileScan::inner_replace_op_exprs(ObRawExprReplacer &replacer)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(replace_exprs_action(replacer, column_exprs_))) {
  }
  return ret;
}

int ObLogFileScan::allocate_dummy_output()
{
  int ret = OB_SUCCESS;
  ObConstRawExpr *dummy_expr = NULL;
  if (OB_ISNULL(get_plan())) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ObRawExprUtils::build_const_int_expr(
                                             get_plan()->get_optimizer_context().get_expr_factory(),
                                             ObIntType,
                                             1,
                                             dummy_expr))) {
  } else if (OB_ISNULL(dummy_expr)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(dummy_expr->extract_info())) {
  } else if (OB_FAIL(output_exprs_.push_back(dummy_expr))) {
  } else if (OB_FAIL(get_plan()->get_optimizer_context().get_all_exprs().append(dummy_expr))) {
  } else { /* do nothing */ }
  return ret;
}

} // namespace sql
} // namespace oceanbase
