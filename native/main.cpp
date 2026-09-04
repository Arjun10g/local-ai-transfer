#include "backend/fixture_backend.hpp"
#include "engine/engine.hpp"
#include "backend/llama_backend.hpp"
#include "model_validation/model_validator.hpp"
#include "server/http_server.hpp"
#include "config/runtime_config.hpp"

#include <csignal>
#include <iostream>
#include <memory>
#include <string>
#include <cstdint>
#include <chrono>
#include <thread>
#include <cstring>
#include <stdexcept>
#include <fstream>
#include <filesystem>
#ifndef _WIN32
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace {
lae::Engine* active_engine = nullptr;
lae::HttpServer* active_server = nullptr;
volatile std::sig_atomic_t stop_requested = 0;
void on_signal(int) { stop_requested = 1; }
void usage() {
  std::cout << "lae-engine 0.1.0\ncommands: serve verify-model version print-build-info probe\nserve options: --config <absolute-json> --backend cpu|intel-vulkan --model <absolute-gguf> --size <bytes> --sha256 <hex> --context <tokens> --gpu-layers <0..99> (--token-file <protected-file> | --token-stdin)\n";
}

bool read_token_file(const std::string& path, std::string& token) {
  if (path.empty() || path.size() > 4096 || !std::filesystem::path(path).is_absolute()) return false;
#ifndef _WIN32
  const int descriptor = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (descriptor < 0) return false;
  struct stat file_stat{};
  if (fstat(descriptor, &file_stat) != 0 || !S_ISREG(file_stat.st_mode) || (file_stat.st_mode & 0077) != 0 || file_stat.st_size > 513) {
    close(descriptor); return false;
  }
  token.clear(); char buffer[128]; ssize_t count = 0;
  while ((count = ::read(descriptor, buffer, sizeof(buffer))) > 0) token.append(buffer, static_cast<size_t>(count));
  const bool read_ok = count == 0;
  close(descriptor);
  if (!read_ok || token.size() > 513) return false;
#else
  // Windows callers use the inherited stdin pipe. Without a platform-native
  // handle/ACL check, accepting a pathname would make the token file
  // protection unverifiable and raceable.
  (void)path;
  (void)token;
  return false;
#endif
  if (!token.empty() && token.back() == '\n') token.pop_back();
  if (!token.empty() && token.back() == '\r') token.pop_back();
  if (token.empty() || token.find_first_of("\r\n") != std::string::npos) return false;
  return true;
}

bool read_token_stdin(std::string& token) {
  token.clear(); char buffer[128];
  while (std::cin.good() && token.size() <= 513) {
    std::cin.read(buffer, sizeof(buffer));
    token.append(buffer, static_cast<size_t>(std::cin.gcount()));
  }
  if (token.size() > 513) return false;
  if (!token.empty() && token.back() == '\n') token.pop_back();
  if (!token.empty() && token.back() == '\r') token.pop_back();
  return !token.empty() && token.find_first_of("\r\n") == std::string::npos;
}

bool valid_bearer_token(const std::string& token) {
  if (token.size() < 16 || token.size() > 512) return false;
  return std::all_of(token.begin(), token.end(), [](unsigned char c) {
    return c >= 0x20 && c != 0x7f;
  });
}
}  // namespace

int main(int argc, char** argv) {
  const std::string command = argc > 1 ? argv[1] : "help";
  if (command == "version") { std::cout << "0.1.0\n"; return 0; }
  if (command == "print-build-info") {
#if LAE_COMPILED_LLAMA_CPP
#if LAE_ENABLE_LLAMA_VULKAN
    constexpr const char* compiled_backend = "llama.cpp/3581ba0c/vulkan";
#else
    constexpr const char* compiled_backend = "llama.cpp/3581ba0c/cpu";
#endif
#else
    constexpr const char* compiled_backend = "fixture-cpu/0.1.0";
#endif
    std::cout << "{\"engine_version\":\"0.1.0\",\"api_version\":\"0.1.0\",\"compiled_backend\":\"" << compiled_backend << "\",\"llama_cpp_revision\":\"3581ba0cf591b3f772fbb002de0f70e294bc0396\",\"selected_backend\":\"runtime-config\",\"model\":\"external-manifest\"}\n";
    return 0;
  }
  if (command == "probe") {
#if LAE_COMPILED_LLAMA_CPP
#if LAE_ENABLE_LLAMA_VULKAN
    constexpr const char* compiled_backend = "llama.cpp/3581ba0c/vulkan";
#else
    constexpr const char* compiled_backend = "llama.cpp/3581ba0c/cpu";
#endif
#else
    constexpr const char* compiled_backend = "fixture-cpu/0.1.0";
#endif
#ifdef _WIN32
    std::cout << "{\"bind\":\"127.0.0.1\",\"platform\":\"windows\",\"compiled_backend\":\"" << compiled_backend << "\"}\n";
#else
    std::cout << "{\"bind\":\"127.0.0.1\",\"platform\":\"posix\",\"compiled_backend\":\"" << compiled_backend << "\"}\n";
#endif
    return 0;
  }
  if (command != "serve" && command != "verify-model") { usage(); return command == "help" ? 0 : 2; }

  unsigned port = 0; unsigned context_tokens = 8192; unsigned gpu_layers = 20; std::uint64_t model_size = 0;
  std::string token; std::string token_file; bool token_file_seen = false; bool token_stdin = false; std::string backend = "fixture-cpu";
  std::string model_path; std::string model_sha256; std::string vulkan_device_name;
  std::string config_path; bool config_seen = false; bool backend_seen = false; bool model_seen = false;
  bool size_seen = false; bool hash_seen = false; bool context_seen = false; bool gpu_layers_seen = false;
  bool vulkan_device_seen = false;
  try {
  for (int i = 2; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--port" && i + 1 < argc) { size_t end = 0; const auto value = std::stoull(argv[++i], &end); if (end != std::strlen(argv[i]) || value > 65535) throw std::invalid_argument("invalid port"); port = static_cast<unsigned>(value); }
    else if (arg == "--token-file" && i + 1 < argc) { token_file = argv[++i]; token_file_seen = true; }
    else if (arg == "--token-stdin") { token_stdin = true; }
    else if (arg == "--backend" && i + 1 < argc) { backend = argv[++i]; backend_seen = true; }
    else if (arg == "--model" && i + 1 < argc) { model_path = argv[++i]; model_seen = true; }
    else if (arg == "--size" && i + 1 < argc) { size_t end = 0; model_size = std::stoull(argv[++i], &end); if (end != std::strlen(argv[i])) throw std::invalid_argument("invalid model size"); size_seen = true; }
    else if (arg == "--sha256" && i + 1 < argc) { model_sha256 = argv[++i]; hash_seen = true; }
    else if (arg == "--context" && i + 1 < argc) { size_t end = 0; const auto value = std::stoull(argv[++i], &end); if (end != std::strlen(argv[i]) || value > 16384) throw std::invalid_argument("invalid context"); context_tokens = static_cast<unsigned>(value); context_seen = true; }
    else if (arg == "--gpu-layers" && i + 1 < argc) { size_t end = 0; const auto value = std::stoull(argv[++i], &end); if (end != std::strlen(argv[i]) || value > 99) throw std::invalid_argument("invalid gpu layers"); gpu_layers = static_cast<unsigned>(value); gpu_layers_seen = true; }
    else if (arg == "--vulkan-device-name" && i + 1 < argc) { vulkan_device_name = argv[++i]; if (vulkan_device_name.empty() || vulkan_device_name.size() > 256) throw std::invalid_argument("invalid Vulkan device name"); vulkan_device_seen = true; }
    else if (arg == "--config" && i + 1 < argc) { config_path = argv[++i]; config_seen = true; }
    else { std::cerr << "unknown argument\n"; return 2; }
  }
  } catch (const std::exception& error) { std::cerr << "invalid numeric argument: " << error.what() << "\n"; return 2; }
  if (config_seen) {
    lae::RuntimeConfigFile file_config;
    std::string config_error;
    if (!lae::load_runtime_config(config_path, file_config, config_error)) { std::cerr << "config load failed: " << config_error << "\n"; return 2; }
    if (!backend_seen) backend = file_config.backend_profile;
    if (!model_seen) model_path = file_config.model_path;
    if (!size_seen) model_size = file_config.model_size_bytes;
    if (!hash_seen) model_sha256 = file_config.model_sha256;
    if (!context_seen) context_tokens = file_config.context_tokens;
    if (!gpu_layers_seen) gpu_layers = file_config.gpu_layers;
    if (!vulkan_device_seen) vulkan_device_name = file_config.vulkan_device_name;
  }
  if (command == "verify-model") {
    if (model_path.empty()) { std::cerr << "verify-model requires --model\n"; return 2; }
    lae::ModelProfile profile; profile.expected_size_bytes = model_size; profile.expected_sha256 = model_sha256;
    const auto result = lae::validate_model_file(model_path, profile);
    std::cout << "{\"valid\":" << (result.valid ? "true" : "false") << ",\"code\":\"" << result.code << "\",\"size_bytes\":" << result.size_bytes << ",\"sha256\":\"" << result.sha256 << "\",\"gguf_version\":" << result.gguf_version << "}\n";
    return result.valid ? 0 : 2;
  }
  if (token_file_seen == token_stdin || (token_file_seen && !read_token_file(token_file, token)) ||
      (token_stdin && !read_token_stdin(token)) || !valid_bearer_token(token)) {
    std::cerr << "serve requires exactly one readable bearer token source (16-512 printable bytes)\n"; return 2;
  }
  if (context_tokens < 1 || context_tokens > 16384) { std::cerr << "context must be between 1 and 16384 tokens\n"; return 2; }
  std::unique_ptr<lae::EngineBackend> backend_instance;
  lae::BackendConfig backend_config; backend_config.backend_profile = backend; backend_config.context_tokens = context_tokens; backend_config.gpu_layers = gpu_layers; backend_config.vulkan_device_name = vulkan_device_name;
  if (backend == "fixture-cpu") {
    if (!model_path.empty()) { std::cerr << "fixture backend does not accept a model path\n"; return 2; }
    backend_instance = std::make_unique<lae::FixtureBackend>();
  } else if (backend == "cpu" || backend == "intel-vulkan") {
    if (model_path.empty()) { std::cerr << "cpu backend requires --model\n"; return 2; }
    if (backend == "intel-vulkan" && (gpu_layers < 1 || gpu_layers > 99)) { std::cerr << "intel-vulkan requires 1..99 gpu layers\n"; return 2; }
    if (backend == "intel-vulkan" && vulkan_device_name.empty()) { std::cerr << "intel-vulkan requires an exact Vulkan device name\n"; return 2; }
    lae::ModelProfile profile; profile.expected_size_bytes = model_size; profile.expected_sha256 = model_sha256;
    const auto result = lae::validate_model_file(model_path, profile);
    if (!result.valid) { std::cerr << "model validation failed: " << result.code << "\n"; return 2; }
    backend_config.model_path = result.canonical_path;
    backend_config.model_id = "qwen35-9b-q4-k-m";
    backend_instance = std::make_unique<lae::LlamaBackend>();
  } else { std::cerr << "unsupported backend profile\n"; return 2; }
  lae::Engine engine(std::move(backend_instance));
  try { engine.initialize(backend_config); } catch (const std::exception& error) { std::cerr << error.what() << "\n"; return 1; }
  lae::HttpServer server(engine, token);
  try { server.start(port); } catch (const std::exception& error) { std::cerr << error.what() << "\n"; return 1; }
  active_engine = &engine; active_server = &server;
  std::signal(SIGINT, on_signal); std::signal(SIGTERM, on_signal);
  std::cout << "{\"event\":\"ready\",\"port\":" << server.port() << ",\"bind\":\"127.0.0.1\",\"token_required\":true}\n" << std::flush;
  while (!stop_requested) std::this_thread::sleep_for(std::chrono::milliseconds(50));
  engine.cancel_all();
  server.stop();
  engine.stop();
  active_server = nullptr; active_engine = nullptr;
  return 0;
}
