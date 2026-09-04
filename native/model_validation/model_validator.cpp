#include "model_validator.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace lae {

struct ModelValidationLease::Impl {
  std::string canonical_path;
  std::uint64_t size = 0;
#ifdef _WIN32
  HANDLE handle = INVALID_HANDLE_VALUE;
  BY_HANDLE_FILE_INFORMATION identity{};
#else
  int descriptor = -1;
  dev_t device = 0;
  ino_t inode = 0;
  std::int64_t modified_seconds = 0;
  std::int64_t modified_nanoseconds = 0;
#endif
};

namespace {
#ifndef _WIN32
std::pair<std::int64_t, std::int64_t> modification_time(const struct stat& value) {
#ifdef __APPLE__
  return {value.st_mtimespec.tv_sec, value.st_mtimespec.tv_nsec};
#else
  return {value.st_mtim.tv_sec, value.st_mtim.tv_nsec};
#endif
}
#endif
}  // namespace

ModelValidationLease::ModelValidationLease(std::unique_ptr<Impl> impl)
    : impl_(std::move(impl)) {}

ModelValidationLease::~ModelValidationLease() {
  if (!impl_) return;
#ifdef _WIN32
  if (impl_->handle != INVALID_HANDLE_VALUE) CloseHandle(impl_->handle);
#else
  if (impl_->descriptor >= 0) close(impl_->descriptor);
#endif
}

std::shared_ptr<ModelValidationLease> ModelValidationLease::acquire(
    const std::filesystem::path& canonical_path, std::uint64_t expected_size,
    std::string& error) {
  error.clear();
  auto impl = std::make_unique<Impl>();
  impl->canonical_path = canonical_path.string();
  impl->size = expected_size;
#ifdef _WIN32
  impl->handle = CreateFileW(canonical_path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                             nullptr, OPEN_EXISTING,
                             FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
  if (impl->handle == INVALID_HANDLE_VALUE ||
      !GetFileInformationByHandle(impl->handle, &impl->identity) ||
      (impl->identity.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) != 0 ||
      ((static_cast<std::uint64_t>(impl->identity.nFileSizeHigh) << 32) |
       impl->identity.nFileSizeLow) != expected_size) {
    if (impl->handle != INVALID_HANDLE_VALUE) CloseHandle(impl->handle);
    impl->handle = INVALID_HANDLE_VALUE;
    error = "model could not be locked as the expected regular file";
    return nullptr;
  }
#else
  impl->descriptor = open(canonical_path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  struct stat value{};
  if (impl->descriptor < 0 || fstat(impl->descriptor, &value) != 0 ||
      !S_ISREG(value.st_mode) || value.st_size < 0 ||
      static_cast<std::uint64_t>(value.st_size) != expected_size) {
    if (impl->descriptor >= 0) close(impl->descriptor);
    impl->descriptor = -1;
    error = "model could not be pinned as the expected regular file";
    return nullptr;
  }
  impl->device = value.st_dev;
  impl->inode = value.st_ino;
  const auto modified = modification_time(value);
  impl->modified_seconds = modified.first;
  impl->modified_nanoseconds = modified.second;
#endif
  return std::shared_ptr<ModelValidationLease>(
      new ModelValidationLease(std::move(impl)));
}

const std::string& ModelValidationLease::canonical_path() const {
  return impl_->canonical_path;
}

std::string ModelValidationLease::load_path() const {
#ifdef _WIN32
  return impl_->canonical_path;
#elif defined(__linux__)
  if (impl_->descriptor < 0 || lseek(impl_->descriptor, 0, SEEK_SET) < 0) return {};
  return "/proc/self/fd/" + std::to_string(impl_->descriptor);
#else
  if (impl_->descriptor < 0 || lseek(impl_->descriptor, 0, SEEK_SET) < 0) return {};
  return "/dev/fd/" + std::to_string(impl_->descriptor);
#endif
}

std::string ModelValidationLease::authorized_load_path(
    const std::string& expected_canonical_path) const {
  if (!impl_ || expected_canonical_path != impl_->canonical_path || !unchanged()) return {};
  const std::string path = load_path();
  if (path.empty() || !unchanged()) return {};
  return path;
}

bool ModelValidationLease::unchanged() const {
  if (!impl_) return false;
#ifdef _WIN32
  BY_HANDLE_FILE_INFORMATION held{};
  if (impl_->handle == INVALID_HANDLE_VALUE ||
      !GetFileInformationByHandle(impl_->handle, &held)) return false;
  const DWORD attributes = GetFileAttributesW(std::filesystem::path(impl_->canonical_path).c_str());
  if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) return false;
  HANDLE current_handle = CreateFileW(std::filesystem::path(impl_->canonical_path).c_str(),
                                      GENERIC_READ, FILE_SHARE_READ, nullptr,
                                      OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (current_handle == INVALID_HANDLE_VALUE) return false;
  BY_HANDLE_FILE_INFORMATION current{};
  const bool queried = GetFileInformationByHandle(current_handle, &current) != 0;
  CloseHandle(current_handle);
  const auto same = [&](const BY_HANDLE_FILE_INFORMATION& value) {
    return value.dwVolumeSerialNumber == impl_->identity.dwVolumeSerialNumber &&
           value.nFileIndexHigh == impl_->identity.nFileIndexHigh &&
           value.nFileIndexLow == impl_->identity.nFileIndexLow &&
           value.nFileSizeHigh == impl_->identity.nFileSizeHigh &&
           value.nFileSizeLow == impl_->identity.nFileSizeLow &&
           value.ftLastWriteTime.dwHighDateTime == impl_->identity.ftLastWriteTime.dwHighDateTime &&
           value.ftLastWriteTime.dwLowDateTime == impl_->identity.ftLastWriteTime.dwLowDateTime;
  };
  return queried && same(held) && same(current);
#else
  struct stat held{};
  struct stat current{};
  if (impl_->descriptor < 0 || fstat(impl_->descriptor, &held) != 0 ||
      lstat(impl_->canonical_path.c_str(), &current) != 0 || !S_ISREG(current.st_mode)) return false;
  const auto held_modified = modification_time(held);
  const auto current_modified = modification_time(current);
  const auto matches = [&](const struct stat& value,
                           const std::pair<std::int64_t, std::int64_t>& modified) {
    return value.st_dev == impl_->device && value.st_ino == impl_->inode &&
           value.st_size >= 0 && static_cast<std::uint64_t>(value.st_size) == impl_->size &&
           modified.first == impl_->modified_seconds &&
           modified.second == impl_->modified_nanoseconds;
  };
  return matches(held, held_modified) && matches(current, current_modified);
#endif
}

namespace {

constexpr std::uint64_t kMaxMetadataEntries = 4096;
constexpr std::uint64_t kMaxMetadataArrayElements = 1000000;
constexpr std::uint64_t kMaxMetadataBytes = 64ULL * 1024 * 1024;
constexpr std::uint64_t kMaxTensorCount = 1000000;
constexpr std::uint64_t kMaxStringBytes = 16ULL * 1024 * 1024;
constexpr std::uint64_t kDefaultAlignment = 32;
constexpr std::uint64_t kProductTensorCount = 427;
constexpr std::uint64_t kProductMetadataCount = 41;
constexpr char kProductChatTemplateSha256[] =
    "a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715";

class Sha256 {
 public:
  Sha256() : state_{0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                    0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u} {}

  void update(const std::uint8_t* data, size_t length) {
    total_ += length;
    while (length) {
      const size_t take = std::min(length, block_.size() - used_);
      std::copy(data, data + take, block_.begin() + used_);
      used_ += take;
      data += take;
      length -= take;
      if (used_ == block_.size()) {
        transform(block_.data());
        used_ = 0;
      }
    }
  }

  std::string finish() {
    const std::uint64_t bits = total_ * 8;
    const std::uint8_t one = 0x80;
    update(&one, 1);
    const std::uint8_t zero = 0;
    while (used_ != 56) update(&zero, 1);
    std::uint8_t length[8];
    for (unsigned i = 0; i < 8; ++i) {
      length[7 - i] = static_cast<std::uint8_t>(bits >> (i * 8));
    }
    update(length, 8);
    std::ostringstream out;
    for (const std::uint32_t word : state_) {
      out << std::hex << std::setfill('0') << std::setw(8) << word;
    }
    return out.str();
  }

 private:
  static std::uint32_t rotr(std::uint32_t value, unsigned count) {
    return (value >> count) | (value << (32 - count));
  }

  void transform(const std::uint8_t* block) {
    static constexpr std::uint32_t k[64] = {
      0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
      0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
      0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
      0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
      0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
      0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
      0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
      0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u};
    std::uint32_t w[64]{};
    for (unsigned i = 0; i < 16; ++i) {
      w[i] = (static_cast<std::uint32_t>(block[i * 4]) << 24) |
             (static_cast<std::uint32_t>(block[i * 4 + 1]) << 16) |
             (static_cast<std::uint32_t>(block[i * 4 + 2]) << 8) |
             block[i * 4 + 3];
    }
    for (unsigned i = 16; i < 64; ++i) {
      const std::uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
      const std::uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    std::uint32_t a=state_[0],b=state_[1],c=state_[2],d=state_[3],e=state_[4],f=state_[5],g=state_[6],h=state_[7];
    for (unsigned i = 0; i < 64; ++i) {
      const std::uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const std::uint32_t ch = (e & f) ^ ((~e) & g);
      const std::uint32_t t1 = h + s1 + ch + k[i] + w[i];
      const std::uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
      const std::uint32_t t2 = s0 + maj;
      h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    state_[0]+=a; state_[1]+=b; state_[2]+=c; state_[3]+=d;
    state_[4]+=e; state_[5]+=f; state_[6]+=g; state_[7]+=h;
  }

  std::array<std::uint32_t, 8> state_;
  std::array<std::uint8_t, 64> block_{};
  size_t used_ = 0;
  std::uint64_t total_ = 0;
};

class ParseError : public std::runtime_error {
 public:
  ParseError(std::string code, std::string message)
      : std::runtime_error(std::move(message)), code_(std::move(code)) {}
  const std::string& code() const { return code_; }

 private:
  std::string code_;
};

class BoundedReader {
 public:
  BoundedReader(std::ifstream& input, std::uint64_t size) : input_(input), size_(size) {}

  std::uint64_t position() const { return position_; }
  std::uint64_t remaining() const { return size_ - position_; }

  void read(void* output, size_t bytes) {
    if (bytes > remaining()) throw ParseError("gguf_truncated", "GGUF structure is truncated");
    input_.read(static_cast<char*>(output), static_cast<std::streamsize>(bytes));
    if (!input_ || static_cast<size_t>(input_.gcount()) != bytes) {
      throw ParseError("gguf_truncated", "GGUF structure is truncated");
    }
    position_ += bytes;
  }

  std::uint8_t read_u8() {
    std::uint8_t value = 0;
    read(&value, sizeof(value));
    return value;
  }

  std::uint32_t read_u32() {
    std::array<std::uint8_t, 4> bytes{};
    read(bytes.data(), bytes.size());
    return static_cast<std::uint32_t>(bytes[0]) |
           (static_cast<std::uint32_t>(bytes[1]) << 8) |
           (static_cast<std::uint32_t>(bytes[2]) << 16) |
           (static_cast<std::uint32_t>(bytes[3]) << 24);
  }

  std::uint64_t read_u64() {
    std::array<std::uint8_t, 8> bytes{};
    read(bytes.data(), bytes.size());
    std::uint64_t value = 0;
    for (unsigned i = 0; i < bytes.size(); ++i) {
      value |= static_cast<std::uint64_t>(bytes[i]) << (i * 8);
    }
    return value;
  }

  void skip(std::uint64_t bytes) {
    if (bytes > remaining()) throw ParseError("gguf_truncated", "GGUF structure is truncated");
    input_.seekg(static_cast<std::streamoff>(bytes), std::ios::cur);
    if (!input_) throw ParseError("gguf_truncated", "GGUF structure is truncated");
    position_ += bytes;
  }

  std::string read_string(std::uint64_t limit, const char* label) {
    const std::uint64_t length = read_u64();
    if (length > limit || length > remaining()) {
      throw ParseError("gguf_string_invalid", std::string(label) + " exceeds its bound");
    }
    std::string value(static_cast<size_t>(length), '\0');
    if (length) read(value.data(), value.size());
    return value;
  }

  std::string hash_bytes(std::uint64_t bytes) {
    if (bytes > remaining()) throw ParseError("gguf_truncated", "GGUF string is truncated");
    Sha256 digest;
    std::array<std::uint8_t, 64 * 1024> chunk{};
    std::uint64_t left = bytes;
    while (left) {
      const size_t take = static_cast<size_t>(std::min<std::uint64_t>(left, chunk.size()));
      read(chunk.data(), take);
      digest.update(chunk.data(), take);
      left -= take;
    }
    return digest.finish();
  }

 private:
  std::ifstream& input_;
  std::uint64_t size_;
  std::uint64_t position_ = 0;
};

struct ValidationProfile {
  std::string expected_filename;
  std::uint64_t expected_size_bytes = 0;
  std::string expected_sha256;
  std::string architecture;
  std::uint64_t expected_tensor_count = 0;
  bool require_product_metadata = false;
  bool allow_empty_sha256_for_tests = false;
};

struct MetadataObservation {
  std::string architecture;
  std::string tokenizer_model;
  std::string chat_template_sha256;
  std::uint64_t alignment = kDefaultAlignment;
  std::uint64_t block_count = 0;
  std::uint64_t embedding_length = 0;
  std::uint64_t head_count = 0;
  std::uint64_t head_count_kv = 0;
  std::uint64_t file_type = std::numeric_limits<std::uint64_t>::max();
  std::uint64_t quantization_version = 0;
  std::uint64_t token_count = 0;
  std::uint64_t token_type_count = 0;
  std::uint64_t merge_count = 0;
  std::uint64_t eos_token_id = std::numeric_limits<std::uint64_t>::max();
};

struct TensorRange {
  std::uint64_t begin = 0;
  std::uint64_t end = 0;
};

struct TensorDescriptor {
  std::vector<std::uint64_t> dimensions;
  std::uint32_t type = 0;
};

struct TypeLayout {
  std::uint64_t block_elements;
  std::uint64_t block_bytes;
};

bool checked_multiply(std::uint64_t left, std::uint64_t right, std::uint64_t& output) {
  if (left != 0 && right > std::numeric_limits<std::uint64_t>::max() / left) return false;
  output = left * right;
  return true;
}

std::uint64_t align_up(std::uint64_t value, std::uint64_t alignment) {
  if (alignment == 0 || value > std::numeric_limits<std::uint64_t>::max() - (alignment - 1)) {
    throw ParseError("gguf_alignment_invalid", "GGUF alignment overflows");
  }
  return (value + alignment - 1) & ~(alignment - 1);
}

TypeLayout type_layout(std::uint32_t type) {
  // The accepted Q4_K_M artifact contains only F32, Q4_K, and Q6_K tensors.
  // The test-only validator also uses F32. Unknown and unsupported GGML types
  // are rejected before backend allocation.
  switch (type) {
    case 0: return {1, 4};       // GGML_TYPE_F32
    case 12: return {256, 144};  // GGML_TYPE_Q4_K
    case 14: return {256, 210};  // GGML_TYPE_Q6_K
    default: throw ParseError("gguf_tensor_type_unsupported", "GGUF tensor type is not accepted");
  }
}

bool printable_identifier(const std::string& value) {
  return !value.empty() && std::all_of(value.begin(), value.end(), [](unsigned char character) {
    return character >= 0x21 && character <= 0x7e && character != '/' && character != '\\';
  });
}

void require_metadata_type(std::uint32_t actual, std::uint32_t expected, const std::string& key) {
  if (actual != expected) {
    throw ParseError("gguf_metadata_type_invalid", "GGUF metadata type is invalid for " + key);
  }
}

void skip_scalar(BoundedReader& reader, std::uint32_t type) {
  switch (type) {
    case 0: case 1: case 7: reader.skip(1); return;
    case 2: case 3: reader.skip(2); return;
    case 4: case 5: case 6: reader.skip(4); return;
    case 10: case 11: case 12: reader.skip(8); return;
    case 8: {
      const std::uint64_t length = reader.read_u64();
      if (length > kMaxStringBytes) throw ParseError("gguf_string_invalid", "GGUF metadata string exceeds safety bound");
      reader.skip(length);
      return;
    }
    default: throw ParseError("gguf_metadata_type_unsupported", "GGUF metadata type is unsupported");
  }
}

void parse_metadata_value(BoundedReader& reader, std::uint32_t type,
                          const std::string& key, MetadataObservation& observed) {
  const auto capture_u32 = [&](std::uint64_t& destination) {
    require_metadata_type(type, 4, key);
    destination = reader.read_u32();
  };
  if (key == "general.architecture" || key == "tokenizer.ggml.model") {
    require_metadata_type(type, 8, key);
    const std::string value = reader.read_string(256, key.c_str());
    if (key == "general.architecture") observed.architecture = value;
    else observed.tokenizer_model = value;
    return;
  }
  if (key == "tokenizer.chat_template") {
    require_metadata_type(type, 8, key);
    const std::uint64_t length = reader.read_u64();
    if (length == 0 || length > 1024 * 1024) {
      throw ParseError("gguf_chat_template_invalid", "GGUF chat template length is invalid");
    }
    observed.chat_template_sha256 = reader.hash_bytes(length);
    return;
  }
  if (key == "general.alignment") { capture_u32(observed.alignment); return; }
  if (key == "qwen35.block_count") { capture_u32(observed.block_count); return; }
  if (key == "qwen35.embedding_length") { capture_u32(observed.embedding_length); return; }
  if (key == "qwen35.attention.head_count") { capture_u32(observed.head_count); return; }
  if (key == "qwen35.attention.head_count_kv") { capture_u32(observed.head_count_kv); return; }
  if (key == "general.file_type") { capture_u32(observed.file_type); return; }
  if (key == "general.quantization_version") { capture_u32(observed.quantization_version); return; }
  if (key == "tokenizer.ggml.eos_token_id") { capture_u32(observed.eos_token_id); return; }
  if (type == 9) {
    const std::uint32_t element_type = reader.read_u32();
    const std::uint64_t count = reader.read_u64();
    if (element_type == 9 || count > kMaxMetadataArrayElements) {
      throw ParseError("gguf_metadata_array_invalid", "GGUF metadata array is invalid");
    }
    if (key == "tokenizer.ggml.tokens") {
      if (element_type != 8) throw ParseError("gguf_metadata_type_invalid", "token array must contain strings");
      observed.token_count = count;
    } else if (key == "tokenizer.ggml.token_type") {
      if (element_type != 5) throw ParseError("gguf_metadata_type_invalid", "token type array must contain int32 values");
      observed.token_type_count = count;
    } else if (key == "tokenizer.ggml.merges") {
      if (element_type != 8) throw ParseError("gguf_metadata_type_invalid", "merge array must contain strings");
      observed.merge_count = count;
    }
    if (element_type == 8) {
      for (std::uint64_t i = 0; i < count; ++i) skip_scalar(reader, element_type);
    } else {
      std::uint64_t element_bytes = 0;
      switch (element_type) {
        case 0: case 1: case 7: element_bytes = 1; break;
        case 2: case 3: element_bytes = 2; break;
        case 4: case 5: case 6: element_bytes = 4; break;
        case 10: case 11: case 12: element_bytes = 8; break;
        default: throw ParseError("gguf_metadata_type_unsupported", "GGUF metadata array type is unsupported");
      }
      std::uint64_t bytes = 0;
      if (!checked_multiply(count, element_bytes, bytes)) {
        throw ParseError("gguf_count_overflow", "GGUF metadata array size overflows");
      }
      reader.skip(bytes);
    }
    return;
  }
  skip_scalar(reader, type);
}

void validate_product_metadata(const MetadataObservation& value) {
  if (value.architecture != "qwen35") throw ParseError("model_architecture_mismatch", "model architecture does not match qwen35");
  if (value.block_count != 32 || value.embedding_length != 4096 ||
      value.head_count != 16 || value.head_count_kv != 4) {
    throw ParseError("model_architecture_profile_mismatch", "Qwen3.5-9B architecture metadata does not match the accepted profile");
  }
  if (value.file_type != 15 || value.quantization_version != 2) {
    throw ParseError("model_quantization_profile_mismatch", "GGUF quantization metadata does not match Q4_K_M");
  }
  if (value.tokenizer_model != "gpt2" || value.token_count != 248320 ||
      value.token_type_count != 248320 || value.merge_count != 247587 ||
      value.eos_token_id != 248046) {
    throw ParseError("model_tokenizer_profile_mismatch", "tokenizer metadata does not match the accepted artifact");
  }
  if (value.chat_template_sha256 != kProductChatTemplateSha256) {
    throw ParseError("model_chat_template_mismatch", "embedded chat template digest does not match the accepted artifact");
  }
}

void require_tensor(const std::map<std::string, TensorDescriptor>& descriptors,
                    const std::string& name, const std::vector<std::uint64_t>& dimensions,
                    std::uint32_t type) {
  const auto item = descriptors.find(name);
  if (item == descriptors.end() || item->second.dimensions != dimensions || item->second.type != type) {
    throw ParseError("model_tensor_profile_mismatch", "required tensor descriptor does not match: " + name);
  }
}

void validate_product_tensors(const std::map<std::string, TensorDescriptor>& descriptors,
                              const std::map<std::uint32_t, std::uint64_t>& type_counts,
                              const std::set<unsigned>& blocks) {
  if (descriptors.size() != kProductTensorCount || type_counts.find(0) == type_counts.end() ||
      type_counts.at(0) != 177 || type_counts.find(12) == type_counts.end() ||
      type_counts.at(12) != 217 || type_counts.find(14) == type_counts.end() ||
      type_counts.at(14) != 33 || type_counts.size() != 3 || blocks.size() != 32) {
    throw ParseError("model_tensor_profile_mismatch", "tensor inventory summary does not match the accepted Q4_K_M artifact");
  }
  require_tensor(descriptors, "output.weight", {4096, 248320}, 14);
  require_tensor(descriptors, "output_norm.weight", {4096}, 0);
  require_tensor(descriptors, "token_embd.weight", {4096, 248320}, 12);
}

void parse_gguf(BoundedReader& reader, const ValidationProfile& profile,
                ModelValidationResult& result) {
  std::array<char, 4> magic{};
  reader.read(magic.data(), magic.size());
  if (std::string(magic.data(), magic.size()) != "GGUF") {
    throw ParseError("gguf_magic_invalid", "GGUF magic is invalid");
  }
  const std::uint32_t version = reader.read_u32();
  if (version != 3) throw ParseError("gguf_version_unsupported", "GGUF version is unsupported");
  const std::uint64_t tensor_count = reader.read_u64();
  const std::uint64_t metadata_count = reader.read_u64();
  if (tensor_count == 0 || tensor_count > kMaxTensorCount ||
      metadata_count == 0 || metadata_count > kMaxMetadataEntries) {
    throw ParseError("gguf_count_invalid", "GGUF metadata/tensor counts are invalid");
  }
  if (profile.expected_tensor_count && tensor_count != profile.expected_tensor_count) {
    throw ParseError("model_tensor_profile_mismatch", "tensor count does not match the accepted profile");
  }
  if (profile.require_product_metadata && metadata_count != kProductMetadataCount) {
    throw ParseError("model_metadata_profile_mismatch", "metadata count does not match the accepted artifact");
  }

  MetadataObservation metadata;
  std::set<std::string> metadata_keys;
  for (std::uint64_t i = 0; i < metadata_count; ++i) {
    const std::string key = reader.read_string(256, "metadata key");
    if (!printable_identifier(key) || !metadata_keys.insert(key).second) {
      throw ParseError("gguf_metadata_key_invalid", "GGUF metadata key is invalid or duplicated");
    }
    const std::uint32_t type = reader.read_u32();
    parse_metadata_value(reader, type, key, metadata);
    if (reader.position() > kMaxMetadataBytes) {
      throw ParseError("gguf_metadata_too_large", "GGUF metadata section exceeds safety bound");
    }
  }
  if (metadata.architecture != profile.architecture) {
    throw ParseError("model_architecture_mismatch", "model architecture does not match the accepted profile");
  }
  if (metadata.alignment == 0 || metadata.alignment > 4096 ||
      (metadata.alignment & (metadata.alignment - 1)) != 0) {
    throw ParseError("gguf_alignment_invalid", "GGUF alignment is invalid");
  }
  if (profile.require_product_metadata) validate_product_metadata(metadata);

  std::set<std::string> tensor_names;
  std::vector<TensorRange> ranges;
  std::map<std::string, TensorDescriptor> descriptors;
  std::map<std::uint32_t, std::uint64_t> type_counts;
  std::set<unsigned> blocks;
  ranges.reserve(static_cast<size_t>(tensor_count));
  for (std::uint64_t i = 0; i < tensor_count; ++i) {
    const std::string name = reader.read_string(256, "tensor name");
    if (!printable_identifier(name) || !tensor_names.insert(name).second) {
      throw ParseError("gguf_tensor_name_invalid", "GGUF tensor name is invalid or duplicated");
    }
    std::string lowered = name;
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (lowered.find("mmproj") != std::string::npos || lowered.find("vision") != std::string::npos ||
        lowered.find("visual") != std::string::npos) {
      throw ParseError("model_mmproj_forbidden", "vision tensors are not accepted by the text-only runtime");
    }
    if (name.rfind("blk.", 0) == 0) {
      const size_t end = name.find('.', 4);
      if (end == std::string::npos || end == 4) throw ParseError("gguf_tensor_name_invalid", "block tensor name is malformed");
      unsigned block = 0;
      for (size_t index = 4; index < end; ++index) {
        if (!std::isdigit(static_cast<unsigned char>(name[index])) || block > 1000) {
          throw ParseError("gguf_tensor_name_invalid", "block tensor index is malformed");
        }
        block = block * 10 + static_cast<unsigned>(name[index] - '0');
      }
      if (profile.require_product_metadata && block >= 32) {
        throw ParseError("model_tensor_profile_mismatch", "tensor references a block outside the accepted architecture");
      }
      blocks.insert(block);
    }
    const std::uint32_t dimension_count = reader.read_u32();
    if (dimension_count == 0 || dimension_count > 4) {
      throw ParseError("gguf_tensor_shape_invalid", "GGUF tensor dimension count is invalid");
    }
    std::vector<std::uint64_t> dimensions;
    dimensions.reserve(dimension_count);
    for (std::uint32_t dimension = 0; dimension < dimension_count; ++dimension) {
      const std::uint64_t extent = reader.read_u64();
      if (extent == 0 || extent > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
        throw ParseError("gguf_tensor_shape_invalid", "GGUF tensor extent is invalid");
      }
      dimensions.push_back(extent);
    }
    const std::uint32_t type = reader.read_u32();
    const TypeLayout layout = type_layout(type);
    if (dimensions[0] % layout.block_elements != 0) {
      throw ParseError("gguf_tensor_shape_invalid", "quantized tensor row is not block-aligned");
    }
    std::uint64_t bytes = 0;
    if (!checked_multiply(dimensions[0] / layout.block_elements, layout.block_bytes, bytes)) {
      throw ParseError("gguf_tensor_size_overflow", "GGUF tensor row size overflows");
    }
    for (size_t dimension = 1; dimension < dimensions.size(); ++dimension) {
      std::uint64_t next = 0;
      if (!checked_multiply(bytes, dimensions[dimension], next)) {
        throw ParseError("gguf_tensor_size_overflow", "GGUF tensor size overflows");
      }
      bytes = next;
    }
    const std::uint64_t offset = reader.read_u64();
    if (offset % metadata.alignment != 0 || offset > std::numeric_limits<std::uint64_t>::max() - bytes) {
      throw ParseError("gguf_tensor_offset_invalid", "GGUF tensor offset is invalid");
    }
    ranges.push_back({offset, offset + bytes});
    descriptors.emplace(name, TensorDescriptor{dimensions, type});
    ++type_counts[type];
  }

  const std::uint64_t tensor_data_offset = align_up(reader.position(), metadata.alignment);
  if (tensor_data_offset > reader.position()) {
    const std::uint64_t padding_size = tensor_data_offset - reader.position();
    std::vector<std::uint8_t> padding(static_cast<size_t>(padding_size));
    reader.read(padding.data(), padding.size());
    if (std::any_of(padding.begin(), padding.end(), [](std::uint8_t value) { return value != 0; })) {
      throw ParseError("gguf_alignment_invalid", "GGUF metadata padding is non-zero");
    }
  }
  const std::uint64_t tensor_data_bytes = reader.remaining();
  std::sort(ranges.begin(), ranges.end(), [](const TensorRange& left, const TensorRange& right) {
    return left.begin < right.begin || (left.begin == right.begin && left.end < right.end);
  });
  if (ranges.empty() || ranges.front().begin != 0) {
    throw ParseError("gguf_tensor_offset_invalid", "GGUF tensor data does not begin at offset zero");
  }
  std::uint64_t previous_end = 0;
  for (const auto& range : ranges) {
    if (range.begin < previous_end) throw ParseError("gguf_tensor_overlap", "GGUF tensor ranges overlap");
    if (range.end > tensor_data_bytes) throw ParseError("gguf_tensor_out_of_bounds", "GGUF tensor exceeds file bounds");
    previous_end = range.end;
  }
  if (previous_end != tensor_data_bytes) {
    throw ParseError("gguf_tensor_data_size_mismatch", "GGUF tensor ranges do not cover the declared file payload");
  }
  if (profile.require_product_metadata) validate_product_tensors(descriptors, type_counts, blocks);

  result.gguf_version = version;
  result.tensor_count = tensor_count;
  result.metadata_count = metadata_count;
  result.tensor_data_offset = tensor_data_offset;
}

ModelValidationResult invalid(const std::string& code, const std::string& message) {
  return {false, code, message};
}

bool unsafe_model_path(const std::filesystem::path& path) {
  const std::string value = path.string();
  if (value.rfind("\\\\", 0) == 0 || value.rfind("//", 0) == 0 ||
      value.rfind("\\\\?\\", 0) == 0 || value.rfind("\\\\.\\", 0) == 0) return true;
  const size_t first_colon = value.find(':');
#ifdef _WIN32
  return (first_colon != std::string::npos &&
          (first_colon != 1 || !std::isalpha(static_cast<unsigned char>(value[0])) ||
           value.find(':', 2) != std::string::npos));
#else
  return first_colon != std::string::npos;
#endif
}

ModelValidationResult validate_with_profile(const std::filesystem::path& input,
                                            const ValidationProfile& profile) {
  if (!input.is_absolute()) return invalid("model_path_not_absolute", "model path must be absolute");
  if (unsafe_model_path(input)) return invalid("model_path_unsafe", "model path must be local and must not use a device or alternate stream");
  std::error_code ec;
  const auto status = std::filesystem::symlink_status(input, ec);
  if (ec || std::filesystem::is_symlink(status)) return invalid("model_symlink_forbidden", "model path must not be a symlink");
  if (!std::filesystem::is_regular_file(status)) return invalid("model_not_regular_file", "model path is not a regular file");
  const auto canonical = std::filesystem::canonical(input, ec);
  if (ec) return invalid("model_path_invalid", "model path cannot be canonicalized");
  if (!profile.expected_filename.empty() && canonical.filename().string() != profile.expected_filename) {
    return invalid("model_filename_mismatch", "model filename does not match the compiled profile");
  }
  if (canonical.filename().string().find("mmproj") != std::string::npos) {
    return invalid("model_mmproj_forbidden", "vision projection is not accepted by text-only runtime");
  }
  const auto size = std::filesystem::file_size(canonical, ec);
  if (ec) return invalid("model_stat_failed", "model file size is unavailable");
  if (profile.expected_size_bytes == 0 || size != profile.expected_size_bytes) {
    return invalid("model_size_mismatch", "model size does not match the compiled profile");
  }
  const auto observed_write_time = std::filesystem::last_write_time(canonical, ec);
  if (ec) return invalid("model_stat_failed", "model modification time is unavailable");

  std::string lease_error;
  auto lease = ModelValidationLease::acquire(canonical, size, lease_error);
  if (!lease) return invalid("model_lock_failed", lease_error);

  std::ifstream file(lease->load_path(), std::ios::binary);
  if (!file) return invalid("model_open_failed", "model file cannot be opened read-only");
  std::error_code opened_ec;
  if (!std::filesystem::equivalent(input, canonical, opened_ec) || opened_ec ||
      std::filesystem::file_size(canonical, opened_ec) != size || opened_ec ||
      !lease->unchanged()) {
    return invalid("model_changed_during_validation", "model changed while validation was starting");
  }

  ModelValidationResult result;
  result.size_bytes = size;
  try {
    BoundedReader reader(file, size);
    parse_gguf(reader, profile, result);
  } catch (const ParseError& error) {
    return invalid(error.code(), error.what());
  }

  file.clear();
  file.seekg(0);
  if (!file) return invalid("model_read_failed", "model file could not be rewound for hashing");
  Sha256 digest;
  std::array<std::uint8_t, 1024 * 1024> chunk{};
  while (file) {
    file.read(reinterpret_cast<char*>(chunk.data()), static_cast<std::streamsize>(chunk.size()));
    const auto count = file.gcount();
    if (count > 0) digest.update(chunk.data(), static_cast<size_t>(count));
  }
  if (file.bad()) return invalid("model_read_failed", "model read failed during validation");
  const auto final_size = std::filesystem::file_size(canonical, ec);
  const auto final_write_time = std::filesystem::last_write_time(canonical, ec);
  if (ec || final_size != size || final_write_time != observed_write_time ||
      !lease->unchanged()) {
    return invalid("model_changed_during_validation", "model changed while validation was running");
  }
  const std::string hash = digest.finish();
  if ((!profile.allow_empty_sha256_for_tests && profile.expected_sha256.empty()) ||
      (!profile.expected_sha256.empty() && hash != profile.expected_sha256)) {
    return invalid("model_hash_mismatch", "model SHA-256 does not match the compiled profile");
  }
  result.valid = true;
  result.code = "ok";
  result.message = "model accepted";
  result.canonical_path = canonical.string();
  result.sha256 = hash;
  result.size_bytes = size;
  result.lease = std::move(lease);
  return result;
}

}  // namespace

ModelValidationResult validate_product_model_file(const std::filesystem::path& path) {
  return validate_with_profile(path, ValidationProfile{
      kProductModelFilename,
      kProductModelSizeBytes,
      kProductModelSha256,
      "qwen35",
      kProductTensorCount,
      true,
      false,
  });
}

#ifdef LAE_ENABLE_TEST_MODEL_IDENTITY
ModelValidationResult validate_model_file_for_tests(
    const std::filesystem::path& path, const TestModelProfile& profile) {
  return validate_with_profile(path, ValidationProfile{
      profile.expected_filename,
      profile.expected_size_bytes,
      profile.expected_sha256,
      profile.architecture,
      profile.expected_tensor_count,
      profile.require_product_metadata,
      true,
  });
}
#endif

}  // namespace lae
