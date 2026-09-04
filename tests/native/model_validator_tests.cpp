#include "model_validation/model_validator.hpp"

#include <cassert>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace {

struct TensorSpec {
  std::string name;
  std::vector<std::uint64_t> dimensions;
  std::uint32_t type = 0;
  std::uint64_t offset = 0;
};

void append_u32(std::vector<std::uint8_t>& bytes, std::uint32_t value) {
  for (unsigned shift = 0; shift < 32; shift += 8) bytes.push_back(static_cast<std::uint8_t>(value >> shift));
}

void append_u64(std::vector<std::uint8_t>& bytes, std::uint64_t value) {
  for (unsigned shift = 0; shift < 64; shift += 8) bytes.push_back(static_cast<std::uint8_t>(value >> shift));
}

void append_string(std::vector<std::uint8_t>& bytes, const std::string& value) {
  append_u64(bytes, value.size());
  bytes.insert(bytes.end(), value.begin(), value.end());
}

std::vector<std::uint8_t> fixture(const std::vector<TensorSpec>& tensors,
                                  size_t payload_bytes, bool nonzero_padding = false) {
  std::vector<std::uint8_t> bytes{'G', 'G', 'U', 'F'};
  append_u32(bytes, 3);
  append_u64(bytes, tensors.size());
  append_u64(bytes, 1);
  append_string(bytes, "general.architecture");
  append_u32(bytes, 8);
  append_string(bytes, "qwen35");
  for (const auto& tensor : tensors) {
    append_string(bytes, tensor.name);
    append_u32(bytes, static_cast<std::uint32_t>(tensor.dimensions.size()));
    for (const auto dimension : tensor.dimensions) append_u64(bytes, dimension);
    append_u32(bytes, tensor.type);
    append_u64(bytes, tensor.offset);
  }
  while (bytes.size() % 32 != 0) bytes.push_back(nonzero_padding ? 0x5a : 0);
  bytes.resize(bytes.size() + payload_bytes, 0);
  return bytes;
}

lae::ModelValidationResult validate(const std::filesystem::path& path,
                                    const std::vector<std::uint8_t>& bytes,
                                    std::string expected_sha256 = {}) {
  {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
  }
  lae::TestModelProfile profile;
  profile.expected_filename = path.filename().string();
  profile.expected_size_bytes = bytes.size();
  profile.expected_sha256 = std::move(expected_sha256);
  profile.expected_tensor_count = 1;
  return lae::validate_model_file_for_tests(path, profile);
}

}  // namespace

int main() {
  const auto root = std::filesystem::temp_directory_path() / "lae-model-validator-tests";
  std::error_code error;
  std::filesystem::create_directories(root, error);
  assert(!error);
  const auto path = std::filesystem::absolute(root / "fixture.gguf");

  const auto valid_bytes = fixture({{"test.weight", {1}, 0, 0}}, 4);
  auto result = validate(path, valid_bytes);
  assert(result.valid && result.code == "ok" && result.tensor_count == 1 && result.tensor_data_offset > 0);
  assert(result.sha256 == "1ac31355ef040452baa229950a231b2a8f651a809a9a5865fbcf622f15bc04cb");
  assert(result.lease && result.lease->unchanged());
  assert(!result.lease->authorized_load_path(result.canonical_path).empty());
  const auto original_path = path.parent_path() / "fixture-original.gguf";
#ifdef _WIN32
  std::filesystem::rename(path, original_path, error);
  assert(error);  // The held Windows handle denies replacement/deletion.
  assert(!result.lease->authorized_load_path(result.canonical_path).empty());
  error.clear();
#else
  std::filesystem::rename(path, original_path, error);
  assert(!error);
  {
    std::ofstream replacement(path, std::ios::binary | std::ios::trunc);
    replacement.write(reinterpret_cast<const char*>(valid_bytes.data()), static_cast<std::streamsize>(valid_bytes.size()));
  }
  assert(!result.lease->unchanged());
  assert(result.lease->authorized_load_path(result.canonical_path).empty());
  std::ifstream held(result.lease->load_path(), std::ios::binary);
  char magic[4]{}; held.read(magic, sizeof(magic));
  assert(std::string(magic, sizeof(magic)) == "GGUF");
  result.lease.reset();
  std::filesystem::remove(path, error); error.clear();
  std::filesystem::rename(original_path, path, error);
  assert(!error);
#endif
  result.lease.reset();

  result = validate(path, valid_bytes, std::string(64, '0'));
  assert(!result.valid && result.code == "model_hash_mismatch");

  auto truncated = valid_bytes;
  truncated.pop_back();
  result = validate(path, truncated);
  assert(!result.valid && result.code == "gguf_tensor_out_of_bounds");

  result = validate(path, fixture({{"test.weight", {}, 0, 0}}, 4));
  assert(!result.valid && result.code == "gguf_tensor_shape_invalid");

  result = validate(path, fixture({{"test.weight", {1}, 0, 1}}, 5));
  assert(!result.valid && result.code == "gguf_tensor_offset_invalid");

  result = validate(path, fixture({{"test.weight", {1}, 99, 0}}, 4));
  assert(!result.valid && result.code == "gguf_tensor_type_unsupported");

  auto duplicate = fixture({{"same.weight", {1}, 0, 0}, {"same.weight", {1}, 0, 0}}, 4);
  lae::TestModelProfile two_tensor_profile;
  two_tensor_profile.expected_filename = path.filename().string();
  two_tensor_profile.expected_size_bytes = duplicate.size();
  two_tensor_profile.expected_tensor_count = 2;
  {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(reinterpret_cast<const char*>(duplicate.data()), static_cast<std::streamsize>(duplicate.size()));
  }
  result = lae::validate_model_file_for_tests(path, two_tensor_profile);
  assert(!result.valid && result.code == "gguf_tensor_name_invalid");

  auto overlap = fixture({{"a.weight", {1}, 0, 0}, {"b.weight", {1}, 0, 0}}, 4);
  two_tensor_profile.expected_size_bytes = overlap.size();
  {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(reinterpret_cast<const char*>(overlap.data()), static_cast<std::streamsize>(overlap.size()));
  }
  result = lae::validate_model_file_for_tests(path, two_tensor_profile);
  assert(!result.valid && result.code == "gguf_tensor_overlap");

  result = validate(path, fixture({{"test.weight", {1}, 0, 0}}, 4, true));
  assert(!result.valid && result.code == "gguf_alignment_invalid");

  const auto product_path = std::filesystem::absolute(root / lae::kProductModelFilename);
  auto shallow = valid_bytes;
  shallow.resize(70, 0);
  {
    std::ofstream output(product_path, std::ios::binary | std::ios::trunc);
    output.write(reinterpret_cast<const char*>(shallow.data()), static_cast<std::streamsize>(shallow.size()));
  }
  result = lae::validate_product_model_file(product_path);
  assert(!result.valid && result.code == "model_size_mismatch");

  std::filesystem::remove_all(root, error);
  std::cout << "model validator structural tests: PASS\n";
}
