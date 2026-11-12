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

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "lib/allocator/ob_allocator.h"
#include "lib/allocator/page_arena.h"
#include "lib/ob_errno.h"
#include "sql/engine/basic/ob_file_csv_reader.h"
#include "sql/engine/basic/ob_file_jsonl_reader.h"
#include "sql/engine/basic/ob_file_parquet_reader.h"
#include "sql/engine/basic/ob_file_path_utils.h"
#include "sql/engine/basic/ob_file_scan_utils.h"

using namespace oceanbase;
using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace
{

// The tests share one allocator and one temporary directory.  The directory
// is created by the first test that needs it.
ObArenaAllocator &test_allocator()
{
  static ObArenaAllocator allocator;
  return allocator;
}

std::string &tmp_dir()
{
  static std::string dir;
  if (dir.empty()) {
    char pattern[] = "/tmp/seekdb_file_scan_XXXXXX";
    char *created = ::mkdtemp(pattern);
    if (nullptr != created) {
      dir = created;
    }
  }
  return dir;
}

// Writes a temporary file and returns its path.  The name may hold a
// directory, it is created when it does not exist.
std::string write_file(const std::string &name, const std::string &content)
{
  const size_t slash = name.find_last_of('/');
  if (std::string::npos != slash) {
    const std::string dir = tmp_dir() + "/" + name.substr(0, slash);
    ::mkdir(dir.c_str(), 0755);
  }
  const std::string path = tmp_dir() + "/" + name;
  FILE *fp = ::fopen(path.c_str(), "wb");
  if (nullptr != fp) {
    ::fwrite(content.data(), 1, content.size(), fp);
    ::fclose(fp);
  }
  return path;
}

int32_t parse_date_helper(const std::string &text)
{
  int32_t value = 0;
  (void)parse_file_date(text, value);
  return value;
}

int32_t &parse_date_out()
{
  static int32_t value = 0;
  return value;
}

// ---------------------------------------------------------------------------
// type inference
// ---------------------------------------------------------------------------

TEST(FileScanUtils, infer_basic_types)
{
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, infer_file_type("42", false));
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, infer_file_type("-7", false));
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, infer_file_type("+15", false));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, infer_file_type("3.14", false));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, infer_file_type("1e3", false));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, infer_file_type("-2.5E-4", false));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, infer_file_type(".5", false));
  ASSERT_EQ(OB_FILE_TYPE_BOOLEAN, infer_file_type("true", false));
  ASSERT_EQ(OB_FILE_TYPE_BOOLEAN, infer_file_type("FALSE", false));
  ASSERT_EQ(OB_FILE_TYPE_DATE, infer_file_type("2026-08-01", false));
  ASSERT_EQ(OB_FILE_TYPE_DATETIME, infer_file_type("2026-08-01 10:11:12", false));
  ASSERT_EQ(OB_FILE_TYPE_DATETIME, infer_file_type("2026-08-01T10:11:12", false));
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, infer_file_type("hello", false));
  ASSERT_EQ(OB_FILE_TYPE_NULL, infer_file_type("", false));
  // A single digit is a number, only the words are booleans.
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, infer_file_type("0", false));
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, infer_file_type("1", false));
}

TEST(FileScanUtils, infer_type_widening)
{
  // A column that only holds NULL has no type of its own, it widens to the
  // type of the values that were seen with it.
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, merge_file_infer_type(OB_FILE_TYPE_NULL, OB_FILE_TYPE_BIGINT));
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, merge_file_infer_type(OB_FILE_TYPE_BIGINT, OB_FILE_TYPE_NULL));
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, merge_file_infer_type(OB_FILE_TYPE_BIGINT, OB_FILE_TYPE_BIGINT));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, merge_file_infer_type(OB_FILE_TYPE_BIGINT, OB_FILE_TYPE_DOUBLE));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, merge_file_infer_type(OB_FILE_TYPE_DOUBLE, OB_FILE_TYPE_BIGINT));
  ASSERT_EQ(OB_FILE_TYPE_DATETIME, merge_file_infer_type(OB_FILE_TYPE_DATE, OB_FILE_TYPE_DATETIME));
  ASSERT_EQ(OB_FILE_TYPE_DATETIME, merge_file_infer_type(OB_FILE_TYPE_DATETIME, OB_FILE_TYPE_DATE));
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, merge_file_infer_type(OB_FILE_TYPE_BOOLEAN, OB_FILE_TYPE_BIGINT));
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, merge_file_infer_type(OB_FILE_TYPE_BOOLEAN, OB_FILE_TYPE_DOUBLE));
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, merge_file_infer_type(OB_FILE_TYPE_BIGINT, OB_FILE_TYPE_VARCHAR));
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, merge_file_infer_type(OB_FILE_TYPE_DATE, OB_FILE_TYPE_VARCHAR));
}

TEST(FileScanUtils, date_boundary_validation)
{
  ASSERT_TRUE(parse_file_date("2026-02-28", parse_date_out()));
  ASSERT_TRUE(parse_file_date("2024-02-29", parse_date_out()));  // leap year
  ASSERT_FALSE(parse_file_date("2026-02-29", parse_date_out())); // not a leap year
  ASSERT_FALSE(parse_file_date("2026-13-01", parse_date_out())); // month out of range
  ASSERT_FALSE(parse_file_date("2026-00-10", parse_date_out())); // month out of range
  ASSERT_FALSE(parse_file_date("2026-04-31", parse_date_out())); // day out of range
  ASSERT_FALSE(parse_file_date("2026-1-01", parse_date_out()));  // not zero padded
  ASSERT_FALSE(parse_file_date("0000-01-01", parse_date_out())); // year out of range
}

TEST(FileScanUtils, all_null_column_stays_varchar)
{
  const std::string content = "a,b\n,\n,\n";
  const std::string path = write_file("all_null.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(2, schema.column_count());
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, schema.columns[0].type);
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, schema.columns[1].type);
  ASSERT_TRUE(schema.columns[0].nullable);
  ASSERT_TRUE(schema.columns[1].nullable);
}

// ---------------------------------------------------------------------------
// CSV reader
// ---------------------------------------------------------------------------

TEST(FileScanUtils, csv_header_and_types)
{
  const std::string content =
      "id,name,score,active,day,created,note\n"
      "1,Alice,98.5,true,2026-08-01,2026-08-01 10:11:12,hello\n"
      "2,Bob,87,false,,,plain\n";
  const std::string path = write_file("basic.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(7, schema.column_count());
  ASSERT_EQ("id", schema.columns[0].name);
  ASSERT_EQ("name", schema.columns[1].name);
  ASSERT_EQ("score", schema.columns[2].name);
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, schema.columns[0].type);
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, schema.columns[1].type);
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, schema.columns[2].type);
  ASSERT_EQ(OB_FILE_TYPE_BOOLEAN, schema.columns[3].type);
  ASSERT_EQ(OB_FILE_TYPE_DATE, schema.columns[4].type);
  ASSERT_EQ(OB_FILE_TYPE_DATETIME, schema.columns[5].type);
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, schema.columns[6].type);
}

TEST(FileScanUtils, csv_quotes_escapes_and_newlines)
{
  const std::string content =
      "id,note\n"
      "1,\"hello,\nworld\"\n"
      "2,\"say \"\"hi\"\"\"\n";
  const std::string path = write_file("quoted.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(2, schema.column_count());
  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ(2, values.size());
  ASSERT_EQ("1", values[0]);
  ASSERT_EQ("hello,\nworld", values[1]);
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ("say \"hi\"", values[1]);
  ASSERT_EQ(OB_ITER_END, reader.next_row(values, nulls));
}

TEST(FileScanUtils, csv_crlf_and_empty_fields)
{
  const std::string content = "a,b\r\n1,\r\n,2\r\n";
  const std::string path = write_file("crlf.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ("1", values[0]);
  ASSERT_TRUE(nulls[1]);
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_TRUE(nulls[0]);
  ASSERT_EQ("2", values[1]);
  ASSERT_EQ(OB_ITER_END, reader.next_row(values, nulls));
}

TEST(FileScanUtils, csv_duplicate_headers_get_suffix)
{
  const std::string content = "id,id\n1,2\n";
  const std::string path = write_file("dup.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(2, schema.column_count());
  ASSERT_EQ("id", schema.columns[0].name);
  ASSERT_EQ("id__2", schema.columns[1].name);
}

TEST(FileScanUtils, csv_without_header_gets_generated_names)
{
  const std::string content = "1,x\n2,y\n";
  const std::string path = write_file("no_header.csv", content);
  ObFileReadOptions options;
  options.has_header = false;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(2, schema.column_count());
  ASSERT_EQ("c1", schema.columns[0].name);
  ASSERT_EQ("c2", schema.columns[1].name);
  std::vector<std::string> values;
  std::vector<bool> nulls;
  // the first row is data, not a header
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ("1", values[0]);
}

TEST(FileScanUtils, csv_null_string_option)
{
  const std::string content = "a,b\nNULL,x\n1,y\n";
  const std::string path = write_file("nullstr.csv", content);
  ObFileReadOptions options;
  options.has_null_string = true;
  options.null_string = ObString("NULL");
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_TRUE(nulls[0]);
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_FALSE(nulls[0]);
}

TEST(FileScanUtils, csv_multiline_record_spans_buffer)
{
  // A quoted field that is longer than the read buffer must still come out
  // as one field.
  std::string long_field(200 * 1024, 'x');
  const std::string content = "a,b\n1,\"" + long_field + "\"\n";
  const std::string path = write_file("long.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ(2, values.size());
  ASSERT_EQ(long_field, values[1]);
  ASSERT_EQ(OB_ITER_END, reader.next_row(values, nulls));
}

TEST(FileScanUtils, csv_padded_numbers_parse)
{
  // Unquoted blanks around a number are trimmed, the value still parses.
  const std::string content = "a,b\n 12 , x\n";
  const std::string path = write_file("padded.csv", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileCsvReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ("12", values[0]);
}

// ---------------------------------------------------------------------------
// JSONL reader
// ---------------------------------------------------------------------------

TEST(FileScanUtils, jsonl_union_schema_and_missing_fields)
{
  const std::string content =
      "{\"id\":1,\"name\":\"Alice\",\"score\":98.5,\"active\":true}\n"
      "{\"id\":2,\"name\":\"Bob\",\"active\":false,\"day\":\"2026-08-02\"}\n"
      "{\"id\":3,\"name\":\"Chen\",\"score\":90,\"active\":true,\"note\":\"ok\"}\n";
  const std::string path = write_file("basic.jsonl", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileJsonlReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(6, schema.column_count());
  ASSERT_EQ("id", schema.columns[0].name);
  ASSERT_EQ("name", schema.columns[1].name);
  ASSERT_EQ("score", schema.columns[2].name);
  ASSERT_EQ("active", schema.columns[3].name);
  ASSERT_EQ("day", schema.columns[4].name);
  ASSERT_EQ("note", schema.columns[5].name);
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, schema.columns[0].type);
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, schema.columns[1].type);
  ASSERT_EQ(OB_FILE_TYPE_DOUBLE, schema.columns[2].type);
  ASSERT_EQ(OB_FILE_TYPE_BOOLEAN, schema.columns[3].type);
  ASSERT_EQ(OB_FILE_TYPE_DATE, schema.columns[4].type);
  ASSERT_EQ(OB_FILE_TYPE_VARCHAR, schema.columns[5].type);

  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ(6, values.size());
  ASSERT_EQ("1", values[0]);
  ASSERT_TRUE(nulls[4]);  // day missing
  ASSERT_TRUE(nulls[5]);  // note missing
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_TRUE(nulls[2]);  // score missing
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_EQ(OB_ITER_END, reader.next_row(values, nulls));
}

TEST(FileScanUtils, jsonl_rejects_nested_objects)
{
  const std::string content = "{\"id\":1,\"nested\":{\"a\":1}}\n";
  const std::string path = write_file("nested.jsonl", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileJsonlReader reader;
  ASSERT_EQ(OB_INVALID_DATA, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
}

TEST(FileScanUtils, jsonl_rejects_arrays)
{
  const std::string content = "{\"id\":1,\"items\":[1,2,3]}\n";
  const std::string path = write_file("array.jsonl", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileJsonlReader reader;
  ASSERT_EQ(OB_INVALID_DATA, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
}

TEST(FileScanUtils, jsonl_null_values)
{
  const std::string content = "{\"a\":null,\"b\":1}\n{\"a\":5,\"b\":null}\n";
  const std::string path = write_file("null.jsonl", content);
  ObFileReadOptions options;
  ObFileSchema schema;
  ObFileJsonlReader reader;
  ASSERT_EQ(OB_SUCCESS, reader.open(ObString(path.c_str()), options, test_allocator(), schema, {}));
  ASSERT_EQ(2, schema.column_count());
  ASSERT_EQ(OB_FILE_TYPE_BIGINT, schema.columns[0].type);
  std::vector<std::string> values;
  std::vector<bool> nulls;
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_TRUE(nulls[0]);
  ASSERT_EQ(OB_SUCCESS, reader.next_row(values, nulls));
  ASSERT_TRUE(nulls[1]);
}

// ---------------------------------------------------------------------------
// glob
// ---------------------------------------------------------------------------

TEST(FileScanUtils, glob_matches_and_sorts)
{
  // The glob uses its own directory so the other files of the tests do not
  // show up in the result.
  const std::string dir = tmp_dir() + "/glob";
  ::mkdir(dir.c_str(), 0755);
  (void)write_file("glob/m1.csv", "a\n1\n");
  (void)write_file("glob/m2.csv", "a\n2\n");
  (void)write_file("glob/other.txt", "x\n");
  std::vector<std::string> files;
  ASSERT_EQ(OB_SUCCESS, file_glob_list(dir + "/*.csv", files));
  ASSERT_EQ(2, files.size());
  // sorted
  ASSERT_TRUE(files[0] < files[1]);
}

TEST(FileScanUtils, glob_no_match_returns_empty)
{
  std::vector<std::string> files;
  ASSERT_EQ(OB_SUCCESS, file_glob_list(tmp_dir() + "/zzz_none_*.csv", files));
  ASSERT_EQ(0, files.size());
}

TEST(FileScanUtils, wildcard_matching)
{
  ASSERT_TRUE(file_name_matches("a.csv", "*"));
  ASSERT_TRUE(file_name_matches("a.csv", "*.csv"));
  ASSERT_TRUE(file_name_matches("a.csv", "?.csv"));
  ASSERT_FALSE(file_name_matches("ab.csv", "?.csv"));
  ASSERT_TRUE(file_name_matches("data_2026.csv", "data_*.csv"));
  ASSERT_FALSE(file_name_matches("data_2026.csv", "data_?.csv"));
}

TEST(FileScanUtils, file_stat_info)
{
  const std::string path = write_file("stat.txt", "hello");
  int64_t size = 0;
  int64_t mtime = 0;
  ASSERT_EQ(OB_SUCCESS, file_stat_info(path, size, mtime));
  ASSERT_EQ(5, size);
  ASSERT_TRUE(0 < mtime);
  ASSERT_EQ(OB_FILE_NOT_EXIST, file_stat_info(tmp_dir() + "/missing.txt", size, mtime));
}

}  // namespace

int main()
{
  return RUN_ALL_TESTS();
}
