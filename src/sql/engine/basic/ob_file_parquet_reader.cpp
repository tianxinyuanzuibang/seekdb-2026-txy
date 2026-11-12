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

#include "sql/engine/basic/ob_file_parquet_reader.h"

#include <sys/stat.h>

#include <algorithm>
#include <cstring>

#include "share/ob_errno.h"

namespace oceanbase
{
namespace sql
{

namespace
{

// The parquet physical types.
enum ParquetType {
  PARQUET_BOOLEAN = 0,
  PARQUET_INT32 = 1,
  PARQUET_INT64 = 2,
  PARQUET_INT96 = 3,
  PARQUET_FLOAT = 4,
  PARQUET_DOUBLE = 5,
  PARQUET_BYTE_ARRAY = 6,
  PARQUET_FIXED_LEN_BYTE_ARRAY = 7,
};

// The parquet encodings.
enum ParquetEncoding {
  ENCODING_PLAIN = 0,
  ENCODING_PLAIN_DICTIONARY = 2,
  ENCODING_RLE = 3,
  ENCODING_BIT_PACKED = 4,
  ENCODING_DELTA_BINARY_PACKED = 5,
  ENCODING_DELTA_LENGTH_BYTE_ARRAY = 6,
  ENCODING_DELTA_BYTE_ARRAY = 7,
  ENCODING_RLE_DICTIONARY = 8,
};

enum ParquetCodec {
  CODEC_UNCOMPRESSED = 0,
  CODEC_SNAPPY = 1,
  CODEC_GZIP = 2,
  CODEC_LZO = 3,
  CODEC_BROTLI = 4,
  CODEC_LZ4 = 5,
  CODEC_ZSTD = 6,
  CODEC_LZ4_RAW = 7,
};

// The parquet page types.
enum ParquetPageType {
  PAGE_DATA_V1 = 0,
  PAGE_INDEX = 1,
  PAGE_DICTIONARY = 2,
  PAGE_DATA_V2 = 3,
};

// A reader of the thrift compact protocol.  It is used to read the footer of
// the file, which holds the schema and the row group metadata.
class CompactReader
{
public:
  CompactReader(const unsigned char *buf, int64_t len)
    : buf_(buf), len_(len), pos_(0), last_id_(0)
  {}

  int64_t pos() const { return pos_; }
  bool eof() const { return pos_ >= len_; }

  // Saves the field id state of the enclosing struct.
  int16_t push_struct()
  {
    const int16_t saved = last_id_;
    last_id_ = 0;
    return saved;
  }

  void pop_struct(int16_t saved) { last_id_ = saved; }

  int read_byte(unsigned char &b)
  {
    if (pos_ >= len_) {
      return OB_INVALID_DATA;
    }
    b = buf_[pos_++];
    return OB_SUCCESS;
  }

  int read_varint(uint64_t &v)
  {
    int ret = OB_SUCCESS;
    v = 0;
    int shift = 0;
    bool done = false;
    while (OB_SUCC(ret) && !done && shift < 64) {
      unsigned char b = 0;
      if (OB_FAIL(read_byte(b))) {
      } else {
        v |= static_cast<uint64_t>(b & 0x7f) << shift;
        shift += 7;
        done = (0 == (b & 0x80));
      }
    }
    if (OB_SUCC(ret) && !done) {
      ret = OB_INVALID_DATA;
    }
    return ret;
  }

  int read_zigzag(int64_t &v)
  {
    int ret = OB_SUCCESS;
    uint64_t u = 0;
    if (OB_FAIL(read_varint(u))) {
    } else {
      v = static_cast<int64_t>((u >> 1) ^ (~(u & 1) + 1));
    }
    return ret;
  }

  int read_binary(std::string &s)
  {
    int ret = OB_SUCCESS;
    uint64_t n = 0;
    if (OB_FAIL(read_varint(n))) {
    } else if (pos_ + static_cast<int64_t>(n) > len_) {
      ret = OB_INVALID_DATA;
    } else {
      s.assign(reinterpret_cast<const char *>(buf_ + pos_), static_cast<size_t>(n));
      pos_ += static_cast<int64_t>(n);
    }
    return ret;
  }

  // Reads the header of the next field.  `stop` is set when the struct ends.
  int next_field(int16_t &field_id, int &type, bool &stop)
  {
    int ret = OB_SUCCESS;
    stop = false;
    unsigned char h = 0;
    if (OB_FAIL(read_byte(h))) {
    } else {
      type = h & 0x0f;
      const int delta = h >> 4;
      if (0 == type) {
        // The struct ends, the field id is not read.
        stop = true;
      } else if (0 == delta) {
        int64_t id = 0;
        if (OB_FAIL(read_zigzag(id))) {
        } else {
          field_id = static_cast<int16_t>(id);
          last_id_ = field_id;
        }
      } else {
        field_id = static_cast<int16_t>(last_id_ + delta);
        last_id_ = field_id;
      }
    }
    return ret;
  }

  int read_list_header(int &elem_type, int64_t &size)
  {
    int ret = OB_SUCCESS;
    unsigned char h = 0;
    if (OB_FAIL(read_byte(h))) {
    } else {
      elem_type = h & 0x0f;
      size = h >> 4;
      if (15 == size) {
        uint64_t n = 0;
        if (OB_FAIL(read_varint(n))) {
        } else {
          size = static_cast<int64_t>(n);
        }
      }
    }
    return ret;
  }

  // Skips a value of the given thrift type.
  int skip_value(int type)
  {
    int ret = OB_SUCCESS;
    switch (type) {
      case 1:
      case 2: break;  // the boolean value sits in the type
      case 3: { unsigned char b = 0; ret = read_byte(b); break; }
      case 4:
      case 5:
      case 6: { int64_t v = 0; ret = read_zigzag(v); break; }
      case 7: {
        // A double is eight bytes.
        if (pos_ + 8 > len_) {
          ret = OB_INVALID_DATA;
        } else {
          pos_ += 8;
        }
        break;
      }
      case 8: { std::string s; ret = read_binary(s); break; }
      case 9:
      case 10: {
        int et = 0;
        int64_t n = 0;
        if (OB_FAIL(read_list_header(et, n))) {
        } else {
          for (int64_t i = 0; OB_SUCC(ret) && i < n; ++i) {
            ret = skip_value(et);
          }
        }
        break;
      }
      case 11: {
        // A map is a list of structs that hold the key and the value.
        int et = 0;
        int64_t n = 0;
        if (OB_FAIL(read_list_header(et, n))) {
        } else {
          for (int64_t i = 0; OB_SUCC(ret) && i < n; ++i) {
            ret = skip_value(12);
          }
        }
        break;
      }
      case 12: {
        int16_t id = 0;
        int t = 0;
        bool stop = false;
        const int16_t saved = last_id_;
        last_id_ = 0;
        while (OB_SUCC(ret) && !stop) {
          if (OB_FAIL(next_field(id, t, stop))) {
          } else if (!stop && OB_FAIL(skip_value(t))) {
          }
        }
        last_id_ = saved;
        break;
      }
      default: ret = OB_INVALID_DATA; break;
    }
    return ret;
  }

private:
  const unsigned char *buf_;
  int64_t len_;
  int64_t pos_;
  int16_t last_id_;
};

// Decompresses a snappy block.
int snappy_uncompress(const unsigned char *in, int64_t in_len, std::string &out)
{
  int ret = OB_SUCCESS;
  int64_t pos = 0;
  uint64_t expected = 0;
  int shift = 0;
  bool done = false;
  while (pos < in_len && !done) {
    expected |= static_cast<uint64_t>(in[pos] & 0x7f) << shift;
    shift += 7;
    done = (0 == (in[pos] & 0x80));
    ++pos;
  }
  out.clear();
  out.reserve(static_cast<size_t>(expected));
  while (OB_SUCC(ret) && pos < in_len) {
    const unsigned char tag = in[pos++];
    const int kind = tag & 0x03;
    if (0 == kind) {
      int64_t len = (tag >> 2) + 1;
      if (len > 60) {
        const int extra = static_cast<int>(len - 60);
        len = 0;
        for (int i = 0; i < extra && pos < in_len; ++i) {
          len |= static_cast<int64_t>(in[pos++]) << (8 * i);
        }
        len += 1;
      }
      if (pos + len > in_len) {
        ret = OB_INVALID_DATA;
      } else {
        out.append(reinterpret_cast<const char *>(in + pos), static_cast<size_t>(len));
        pos += len;
      }
    } else {
      int64_t len = 0;
      int64_t offset = 0;
      if (1 == kind) {
        len = ((tag >> 2) & 0x07) + 4;
        if (pos >= in_len) {
          ret = OB_INVALID_DATA;
          break;
        }
        offset = ((static_cast<int64_t>(tag) >> 5) << 8) | in[pos++];
      } else if (2 == kind) {
        len = (tag >> 2) + 1;
        if (pos + 2 > in_len) {
          ret = OB_INVALID_DATA;
          break;
        }
        offset = static_cast<int64_t>(in[pos]) | (static_cast<int64_t>(in[pos + 1]) << 8);
        pos += 2;
      } else {
        len = (tag >> 2) + 1;
        if (pos + 4 > in_len) {
          ret = OB_INVALID_DATA;
          break;
        }
        offset = static_cast<int64_t>(in[pos]) | (static_cast<int64_t>(in[pos + 1]) << 8)
                 | (static_cast<int64_t>(in[pos + 2]) << 16)
                 | (static_cast<int64_t>(in[pos + 3]) << 24);
        pos += 4;
      }
      if (offset <= 0 || static_cast<int64_t>(out.size()) < offset) {
        ret = OB_INVALID_DATA;
      } else {
        const size_t start = out.size() - static_cast<size_t>(offset);
        for (int64_t i = 0; i < len; ++i) {
          out.push_back(out[start + static_cast<size_t>(i)]);
        }
      }
    }
  }
  if (OB_SUCC(ret) && out.size() != expected) {
    ret = OB_INVALID_DATA;
  }
  return ret;
}

// Decodes the RLE / bit packed hybrid the definition levels use.
int decode_rle_bp(const unsigned char *buf, int64_t len, int width, int64_t count,
                  std::vector<int64_t> &out)
{
  int ret = OB_SUCCESS;
  int64_t pos = 0;
  out.clear();
  const int byte_width = (width + 7) / 8;
  while (OB_SUCC(ret) && pos < len && static_cast<int64_t>(out.size()) < count) {
    uint64_t header = 0;
    int shift = 0;
    bool done = false;
    while (pos < len && !done) {
      header |= static_cast<uint64_t>(buf[pos] & 0x7f) << shift;
      shift += 7;
      done = (0 == (buf[pos] & 0x80));
      ++pos;
    }
    if (header & 1) {
      // A bit packed run holds groups of eight values.
      const int64_t groups = static_cast<int64_t>(header >> 1);
      const int64_t total = groups * 8;
      const int64_t run_bytes = groups * width;
      if (pos + run_bytes > len) {
        ret = OB_INVALID_DATA;
        break;
      }
      const uint64_t mask = (width >= 64) ? ~static_cast<uint64_t>(0)
                             : ((static_cast<uint64_t>(1) << width) - 1);
      for (int64_t i = 0; i < total && static_cast<int64_t>(out.size()) < count; ++i) {
        const int64_t bit = i * width;
        const int64_t byte = pos + (bit >> 3);
        uint64_t chunk = 0;
        for (int b = 0; b < 8 && byte + b < len; ++b) {
          chunk |= static_cast<uint64_t>(buf[byte + b]) << (8 * b);
        }
        out.push_back(static_cast<int64_t>((chunk >> (bit & 7)) & mask));
      }
      pos += run_bytes;
    } else {
      // An RLE run holds one value.
      const int64_t run = static_cast<int64_t>(header >> 1);
      if (pos + byte_width > len) {
        ret = OB_INVALID_DATA;
        break;
      }
      int64_t v = 0;
      for (int b = 0; b < byte_width; ++b) {
        v |= static_cast<int64_t>(buf[pos + b]) << (8 * b);
      }
      pos += byte_width;
      for (int64_t i = 0; i < run && static_cast<int64_t>(out.size()) < count; ++i) {
        out.push_back(v);
      }
    }
  }
  return ret;
}

// The header of a data page.
struct PageInfo
{
  int type;
  int64_t uncompressed;
  int64_t compressed;
  int64_t num_values;
  int encoding;
  int def_encoding;
  int64_t def_len;   // the data page v2
  int64_t rep_len;   // the data page v2
  bool v2_compressed;
  PageInfo()
    : type(-1), uncompressed(0), compressed(0), num_values(0), encoding(0),
      def_encoding(ENCODING_RLE), def_len(0), rep_len(0), v2_compressed(true)
  {}
};

int parse_data_page_header(CompactReader &rd, PageInfo &page)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int type = 0;
  bool stop = false;
  while (OB_SUCC(ret)) {
    if (OB_FAIL(rd.next_field(id, type, stop))) {
    } else if (stop) {
      break;
    } else if (1 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.num_values = v;
      }
    } else if (2 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.encoding = static_cast<int>(v);
      }
    } else if (3 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.def_encoding = static_cast<int>(v);
      }
    } else if (OB_FAIL(rd.skip_value(type))) {
    }
  }
  return ret;
}

int parse_v2_page_header(CompactReader &rd, PageInfo &page)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int type = 0;
  bool stop = false;
  while (OB_SUCC(ret)) {
    if (OB_FAIL(rd.next_field(id, type, stop))) {
    } else if (stop) {
      break;
    } else if (1 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.num_values = v;
      }
    } else if (4 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.encoding = static_cast<int>(v);
      }
    } else if (5 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.def_len = v;
      }
    } else if (6 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.rep_len = v;
      }
    } else if (7 == id && (1 == type || 2 == type)) {
      page.v2_compressed = (1 == type);
    } else if (OB_FAIL(rd.skip_value(type))) {
    }
  }
  return ret;
}

int parse_page_header(CompactReader &rd, PageInfo &page)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int type = 0;
  bool stop = false;
  while (OB_SUCC(ret)) {
    if (OB_FAIL(rd.next_field(id, type, stop))) {
    } else if (stop) {
      break;
    } else if (1 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.type = static_cast<int>(v);
      }
    } else if (2 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.uncompressed = v;
      }
    } else if (3 == id && 5 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
      } else {
        page.compressed = v;
      }
    } else if (5 == id && 12 == type) {
      const int16_t outer = rd.push_struct();
      const int dph_ret = parse_data_page_header(rd, page);
      rd.pop_struct(outer);
      if (OB_FAIL(dph_ret)) {
      }
    } else if (8 == id && 12 == type) {
      const int16_t outer = rd.push_struct();
      const int v2_ret = parse_v2_page_header(rd, page);
      rd.pop_struct(outer);
      if (OB_FAIL(v2_ret)) {
      }
    } else if (OB_FAIL(rd.skip_value(type))) {
    }
  }
  return ret;
}

// Reads one PLAIN encoded value and returns its text form.
int read_plain_value(int ptype, const unsigned char *p, int64_t avail,
                     std::string &out, int64_t &used)
{
  int ret = OB_SUCCESS;
  used = 0;
  char buf[64];
  switch (ptype) {
    case PARQUET_BOOLEAN: {
      if (avail < 1) {
        ret = OB_INVALID_DATA;
      } else {
        out = (p[0] & 1) ? "true" : "false";
        used = 1;
      }
      break;
    }
    case PARQUET_INT32: {
      if (avail < 4) {
        ret = OB_INVALID_DATA;
      } else {
        const int32_t v = static_cast<int32_t>(
            static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8)
            | (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24));
        snprintf(buf, sizeof(buf), "%d", v);
        out = buf;
        used = 4;
      }
      break;
    }
    case PARQUET_INT64: {
      if (avail < 8) {
        ret = OB_INVALID_DATA;
      } else {
        uint64_t u = 0;
        for (int i = 0; i < 8; ++i) {
          u |= static_cast<uint64_t>(p[i]) << (8 * i);
        }
        snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(static_cast<int64_t>(u)));
        out = buf;
        used = 8;
      }
      break;
    }
    case PARQUET_FLOAT: {
      if (avail < 4) {
        ret = OB_INVALID_DATA;
      } else {
        uint32_t u = 0;
        for (int i = 0; i < 4; ++i) {
          u |= static_cast<uint32_t>(p[i]) << (8 * i);
        }
        float f = 0;
        memcpy(&f, &u, sizeof(f));
        snprintf(buf, sizeof(buf), "%.9g", static_cast<double>(f));
        out = buf;
        used = 4;
      }
      break;
    }
    case PARQUET_DOUBLE: {
      if (avail < 8) {
        ret = OB_INVALID_DATA;
      } else {
        uint64_t u = 0;
        for (int i = 0; i < 8; ++i) {
          u |= static_cast<uint64_t>(p[i]) << (8 * i);
        }
        double d = 0;
        memcpy(&d, &u, sizeof(d));
        snprintf(buf, sizeof(buf), "%.17g", d);
        out = buf;
        used = 8;
      }
      break;
    }
    case PARQUET_BYTE_ARRAY: {
      if (avail < 4) {
        ret = OB_INVALID_DATA;
      } else {
        const uint32_t n = static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8)
                           | (static_cast<uint32_t>(p[2]) << 16)
                           | (static_cast<uint32_t>(p[3]) << 24);
        if (avail < 4 + static_cast<int64_t>(n)) {
          ret = OB_INVALID_DATA;
        } else {
          out.assign(reinterpret_cast<const char *>(p + 4), n);
          used = 4 + n;
        }
      }
      break;
    }
    case PARQUET_FIXED_LEN_BYTE_ARRAY: {
      // The length is not part of the page, so it cannot be decoded here.
      ret = OB_NOT_SUPPORTED;
      break;
    }
    default:
      ret = OB_NOT_SUPPORTED;
      break;
  }
  return ret;
}

}  // namespace

ObFileParquetReader::ObFileParquetReader()
  : fp_(nullptr),
    file_size_(0),
    file_mtime_(0),
    file_changed_(false),
    options_(),
    schema_columns_(),
    row_groups_(),
    projected_(),
    total_rows_(0),
    current_group_(0),
    current_row_(0),
    group_values_(),
    group_nulls_()
{
}

ObFileParquetReader::~ObFileParquetReader()
{
  close();
}

void ObFileParquetReader::close()
{
  if (nullptr != fp_) {
    ::fclose(fp_);
    fp_ = nullptr;
  }
}

int ObFileParquetReader::read_at(int64_t offset, int64_t len, std::string &buf)
{
  int ret = OB_SUCCESS;
  buf.clear();
  if (offset < 0 || len < 0 || offset + len > file_size_) {
    ret = OB_INVALID_DATA;
  } else if (0 == len) {
  } else {
    buf.resize(static_cast<size_t>(len));
    if (OB_FAIL(::fseek(fp_, static_cast<long>(offset), SEEK_SET))) {
      ret = OB_IO_ERROR;
    } else {
      const size_t read_bytes = ::fread(&buf[0], 1, static_cast<size_t>(len), fp_);
      if (static_cast<int64_t>(read_bytes) != len) {
        ret = OB_IO_ERROR;
      } else {
        buf.resize(static_cast<size_t>(read_bytes));
      }
    }
  }
  return ret;
}

int ObFileParquetReader::open(const common::ObString &path,
                              const ObFileReadOptions &options,
                              common::ObIAllocator &allocator,
                              ObFileSchema &schema,
                              const std::vector<int64_t> &projected)
{
  int ret = OB_SUCCESS;
  UNUSED(allocator);
  options_ = options;
  projected_ = projected;
  fp_ = ::fopen(path.ptr(), "rb");
  if (nullptr == fp_) {
    ret = OB_FILE_NOT_EXIST;
    LOG_USER_ERROR(OB_FILE_NOT_EXIST, path.ptr());
    return ret;
  }
  struct stat stat_buf;
  if (0 == ::stat(path.ptr(), &stat_buf)) {
    file_size_ = stat_buf.st_size;
    file_mtime_ = stat_buf.st_mtime;
  }
  if (OB_FAIL(read_footer(schema))) {
  } else if (OB_FAIL(read_row_groups())) {
  } else {
    // The projection is resolved against the schema.
    if (projected_.empty()) {
      for (size_t i = 0; i < schema_columns_.size(); ++i) {
        projected_.push_back(static_cast<int64_t>(i));
      }
    }
  }
  if (OB_FAIL(ret)) {
    close();
  }
  return ret;
}

int ObFileParquetReader::open_for_scan(const common::ObString &path,
                                        const ObFileReadOptions &options,
                                        common::ObIAllocator &allocator)
{
  int ret = OB_SUCCESS;
  UNUSED(allocator);
  options_ = options;
  fp_ = ::fopen(path.ptr(), "rb");
  if (nullptr == fp_) {
    ret = OB_FILE_NOT_EXIST;
    LOG_USER_ERROR(OB_FILE_NOT_EXIST, path.ptr());
    return ret;
  }
  struct stat stat_buf;
  if (0 == ::stat(path.ptr(), &stat_buf)) {
    file_size_ = stat_buf.st_size;
    file_mtime_ = stat_buf.st_mtime;
  }
  // The footer and the row groups were read when the statement was resolved,
  // they are read again because the reader holds them itself.
  ObFileSchema schema;
  if (OB_FAIL(read_footer(schema))) {
  } else if (OB_FAIL(read_row_groups())) {
  } else {
    if (projected_.empty()) {
      for (size_t i = 0; i < schema_columns_.size(); ++i) {
        projected_.push_back(static_cast<int64_t>(i));
      }
    }
  }
  if (OB_FAIL(ret)) {
    close();
  }
  return ret;
}

int ObFileParquetReader::read_footer(ObFileSchema &schema)
{
  int ret = OB_SUCCESS;
  std::string tail;
  if (file_size_ < 12) {
    ret = OB_INVALID_DATA;
    LOG_USER_ERROR(OB_INVALID_DATA, "the parquet file is too small");
    return ret;
  }
  if (OB_FAIL(read_at(file_size_ - 8, 8, tail))) {
    return ret;
  }
  // The last eight bytes of the file hold the length of the footer and the
  // magic "PAR1".
  if ('P' != tail[4] || 'A' != tail[5] || 'R' != tail[6] || '1' != tail[7]) {
    ret = OB_INVALID_DATA;
    LOG_USER_ERROR(OB_INVALID_DATA, "the file is not a parquet file");
    return ret;
  }
  const int64_t footer_len = static_cast<int64_t>(
      static_cast<uint32_t>(tail[0]) | (static_cast<uint32_t>(tail[1]) << 8)
      | (static_cast<uint32_t>(tail[2]) << 16) | (static_cast<uint32_t>(tail[3]) << 24));
  if (footer_len <= 0 || footer_len > file_size_ - 8) {
    ret = OB_INVALID_DATA;
    return ret;
  }
  std::string footer;
  if (OB_FAIL(read_at(file_size_ - 8 - footer_len, footer_len, footer))) {
    return ret;
  }
  CompactReader rd(reinterpret_cast<const unsigned char *>(footer.data()),
                    static_cast<int64_t>(footer.size()));
  int16_t id = 0;
  int type = 0;
  bool stop = false;
  // The schema of the file.
  while (OB_SUCC(ret)) {
    if (OB_FAIL(rd.next_field(id, type, stop))) {
      break;
    } else if (stop) {
      break;
    } else if (2 == id && 9 == type) {
      // The list of the schema elements.
      int et = 0;
      int64_t n = 0;
      if (OB_FAIL(rd.read_list_header(et, n))) {
        break;
      }
      for (int64_t i = 0; OB_SUCC(ret) && i < n; ++i) {
        ParquetColumnMeta column;
        std::string name;
        bool has_type = false;
        bool optional = false;
        int32_t num_children = 0;
        const int16_t outer = rd.push_struct();
        while (OB_SUCC(ret)) {
          int16_t sid = 0;
          int stype = 0;
          bool sstop = false;
          if (OB_FAIL(rd.next_field(sid, stype, sstop))) {
            break;
          } else if (sstop) {
            break;
          } else if (1 == sid && 5 == stype) {
            int64_t v = 0;
            if (OB_FAIL(rd.read_zigzag(v))) {
              break;
            } else {
              column.type = static_cast<int>(v);
              has_type = true;
            }
          } else if (3 == sid && 5 == stype) {
            int64_t v = 0;
            if (OB_FAIL(rd.read_zigzag(v))) {
              break;
            } else {
              optional = (1 == v);
            }
          } else if (4 == sid && 8 == stype) {
            if (OB_FAIL(rd.read_binary(name))) {
              break;
            }
          } else if (5 == sid && 5 == stype) {
            int64_t v = 0;
            if (OB_FAIL(rd.read_zigzag(v))) {
              break;
            } else {
              num_children = static_cast<int32_t>(v);
            }
          } else if (OB_FAIL(rd.skip_value(stype))) {
            break;
          }
        }
        rd.pop_struct(outer);
        if (OB_FAIL(ret)) {
          break;
        }
        // A leaf column carries a type; a group carries children.
        if (has_type && 0 == num_children) {
          column.name = name;
          column.optional = optional;
          schema_columns_.push_back(column);
        }
      }
    } else if (OB_FAIL(rd.skip_value(type))) {
      break;
    }
  }
  if (OB_FAIL(ret)) {
    return ret;
  }
  if (schema_columns_.empty()) {
    ret = OB_INVALID_DATA;
    LOG_USER_ERROR(OB_INVALID_DATA, "the parquet file holds no column");
    return ret;
  }
  schema.columns.clear();
  for (size_t i = 0; i < schema_columns_.size(); ++i) {
    ObFileColumn column(schema_columns_[i].name);
    column.nullable = schema_columns_[i].optional;
    switch (schema_columns_[i].type) {
      case PARQUET_BOOLEAN: column.type = OB_FILE_TYPE_BOOLEAN; break;
      case PARQUET_INT32:
      case PARQUET_INT64: column.type = OB_FILE_TYPE_BIGINT; break;
      case PARQUET_FLOAT:
      case PARQUET_DOUBLE: column.type = OB_FILE_TYPE_DOUBLE; break;
      case PARQUET_BYTE_ARRAY: column.type = OB_FILE_TYPE_VARCHAR; break;
      default: column.type = OB_FILE_TYPE_VARCHAR; break;
    }
    schema.columns.push_back(column);
  }
  return ret;
}

int ObFileParquetReader::read_row_groups()
{
  int ret = OB_SUCCESS;
  // The row group metadata sits in the same footer.
  std::string tail;
  if (OB_FAIL(read_at(file_size_ - 8, 8, tail))) {
    return ret;
  }
  const int64_t footer_len = static_cast<int64_t>(
      static_cast<uint32_t>(tail[0]) | (static_cast<uint32_t>(tail[1]) << 8)
      | (static_cast<uint32_t>(tail[2]) << 16) | (static_cast<uint32_t>(tail[3]) << 24));
  std::string footer;
  if (OB_FAIL(read_at(file_size_ - 8 - footer_len, footer_len, footer))) {
    return ret;
  }
  CompactReader rd(reinterpret_cast<const unsigned char *>(footer.data()),
                    static_cast<int64_t>(footer.size()));
  int16_t id = 0;
  int type = 0;
  bool stop = false;
  while (OB_SUCC(ret)) {
    if (OB_FAIL(rd.next_field(id, type, stop))) {
      break;
    } else if (stop) {
      break;
    } else if (3 == id && 6 == type) {
      int64_t v = 0;
      if (OB_FAIL(rd.read_zigzag(v))) {
        break;
      } else {
        total_rows_ = v;
      }
    } else if (4 == id && 9 == type) {
      int et = 0;
      int64_t n = 0;
      if (OB_FAIL(rd.read_list_header(et, n))) {
        break;
      }
      for (int64_t g = 0; OB_SUCC(ret) && g < n; ++g) {
        ParquetRowGroup group;
        const int16_t rg_outer = rd.push_struct();
        while (OB_SUCC(ret)) {
          int16_t rid = 0;
          int rtype = 0;
          bool rstop = false;
          if (OB_FAIL(rd.next_field(rid, rtype, rstop))) {
            break;
          } else if (rstop) {
            break;
          } else if (1 == rid && 9 == rtype) {
            // The list of the column chunks.
            int cet = 0;
            int64_t cn = 0;
            if (OB_FAIL(rd.read_list_header(cet, cn))) {
              break;
            }
            for (int64_t c = 0; OB_SUCC(ret) && c < cn; ++c) {
              ParquetColumnMeta column;
              const int16_t cc_outer = rd.push_struct();
              while (OB_SUCC(ret)) {
                int16_t cid = 0;
                int ctype = 0;
                bool cstop = false;
                if (OB_FAIL(rd.next_field(cid, ctype, cstop))) {
                  break;
                } else if (cstop) {
                  break;
                } else if (3 == cid && 12 == ctype) {
                  // The metadata of the column chunk.
                  const int16_t meta_outer = rd.push_struct();
                  while (OB_SUCC(ret)) {
                    int16_t mid = 0;
                    int mtype = 0;
                    bool mstop = false;
                    if (OB_FAIL(rd.next_field(mid, mtype, mstop))) {
                      break;
                    } else if (mstop) {
                      break;
                    } else if (1 == mid && 5 == mtype) {
                      int64_t v = 0;
                      if (OB_FAIL(rd.read_zigzag(v))) {
                        break;
                      } else {
                        column.type = static_cast<int>(v);
                      }
                    } else if (4 == mid && 5 == mtype) {
                      int64_t v = 0;
                      if (OB_FAIL(rd.read_zigzag(v))) {
                        break;
                      } else {
                        column.codec = static_cast<int>(v);
                      }
                    } else if (5 == mid && 6 == mtype) {
                      int64_t v = 0;
                      if (OB_FAIL(rd.read_zigzag(v))) {
                        break;
                      } else {
                        column.num_values = v;
                      }
                    } else if (9 == mid && 6 == mtype) {
                      int64_t v = 0;
                      if (OB_FAIL(rd.read_zigzag(v))) {
                        break;
                      } else {
                        column.data_page_offset = v;
                      }
                    } else if (OB_FAIL(rd.skip_value(mtype))) {
                      break;
                    }
                  }
                  rd.pop_struct(meta_outer);
                } else if (OB_FAIL(rd.skip_value(ctype))) {
                  break;
                }
              }
              rd.pop_struct(cc_outer);
              if (OB_FAIL(ret)) {
                break;
              }
              // The chunk holds the type of the column.
              if (column.type < 0 && c < static_cast<int64_t>(schema_columns_.size())) {
                column.type = schema_columns_[static_cast<size_t>(c)].type;
              }
              group.columns.push_back(column);
            }
          } else if (3 == rid && 6 == rtype) {
            int64_t v = 0;
            if (OB_FAIL(rd.read_zigzag(v))) {
              break;
            } else {
              group.num_rows = v;
            }
          } else if (OB_FAIL(rd.skip_value(rtype))) {
            break;
          }
        }
        rd.pop_struct(rg_outer);
        if (OB_FAIL(ret)) {
          break;
        }
        row_groups_.push_back(group);
      }
    } else if (OB_FAIL(rd.skip_value(type))) {
      break;
    }
  }
  if (OB_FAIL(ret)) {
    return ret;
  }
  if (row_groups_.empty()) {
    ret = OB_INVALID_DATA;
    LOG_USER_ERROR(OB_INVALID_DATA, "the parquet file holds no row group");
  }
  return ret;
}

int ObFileParquetReader::decode_column_chunk(const ParquetColumnMeta &meta,
                                             std::vector<std::string> &values,
                                             std::vector<bool> &nulls)
{
  int ret = OB_SUCCESS;
  if (meta.data_page_offset < 0) {
    ret = OB_INVALID_DATA;
    return ret;
  }
  int64_t offset = meta.data_page_offset;
  int64_t produced = 0;
  const int64_t file_end = file_size_ - 8;
  while (OB_SUCC(ret) && produced < meta.num_values && offset < file_end) {
    // The header of the page.
    std::string header;
    if (OB_FAIL(read_at(offset, std::min<int64_t>(file_end - offset, 1024 * 1024), header))) {
      return ret;
    }
    CompactReader rd(reinterpret_cast<const unsigned char *>(header.data()),
                      static_cast<int64_t>(header.size()));
    PageInfo page;
    if (OB_FAIL(parse_page_header(rd, page))) {
      return ret;
    }
    const int64_t page_start = offset + rd.pos();
    if (PAGE_DATA_V1 != page.type && PAGE_DATA_V2 != page.type) {
      // Skip the index and the dictionary pages.
      offset = page_start + page.compressed;
      continue;
    }
    if (page_start + page.compressed > file_end) {
      ret = OB_INVALID_DATA;
      return ret;
    }
    std::string payload;
    if (OB_FAIL(read_at(page_start, page.compressed, payload))) {
      return ret;
    }
    const unsigned char *data = reinterpret_cast<const unsigned char *>(payload.data());
    const int64_t data_len = static_cast<int64_t>(payload.size());
    const unsigned char *values_ptr = nullptr;
    int64_t values_len = 0;
    std::vector<int64_t> defs;
    if (PAGE_DATA_V2 == page.type) {
      // The levels of the data page v2 sit uncompressed in front of the values.
      if (page.rep_len + page.def_len > data_len) {
        ret = OB_INVALID_DATA;
        return ret;
      }
      if (meta.optional && page.def_len > 0) {
        if (OB_FAIL(decode_rle_bp(data + page.rep_len, page.def_len, 1,
                                  page.num_values, defs))) {
          return ret;
        }
      }
      values_ptr = data + page.rep_len + page.def_len;
      values_len = data_len - page.rep_len - page.def_len;
      if (page.v2_compressed && CODEC_SNAPPY == meta.codec) {
        std::string plain;
        if (OB_FAIL(snappy_uncompress(values_ptr, values_len, plain))) {
          return ret;
        }
        values_ptr = reinterpret_cast<const unsigned char *>(plain.data());
        values_len = static_cast<int64_t>(plain.size());
      }
    } else {
      // The data page v1 holds the whole page compressed.
      const unsigned char *payload_ptr = data;
      int64_t payload_len = data_len;
      if (CODEC_SNAPPY == meta.codec) {
        std::string plain;
        if (OB_FAIL(snappy_uncompress(data, data_len, plain))) {
          return ret;
        }
        payload_ptr = reinterpret_cast<const unsigned char *>(plain.data());
        payload_len = static_cast<int64_t>(plain.size());
      } else if (CODEC_UNCOMPRESSED != meta.codec) {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "the parquet codec of the file is not supported");
        return ret;
      }
      int64_t pos = 0;
      if (meta.optional) {
        if (pos + 4 > payload_len) {
          ret = OB_INVALID_DATA;
          return ret;
        }
        const int64_t dl_len = static_cast<int64_t>(
            static_cast<uint32_t>(payload_ptr[pos]) | (static_cast<uint32_t>(payload_ptr[pos + 1]) << 8)
            | (static_cast<uint32_t>(payload_ptr[pos + 2]) << 16)
            | (static_cast<uint32_t>(payload_ptr[pos + 3]) << 24));
        pos += 4;
        if (pos + dl_len > payload_len) {
          ret = OB_INVALID_DATA;
          return ret;
        }
        if (OB_FAIL(decode_rle_bp(payload_ptr + pos, dl_len, 1, page.num_values, defs))) {
          return ret;
        }
        pos += dl_len;
      }
      values_ptr = payload_ptr + pos;
      values_len = payload_len - pos;
    }
    // Decode the values of the page.
    if (ENCODING_PLAIN != page.encoding && ENCODING_RLE != page.encoding) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "the parquet encoding of the file is not supported");
      return ret;
    }
    int64_t pos = 0;
    for (int64_t i = 0; i < page.num_values && OB_SUCC(ret); ++i) {
      const bool present = meta.optional
                           ? (i < static_cast<int64_t>(defs.size()) && defs[i] > 0)
                           : true;
      if (!present) {
        values.push_back(std::string());
        nulls.push_back(true);
        continue;
      }
      std::string value;
      int64_t used = 0;
      if (OB_FAIL(read_plain_value(meta.type, values_ptr + pos, values_len - pos,
                                   value, used))) {
        return ret;
      }
      if (0 == used || pos + used > values_len) {
        ret = OB_INVALID_DATA;
        return ret;
      }
      pos += used;
      values.push_back(value);
      nulls.push_back(false);
    }
    if (OB_FAIL(ret)) {
      return ret;
    }
    produced += page.num_values;
    offset = page_start + page.compressed;
  }
  return ret;
}

int ObFileParquetReader::decode_row_group()
{
  int ret = OB_SUCCESS;
  const ParquetRowGroup &group = row_groups_[current_group_];
  group_values_.assign(schema_columns_.size(), std::vector<std::string>());
  group_nulls_.assign(schema_columns_.size(), std::vector<bool>());
  // Only the projected columns are read; the others stay empty.
  for (size_t i = 0; i < projected_.size(); ++i) {
    const int64_t column_index = projected_[i];
    if (column_index < 0 || column_index >= static_cast<int64_t>(group.columns.size())) {
      continue;
    }
    const ParquetColumnMeta &meta = group.columns[static_cast<size_t>(column_index)];
    if (OB_FAIL(decode_column_chunk(meta,
                                    group_values_[static_cast<size_t>(column_index)],
                                    group_nulls_[static_cast<size_t>(column_index)]))) {
      return ret;
    }
  }
  current_row_ = 0;
  return ret;
}

int ObFileParquetReader::next_row(std::vector<std::string> &values, std::vector<bool> &nulls)
{
  int ret = OB_SUCCESS;
  if (nullptr == fp_) {
    ret = OB_ITER_END;
    return ret;
  }
  // Move to the next row group when the current one is exhausted.
  while (current_group_ < row_groups_.size()
         && current_row_ >= row_groups_[current_group_].num_rows) {
    ++current_group_;
    current_row_ = 0;
    group_values_.clear();
    group_nulls_.clear();
  }
  if (current_group_ >= row_groups_.size()) {
    ret = OB_ITER_END;
    return ret;
  }
  // Decode the row group when it is not decoded yet.
  if (group_values_.empty()) {
    if (OB_FAIL(decode_row_group())) {
      return ret;
    }
  }
  const int64_t row = current_row_++;
  values.assign(schema_columns_.size(), std::string());
  nulls.assign(schema_columns_.size(), true);
  for (size_t i = 0; i < schema_columns_.size(); ++i) {
    if (i >= group_values_.size()) {
      continue;
    }
    const std::vector<std::string> &column_values = group_values_[i];
    const std::vector<bool> &column_nulls = group_nulls_[i];
    if (row < static_cast<int64_t>(column_values.size())) {
      values[i] = column_values[static_cast<size_t>(row)];
      nulls[i] = (row < static_cast<int64_t>(column_nulls.size()))
                 ? column_nulls[static_cast<size_t>(row)]
                 : true;
    }
  }
  return ret;
}

}  // namespace sql
}  // namespace oceanbase
