#include "runtime_config.hpp"

#include <cctype>
#include <fstream>
#include <limits>

namespace lae {
namespace {

constexpr size_t kMaxConfigBytes = 64 * 1024;

bool locate(const std::string& json, const char* key, size_t& position) {
  const std::string marker = std::string("\"") + key + "\"";
  position = json.find(marker);
  if (position == std::string::npos) return false;
  position = json.find(':', position + marker.size());
  return position != std::string::npos;
}

bool string_field(const std::string& json, const char* key, std::string& output, bool& present) {
  size_t position = 0;
  present = locate(json, key, position);
  if (!present) return true;
  ++position;
  while (position < json.size() && std::isspace(static_cast<unsigned char>(json[position]))) ++position;
  if (position >= json.size() || json[position++] != '"') return false;
  output.clear();
  while (position < json.size()) {
    const char character = json[position++];
    if (character == '"') return true;
    if (character == '\\') {
      if (position >= json.size()) return false;
      const char escaped = json[position++];
      if (escaped == '"' || escaped == '\\' || escaped == '/') output.push_back(escaped);
      else if (escaped == 'n') output.push_back('\n');
      else if (escaped == 'r') output.push_back('\r');
      else if (escaped == 't') output.push_back('\t');
      else return false;
    } else {
      if (static_cast<unsigned char>(character) < 0x20) return false;
      output.push_back(character);
    }
    if (output.size() > 4096) return false;
  }
  return false;
}

bool number_field(const std::string& json, const char* key, std::uint64_t& output, bool& present) {
  size_t position = 0;
  present = locate(json, key, position);
  if (!present) return true;
  ++position;
  while (position < json.size() && std::isspace(static_cast<unsigned char>(json[position]))) ++position;
  if (position >= json.size() || !std::isdigit(static_cast<unsigned char>(json[position]))) return false;
  const size_t start = position;
  while (position < json.size() && std::isdigit(static_cast<unsigned char>(json[position]))) ++position;
  try { output = std::stoull(json.substr(start, position - start)); }
  catch (...) { return false; }
  return position == json.size() || std::isspace(static_cast<unsigned char>(json[position])) || json[position] == ',' || json[position] == '}';
}

}  // namespace

bool load_runtime_config(const std::filesystem::path& path, RuntimeConfigFile& config, std::string& error) {
  error.clear();
  if (!path.is_absolute()) { error = "config path must be absolute"; return false; }
  std::ifstream input(path, std::ios::binary);
  if (!input) { error = "config file cannot be opened"; return false; }
  input.seekg(0, std::ios::end);
  const auto length = input.tellg();
  if (length < 2 || static_cast<std::uint64_t>(length) > kMaxConfigBytes) { error = "config file is empty or too large"; return false; }
  input.seekg(0);
  std::string json(static_cast<size_t>(length), '\0');
  input.read(json.data(), static_cast<std::streamsize>(json.size()));
  if (!input || json.front() != '{' || json.back() != '}') { error = "config must be a JSON object"; return false; }
  bool present = false;
  if (!string_field(json, "model_path", config.model_path, present) || !present || config.model_path.empty()) { error = "config requires model_path"; return false; }
  if (!string_field(json, "backend_profile", config.backend_profile, present) || (present && config.backend_profile.empty())) { error = "config backend_profile is invalid"; return false; }
  if (!string_field(json, "model_sha256", config.model_sha256, present)) { error = "config model_sha256 is invalid"; return false; }
  std::uint64_t number = 0;
  if (!number_field(json, "model_size_bytes", number, present)) { error = "config model_size_bytes is invalid"; return false; }
  if (present) config.model_size_bytes = number;
  if (!number_field(json, "context_tokens", number, present) || (present && (number < 1 || number > 16384))) { error = "config context_tokens is invalid"; return false; }
  if (present) config.context_tokens = static_cast<unsigned>(number);
  if (!number_field(json, "gpu_layers", number, present) || (present && number > 99)) { error = "config gpu_layers is invalid"; return false; }
  if (present) config.gpu_layers = static_cast<unsigned>(number);
  if (!string_field(json, "vulkan_device_name", config.vulkan_device_name, present)) { error = "config vulkan_device_name is invalid"; return false; }
  if (!string_field(json, "cuda_device_name", config.cuda_device_name, present)) { error = "config cuda_device_name is invalid"; return false; }
  return true;
}

}  // namespace lae
