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

#include "lib/allocator/ob_allocator.h"
#include "lib/ob_define.h"
#include "lib/string/ob_string.h"

#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{

// The file table functions (`read_csv`, `read_json`, `read_ndjson`,
// `read_parquet` and `glob`) share the reader options, the inferred column
// schema and the type inference rules.  They live in this header so the
// resolver, the physical operator and the unit tests all agree on the
// behaviour.
//
// The readers are streaming: a file is never materialised as a whole.  Only
// the inferred schema (column names, types and nullability) is kept in
// memory, the rows are produced one at a time while the file is scanned.

enum ObFileFormat
{
  OB_FILE_FORMAT_CSV = 0,
  OB_FILE_FORMAT_JSONL = 1,
  OB_FILE_FORMAT_PARQUET = 2,
  OB_FILE_FORMAT_GLOB = 3,
};

// The seven types the deterministic type inference may produce.
enum ObFileInferType
{
  OB_FILE_TYPE_NULL = 0,
  OB_FILE_TYPE_BIGINT,
  OB_FILE_TYPE_DOUBLE,
  OB_FILE_TYPE_BOOLEAN,
  OB_FILE_TYPE_DATE,
  OB_FILE_TYPE_DATETIME,
  OB_FILE_TYPE_VARCHAR,
};

// The reader options.  `sample_size` only accepts -1: the schema inference
// always looks at the whole file so the result does not depend on how many
// rows were sampled.
struct ObFileReadOptions
{
  ObFileReadOptions()
    : format(OB_FILE_FORMAT_CSV),
      has_header(true),
      sample_size(-1),
      delimiter(','),
      quote('"'),
      escape('"'),
      null_string("NULL"),
      has_null_string(false),
      max_rows(-1)
  {}

  ObFileFormat format;
  bool has_header;
  int64_t sample_size;
  char delimiter;
  char quote;
  char escape;
  common::ObString null_string;
  bool has_null_string;
  // -1 means "no limit".  Used to bound the work of a single scan.
  int64_t max_rows;

  bool is_csv() const { return OB_FILE_FORMAT_CSV == format; }
  bool is_jsonl() const { return OB_FILE_FORMAT_JSONL == format; }
  bool is_parquet() const { return OB_FILE_FORMAT_PARQUET == format; }
  bool is_glob() const { return OB_FILE_FORMAT_GLOB == format; }
};

// One column of the inferred schema.
struct ObFileColumn
{
  ObFileColumn()
    : type(OB_FILE_TYPE_NULL), nullable(false)
  {}
  explicit ObFileColumn(const std::string &n)
    : name(n), type(OB_FILE_TYPE_NULL), nullable(false)
  {}

  std::string name;
  ObFileInferType type;
  bool nullable;
};

// The inferred schema of a file.  `columns` keeps the file order; the
// duplicate header names are disambiguated with a `__2`, `__3`, ... suffix.
struct ObFileSchema
{
  std::vector<ObFileColumn> columns;

  int64_t column_count() const { return static_cast<int64_t>(columns.size()); }
  int find_column(const std::string &name) const;
};

// Merges two inferred types into the type that can hold both.  The rules are
// the deterministic widening rules:
//   NULL + X           -> X
//   BIGINT + DOUBLE    -> DOUBLE
//   DATE + DATETIME    -> DATETIME
//   BOOLEAN + BIGINT   -> BIGINT
//   BOOLEAN + DOUBLE   -> DOUBLE
//   anything else      -> VARCHAR
ObFileInferType merge_file_infer_type(ObFileInferType a, ObFileInferType b);

// Infers the type of one text field.  `is_json` tells whether the text is a
// raw JSON value (so a quoted string is a string) or a bare CSV field.
ObFileInferType infer_file_type(const std::string &text, bool is_json);

// Returns the SQL type name of an inferred type.
const char *file_infer_type_name(ObFileInferType type);

// Parses a textual date into the internal date representation (days since the
// epoch).  Returns false when the text is not a valid date.
bool parse_file_date(const std::string &text, int32_t &date_value);

// Parses a textual timestamp into the internal datetime representation
// (microseconds since the epoch).  Returns false when the text is not a
// valid timestamp.
bool parse_file_datetime(const std::string &text, int64_t &datetime_value);

// Parses a textual integer.  Returns false when the text is not an integer
// or does not fit into 64 bits.
bool parse_file_integer(const std::string &text, int64_t &value);

// Parses a textual floating point number.  Returns false when the text is
// not a number.
bool parse_file_double(const std::string &text, double &value);

}  // namespace sql
}  // namespace oceanbase
