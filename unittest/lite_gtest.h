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

// A minimal gtest-compatible assertion layer.
//
// The gtest library that ships with the dependency toolchain was built with
// a different C++ standard library ABI than the rest of the project, so the
// tests cannot link against it.  The tests keep the gtest shape (a TEST
// macro, ASSERT_* checks and a runnable main) but the assertions are
// implemented here with the standard library only.
//
// The checks are statements instead of expressions, so the arguments of the
// checks may hold a comma at the top level.

#pragma once

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

namespace testing_lite
{

struct TestCase
{
  const char *suite;
  const char *name;
  std::function<void()> body;
};

inline std::vector<TestCase> &test_registry()
{
  static std::vector<TestCase> registry;
  return registry;
}

inline int &failure_count()
{
  static int count = 0;
  return count;
}

struct Registrar
{
  Registrar(const char *suite, const char *name, std::function<void()> body)
  {
    TestCase entry;
    entry.suite = suite;
    entry.name = name;
    entry.body = body;
    test_registry().push_back(entry);
  }
};

inline void report_failure(const char *file, int line, const std::string &what)
{
  ++failure_count();
  fprintf(stderr, "[  FAILED  ] %s:%d: %s\n", file, line, what.c_str());
}

inline int run_all_tests()
{
  int failed_tests = 0;
  for (size_t i = 0; i < test_registry().size(); ++i) {
    const TestCase &test = test_registry()[i];
    const int before = failure_count();
    fprintf(stdout, "[ RUN      ] %s.%s\n", test.suite, test.name);
    fflush(stdout);
    test.body();
    if (failure_count() > before) {
      ++failed_tests;
      fprintf(stdout, "[  FAILED  ] %s.%s\n", test.suite, test.name);
    } else {
      fprintf(stdout, "[       OK ] %s.%s\n", test.suite, test.name);
    }
    fflush(stdout);
  }
  fprintf(stdout, "[==========] %zu tests ran, %d failed.\n",
          test_registry().size(), failed_tests);
  return (0 == failed_tests) ? 0 : 1;
}

// A value of a check.  The strings are compared by their content, the other
// values with the operator of their type.
class LiteValue
{
public:
  LiteValue(const char *value) : text_(value), is_text_(true) {}
  LiteValue(char *value) : text_(value), is_text_(true) {}
  LiteValue(const std::string &value) : text_(value), is_text_(true) {}
  template <typename T>
  LiteValue(const T &value) : is_text_(false)
  {
    std::ostringstream out;
    if constexpr (std::is_enum<T>::value) {
      out << static_cast<int64_t>(value);
    } else {
      out << value;
    }
    text_ = out.str();
  }

  bool equals(const LiteValue &other) const
  {
    return text_ == other.text_;
  }

  const std::string &text() const { return text_; }

private:
  std::string text_;
  bool is_text_;
};

}  // namespace testing_lite

// Registers one test.  The body is a function without arguments.
#define LITE_TEST(suite_name, test_name)                                        \
  static void lite_test_##suite_name##_##test_name##_body();                    \
  static testing_lite::Registrar lite_test_##suite_name##_##test_name##_reg(    \
      #suite_name, #test_name,                                                  \
      lite_test_##suite_name##_##test_name##_body);                             \
  static void lite_test_##suite_name##_##test_name##_body()

// The body of a check.  `cond` is evaluated as a statement, so a comma in the
// arguments does not break the macro.
#define LITE_CHECK(cond, what)                                                  \
  do {                                                                          \
    if (cond) {                                                                 \
    } else {                                                                    \
      testing_lite::report_failure(__FILE__, __LINE__, what);                   \
      return;                                                                   \
    }                                                                           \
  } while (0)

#define LITE_TEXT_OF(x) (testing_lite::LiteValue(x).text())

#define ASSERT_EQ(a, b)                                                        \
  do {                                                                         \
    const std::string lite_va = LITE_TEXT_OF(a);                               \
    const std::string lite_vb = LITE_TEXT_OF(b);                               \
    LITE_CHECK(lite_va == lite_vb,                                             \
               std::string("check failed: ") + #a + " == " + #b                \
               + " (" + lite_va + " vs " + lite_vb + ")");                     \
  } while (0)

#define ASSERT_NE(a, b)                                                        \
  do {                                                                         \
    const std::string lite_va = LITE_TEXT_OF(a);                               \
    const std::string lite_vb = LITE_TEXT_OF(b);                               \
    LITE_CHECK(lite_va != lite_vb,                                             \
               std::string("check failed: ") + #a + " != " + #b);              \
  } while (0)

#define ASSERT_TRUE(a)                                                         \
  do {                                                                         \
    LITE_CHECK(!!(a), std::string("check failed: ") + #a);                     \
  } while (0)

#define ASSERT_FALSE(a)                                                        \
  do {                                                                         \
    LITE_CHECK(!(a), std::string("check failed: ") + #a);                      \
  } while (0)

#define ASSERT_LT(a, b)                                                        \
  do {                                                                         \
    LITE_CHECK((a) < (b),                                                      \
               std::string("check failed: ") + #a + " < " + #b);               \
  } while (0)

#define EXPECT_EQ(a, b) ASSERT_EQ(a, b)
#define EXPECT_NE(a, b) ASSERT_NE(a, b)
#define EXPECT_TRUE(a) ASSERT_TRUE(a)
#define EXPECT_FALSE(a) ASSERT_FALSE(a)
#define EXPECT_LT(a, b) ASSERT_LT(a, b)

#define TEST(suite_name, test_name) LITE_TEST(suite_name, test_name)
#define RUN_ALL_TESTS() testing_lite::run_all_tests()
