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

# The unit tests of one module are built as one binary.  The binary is
# registered with gtest and dispatched by the shared test main.

foreach(SEEKDB_PRETEST_MODULE IN LISTS SEEKDB_PRETEST_MODULES)
  string(TOUPPER "${SEEKDB_PRETEST_MODULE}" SEEKDB_PRETEST_MODULE_UPPER)
  set(SEEKDB_PRETEST_PREFIX
    "SEEKDB_PRETEST_${SEEKDB_PRETEST_MODULE_UPPER}")
  set(SEEKDB_PRETEST_TARGET "${${SEEKDB_PRETEST_PREFIX}_TARGET}")
  set(SEEKDB_PRETEST_UNITY_SIZE "${${SEEKDB_PRETEST_PREFIX}_UNITY_SIZE}")
  set(SEEKDB_PRETEST_UNITY_EXCEPTIONS
    "${${SEEKDB_PRETEST_PREFIX}_UNITY_EXCEPTIONS}")

  add_subdirectory(${SEEKDB_PRETEST_MODULE} ${SEEKDB_PRETEST_MODULE}_tests)
endforeach()
