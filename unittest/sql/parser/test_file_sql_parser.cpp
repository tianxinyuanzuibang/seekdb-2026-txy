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

#include "unittest/lite_gtest.h"

#include <algorithm>
#include <vector>

#include "lib/allocator/ob_allocator.h"
#include "lib/allocator/page_arena.h"
#include "lib/ob_errno.h"
#include "sql/parser/parse_node.h"
#include "sql/parser/ob_sql_parser.h"
#include "query/ob_sql_define.h"
#include "objit/common/ob_item_type.h"

using namespace oceanbase;
using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace
{

ObArenaAllocator &parser_allocator()
{
  static ObArenaAllocator allocator;
  return allocator;
}

// Parses a statement into a fresh arena.  The result stays valid until the
// next call.
int parse(const std::string &sql, ParseResult &result)
{
  parser_allocator().reset();
  memset(&result, 0, sizeof(result));
  result.malloc_pool_ = &parser_allocator();
  ObSQLParser parser(parser_allocator(), SM_NONE);
  return parser.parse(sql.c_str(), sql.length(), result);
}

// Collects the arguments of a file table function.  The arguments are a link
// chain, the last link holds the list node with the first argument.
std::vector<const ParseNode *> file_table_args(const ParseNode *file_table)
{
  std::vector<const ParseNode *> args;
  if (nullptr == file_table) {
    return args;
  }
  const ParseNode *link = file_table->children_[0];
  while (nullptr != link && T_LINK_NODE == link->type_) {
    if (link->num_child_ >= 2 && nullptr != link->children_[1]) {
      args.push_back(link->children_[1]);
    }
    link = link->children_[0];
  }
  if (nullptr != link && T_EXPR_LIST == link->type_ && nullptr != link->children_[0]) {
    args.push_back(link->children_[0]);
  }
  std::reverse(args.begin(), args.end());
  return args;
}

const ParseNode *select_of(ParseResult &result)
{
  return (nullptr != result.result_tree_) ? result.result_tree_->children_[0] : nullptr;
}

const ParseNode *file_table_of(ParseResult &result)
{
  const ParseNode *root = select_of(result);
  if (nullptr == root) {
    return nullptr;
  }
  const ParseNode *from = root->children_[PARSE_SELECT_FROM];
  if (nullptr == from || T_FROM_LIST != from->type_ || from->num_child_ < 1) {
    return nullptr;
  }
  return from->children_[0];
}

}  // namespace

TEST(FileSqlParser, read_csv_statement_parses)
{
  ParseResult result;
  const std::string sql = "select id, name from read_csv('/tmp/a.csv') f order by id";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
  ASSERT_TRUE(nullptr != result.result_tree_);
  const ParseNode *root = select_of(result);
  ASSERT_EQ(T_SELECT, root->type_);
  const ParseNode *file_table = file_table_of(result);
  ASSERT_TRUE(nullptr != file_table);
  ASSERT_EQ(T_FILE_TABLE_EXPRESSION, file_table->type_);
  // The node holds the arguments, the alias and the name of the function.
  ASSERT_EQ(3, file_table->num_child_);
  const std::vector<const ParseNode *> args = file_table_args(file_table);
  ASSERT_EQ(1, args.size());
  ASSERT_TRUE(nullptr != args[0]);
  ASSERT_TRUE(nullptr != file_table->children_[1]);
  const ParseNode *func = file_table->children_[2];
  ASSERT_TRUE(nullptr != func);
  ASSERT_TRUE(nullptr != func->str_value_);
  ASSERT_TRUE(0 == strncmp("read_csv", func->str_value_, 8));
}

TEST(FileSqlParser, read_csv_named_arguments_parse)
{
  ParseResult result;
  const std::string sql =
      "select * from read_csv('/tmp/a.csv', header = false, sample_size = -1) f";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
  const ParseNode *file_table = file_table_of(result);
  ASSERT_TRUE(nullptr != file_table);
  const std::vector<const ParseNode *> args = file_table_args(file_table);
  ASSERT_EQ(3, args.size());
  // The first argument is the path.
  ASSERT_TRUE(T_FILE_TABLE_OPTION != args[0]->type_);
  // The other ones are named options.
  ASSERT_EQ(T_FILE_TABLE_OPTION, args[1]->type_);
  ASSERT_EQ(T_FILE_TABLE_OPTION, args[2]->type_);
}

TEST(FileSqlParser, read_csv_second_positional_format)
{
  ParseResult result;
  const std::string sql = "select * from read_csv('/tmp/a.csv', 'csv') f";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
  const ParseNode *file_table = file_table_of(result);
  ASSERT_TRUE(nullptr != file_table);
  const std::vector<const ParseNode *> args = file_table_args(file_table);
  ASSERT_EQ(2, args.size());
  ASSERT_TRUE(T_FILE_TABLE_OPTION != args[0]->type_);
  ASSERT_TRUE(T_FILE_TABLE_OPTION != args[1]->type_);
}

TEST(FileSqlParser, read_json_and_ndjson_parse)
{
  ParseResult result;
  ASSERT_EQ(OB_SUCCESS, parse(
      "select * from read_json('/tmp/a.jsonl', format = 'newline_delimited') f", result));
  ASSERT_EQ(OB_SUCCESS, parse(
      "select * from read_ndjson('/tmp/a.jsonl') f", result));
}

TEST(FileSqlParser, read_parquet_and_glob_parse)
{
  ParseResult result;
  ASSERT_EQ(OB_SUCCESS, parse(
      "select * from read_parquet('/tmp/a.parquet') p", result));
  ASSERT_EQ(OB_SUCCESS, parse(
      "select file from glob('/tmp/data/*.csv') g", result));
}

TEST(FileSqlParser, file_table_in_join_parses)
{
  ParseResult result;
  const std::string sql =
      "select * from read_csv('/tmp/a.csv') a join read_csv('/tmp/b.csv') b"
      " on a.id = b.id";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
  const ParseNode *root = select_of(result);
  ASSERT_TRUE(nullptr != root);
  const ParseNode *from = root->children_[PARSE_SELECT_FROM];
  ASSERT_TRUE(nullptr != from);
}

TEST(FileSqlParser, copy_to_statement_parses)
{
  ParseResult result;
  const std::string sql =
      "copy (select id, name from read_csv('/tmp/a.csv') order by id)"
      " to '/tmp/export.csv' (format csv, header, nullstr 'NULL')";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
  ASSERT_TRUE(nullptr != result.result_tree_);
  const ParseNode *root = select_of(result);
  ASSERT_EQ(T_SELECT, root->type_);
  const ParseNode *into = root->children_[PARSE_SELECT_INTO];
  ASSERT_TRUE(nullptr != into);
  ASSERT_EQ(T_COPY_TO, into->type_);
  ASSERT_EQ(3, into->num_child_);
  const ParseNode *options = into->children_[2];
  ASSERT_TRUE(nullptr != options);
  // The options are a link chain as well.  The chain ends in a link that
  // holds the first option.
  std::vector<const ParseNode *> option_list;
  const ParseNode *link = options;
  while (nullptr != link && T_LINK_NODE == link->type_) {
    // A link with one child holds the first option, a link with two children
    // points back at the previous link and holds the next option.
    if (link->num_child_ >= 2 && nullptr != link->children_[1]) {
      option_list.push_back(link->children_[1]);
    }
    link = link->children_[0];
  }
  if (nullptr != link) {
    option_list.push_back(link);
  }
  ASSERT_EQ(3, option_list.size());
  bool has_format = false;
  bool has_header = false;
  bool has_nullstr = false;
  for (size_t i = 0; i < option_list.size(); ++i) {
    const ParseNode *opt = option_list[i];
    if (T_COPY_FORMAT == opt->type_) {
      has_format = true;
    } else if (T_COPY_HEADER == opt->type_) {
      has_header = true;
    } else if (T_COPY_NULLSTR == opt->type_) {
      has_nullstr = true;
    }
  }
  ASSERT_TRUE(has_format);
  ASSERT_TRUE(has_header);
  ASSERT_TRUE(has_nullstr);
}

TEST(FileSqlParser, copy_to_without_options_parses)
{
  ParseResult result;
  const std::string sql = "copy (select 1 from dual) to '/tmp/out.csv'";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
  const ParseNode *root = select_of(result);
  ASSERT_EQ(T_SELECT, root->type_);
  const ParseNode *into = root->children_[PARSE_SELECT_INTO];
  ASSERT_EQ(T_COPY_TO, into->type_);
  ASSERT_EQ(3, into->num_child_);
  ASSERT_TRUE(nullptr == into->children_[2]);
}

TEST(FileSqlParser, copy_to_rejects_into_inside)
{
  ParseResult result;
  const std::string sql =
      "copy (select 1 from dual into outfile '/tmp/other.csv') to '/tmp/out.csv'";
  ASSERT_TRUE(OB_SUCCESS != parse(sql, result));
}

TEST(FileSqlParser, read_csv_in_subquery_parses)
{
  ParseResult result;
  const std::string sql =
      "select * from (select id from read_csv('/tmp/a.csv') f) t";
  ASSERT_EQ(OB_SUCCESS, parse(sql, result));
}

TEST(FileSqlParser, ordinary_select_still_parses)
{
  ParseResult result;
  ASSERT_EQ(OB_SUCCESS, parse("select 1 from dual", result));
  ASSERT_EQ(OB_SUCCESS, parse("select * from t1 where c1 > 3 order by c2 limit 10", result));
  ASSERT_EQ(OB_SUCCESS, parse("select count(*) from t1 group by c1 having count(*) > 2", result));
}
