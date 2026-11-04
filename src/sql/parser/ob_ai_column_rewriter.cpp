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
    if (lower_ascii(sql).find("ai column") == std::string::npos) {
      // fast path, ordinary statements are untouched
    } else {
      std::string rewritten_sql;
      bool changed = false;
      if (OB_FAIL(rewrite_create_table(sql, rewritten_sql, changed))) {
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
