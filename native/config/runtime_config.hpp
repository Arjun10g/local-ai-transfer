#pragma once

#include <cstdint>
#include <filesystem>
#include <string>

namespace lae {

struct RuntimeConfigFile {
  std::string model_path;
  std::string backend_profile = "cpu";
  unsigned context_tokens = 8192;
  unsigned gpu_layers = 0;
  std::string vulkan_device_name;
  std::string cuda_device_name;
};

bool load_runtime_config(const std::filesystem::path& path, RuntimeConfigFile& config,
                         std::string& error);

}  // namespace lae
