#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>

namespace lae {

inline constexpr char kProductModelFilename[] = "Qwen3.5-9B-Q4_K_M.gguf";
inline constexpr std::uint64_t kProductModelSizeBytes = 5629109088ULL;
inline constexpr char kProductModelSha256[] =
    "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b";
inline constexpr char kProductModelId[] = "qwen35-9b-q4-k-m";

// Keeps the validated filesystem object pinned across backend load. Windows
// uses a share-deny handle; POSIX loads through the held descriptor and checks
// descriptor/path identity before and after llama.cpp accepts the model.
class ModelValidationLease {
 public:
  ~ModelValidationLease();
  ModelValidationLease(const ModelValidationLease&) = delete;
  ModelValidationLease& operator=(const ModelValidationLease&) = delete;

  static std::shared_ptr<ModelValidationLease> acquire(
      const std::filesystem::path& canonical_path, std::uint64_t expected_size,
      std::string& error);
  const std::string& canonical_path() const;
  std::string load_path() const;
  // This is the backend/test seam: a stale or mismatched lease never yields a
  // path that llama.cpp can open. Windows replacement is also denied by the
  // held share mode; POSIX returns the held descriptor path.
  std::string authorized_load_path(const std::string& expected_canonical_path) const;
  bool unchanged() const;

 private:
  struct Impl;
  explicit ModelValidationLease(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
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
  std::uint64_t tensor_data_offset = 0;
  std::shared_ptr<ModelValidationLease> lease;
};

// Product verification has no caller-supplied size, digest, architecture, or
// tensor profile. These values are compiled from the accepted artifact
// identity above and are shared by verify-model and serve.
ModelValidationResult validate_product_model_file(const std::filesystem::path& path);

#ifdef LAE_ENABLE_TEST_MODEL_IDENTITY
// This API is compiled only into the standalone validator-test executable.
// It is absent from lae_runtime and every product/release engine binary.
struct TestModelProfile {
  std::string expected_filename;
  std::uint64_t expected_size_bytes = 0;
  std::string expected_sha256;
  std::string architecture = "qwen35";
  std::uint64_t expected_tensor_count = 0;
  bool require_product_metadata = false;
};

ModelValidationResult validate_model_file_for_tests(
    const std::filesystem::path& path, const TestModelProfile& profile);
#endif

}  // namespace lae
