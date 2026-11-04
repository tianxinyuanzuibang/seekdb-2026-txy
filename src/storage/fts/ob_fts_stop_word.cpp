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
    // Every built-in stop word is a lower case ASCII word, and a word of
    // FTS_STOP_WORD_MAX_LENGTH bytes or more can not be one of them.  Lower case
    // the token in place and ask the hash set once instead of walking the whole
    // compile time list for every token.
    const ObString ascii_word = word.get_word().get_string();
    if (ascii_word.length() >= FTS_STOP_WORD_MAX_LENGTH) {
      is_stopword = false;
    } else {
      char lower_buf[FTS_STOP_WORD_MAX_LENGTH];
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
      ObFTWord lookup_word(ascii_word.length(), lookup_ptr, stopword_type_);
      ret = stopword_set_.exist_refactored(lookup_word);
      if (OB_HASH_NOT_EXIST == ret) {
        is_stopword = false;
        ret = OB_SUCCESS;
      } else if (OB_HASH_EXIST == ret) {
        is_stopword = true;
        ret = OB_SUCCESS;
      } else if (OB_SUCC(ret)) {
        ret = OB_ERR_UNEXPECTED;
      }
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
