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

#include "sql/engine/basic/ob_file_scan_utils.h"

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <string>

#include "common/timezone/ob_time_convert.h"
#include "share/ob_errno.h"

namespace oceanbase
{
namespace sql
{

int ObFileSchema::find_column(const std::string &name) const
{
  for (int64_t i = 0; i < static_cast<int64_t>(columns.size()); ++i) {
    if (columns[static_cast<size_t>(i)].name == name) {
      return static_cast<int>(i);
    }
  }
  return -1;
}

ObFileInferType merge_file_infer_type(ObFileInferType a, ObFileInferType b)
{
  if (OB_FILE_TYPE_NULL == a) {
    return b;
  }
  if (OB_FILE_TYPE_NULL == b) {
    return a;
  }
  if (a == b) {
    return a;
  }
  const bool a_num = (OB_FILE_TYPE_BIGINT == a || OB_FILE_TYPE_DOUBLE == a);
  const bool b_num = (OB_FILE_TYPE_BIGINT == b || OB_FILE_TYPE_DOUBLE == b);
  if (a_num && b_num) {
    // an integer that does not fit stays an integer, only a fractional value
    // widens the column to DOUBLE
    return OB_FILE_TYPE_DOUBLE;
  }
  if ((OB_FILE_TYPE_DATE == a && OB_FILE_TYPE_DATETIME == b) ||
      (OB_FILE_TYPE_DATETIME == a && OB_FILE_TYPE_DATE == b)) {
    return OB_FILE_TYPE_DATETIME;
  }
  if ((OB_FILE_TYPE_BOOLEAN == a && OB_FILE_TYPE_BIGINT == b) ||
      (OB_FILE_TYPE_BIGINT == a && OB_FILE_TYPE_BOOLEAN == b)) {
    return OB_FILE_TYPE_BIGINT;
  }
  if ((OB_FILE_TYPE_BOOLEAN == a && OB_FILE_TYPE_DOUBLE == b) ||
      (OB_FILE_TYPE_DOUBLE == a && OB_FILE_TYPE_BOOLEAN == b)) {
    return OB_FILE_TYPE_DOUBLE;
  }
  // every other combination falls back to text
  return OB_FILE_TYPE_VARCHAR;
}

const char *file_infer_type_name(ObFileInferType type)
{
  switch (type) {
    case OB_FILE_TYPE_BIGINT: return "BIGINT";
    case OB_FILE_TYPE_DOUBLE: return "DOUBLE";
    case OB_FILE_TYPE_BOOLEAN: return "BOOLEAN";
    case OB_FILE_TYPE_DATE: return "DATE";
    case OB_FILE_TYPE_DATETIME: return "DATETIME";
    case OB_FILE_TYPE_VARCHAR: return "VARCHAR";
    default: return "VARCHAR";
  }
}

namespace
{

bool is_ascii_digit(char c) { return c >= '0' && c <= '9'; }

// A signed decimal integer without exponent and without a fractional part.
bool is_integer_text(const std::string &s)
{
  if (s.empty()) {
    return false;
  }
  size_t i = 0;
  if ('-' == s[0] || '+' == s[0]) {
    i = 1;
  }
  if (i >= s.size()) {
    return false;
  }
  for (; i < s.size(); ++i) {
    if (!is_ascii_digit(s[i])) {
      return false;
    }
  }
  return true;
}

// A floating point number: digits with an optional fraction and exponent.
bool is_double_text(const std::string &s)
{
  if (s.empty()) {
    return false;
  }
  char *end = nullptr;
  errno = 0;
  const double value = strtod(s.c_str(), &end);
  (void)value;
  if (nullptr == end || '\0' != *end) {
    return false;
  }
  // strtod also accepts hexadecimal and "inf"/"nan" spellings, which are not
  // ordinary data values, so require a decimal shape.
  bool has_digit = false;
  bool has_dot = false;
  bool has_exp = false;
  size_t i = ('-' == s[0] || '+' == s[0]) ? 1 : 0;
  for (; i < s.size(); ++i) {
    const char c = s[i];
    if (is_ascii_digit(c)) {
      has_digit = true;
    } else if ('.' == c && !has_dot && !has_exp) {
      has_dot = true;
    } else if (('e' == c || 'E' == c) && has_digit && !has_exp) {
      has_exp = true;
      if (i + 1 < s.size() && ('-' == s[i + 1] || '+' == s[i + 1])) {
        ++i;
      }
      if (i + 1 >= s.size()) {
        return false;
      }
    } else {
      return false;
    }
  }
  return has_digit && (has_dot || has_exp);
}

bool is_boolean_text(const std::string &s)
{
  return "true" == s || "false" == s || "TRUE" == s || "FALSE" == s;
}

// ".5" and "5." are floating point numbers without one side of the fraction.
bool is_double_text_relaxed(const std::string &s)
{
  if (s.empty()) {
    return false;
  }
  size_t i = ('-' == s[0] || '+' == s[0]) ? 1 : 0;
  if (i >= s.size()) {
    return false;
  }
  bool has_digit = false;
  bool has_dot = false;
  for (; i < s.size(); ++i) {
    const char c = s[i];
    if (is_ascii_digit(c)) {
      has_digit = true;
    } else if ('.' == c && !has_dot) {
      has_dot = true;
    } else {
      return false;
    }
  }
  return has_dot && (has_digit || (s.size() > 1));
}

// Checks the calendar shape of a date: YYYY-MM-DD with a valid month and day.
bool is_valid_date_shape(const std::string &s)
{
  if (s.size() != 10) {
    return false;
  }
  if ('-' != s[4] || '-' != s[7]) {
    return false;
  }
  for (size_t i = 0; i < s.size(); ++i) {
    if (4 == i || 7 == i) {
      continue;
    }
    if (!is_ascii_digit(s[i])) {
      return false;
    }
  }
  const int year = (s[0] - '0') * 1000 + (s[1] - '0') * 100 + (s[2] - '0') * 10 + (s[3] - '0');
  const int month = (s[5] - '0') * 10 + (s[6] - '0');
  const int day = (s[8] - '0') * 10 + (s[9] - '0');
  if (year < 1 || month < 1 || month > 12 || day < 1) {
    return false;
  }
  static const int days_in_month[12] = {31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  int max_day = days_in_month[month - 1];
  if (2 == month && (0 != (year % 4) || (0 == (year % 100) && 0 != (year % 400)))) {
    max_day = 28;
  }
  return day <= max_day;
}

// Checks the calendar shape of a timestamp: YYYY-MM-DD HH:MM:SS[.ffffff].
bool is_valid_datetime_shape(const std::string &s)
{
  if (s.size() < 19) {
    return false;
  }
  if (!is_valid_date_shape(s.substr(0, 10))) {
    return false;
  }
  if (' ' != s[10] && 'T' != s[10]) {
    return false;
  }
  if (':' != s[13] || ':' != s[16]) {
    return false;
  }
  for (size_t i = 11; i < 19; ++i) {
    if (13 == i || 16 == i) {
      continue;
    }
    if (!is_ascii_digit(s[i])) {
      return false;
    }
  }
  const int hour = (s[11] - '0') * 10 + (s[12] - '0');
  const int minute = (s[14] - '0') * 10 + (s[15] - '0');
  const int second = (s[17] - '0') * 10 + (s[18] - '0');
  if (hour > 23 || minute > 59 || second > 59) {
    return false;
  }
  if (s.size() > 19) {
    if ('.' != s[19]) {
      return false;
    }
    for (size_t i = 20; i < s.size(); ++i) {
      if (!is_ascii_digit(s[i])) {
        return false;
      }
    }
  }
  return true;
}

}  // namespace

ObFileInferType infer_file_type(const std::string &text, bool is_json)
{
  if (text.empty()) {
    return OB_FILE_TYPE_NULL;
  }
  if (is_json) {
    // A JSON string keeps its quotes, so strip them before looking at the
    // content.  Numbers, booleans and null keep their JSON spelling.
    if ('"' == text[0]) {
      if (text.size() < 2 || '"' != text[text.size() - 1]) {
        return OB_FILE_TYPE_VARCHAR;
      }
      const std::string inner = text.substr(1, text.size() - 2);
      if (is_valid_date_shape(inner)) {
        return OB_FILE_TYPE_DATE;
      }
      if (is_valid_datetime_shape(inner)) {
        return OB_FILE_TYPE_DATETIME;
      }
      return OB_FILE_TYPE_VARCHAR;
    }
    if ("null" == text) {
      return OB_FILE_TYPE_NULL;
    }
    if ("true" == text || "false" == text) {
      return OB_FILE_TYPE_BOOLEAN;
    }
    if (is_integer_text(text)) {
      return OB_FILE_TYPE_BIGINT;
    }
    if (is_double_text(text) || is_double_text_relaxed(text)) {
      return OB_FILE_TYPE_DOUBLE;
    }
    return OB_FILE_TYPE_VARCHAR;
  }
  if (is_boolean_text(text)) {
    return OB_FILE_TYPE_BOOLEAN;
  }
  if (is_integer_text(text)) {
    return OB_FILE_TYPE_BIGINT;
  }
  if (is_double_text(text) || is_double_text_relaxed(text)) {
    return OB_FILE_TYPE_DOUBLE;
  }
  if (is_valid_date_shape(text)) {
    return OB_FILE_TYPE_DATE;
  }
  if (is_valid_datetime_shape(text)) {
    return OB_FILE_TYPE_DATETIME;
  }
  return OB_FILE_TYPE_VARCHAR;
}

bool parse_file_date(const std::string &text, int32_t &date_value)
{
  if (!is_valid_date_shape(text)) {
    return false;
  }
  const common::ObString value(static_cast<int32_t>(text.size()), text.c_str());
  int32_t result = 0;
  if (OB_SUCCESS != common::ObTimeConverter::str_to_date(value, result)) {
    return false;
  }
  date_value = result;
  return true;
}

bool parse_file_datetime(const std::string &text, int64_t &datetime_value)
{
  if (!is_valid_datetime_shape(text)) {
    return false;
  }
  const common::ObString value(static_cast<int32_t>(text.size()), text.c_str());
  common::ObTimeConvertCtx cvrt_ctx(NULL, false);
  int64_t result = 0;
  if (OB_SUCCESS != common::ObTimeConverter::str_to_datetime(value, cvrt_ctx, result)) {
    return false;
  }
  datetime_value = result;
  return true;
}

bool parse_file_integer(const std::string &text, int64_t &value)
{
  if (!is_integer_text(text)) {
    return false;
  }
  errno = 0;
  char *end = nullptr;
  const long long result = strtoll(text.c_str(), &end, 10);
  if (nullptr == end || '\0' != *end || 0 != errno) {
    return false;
  }
  value = static_cast<int64_t>(result);
  return true;
}

bool parse_file_double(const std::string &text, double &value)
{
  if (!is_double_text(text) && !is_double_text_relaxed(text)) {
    return false;
  }
  errno = 0;
  char *end = nullptr;
  const double result = strtod(text.c_str(), &end);
  if (nullptr == end || '\0' != *end) {
    return false;
  }
  value = result;
  return true;
}

}  // namespace sql
}  // namespace oceanbase
