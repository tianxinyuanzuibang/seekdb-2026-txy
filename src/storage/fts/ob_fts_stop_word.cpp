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

#include "object/ob_object.h"
#define USING_LOG_PREFIX STORAGE_FTS

#include "share/rc/ob_server_runtime.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "storage/fts/ob_fts_parser_helper.h"
#include "storage/fts/ob_fts_parser_property.h"

namespace oceanbase
{
namespace storage
{

namespace
{
OB_INLINE bool is_ascii_word(const ObString &word)
{
  bool is_ascii = true;
  for (int64_t i = 0; is_ascii && i < word.length(); ++i) {
    is_ascii = (0 == (static_cast<unsigned char>(word.ptr()[i]) & 0x80));
  }
  return is_ascii;
}

// The built in stop word table is a compile time constant: a few dozen lower case
// ASCII words.  Deciding whether a token is one of them is on the hot path of
// every single document, and an index build runs it from several worker threads
// at once, so the check must not touch any shared writable state.  The table is
// therefore folded once into a read only list of (length, leading bytes) pairs
// and membership is decided by comparing two integers per entry: no lock, no
// allocation, no charset work, and nothing that several threads can contend on.
class ObStopWordLut
{
public:
  static const int64_t MAX_WORD_LEN = 8;
  static const int64_t MAX_WORD_CNT = 64;

  ObStopWordLut() : count_(0)
  {
    const int64_t table_cnt = static_cast<int64_t>(
        sizeof(ob_stop_word_list) / sizeof(ob_stop_word_list[0]));
    for (int64_t i = 0; i < table_cnt && count_ < MAX_WORD_CNT; ++i) {
      const char *word = ob_stop_word_list[i];
      int64_t len = 0;
      while (len < MAX_WORD_LEN && '\0' != word[len]) {
        ++len;
      }
      if (len > 0) {
        entries_[count_].prefix_ = pack_prefix(word, len);
        entries_[count_].len_ = static_cast<uint8_t>(len);
        ++count_;
      }
    }
  }

  bool contains(const char *word, const int64_t len) const
  {
    bool found = false;
    if (len > 0 && len <= MAX_WORD_LEN) {
      const uint64_t prefix = pack_prefix(word, len);
      for (int64_t i = 0; !found && i < count_; ++i) {
        found = (entries_[i].len_ == static_cast<uint8_t>(len)
                 && entries_[i].prefix_ == prefix);
      }
    }
    return found;
  }

private:
  struct Entry
  {
    uint64_t prefix_;
    uint8_t len_;
  };

  static uint64_t pack_prefix(const char *word, const int64_t len)
  {
    uint64_t prefix = 0;
    for (int64_t i = len - 1; i >= 0; --i) {
      prefix = (prefix << 8) | static_cast<uint8_t>(word[i]);
    }
    return prefix;
  }

private:
  Entry entries_[MAX_WORD_CNT];
  int64_t count_;
};

const ObStopWordLut &get_stop_word_lut()
{
  // Built once, then only read; the guard of a function local static is a plain
  // load after the first call, so no thread can make another one wait.
  static const ObStopWordLut stop_word_lut;
  return stop_word_lut;
}

// Decide whether casedown can change the token. CJK Unified Ideographs and
// lowercase/non-letter ASCII characters have no case mapping, so tokens made of
// them can skip ObCharset::tolower (which always allocates a copy of the word).
OB_INLINE bool casedown_may_change(const ObString &word)
{
  bool may_change = false;
  const uint8_t *ptr = reinterpret_cast<const uint8_t *>(word.ptr());
  const int64_t len = word.length();
  int64_t pos = 0;
  while (!may_change && pos < len) {
    const uint8_t ch = ptr[pos];
    if (ch < 0x80) {
      may_change = (ch >= 'A' && ch <= 'Z');
      ++pos;
    } else if (ch >= 0xE4 && ch <= 0xE9 && pos + 2 < len
               && 0x80 == (ptr[pos + 1] & 0xC0) && 0x80 == (ptr[pos + 2] & 0xC0)) {
      const uint32_t cp = (static_cast<uint32_t>(ch & 0x0F) << 12)
                          | (static_cast<uint32_t>(ptr[pos + 1] & 0x3F) << 6)
                          | static_cast<uint32_t>(ptr[pos + 2] & 0x3F);
      may_change = (cp < 0x4E00 || cp > 0x9FFF);
      pos += 3;
    } else {
      // Be conservative for every other script / encoding.
      may_change = true;
    }
  }
  return may_change;
}
} // namespace

////////////////////////////////////////////////////////////////////////////////
// class ObStopWordChecker
ObStopWordChecker::~ObStopWordChecker()
{
  destroy();
}

int ObStopWordChecker::init()
{
  int ret = OB_SUCCESS;
  
  

  if (inited_) {
    ret = OB_INIT_TWICE;
  } else if (OB_FAIL(stopword_set_.create(DEFAULT_STOPWORD_BUCKET_NUM, "StopWordSet", "StopWordSet"))) {
  } else {
    ObObjMeta stop_meta;
    stop_meta.set_varchar();
    stop_meta.set_collation_type(ObCollationType::CS_TYPE_UTF8MB4_GENERAL_CI);

    stopword_type_.set_meta(stop_meta);
    const int64_t stopword_count = sizeof(ob_stop_word_list) / sizeof(ob_stop_word_list[0]);
    for (int64_t i = 0; OB_SUCC(ret) && i < stopword_count; ++i) {
      ObFTWord stopword(STRLEN(ob_stop_word_list[i]), ob_stop_word_list[i], stopword_type_);
      if (OB_FAIL(stopword_set_.set_refactored(stopword))) {
      }
    }

    if (OB_SUCC(ret)) {
      inited_ = true;
    }
  }
  return ret;
}

void ObStopWordChecker::destroy()
{
  if (inited_) {
    stopword_set_.destroy();
    inited_ = false;
  }
}

int ObStopWordChecker::check_stopword(const ObFTWord &word, bool &is_stopword)
{
  int ret = OB_SUCCESS;
  is_stopword = false;
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else if (OB_UNLIKELY(word.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (is_ascii_word(word.get_word().get_string())) {
    // general_ci comparison over ASCII is case insensitive byte comparison, so a
    // lower cased copy of the token is what the table has to be asked about.
    const ObString ascii_word = word.get_word().get_string();
    if (ascii_word.length() > ObStopWordLut::MAX_WORD_LEN) {
      is_stopword = false;
    } else {
      char lower_buf[ObStopWordLut::MAX_WORD_LEN];
      bool has_upper = false;
      for (int64_t i = 0; !has_upper && i < ascii_word.length(); ++i) {
        has_upper = (ascii_word.ptr()[i] >= 'A' && ascii_word.ptr()[i] <= 'Z');
      }
      const char *lookup_ptr = ascii_word.ptr();
      if (has_upper) {
        for (int64_t i = 0; i < ascii_word.length(); ++i) {
          const char c = ascii_word.ptr()[i];
          lower_buf[i] = (c >= 'A' && c <= 'Z') ? static_cast<char>(c - 'A' + 'a') : c;
        }
        lookup_ptr = lower_buf;
      }
      is_stopword = get_stop_word_lut().contains(lookup_ptr, ascii_word.length());
    }
  } else {
    common::ObString cmp_str;
    // Skip the conversion copy when the charsets already match (the common case:
    // an utf8mb4 token against the utf8mb4 stop word list).
    const ObCharsetType word_cs = ObCharset::charset_type_by_coll(word.get_collation_type());
    const ObCharsetType stop_cs = ObCharset::charset_type_by_coll(stopword_type_.get_collation_type());
    if (word_cs == stop_cs && common::CHARSET_INVALID != word_cs) {
      cmp_str = word.get_word().get_string();
    } else {
      common::ObArenaAllocator allocator(lib::ObMemAttr("ChkStopWord"));
      if (OB_FAIL(common::ObCharset::charset_convert(
                                       allocator,
                                       word.get_word().get_string(),
                                       word.get_collation_type(),
                                       stopword_type_.get_collation_type(),
                                       cmp_str))) {
      }
    }
    if (OB_SUCC(ret)) {
      ObFTWord converted(cmp_str.length(), cmp_str.ptr(), stopword_type_);
      ret = stopword_set_.exist_refactored(converted);
      if (OB_HASH_NOT_EXIST == ret) {
        is_stopword = false;
        ret = OB_SUCCESS;
      } else if (OB_HASH_EXIST == ret) {
        is_stopword = true;
        ret = OB_SUCCESS;
      } else if (OB_SUCC(ret)) {
        ret = OB_ERR_UNEXPECTED;
      } else {
      }
    }
  }
  return ret;
}

////////////////////////////////////////////////////////////////////////////////
// class ObAddWord
ObAddWord::ObAddWord(
    const ObFTParserProperty &property,
    const ObObjMeta &meta,
    const ObAddWordFlag &flag,
    common::ObIAllocator &allocator,
    ObFTWordMap &word_map)
  : word_meta_(meta),
    allocator_(allocator),
    word_map_(word_map),
    min_max_word_cnt_(0),
    non_stopword_cnt_(0),
    stopword_cnt_(0),
    min_token_size_(property.min_token_size_),
    max_token_size_(property.max_token_size_),
    flag_(flag)
{
}

int ObAddWord::process_word(
    const char *word,
    const int64_t word_len,
    const int64_t char_cnt,
    const int64_t word_freq)
{
  int ret = OB_SUCCESS;
  bool is_stopword = false;
  ObFTWord src_word(word_len, word, word_meta_);
  ObFTWord dst_word;
  if (OB_ISNULL(word) || OB_UNLIKELY(0 >= word_len || 0 >= char_cnt || 0 >= word_freq)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (is_min_max_word(char_cnt)) {
    ++min_max_word_cnt_;
  } else if (OB_FAIL(casedown_word(src_word, dst_word))) {
  } else if (OB_FAIL(check_stopword(dst_word, is_stopword))) {
  } else if (OB_UNLIKELY(is_stopword)) {
    ++stopword_cnt_;
  } else if (OB_FAIL(groupby_word(dst_word, word_freq))) {
  } else {
    non_stopword_cnt_ += word_freq;
  }
  return ret;
}

bool ObAddWord::is_min_max_word(const int64_t c_len) const
{
  return flag_.min_max_word() && (c_len < min_token_size_ || c_len > max_token_size_);
}

int ObAddWord::casedown_word(const ObFTWord &src, ObFTWord &dst)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(src.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (flag_.casedown()) {
    if (!casedown_may_change(src.get_word().get_string())) {
      // No character in the token has a case mapping, so the conversion result
      // is identical to the input and its allocation can be skipped.
      dst = src;
    } else {
      ObString dst_str;
      if (OB_FAIL(ObCharset::tolower(
                      word_meta_.get_collation_type(),
                      src.get_word().get_string(),
                      dst_str,
                      allocator_))) {
      } else {
        ObFTWord tmp(dst_str.length(), dst_str.ptr(), word_meta_);
        dst = tmp;
      }
    }
  } else {
    dst = src;
  }
  return ret;
}

int ObAddWord::check_stopword(const ObFTWord &ft_word, bool &is_stopword)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(ft_word.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (flag_.stopword()) {
    ObStopWordChecker *stop_word_checker = ObFTParseData::instance().stop_word_checker();
    if (OB_ISNULL(stop_word_checker)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(stop_word_checker->check_stopword(ft_word, is_stopword))) {
    }
  }
  return ret;
}

int ObAddWord::groupby_word(const ObFTWord &word, const int64_t word_freq)
{
  int ret = OB_SUCCESS;
  int64_t word_count = 0;
  if (OB_UNLIKELY(word.empty() || word_freq <= 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (!flag_.groupby_word()) {
    if (OB_FAIL(word_map_.set_refactored(word, 1/*word count*/))) {
    }
  } else if (OB_SUCC(word_map_.set_refactored(word, 1/*first word count*/))) {
    // The word was not in the map yet, so the single insert above is enough.
    // Only repeated words need the extra read-modify-write round trip.
  } else if (OB_HASH_EXIST != ret) {
    // keep the unexpected error
  } else if (OB_FAIL(word_map_.get_refactored(word, word_count))) {
  } else if (FALSE_IT(word_count += word_freq)) {
  } else if (OB_FAIL(word_map_.set_refactored(word, word_count, 1/*overwrite*/))) {
  }
  return ret;
}

} // end namespace storage
} // end namespace oceanbase
