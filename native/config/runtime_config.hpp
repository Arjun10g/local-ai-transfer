#pragma once

#include <cstdint>
#include <filesystem>
#include <string>

namespace lae {

struct RuntimeConfigFile {
  std::string model_path;
  std::string backend_profile = "cpu";
  std::uint64_t model_size_bytes = 0;
  std::string model_sha256;
  unsigned context_tokens = 8192;
  unsigned gpu_layers = 20;
  std::string vulkan_device_name;
  std::string cuda_device_name;
};

bool load_runtime_config(const std::filesystem::path& path, RuntimeConfigFile& config,
                         std::string& error);

}  // namespace lae
