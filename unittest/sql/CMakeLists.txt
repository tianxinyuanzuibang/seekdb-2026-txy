# Copyright (c) 2025 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The unit tests of the sql module.

set(SQL_TEST_SOURCES
  engine/basic/test_file_scan_utils.cpp
  parser/test_file_sql_parser.cpp
)

add_executable(${SEEKDB_PRETEST_TARGET} ${SQL_TEST_SOURCES})

target_include_directories(${SEEKDB_PRETEST_TARGET} PRIVATE
  ${CMAKE_SOURCE_DIR}
  ${CMAKE_SOURCE_DIR}/src
  ${CMAKE_SOURCE_DIR}/src/oblib
  ${CMAKE_SOURCE_DIR}/src/objit/include
  ${CMAKE_SOURCE_DIR}/src/query/api
  ${CMAKE_BINARY_DIR}
)

target_link_libraries(${SEEKDB_PRETEST_TARGET} PRIVATE
  ob_sql_static
  ob_base
  gtest
)
