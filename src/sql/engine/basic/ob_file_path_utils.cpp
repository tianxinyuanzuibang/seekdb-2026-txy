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

#include "sql/engine/basic/ob_file_path_utils.h"

#include <dirent.h>
#include <sys/stat.h>

#include <algorithm>
#include <cstdlib>
#include <limits.h>

#include "share/ob_errno.h"

#ifdef _WIN32
#include <windows.h>
#endif

namespace oceanbase
{
namespace sql
{

bool file_name_matches(const std::string &name, const std::string &mask)
{
  // The usual wildcard match with a backtrack on the last `*`.
  size_t mi = 0;
  size_t ni = 0;
  size_t star = std::string::npos;
  size_t mark = 0;
  while (ni < name.size()) {
    if (mi < mask.size() && ('?' == mask[mi] || mask[mi] == name[ni])) {
      ++mi;
      ++ni;
    } else if (mi < mask.size() && '*' == mask[mi]) {
      star = mi++;
      mark = ni;
    } else if (std::string::npos != star) {
      mi = star + 1;
      ni = ++mark;
    } else {
      return false;
    }
  }
  while (mi < mask.size() && '*' == mask[mi]) {
    ++mi;
  }
  return mi == mask.size();
}

int file_glob_list(const std::string &pattern, std::vector<std::string> &files)
{
  int ret = OB_SUCCESS;
  files.clear();
  const size_t slash = pattern.find_last_of('/');
  const std::string dir = (std::string::npos == slash) ? std::string(".")
                                                        : pattern.substr(0, slash + 1);
  const std::string mask = (std::string::npos == slash) ? pattern
                                                        : pattern.substr(slash + 1);
  const bool has_wild = (mask.find('*') != std::string::npos)
                        || (mask.find('?') != std::string::npos);
  if (!has_wild) {
    // An exact path is returned when it exists.
    struct stat stat_buf;
    if (0 == ::stat(pattern.c_str(), &stat_buf) && S_ISREG(stat_buf.st_mode)) {
      files.push_back(pattern);
    }
    return ret;
  }
  DIR *dirp = ::opendir(dir.c_str());
  if (nullptr == dirp) {
    ret = OB_FILE_NOT_EXIST;
    return ret;
  }
  struct dirent *entry = nullptr;
  while (nullptr != (entry = ::readdir(dirp))) {
    const std::string name(entry->d_name);
    if ("." == name || ".." == name) {
      continue;
    }
    if (!file_name_matches(name, mask)) {
      continue;
    }
    const std::string full = dir + name;
    struct stat stat_buf;
    if (0 == ::stat(full.c_str(), &stat_buf) && S_ISREG(stat_buf.st_mode)) {
      files.push_back(full);
    }
  }
  ::closedir(dirp);
  // The order of the directory is not stable, so the result is sorted.
  std::sort(files.begin(), files.end());
  return ret;
}

int file_absolute_path(const std::string &path, std::string &absolute_path)
{
  int ret = OB_SUCCESS;
  absolute_path.clear();
  // The file does not have to exist, so the path is resolved as far as it
  // exists and the rest is appended.
  std::string rest;
  std::string dir = path;
  while (!dir.empty()) {
    char buf[PATH_MAX + 1];
    if (nullptr != ::realpath(dir.c_str(), buf)) {
      std::string resolved(buf);
      if (rest.empty()) {
        absolute_path = resolved;
      } else if ('/' == resolved[resolved.size() - 1]) {
        absolute_path = resolved + rest;
      } else {
        absolute_path = resolved + "/" + rest;
      }
      return ret;
    }
    // The last element of the directory does not exist, strip it and try
    // again.
    const size_t slash = dir.find_last_of('/');
    if (std::string::npos == slash) {
      break;
    }
    rest = dir.substr(slash + 1) + (rest.empty() ? "" : "/" + rest);
    dir = dir.substr(0, slash);
  }
  // Nothing could be resolved, the path is used as it is.
  absolute_path = path;
  return ret;
}

int file_stat_info(const std::string &path, int64_t &file_size, int64_t &file_mtime)
{
  int ret = OB_SUCCESS;
  file_size = 0;
  file_mtime = 0;
  struct stat stat_buf;
  if (0 != ::stat(path.c_str(), &stat_buf)) {
    ret = OB_FILE_NOT_EXIST;
  } else {
    file_size = stat_buf.st_size;
    file_mtime = stat_buf.st_mtime;
  }
  return ret;
}

}  // namespace sql
}  // namespace oceanbase
