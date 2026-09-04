#include "runtime_config.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

namespace lae {
namespace {

constexpr std::uint64_t kMaxConfigBytes = 64 * 1024;
constexpr size_t kMaxModelPathBytes = 4096;
constexpr size_t kMaxDeviceNameBytes = 256;

bool has_control_character(const std::string& value) {
  return std::any_of(value.begin(), value.end(), [](unsigned char character) {
    return character < 0x20 || character == 0x7f;
  });
}

bool unsafe_local_model_path(const std::string& value) {
  if (value.empty() || value.size() > kMaxModelPathBytes || has_control_character(value)) return true;
  if (value.rfind("\\\\", 0) == 0 || value.rfind("//", 0) == 0 ||
      value.rfind("\\\\?\\", 0) == 0 || value.rfind("\\\\.\\", 0) == 0) return true;
  const std::filesystem::path path(value);
  if (!path.is_absolute()) return true;
  const size_t colon = value.find(':');
#ifdef _WIN32
  return colon != std::string::npos &&
         (colon != 1 || !std::isalpha(static_cast<unsigned char>(value[0])) ||
          value.find(':', 2) != std::string::npos);
#else
  return colon != std::string::npos;
#endif
}

bool unsigned_integer(const nlohmann::json& value, std::uint64_t& output) {
  if (!value.is_number_unsigned()) return false;
  output = value.get<std::uint64_t>();
  return true;
}

}  // namespace

bool load_runtime_config(const std::filesystem::path& path, RuntimeConfigFile& config,
                         std::string& error) {
  error.clear();
  config = RuntimeConfigFile{};
  if (!path.is_absolute()) { error = "config path must be absolute"; return false; }
  std::error_code file_error;
  const auto status = std::filesystem::symlink_status(path, file_error);
  if (file_error || std::filesystem::is_symlink(status) || !std::filesystem::is_regular_file(status)) {
    error = "config path must be a regular non-link file";
    return false;
  }
  const auto length = std::filesystem::file_size(path, file_error);
  if (file_error || length < 2 || length > kMaxConfigBytes) {
    error = "config file is empty or too large";
    return false;
  }
  std::ifstream input(path, std::ios::binary);
  if (!input) { error = "config file cannot be opened"; return false; }
  std::string text(static_cast<size_t>(length), '\0');
  input.read(text.data(), static_cast<std::streamsize>(text.size()));
  if (!input || static_cast<size_t>(input.gcount()) != text.size()) {
    error = "config file could not be read completely";
    return false;
  }

  bool duplicate_key = false;
  std::set<std::string> observed_keys;
  nlohmann::json value;
  try {
    const auto duplicate_guard = [&](int, nlohmann::json::parse_event_t event,
                                     nlohmann::json& parsed) {
      if (event == nlohmann::json::parse_event_t::key) {
        const std::string key = parsed.get<std::string>();
        if (!observed_keys.insert(key).second) duplicate_key = true;
      }
      return true;
    };
    value = nlohmann::json::parse(text, duplicate_guard, true, false);
  } catch (const nlohmann::json::exception&) {
    error = "config is not valid strict JSON";
    return false;
  }
  if (duplicate_key) { error = "config contains a duplicate key"; return false; }
  if (!value.is_object()) { error = "config must be a JSON object"; return false; }

  static const std::set<std::string> allowed_keys = {
      "model_path", "backend_profile", "context_tokens", "gpu_layers",
      "vulkan_device_name", "cuda_device_name"};
  for (const auto& item : value.items()) {
    if (allowed_keys.find(item.key()) == allowed_keys.end()) {
      error = "config contains unknown key: " + item.key();
      return false;
    }
  }
  const auto model = value.find("model_path");
  if (model == value.end() || !model->is_string()) { error = "config requires string model_path"; return false; }
  config.model_path = model->get<std::string>();
  if (unsafe_local_model_path(config.model_path)) { error = "config model_path is unsafe or not absolute"; return false; }

  const auto backend = value.find("backend_profile");
  if (backend != value.end()) {
    if (!backend->is_string()) { error = "config backend_profile must be a string"; return false; }
    config.backend_profile = backend->get<std::string>();
  }
  if (config.backend_profile != "cpu" && config.backend_profile != "intel-vulkan" &&
      config.backend_profile != "cuda") {
    error = "config backend_profile is unsupported";
    return false;
  }

  std::uint64_t number = 0;
  const auto context = value.find("context_tokens");
  if (context != value.end()) {
    if (!unsigned_integer(*context, number) || number < 1 || number > 16384) {
      error = "config context_tokens must be an integer from 1 through 16384";
      return false;
    }
    config.context_tokens = static_cast<unsigned>(number);
  }
  const auto layers = value.find("gpu_layers");
  const bool layers_present = layers != value.end();
  if (layers_present) {
    if (!unsigned_integer(*layers, number) || number > 99) {
      error = "config gpu_layers must be an integer from 0 through 99";
      return false;
    }
    config.gpu_layers = static_cast<unsigned>(number);
  }
  const auto device = value.find("vulkan_device_name");
  const bool device_present = device != value.end();
  if (device_present) {
    if (!device->is_string()) { error = "config vulkan_device_name must be a string"; return false; }
    config.vulkan_device_name = device->get<std::string>();
    if (config.vulkan_device_name.empty() || config.vulkan_device_name.size() > kMaxDeviceNameBytes ||
        has_control_character(config.vulkan_device_name)) {
      error = "config vulkan_device_name is invalid";
      return false;
    }
  }
  const auto cuda_device = value.find("cuda_device_name");
  const bool cuda_device_present = cuda_device != value.end();
  if (cuda_device_present) {
    if (!cuda_device->is_string()) { error = "config cuda_device_name must be a string"; return false; }
    config.cuda_device_name = cuda_device->get<std::string>();
    if (config.cuda_device_name.empty() || config.cuda_device_name.size() > kMaxDeviceNameBytes ||
        has_control_character(config.cuda_device_name)) {
      error = "config cuda_device_name is invalid";
      return false;
    }
  }
  if (config.backend_profile == "cpu" &&
      ((layers_present && config.gpu_layers != 0) || device_present || cuda_device_present)) {
    error = "CPU config must not contain accelerated device or offload settings";
    return false;
  }
  if (config.backend_profile == "intel-vulkan" &&
      (!layers_present || config.gpu_layers < 1 || !device_present || cuda_device_present)) {
    error = "intel-vulkan config requires gpu_layers 1..99 and only exact vulkan_device_name";
    return false;
  }
  if (config.backend_profile == "cuda" &&
      (!layers_present || config.gpu_layers < 1 || !cuda_device_present || device_present)) {
    error = "cuda config requires gpu_layers 1..99 and only exact cuda_device_name";
    return false;
  }
  return true;
}

}  // namespace lae
