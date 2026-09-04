#pragma once

#include <cstdint>
#include <filesystem>
#include <string>

namespace lae {

struct ModelProfile {
  std::string expected_filename = "Qwen3.5-9B-Q4_K_M.gguf";
  std::uint64_t expected_size_bytes = 0;
  std::string expected_sha256;
  std::string architecture = "qwen35";
  unsigned default_context_tokens = 8192;
  unsigned maximum_context_tokens = 16384;
  bool text_only_no_mmproj = true;
};

struct ModelValidationResult {
  bool valid = false;
  std::string code;
  std::string message;
  std::string canonical_path;
  std::string sha256;
  std::uint64_t size_bytes = 0;
  std::uint32_t gguf_version = 0;
  std::uint64_t tensor_count = 0;
  std::uint64_t metadata_count = 0;
};

// Performs bounded header checks before any backend model allocation. A zero
// expected size/hash is permitted only for fixture tests, never for release.
ModelValidationResult validate_model_file(const std::filesystem::path& path,
                                          const ModelProfile& profile,
                                          bool fixture_mode = false);

}  // namespace lae
