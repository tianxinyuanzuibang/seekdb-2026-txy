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

#include <cstring>
#include <string>
#include <vector>

namespace oceanbase
{
namespace share
{
namespace schema
{

// One AI column declared by `col <type> AI COLUMN (<expr>) WITH (...)`.
//
// The rewrite stage normalizes the declaration into a plain column plus three
// hidden helper columns.  The generation rule itself is carried inside the
// table schema: it is encoded into the comment of the `__ai_spec_<col>` helper
// column, because the column comment survives schema serialization.  That
// matters because the DDL that finally writes __all_ai_column runs in the
// root server process while the rewrite happens in the observer process, so a
// plain in-process registry could not hand the rule over.
//
// Encoding format (all fields separated by \x1f, comment text escaped):
//   v1 \x1f <ai_func_type> \x1f <max_retries> \x1f <sync_mode>
//      \x1f <is_json> \x1f <model_name> \x1f <normalized_expr>
struct ObAiColumnSpec
{
  std::string table_name_;
  std::string column_name_;
  std::string model_name_;
  std::string normalized_expr_;   // ready-to-run SQL text of the AI function
  std::string sync_mode_;
  std::string dep_columns_;       // comma separated source column names
  int64_t ai_func_type_;          // 1 = completion, 2 = dense embedding
  int64_t max_retries_;
  bool is_json_;

  // Validation outcome, computed while the declaration is rewritten and
  // checked later in the execution stage.
  //
  // The check cannot run in the rewrite stage: the error packet of a DDL is
  // sent from the thread that executes the statement, while the warning buffer
  // that carries the user message is thread local.  A message written during
  // parsing would therefore be invisible to the client.
  int64_t declared_dim_;          // dimension declared by the column type
  int64_t actual_dim_;            // dimension requested by the AI function
  bool dim_mismatch_;             // declared_dim_ != actual_dim_
  std::string ai_dep_column_;     // AI column this column depends on, if any

  ObAiColumnSpec()
    : ai_func_type_(1), max_retries_(0), is_json_(false),
      declared_dim_(0), actual_dim_(0), dim_mismatch_(false) {}

  static const char *STATE_PREFIX() { return "__ai_state_"; }
  static const char *REQUEST_PREFIX() { return "__ai_req_"; }
  static const char *SPEC_PREFIX() { return "__ai_spec_"; }

  static std::string state_column_name(const std::string &column_name)
  {
    return std::string(STATE_PREFIX()) + column_name;
  }

  static std::string request_column_name(const std::string &column_name)
  {
    return std::string(REQUEST_PREFIX()) + column_name;
  }

  static std::string spec_column_name(const std::string &column_name)
  {
    return std::string(SPEC_PREFIX()) + column_name;
  }

  // Encodes the rule so that it can travel inside the column comment.
  // The payload is hex encoded, so no quoting or control character can break
  // the DDL statement that carries it.
  std::string encode() const
  {
    std::string raw;
    raw.append(std::to_string(ai_func_type_));
    raw.push_back('\x1f');
    raw.append(std::to_string(max_retries_));
    raw.push_back('\x1f');
    raw.append(sync_mode_);
    raw.push_back('\x1f');
    raw.append(is_json_ ? "1" : "0");
    raw.push_back('\x1f');
    raw.append(model_name_);
    raw.push_back('\x1f');
    raw.append(normalized_expr_);
    raw.push_back('\x1f');
    raw.append(dep_columns_);
    raw.push_back('\x1f');
    raw.append(std::to_string(declared_dim_));
    raw.push_back('\x1f');
    raw.append(std::to_string(actual_dim_));
    raw.push_back('\x1f');
    raw.append(dim_mismatch_ ? "1" : "0");
    raw.push_back('\x1f');
    raw.append(ai_dep_column_);
    static const char *HEX = "0123456789abcdef";
    std::string out = "v1:";
    out.reserve(3 + raw.size() * 2);
    for (size_t i = 0; i < raw.size(); ++i) {
      const unsigned char c = static_cast<unsigned char>(raw[i]);
      out.push_back(HEX[c >> 4]);
      out.push_back(HEX[c & 0x0f]);
    }
    return out;
  }

  static int hex_value(char c)
  {
    if (c >= '0' && c <= '9') {
      return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
      return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
      return c - 'A' + 10;
    }
    return -1;
  }

  // Reverse of encode().  Returns false when the text is not a valid rule.
  static bool decode(const std::string &text, ObAiColumnSpec &spec)
  {
    bool ok = false;
    if (text.size() > 3 && 0 == text.compare(0, 3, "v1:")) {
      std::string raw;
      const size_t body = text.size() - 3;
      if (0 == body % 2) {
        bool bad = false;
        raw.reserve(body / 2);
        for (size_t i = 3; i + 1 < text.size() && !bad; i += 2) {
          const int hi = hex_value(text[i]);
          const int lo = hex_value(text[i + 1]);
          if (hi < 0 || lo < 0) {
            bad = true;
          } else {
            raw.push_back(static_cast<char>((hi << 4) | lo));
          }
        }
        if (!bad) {
          std::vector<std::string> parts;
          std::string field;
          for (size_t i = 0; i < raw.size(); ++i) {
            if (raw[i] == '\x1f') {
              parts.push_back(field);
              field.clear();
            } else {
              field.push_back(raw[i]);
            }
          }
          parts.push_back(field);
          if (parts.size() >= 7) {
            spec.ai_func_type_ = atol(parts[0].c_str());
            spec.max_retries_ = atol(parts[1].c_str());
            spec.sync_mode_ = parts[2];
            spec.is_json_ = ("1" == parts[3]);
            spec.model_name_ = parts[4];
            spec.normalized_expr_ = parts[5];
            spec.dep_columns_ = parts[6];
            if (parts.size() >= 11) {
              spec.declared_dim_ = atol(parts[7].c_str());
              spec.actual_dim_ = atol(parts[8].c_str());
              spec.dim_mismatch_ = ("1" == parts[9]);
              spec.ai_dep_column_ = parts[10];
            }
            ok = true;
          }
        }
      }
    }
    return ok;
  }
};

// Marker placed in the column comment of a helper column so that the DDL
// writer can recognize the column without relying on the name alone.
inline bool is_ai_helper_column(const std::string &column_name)
{
  return 0 == column_name.compare(0, strlen(ObAiColumnSpec::SPEC_PREFIX()),
                                  ObAiColumnSpec::SPEC_PREFIX())
         || 0 == column_name.compare(0, strlen(ObAiColumnSpec::STATE_PREFIX()),
                                     ObAiColumnSpec::STATE_PREFIX())
         || 0 == column_name.compare(0, strlen(ObAiColumnSpec::REQUEST_PREFIX()),
                                     ObAiColumnSpec::REQUEST_PREFIX());
}

} // namespace schema
} // namespace share
} // namespace oceanbase
