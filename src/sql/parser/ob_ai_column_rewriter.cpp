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

#define USING_LOG_PREFIX SQL_PARSER

#include "ob_ai_column_rewriter.h"

#include <ctype.h>
#include <string>
#include <vector>

#include "lib/ob_errno.h"
#include "share/ob_errno.h"
#include "lib/oblog/ob_log_module.h"
#include "share/schema/ob_ai_column_spec.h"

using oceanbase::share::schema::ObAiColumnSpec;

namespace oceanbase
{
namespace sql
{
namespace
{

static const char *JSON_SYSTEM_PROMPT =
    "Return exactly one valid JSON value, then stop.";

// System instruction for a column that holds a plain number.  The model is
// asked for a single integer so that the value can be stored in the column
// without a conversion step.
static const char *INTEGER_SYSTEM_PROMPT =
    "Return exactly one valid base-10 integer, then stop.";

// System instruction for a column that holds free text.
static const char *TEXT_SYSTEM_PROMPT =
    "Return exactly one valid text value, then stop.";


// Escapes a string so that it can be embedded in a single quoted SQL literal.
std::string escape_sql_string(const std::string &in)
{
  std::string out;
  out.reserve(in.size());
  for (size_t i = 0; i < in.size(); ++i) {
    char c = in[i];
    if (c == '\'' || c == '\\') {
      out.push_back('\\');
      out.push_back(c);
    } else if (c == '\n') {
      out.append("\\n");
    } else if (c == '\r') {
      out.append("\\r");
    } else {
      out.push_back(c);
    }
  }
  return out;
}

std::string lower_ascii(const std::string &in)
{
  std::string out = in;
  for (size_t i = 0; i < out.size(); ++i) {
    out[i] = static_cast<char>(tolower(static_cast<unsigned char>(out[i])));
  }
  return out;
}

std::string trim(const std::string &in)
{
  size_t b = 0;
  size_t e = in.size();
  while (b < e && isspace(static_cast<unsigned char>(in[b]))) {
    ++b;
  }
  while (e > b && isspace(static_cast<unsigned char>(in[e - 1]))) {
    --e;
  }
  return in.substr(b, e - b);
}

bool is_word_char(char c)
{
  return isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '$';
}

// Finds `needle` (already lower case) outside string literals and backticks.
size_t find_word(const std::string &low, const std::string &needle, size_t from = 0)
{
  size_t pos = from;
  while (pos < low.size()) {
    pos = low.find(needle, pos);
    if (pos == std::string::npos) {
      return std::string::npos;
    }
    bool left_ok = (pos == 0) || !is_word_char(low[pos - 1]);
    size_t end = pos + needle.size();
    bool right_ok = (end >= low.size()) || !is_word_char(low[end]);
    if (left_ok && right_ok) {
      return pos;
    }
    pos = end;
  }
  return std::string::npos;
}

// Index of the ')' that matches the '(' at `open`, ignoring literals.
size_t match_paren(const std::string &s, size_t open)
{
  if (open >= s.size() || s[open] != '(') {
    return std::string::npos;
  }
  int depth = 0;
  char quote = 0;
  for (size_t i = open; i < s.size(); ++i) {
    char c = s[i];
    if (quote != 0) {
      if (c == '\\') {
        ++i;
      } else if (c == quote) {
        quote = 0;
      }
      continue;
    }
    if (c == '\'' || c == '"' || c == '`') {
      quote = c;
    } else if (c == '(') {
      ++depth;
    } else if (c == ')') {
      --depth;
      if (depth == 0) {
        return i;
      }
    }
  }
  return std::string::npos;
}

std::vector<std::string> split_top_level(const std::string &body)
{
  std::vector<std::string> out;
  int depth = 0;
  char quote = 0;
  size_t start = 0;
  for (size_t i = 0; i < body.size(); ++i) {
    char c = body[i];
    if (quote != 0) {
      if (c == '\\') {
        ++i;
      } else if (c == quote) {
        quote = 0;
      }
      continue;
    }
    if (c == '\'' || c == '"' || c == '`') {
      quote = c;
    } else if (c == '(') {
      ++depth;
    } else if (c == ')') {
      --depth;
    } else if (c == ',' && depth == 0) {
      out.push_back(body.substr(start, i - start));
      start = i + 1;
    }
  }
  out.push_back(body.substr(start));
  return out;
}

// True when the type is an integral type, so that the model is asked for an
// integer instead of free text.
bool is_integer_type(const std::string &type_text)
{
  std::string low = lower_ascii(type_text);
  // Strip a possible length or precision, e.g. "bigint(20)".
  size_t lp = low.find('(');
  if (lp != std::string::npos) {
    low = trim(low.substr(0, lp));
  }
  size_t sp = low.find(' ');
  if (sp != std::string::npos) {
    low = trim(low.substr(0, sp));
  }
  return low == "tinyint" || low == "smallint" || low == "mediumint"
         || low == "int" || low == "integer" || low == "bigint";
}

// The argument of the AI function that carries the input.  For
// AI_COMPLETE(model, expr) and AI_EMBED(model, expr, dim) it is the second
// argument.
std::string prompt_argument(const std::string &expr)
{
  std::string low = lower_ascii(expr);
  size_t p = find_word(low, "ai_complete");
  if (p == std::string::npos) {
    p = find_word(low, "ai_embed");
  }
  if (p == std::string::npos) {
    return std::string();
  }
  size_t lp = expr.find('(', p);
  size_t rp = (lp == std::string::npos) ? std::string::npos : match_paren(expr, lp);
  if (lp == std::string::npos || rp == std::string::npos) {
    return std::string();
  }
  std::vector<std::string> args = split_top_level(expr.substr(lp + 1, rp - lp - 1));
  if (args.size() < 2) {
    return std::string();
  }
  return trim(args.at(1));
}

// Column names referenced by an expression.  Only plain identifiers are
// collected: a name that is followed by an opening parenthesis is a function
// call, and text inside string literals is never a column reference.
std::vector<std::string> referenced_columns(const std::string &expr,
                                            const std::vector<std::string> &known_columns)
{
  std::vector<std::string> found;
  size_t i = 0;
  while (i < expr.size()) {
    const char c = expr[i];
    if (c == '\'' || c == '"' || c == '`') {
      const char quote = c;
      ++i;
      while (i < expr.size()) {
        if (expr[i] == '\\' && quote != '`') {
          i += 2;
          continue;
        }
        if (expr[i] == quote) {
          ++i;
          break;
        }
        ++i;
      }
      continue;
    }
    if (isalpha(static_cast<unsigned char>(c)) || c == '_') {
      const size_t start = i;
      while (i < expr.size()
             && (isalnum(static_cast<unsigned char>(expr[i])) || expr[i] == '_')) {
        ++i;
      }
      const std::string token = expr.substr(start, i - start);
      // Skip a function call: the name is followed by an opening parenthesis.
      size_t j = i;
      while (j < expr.size() && isspace(static_cast<unsigned char>(expr[j]))) {
        ++j;
      }
      if (j < expr.size() && expr[j] == '(') {
        continue;
      }
      const std::string low = lower_ascii(token);
      if (low == "and" || low == "or" || low == "not" || low == "null"
          || low == "true" || low == "false" || low == "case" || low == "when"
          || low == "then" || low == "else" || low == "end" || low == "is"
          || low == "in" || low == "like" || low == "between" || low == "distinct") {
        continue;
      }
      for (size_t k = 0; k < known_columns.size(); ++k) {
        if (known_columns.at(k) == token) {
          bool seen = false;
          for (size_t m = 0; m < found.size(); ++m) {
            if (found.at(m) == token) {
              seen = true;
              break;
            }
          }
          if (!seen) {
            found.push_back(token);
          }
          break;
        }
      }
      continue;
    }
    ++i;
  }
  return found;
}


std::string strip_quotes(const std::string &in)
{
  std::string s = trim(in);
  if (s.size() >= 2 && (s[0] == '`' || s[0] == '"')) {
    s = s.substr(1, s.size() - 2);
  }
  return s;
}

// First token of a column definition, i.e. the column name.
std::string first_token(const std::string &in)
{
  std::string s = trim(in);
  size_t i = 0;
  if (i < s.size() && (s[i] == '`' || s[i] == '"')) {
    char q = s[i];
    size_t e = s.find(q, i + 1);
    return e == std::string::npos ? s.substr(i + 1) : s.substr(i + 1, e - i - 1);
  }
  while (i < s.size() && !isspace(static_cast<unsigned char>(s[i])) && s[i] != '(') {
    ++i;
  }
  return s.substr(0, i);
}

// vector(4) -> 4
bool parse_vector_dim(const std::string &type_text, int64_t &dim)
{
  bool found = false;
  std::string low = lower_ascii(type_text);
  size_t p = low.find("vector");
  if (p != std::string::npos) {
    size_t lp = low.find('(', p);
    size_t rp = (lp == std::string::npos) ? std::string::npos : low.find(')', lp);
    if (lp != std::string::npos && rp != std::string::npos) {
      std::string num = trim(low.substr(lp + 1, rp - lp - 1));
      if (!num.empty() && num.find_first_not_of("0123456789") == std::string::npos) {
        dim = atol(num.c_str());
        found = true;
      }
    }
  }
  return found;
}

// Third argument of AI_EMBED(model, input, dim), when it is a bare integer.
bool parse_embed_dim(const std::string &expr, int64_t &dim)
{
  bool found = false;
  std::string low = lower_ascii(expr);
  size_t p = find_word(low, "ai_embed");
  if (p != std::string::npos) {
    size_t lp = expr.find('(', p);
    size_t rp = (lp == std::string::npos) ? std::string::npos : match_paren(expr, lp);
    if (lp != std::string::npos && rp != std::string::npos) {
      std::vector<std::string> args = split_top_level(expr.substr(lp + 1, rp - lp - 1));
      if (args.size() >= 3) {
        std::string num = trim(args.at(2));
        if (!num.empty() && num.find_first_not_of("0123456789") == std::string::npos) {
          dim = atol(num.c_str());
          found = true;
        }
      }
    }
  }
  return found;
}

std::string first_string_literal(const std::string &in)
{
  for (size_t i = 0; i + 1 < in.size(); ++i) {
    if (in[i] == '\'') {
      size_t e = in.find('\'', i + 1);
      if (e != std::string::npos) {
        return in.substr(i + 1, e - i - 1);
      }
    }
  }
  return std::string();
}

// WITH (sync_mode='async', max_retries=2)
void parse_options(const std::string &text, int64_t &max_retries, std::string &sync_mode)
{
  std::string low = lower_ascii(text);
  size_t p = low.find("with");
  if (p == std::string::npos) {
    return;
  }
  size_t lp = text.find('(', p);
  size_t rp = (lp == std::string::npos) ? std::string::npos : match_paren(text, lp);
  if (lp == std::string::npos || rp == std::string::npos) {
    return;
  }
  std::vector<std::string> items = split_top_level(text.substr(lp + 1, rp - lp - 1));
  for (size_t i = 0; i < items.size(); ++i) {
    std::string item = trim(items.at(i));
    size_t eq = item.find('=');
    if (eq == std::string::npos) {
      continue;
    }
    std::string key = lower_ascii(trim(item.substr(0, eq)));
    std::string val = trim(item.substr(eq + 1));
    if (key == "max_retries") {
      max_retries = atol(val.c_str());
    } else if (key == "sync_mode") {
      sync_mode = strip_quotes(val);
    }
  }
}

std::string table_name_of(const std::string &sql, size_t after)
{
  std::string rest = trim(sql.substr(after));
  size_t lp = rest.find('(');
  std::string name = trim(rest.substr(0, lp));
  std::string low = lower_ascii(name);
  if (0 == low.compare(0, 7, "if not ")) {
    name = trim(name.substr(7));
    low = lower_ascii(name);
  }
  if (0 == low.compare(0, 7, "exists ")) {
    name = trim(name.substr(7));
  }
  size_t dot = name.rfind('.');
  if (dot != std::string::npos) {
    name = name.substr(dot + 1);
  }
  return strip_quotes(name);
}

int rewrite_create_table(const std::string &sql, std::string &out, bool &rewritten)
{
  int ret = OB_SUCCESS;
  rewritten = false;
  std::string low = lower_ascii(sql);
  size_t head = low.find("create table");
  if (head == std::string::npos) {
    return ret;
  }
  size_t lp = sql.find('(', head);
  size_t rp = (lp == std::string::npos) ? std::string::npos : match_paren(sql, lp);
  if (lp == std::string::npos || rp == std::string::npos) {
    return ret;
  }
  std::string table_name = table_name_of(sql, head + 12);
  std::string body = sql.substr(lp + 1, rp - lp - 1);
  std::vector<std::string> elements = split_top_level(body);
  std::vector<std::string> ai_names;
  bool changed = false;

  // Every column of the table, so that a reference in an AI expression can be
  // resolved to a column of this table.
  std::vector<std::string> all_columns;
  for (size_t i = 0; i < elements.size(); ++i) {
    std::string element_low = lower_ascii(elements.at(i));
    if (element_low.find("primary key") != std::string::npos
        || element_low.find("unique") != std::string::npos
        || element_low.find("key ") != std::string::npos) {
      continue;
    }
    const std::string name = first_token(elements.at(i));
    if (!name.empty()) {
      all_columns.push_back(name);
    }
  }

  for (size_t i = 0; i < elements.size() && OB_SUCC(ret); ++i) {
    std::string &element = elements.at(i);
    std::string element_low = lower_ascii(element);
    size_t ac = find_word(element_low, "ai column");
    if (ac == std::string::npos) {
      continue;
    }
    std::string prefix = trim(element.substr(0, ac));
    std::string rest = element.substr(ac + 9);
    size_t expr_lp = rest.find('(');
    size_t expr_rp = (expr_lp == std::string::npos) ? std::string::npos : match_paren(rest, expr_lp);
    if (expr_lp == std::string::npos || expr_rp == std::string::npos) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("ai column expression is not closed", K(ret));
      break;
    }
    std::string expr = trim(rest.substr(expr_lp + 1, expr_rp - expr_lp - 1));
    std::string trailer = rest.substr(expr_rp + 1);
    std::string column_name = first_token(prefix);
    std::string type_text = trim(prefix.substr(prefix.find(column_name) + column_name.size()));
    std::string low_type = lower_ascii(type_text);
    bool is_json = low_type.find("json") != std::string::npos;
    bool is_embed = lower_ascii(expr).find("ai_embed") != std::string::npos;

    // A generated column may not read another generated column, and the same
    // rule applies here: an AI column can only depend on ordinary columns.
    //
    // The violation is only recorded here.  Raising it in this stage would
    // lose the message, because the warning buffer that carries the user
    // message is thread local while the error packet of a DDL is sent by the
    // thread that executes the statement.
    std::string ai_dep_column;
    for (size_t j = 0; j < ai_names.size(); ++j) {
      if (find_word(lower_ascii(expr), lower_ascii(ai_names.at(j))) != std::string::npos) {
        ai_dep_column = ai_names.at(j);
        break;
      }
    }

    int64_t declared_dim = 0;
    int64_t embed_dim = 0;
    bool dim_mismatch = false;
    if (parse_vector_dim(type_text, declared_dim) && parse_embed_dim(expr, embed_dim)
        && declared_dim != embed_dim) {
      dim_mismatch = true;
    }

    ObAiColumnSpec spec;
    spec.table_name_ = table_name;
    spec.column_name_ = column_name;
    spec.model_name_ = first_string_literal(expr);
    spec.is_json_ = is_json;
    spec.ai_func_type_ = is_embed ? 2 : 1;
    spec.sync_mode_ = "async";
    int64_t max_retries = 0;
    parse_options(trailer, max_retries, spec.sync_mode_);
    spec.max_retries_ = max_retries;
    spec.declared_dim_ = declared_dim;
    spec.actual_dim_ = embed_dim;
    spec.dim_mismatch_ = dim_mismatch;
    spec.ai_dep_column_ = ai_dep_column;
    spec.column_type_ = low_type;
    spec.prompt_expr_ = prompt_argument(expr);
    {
      // The system instruction follows the type of the target column: a JSON
      // column has to receive a JSON document, an integral column a single
      // integer, and anything else free text.
      if (is_json) {
        spec.system_prompt_ = JSON_SYSTEM_PROMPT;
      } else if (is_integer_type(type_text)) {
        spec.system_prompt_ = INTEGER_SYSTEM_PROMPT;
      } else {
        spec.system_prompt_ = TEXT_SYSTEM_PROMPT;
      }
      std::vector<std::string> deps = referenced_columns(expr, all_columns);
      // An AI column never depends on itself, and it may not depend on
      // another AI column; that case is reported by the execution stage.
      std::string dep_text;
      for (size_t k = 0; k < deps.size(); ++k) {
        if (deps.at(k) == column_name) {
          continue;
        }
        bool is_ai = false;
        for (size_t m = 0; m < ai_names.size(); ++m) {
          if (deps.at(k) == ai_names.at(m)) {
            is_ai = true;
            break;
          }
        }
        if (is_ai) {
          continue;
        }
        if (!dep_text.empty()) {
          dep_text += ",";
        }
        dep_text += deps.at(k);
      }
      spec.dep_columns_ = dep_text;
    }
    if (is_json) {
      spec.normalized_expr_ = std::string(is_embed ? "AI_EMBED(" : "AI_COMPLETE(")
                              + expr + ", '" + JSON_SYSTEM_PROMPT + "')";
    } else {
      spec.normalized_expr_ = std::string(is_embed ? "AI_EMBED(" : "AI_COMPLETE(") + expr + ")";
    }
    ai_names.push_back(column_name);

    std::string state_col = ObAiColumnSpec::state_column_name(column_name);
    std::string request_col = ObAiColumnSpec::request_column_name(column_name);
    std::string spec_col = ObAiColumnSpec::spec_column_name(column_name);
    // The generation rule travels inside the column comment, because the
    // comment is part of the serialized table schema and the DDL writer lives
    // in another process.
    std::string spec_text = spec.encode();
    element = prefix
        + ", `" + state_col + "` varchar(64) default null"
        + ", `" + request_col + "` varchar(64) default null"
        + ", `" + spec_col + "` longtext default null comment '"
        + escape_sql_string(spec_text) + "'";
    changed = true;
  }

  if (OB_SUCC(ret) && changed) {
    std::string rebuilt;
    for (size_t i = 0; i < elements.size(); ++i) {
      if (i > 0) {
        rebuilt += ",";
      }
      rebuilt += elements.at(i);
    }
    out = sql.substr(0, lp + 1) + rebuilt + sql.substr(rp);
    rewritten = true;
  }
  return ret;
}



// Removes the AI column option block `WITH (...)` from `trailer` and returns
// whatever else remains (for example a trailing algorithm clause).
std::string strip_with_options(const std::string &trailer)
{
  std::string out = trailer;
  std::string low = lower_ascii(out);
  size_t p = find_word(low, "with");
  if (p != std::string::npos) {
    size_t lp = out.find('(', p);
    size_t rp = (lp == std::string::npos) ? std::string::npos : match_paren(out, lp);
    if (lp != std::string::npos && rp != std::string::npos) {
      out = out.substr(0, p) + out.substr(rp + 1);
    }
  }
  return out;
}

int rewrite_alter_table(const std::string &sql, std::string &out, bool &rewritten)
{
  int ret = OB_SUCCESS;
  rewritten = false;
  std::string low = lower_ascii(sql);
  size_t at = low.find("alter table");
  if (at == std::string::npos) {
    return ret;
  }
  size_t ac = find_word(low, "add column");
  if (ac == std::string::npos) {
    return ret;
  }
  size_t def_start = ac + 10;
  size_t ai = find_word(low, "ai column", def_start);
  if (ai == std::string::npos) {
    return ret;
  }
  std::string table_name;
  {
    std::string between = trim(sql.substr(at + 11, ac - (at + 11)));
    size_t dot = between.rfind('.');
    if (dot != std::string::npos) {
      between = between.substr(dot + 1);
    }
    table_name = strip_quotes(between);
  }
  std::string prefix = trim(sql.substr(def_start, ai - def_start));
  std::string rest = sql.substr(ai + 9);
  size_t expr_lp = rest.find('(');
  size_t expr_rp = (expr_lp == std::string::npos) ? std::string::npos : match_paren(rest, expr_lp);
  if (expr_lp == std::string::npos || expr_rp == std::string::npos) {
    return ret;
  }
  std::string expr = trim(rest.substr(expr_lp + 1, expr_rp - expr_lp - 1));
  std::string trailer = rest.substr(expr_rp + 1);
  // The WITH(...) block is consumed into the spec, it must not stay in the
  // rewritten ALTER statement.
  const std::string trailing_sql = strip_with_options(trailer);
  std::string column_name = first_token(prefix);
  std::string type_text = trim(prefix.substr(prefix.find(column_name) + column_name.size()));
  bool is_json = lower_ascii(type_text).find("json") != std::string::npos;
  bool is_embed = lower_ascii(expr).find("ai_embed") != std::string::npos;

  int64_t declared_dim = 0;
  int64_t embed_dim = 0;
  bool dim_mismatch = false;
  if (parse_vector_dim(type_text, declared_dim) && parse_embed_dim(expr, embed_dim)
      && declared_dim != embed_dim) {
    dim_mismatch = true;
  }

  ObAiColumnSpec spec;
  spec.table_name_ = table_name;
  spec.column_name_ = column_name;
  spec.model_name_ = first_string_literal(expr);
  spec.is_json_ = is_json;
  spec.ai_func_type_ = is_embed ? 2 : 1;
  spec.sync_mode_ = "async";
  int64_t max_retries = 0;
  parse_options(trailer, max_retries, spec.sync_mode_);
  spec.max_retries_ = max_retries;
  spec.declared_dim_ = declared_dim;
  spec.actual_dim_ = embed_dim;
  spec.dim_mismatch_ = dim_mismatch;
  spec.column_type_ = lower_ascii(type_text);
  spec.prompt_expr_ = prompt_argument(expr);
  if (is_json) {
    spec.system_prompt_ = JSON_SYSTEM_PROMPT;
  } else if (is_integer_type(type_text)) {
    spec.system_prompt_ = INTEGER_SYSTEM_PROMPT;
  } else {
    spec.system_prompt_ = TEXT_SYSTEM_PROMPT;
  }
  if (is_json) {
    spec.normalized_expr_ = std::string(is_embed ? "AI_EMBED(" : "AI_COMPLETE(")
                            + expr + ", '" + JSON_SYSTEM_PROMPT + "')";
  } else {
    spec.normalized_expr_ = std::string(is_embed ? "AI_EMBED(" : "AI_COMPLETE(") + expr + ")";
  }
  std::string state_col = ObAiColumnSpec::state_column_name(column_name);
  std::string request_col = ObAiColumnSpec::request_column_name(column_name);
  std::string spec_col = ObAiColumnSpec::spec_column_name(column_name);
  std::string spec_text = spec.encode();
  out = sql.substr(0, def_start) + " " + prefix
      + ", add column `" + state_col + "` varchar(64) default null"
      + ", add column `" + request_col + "` varchar(64) default null"
      + ", add column `" + spec_col + "` longtext default null comment '"
      + escape_sql_string(spec_text) + "'"
      + trailing_sql;
  rewritten = true;
  return ret;
}

// ---------------------------------------------------------------------------
// ALTER TABLE ... RETRY AI COLUMN
//
// The command does not change the schema: it invalidates the current results
// of the selected rows and records a task for every one of them.  Both steps
// have to run in the execution stage, because only there can an error message
// reach the client (see the comment of the class).  The statement is therefore
// rewritten into a harmless ALTER that adds a marker column, and the request
// travels in the comment of that column.  The payload uses the same hex
// encoding as the rule of an AI column:
//
//   v1:<hex action \x1f errno \x1f db \x1f table \x1f column \x1f predicate
//          \x1f error message>
//
// The decoder in sql/engine/cmd/ob_table_executor.cpp has to be kept in sync
// with this layout.
static const char *AI_RETRY_MARKER_COLUMN = "__ai_retry_marker";
static const char *AI_RETRY_ACTION_RETRY = "retry";
static const char *AI_RETRY_ACTION_ERROR = "error";
// MAX_COLUMN_COMMENT_CHAR_LENGTH of the resolver: the comment of the marker
// column cannot be longer than this.
static const int64_t AI_RETRY_MAX_COMMENT_LENGTH = 1024;

size_t retry_skip_blank(const std::string &text, size_t pos)
{
  while (pos < text.size() && isspace(static_cast<unsigned char>(text[pos]))) {
    ++pos;
  }
  return pos;
}

// Reads `keyword` (already lower case) at `pos`; a word must end at the
// keyword, so "retry_x" does not match "retry".
bool retry_read_keyword(const std::string &text, size_t &pos, const char *keyword)
{
  bool found = false;
  const std::string wanted(keyword);
  const size_t begin = retry_skip_blank(text, pos);
  if (begin + wanted.size() <= text.size()
      && lower_ascii(text.substr(begin, wanted.size())) == wanted) {
    const size_t end = begin + wanted.size();
    if (end == text.size() || !is_word_char(text[end])) {
      pos = end;
      found = true;
    }
  }
  return found;
}

// Reads a plain or backtick quoted identifier.  `pos` ends right after it.
bool retry_read_identifier(const std::string &text, size_t &pos, std::string &name)
{
  bool found = false;
  const size_t begin = retry_skip_blank(text, pos);
  if (begin < text.size() && '`' == text[begin]) {
    std::string value;
    size_t i = begin + 1;
    bool closed = false;
    while (!closed && i < text.size()) {
      if ('`' == text[i]) {
        if (i + 1 < text.size() && '`' == text[i + 1]) {
          value.push_back('`');
          i += 2;
        } else {
          ++i;
          closed = true;
        }
      } else {
        value.push_back(text[i]);
        ++i;
      }
    }
    if (closed) {
      name = value;
      pos = i;
      found = true;
    }
  } else {
    size_t i = begin;
    while (i < text.size() && is_word_char(text[i])) {
      ++i;
    }
    if (i > begin) {
      name = text.substr(begin, i - begin);
      pos = i;
      found = true;
    }
  }
  return found;
}

// Index of the first comma that is not inside a string, an identifier or a
// pair of parentheses, or npos when there is none.
size_t retry_find_top_level_comma(const std::string &text)
{
  size_t result = std::string::npos;
  int depth = 0;
  char quote = 0;
  for (size_t i = 0; i < text.size() && std::string::npos == result; ++i) {
    const char c = text[i];
    if (0 != quote) {
      if ('\\' == c && '`' != quote) {
        ++i;
      } else if (c == quote) {
        if (i + 1 < text.size() && text[i + 1] == quote) {
          ++i;  // an escaped quote is written twice
        } else {
          quote = 0;
        }
      }
    } else if ('\'' == c || '"' == c || '`' == c) {
      quote = c;
    } else if ('(' == c) {
      ++depth;
    } else if (')' == c) {
      if (depth > 0) {
        --depth;
      }
    } else if (',' == c && 0 == depth) {
      result = i;
    }
  }
  return result;
}

std::string retry_hex_encode(const std::string &raw)
{
  static const char *HEX = "0123456789abcdef";
  std::string out;
  out.reserve(raw.size() * 2);
  for (size_t i = 0; i < raw.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(raw[i]);
    out.push_back(HEX[c >> 4]);
    out.push_back(HEX[c & 0x0f]);
  }
  return out;
}

std::string retry_make_payload(const std::string &action,
                               int64_t errcode,
                               const std::string &db,
                               const std::string &table,
                               const std::string &column,
                               const std::string &predicate,
                               const std::string &message)
{
  std::string raw;
  raw.append(action);
  raw.push_back('\x1f');
  raw.append(std::to_string(errcode));
  raw.push_back('\x1f');
  raw.append(db);
  raw.push_back('\x1f');
  raw.append(table);
  raw.push_back('\x1f');
  raw.append(column);
  raw.push_back('\x1f');
  raw.append(predicate);
  raw.push_back('\x1f');
  raw.append(message);
  return std::string("v1:") + retry_hex_encode(raw);
}

// Recognizes `ALTER TABLE [db.]table RETRY AI COLUMN column [WHERE predicate]`
// and rewrites it into the marker ALTER described above.  Everything that is
// not exactly this command is left untouched, so ordinary statements are
// parsed as before.
int rewrite_retry_ai_column(const std::string &sql, std::string &out, bool &rewritten)
{
  int ret = OB_SUCCESS;
  rewritten = false;
  size_t pos = 0;
  if (!retry_read_keyword(sql, pos, "alter") || !retry_read_keyword(sql, pos, "table")) {
    // Not an ALTER TABLE statement.
  } else {
    std::string db_name;
    std::string table_name;
    size_t target_end = 0;
    bool target_ok = false;
    {
      std::string first_name;
      if (retry_read_identifier(sql, pos, first_name)) {
        const size_t after_name = retry_skip_blank(sql, pos);
        if (after_name < sql.size() && '.' == sql[after_name]) {
          size_t second_pos = retry_skip_blank(sql, after_name + 1);
          if (retry_read_identifier(sql, second_pos, table_name)) {
            db_name = first_name;
            target_end = second_pos;
            target_ok = true;
          }
        } else {
          table_name = first_name;
          target_end = pos;
          target_ok = true;
        }
      }
    }
    if (target_ok) {
      std::string column_name;
      size_t after_column = target_end;
      if (retry_read_keyword(sql, after_column, "retry")
          && retry_read_keyword(sql, after_column, "ai")
          && retry_read_keyword(sql, after_column, "column")
          && retry_read_identifier(sql, after_column, column_name)) {
        std::string rest = trim(sql.substr(after_column));
        // A trailing statement separator is not part of the command.
        while (!rest.empty() && ';' == rest[rest.size() - 1]) {
          rest = trim(rest.substr(0, rest.size() - 1));
        }
        std::string predicate;
        std::string action(AI_RETRY_ACTION_RETRY);
        int64_t errcode = 0;
        std::string message;
        bool build = true;
        const size_t comma = retry_find_top_level_comma(rest);
        if (std::string::npos != comma) {
          // A second ALTER action would be silently ignored if the command
          // were executed as an ordinary ALTER, so it is rejected instead.
          action = AI_RETRY_ACTION_ERROR;
          errcode = OB_NOT_SUPPORTED;
          message = "combining AI COLUMN RETRY with another ALTER action not supported";
        } else if (!rest.empty()) {
          size_t where_pos = 0;
          if (retry_read_keyword(rest, where_pos, "where")) {
            predicate = trim(rest.substr(where_pos));
            // An empty predicate is a syntax error, which the parser reports.
            build = !predicate.empty();
          } else {
            // Nothing else is valid here; leave the statement to the parser.
            build = false;
          }
        }
        if (build) {
          std::string payload = retry_make_payload(action, errcode, db_name, table_name,
                                                   column_name, predicate, message);
          if (payload.size() > static_cast<size_t>(AI_RETRY_MAX_COMMENT_LENGTH)) {
            // The comment of the marker column cannot carry the request; fail
            // with a message instead of letting the resolver report a comment
            // that is too long.
            payload = retry_make_payload(AI_RETRY_ACTION_ERROR, OB_NOT_SUPPORTED, db_name,
                                         table_name, column_name, std::string(),
                                         "AI COLUMN RETRY statement is too long");
          }
          out = sql.substr(0, target_end)
              + " add column `" + std::string(AI_RETRY_MARKER_COLUMN)
              + "` longtext null comment '" + payload + "'";
          rewritten = true;
        }
      }
    }
  }
  return ret;
}

} // namespace

int ObAiColumnRewriter::maybe_rewrite(ObIAllocator &alloc,
                                      const common::ObString &stmt,
                                      common::ObString &out,
                                      bool &rewritten)
{
  int ret = OB_SUCCESS;
  rewritten = false;
  out.reset();
  if (stmt.empty()) {
  } else {
    std::string sql(stmt.ptr(), stmt.length());
    const std::string lower_sql = lower_ascii(sql);
    // An ordinary statement is left untouched without any cost.  The RETRY
    // command is looked for through the word "retry" too, because its words
    // may be separated by any white space.
    if (lower_sql.find("ai column") == std::string::npos
        && lower_sql.find("retry") == std::string::npos) {
      // fast path, ordinary statements are untouched
    } else {
      std::string rewritten_sql;
      bool changed = false;
      if (OB_FAIL(rewrite_retry_ai_column(sql, rewritten_sql, changed))) {
      } else if (!changed && OB_FAIL(rewrite_create_table(sql, rewritten_sql, changed))) {
      } else if (!changed && OB_FAIL(rewrite_alter_table(sql, rewritten_sql, changed))) {
      } else if (changed) {
        char *buffer = static_cast<char *>(alloc.alloc(rewritten_sql.size()));
        if (OB_ISNULL(buffer)) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
          LOG_WARN("allocate rewritten sql failed", K(ret));
        } else {
          MEMCPY(buffer, rewritten_sql.data(), rewritten_sql.size());
          out.assign_ptr(buffer, static_cast<int32_t>(rewritten_sql.size()));
          rewritten = true;
        }
      }
    }
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
