#include "model_validator.hpp"

#include <array>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <vector>

namespace lae {
namespace {

class Sha256 {
 public:
  Sha256() : state_{0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                    0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u} {}
  void update(const std::uint8_t* data, size_t length) {
    total_ += length;
    while (length) {
      const size_t take = std::min(length, block_.size() - used_);
      std::copy(data, data + take, block_.begin() + used_);
      used_ += take; data += take; length -= take;
      if (used_ == block_.size()) { transform(block_.data()); used_ = 0; }
    }
  }
  std::string finish() {
    const std::uint64_t bits = total_ * 8;
    std::uint8_t one = 0x80; update(&one, 1);
    std::uint8_t zero = 0;
    while (used_ != 56) update(&zero, 1);
    std::uint8_t length[8];
    for (unsigned i = 0; i < 8; ++i) length[7 - i] = static_cast<std::uint8_t>(bits >> (i * 8));
    update(length, 8);
    std::ostringstream out;
    for (const std::uint32_t word : state_) out << std::hex << std::setfill('0') << std::setw(8) << word;
    return out.str();
  }

 private:
  static std::uint32_t rotr(std::uint32_t value, unsigned count) { return (value >> count) | (value << (32 - count)); }
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
    for (unsigned i = 0; i < 16; ++i) w[i] = (static_cast<std::uint32_t>(block[i * 4]) << 24) |
      (static_cast<std::uint32_t>(block[i * 4 + 1]) << 16) | (static_cast<std::uint32_t>(block[i * 4 + 2]) << 8) | block[i * 4 + 3];
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
    state_[0]+=a; state_[1]+=b; state_[2]+=c; state_[3]+=d; state_[4]+=e; state_[5]+=f; state_[6]+=g; state_[7]+=h;
  }
  std::array<std::uint32_t, 8> state_;
  std::array<std::uint8_t, 64> block_{};
  size_t used_ = 0;
  std::uint64_t total_ = 0;
};

std::uint32_t u32(const std::uint8_t* p) { return static_cast<std::uint32_t>(p[0]) | (static_cast<std::uint32_t>(p[1]) << 8) | (static_cast<std::uint32_t>(p[2]) << 16) | (static_cast<std::uint32_t>(p[3]) << 24); }
std::uint64_t u64(const std::uint8_t* p) { std::uint64_t value = 0; for (unsigned i = 0; i < 8; ++i) value |= static_cast<std::uint64_t>(p[i]) << (i * 8); return value; }

bool skip_value(const std::vector<std::uint8_t>& data, size_t& offset, std::uint32_t type, unsigned depth) {
  if (depth > 8 || offset > data.size()) return false;
  const auto fixed = [&](size_t bytes) { if (bytes > data.size() - offset) return false; offset += bytes; return true; };
  switch (type) {
    case 0: case 1: case 7: return fixed(1);
    case 2: case 3: return fixed(2);
    case 4: case 5: case 6: return fixed(4);
    case 10: case 11: case 12: return fixed(8);
    case 8: {
      if (data.size() - offset < 8) return false; const auto length = u64(data.data() + offset); offset += 8;
      return length <= data.size() - offset && (offset += static_cast<size_t>(length), true);
    }
    case 9: {
      if (data.size() - offset < 12) return false; const auto element_type = u32(data.data() + offset); offset += 4; const auto count = u64(data.data() + offset); offset += 8;
      if (count > 1000000) return false; for (std::uint64_t i = 0; i < count; ++i) if (!skip_value(data, offset, element_type, depth + 1)) return false; return true;
    }
    default: return false;
  }
}

bool find_architecture(const std::vector<std::uint8_t>& data, std::uint64_t metadata_count,
                       std::string& architecture) {
  size_t offset = 24;
  for (std::uint64_t i = 0; i < metadata_count; ++i) {
    if (offset > data.size()) return false;
    if (data.size() - offset < 8) return false; const auto key_length = u64(data.data() + offset); offset += 8;
    if (key_length > data.size() - offset) return false;
    const std::string key(reinterpret_cast<const char*>(data.data() + offset), static_cast<size_t>(key_length)); offset += static_cast<size_t>(key_length);
    if (data.size() - offset < 4) return false; const auto type = u32(data.data() + offset); offset += 4;
    if (key == "general.architecture" && type == 8) {
      if (data.size() - offset < 8) return false; const auto value_length = u64(data.data() + offset); offset += 8;
      if (value_length > data.size() - offset) return false;
      architecture.assign(reinterpret_cast<const char*>(data.data() + offset), static_cast<size_t>(value_length)); return true;
    }
    if (!skip_value(data, offset, type, 0)) return false;
  }
  return false;
}

ModelValidationResult invalid(const std::string& code, const std::string& message) { return {false, code, message}; }

}  // namespace

ModelValidationResult validate_model_file(const std::filesystem::path& input,
                                          const ModelProfile& profile, bool fixture_mode) {
  if (!input.is_absolute()) return invalid("model_path_not_absolute", "model path must be absolute");
  std::error_code ec;
  const auto status = std::filesystem::symlink_status(input, ec);
  if (ec || std::filesystem::is_symlink(status)) return invalid("model_symlink_forbidden", "model path must not be a symlink");
  if (!std::filesystem::is_regular_file(status)) return invalid("model_not_regular_file", "model path is not a regular file");
  const auto canonical = std::filesystem::weakly_canonical(input, ec);
  if (ec) return invalid("model_path_invalid", "model path cannot be canonicalized");
  const auto size = std::filesystem::file_size(canonical, ec);
  if (ec) return invalid("model_stat_failed", "model file size is unavailable");
  if (!fixture_mode && profile.expected_size_bytes == 0) return invalid("model_profile_incomplete", "release profile requires expected size");
  if (profile.expected_size_bytes && size != profile.expected_size_bytes) return invalid("model_size_mismatch", "model size does not match profile");
  if (!profile.expected_filename.empty() && canonical.filename().string() != profile.expected_filename)
    return invalid("model_filename_mismatch", "model filename does not match profile");
  if (profile.text_only_no_mmproj && canonical.filename().string().find("mmproj") != std::string::npos)
    return invalid("model_mmproj_forbidden", "vision projection is not accepted by text-only runtime");
  std::ifstream file(canonical, std::ios::binary);
  if (!file) return invalid("model_open_failed", "model file cannot be opened read-only");
  constexpr size_t kHeaderLimit = 1024 * 1024;
  std::vector<std::uint8_t> header(kHeaderLimit);
  file.read(reinterpret_cast<char*>(header.data()), header.size());
  const size_t bytes = static_cast<size_t>(file.gcount());
  if (bytes < 24) return invalid("gguf_truncated", "GGUF header is truncated");
  if (std::string(reinterpret_cast<char*>(header.data()), 4) != "GGUF") return invalid("gguf_magic_invalid", "GGUF magic is invalid");
  const auto version = u32(header.data() + 4);
  if (version != 3) return invalid("gguf_version_unsupported", "GGUF version is unsupported");
  const auto tensor_count = u64(header.data() + 8);
  const auto metadata_count = u64(header.data() + 16);
  if (tensor_count > 1000000 || metadata_count > 1000000) return invalid("gguf_count_overflow", "GGUF counts exceed safety bound");
  if (!fixture_mode && (tensor_count == 0 || metadata_count == 0)) return invalid("model_profile_missing_metadata", "model metadata/tensors are missing");
  if (!fixture_mode) {
    std::string architecture;
    if (!find_architecture(header, metadata_count, architecture)) return invalid("gguf_metadata_truncated", "required architecture metadata is missing or truncated");
    if (architecture != profile.architecture) return invalid("model_architecture_mismatch", "model architecture does not match profile");
  }
  Sha256 digest;
  file.clear(); file.seekg(0);
  std::array<char, 1024 * 1024> chunk{};
  while (file) { file.read(chunk.data(), chunk.size()); const auto n = file.gcount(); if (n > 0) digest.update(reinterpret_cast<const std::uint8_t*>(chunk.data()), static_cast<size_t>(n)); }
  const std::string hash = digest.finish();
  if (!fixture_mode && profile.expected_sha256.empty()) return invalid("model_profile_incomplete", "release profile requires expected SHA-256");
  if (!profile.expected_sha256.empty() && hash != profile.expected_sha256) return invalid("model_hash_mismatch", "model SHA-256 does not match profile");
  ModelValidationResult result{true, "ok", "model accepted"};
  result.canonical_path = canonical.string(); result.sha256 = hash; result.size_bytes = size; result.gguf_version = version;
  result.tensor_count = tensor_count; result.metadata_count = metadata_count;
  return result;
}

}  // namespace lae
