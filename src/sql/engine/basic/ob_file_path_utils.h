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
#include "lib/string/ob_string.h"

#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{

// The file helpers of the file table functions.

// Matches a file name against a mask that holds the `*` and `?` wildcards.
bool file_name_matches(const std::string &name, const std::string &mask);

// Lists the files a glob pattern matches.  Only the last path element may
// hold a wildcard, the directory part is taken as it is.  The result is
// sorted so the output of the query is stable.
int file_glob_list(const std::string &pattern, std::vector<std::string> &files);

// Returns the absolute path of a file.  The path does not have to exist, the
// last element may be a wildcard.
int file_absolute_path(const std::string &path, std::string &absolute_path);

// Returns the size and the modification time of a file.
int file_stat_info(const std::string &path, int64_t &file_size, int64_t &file_mtime);

}  // namespace sql
}  // namespace oceanbase
