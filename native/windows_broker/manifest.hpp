#pragma once

#include <cstdint>
#include <cstddef>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

namespace lae::windows_broker {

inline constexpr std::size_t kMaxManifestBytes = 256 * 1024;
inline constexpr std::size_t kMaxExecutables = 32;
inline constexpr std::size_t kMaxDirectories = 32;
inline constexpr std::size_t kMaxActions = 64;
inline constexpr std::uint64_t kMaxExecutableBytes = 512ull * 1024 * 1024;
inline constexpr std::uint32_t kMaxCapturedStreamBytes = 256 * 1024;

enum class ActionKind {
  kProcess,
  kApplication,
  kBrowser,
  kCopilot,
  kClipboardRead,
  kClipboardWrite,
};

enum class ExecutableClass {
  kProcessTool,
  kApplication,
  kBrowser,
  kCopilot,
};

enum class ParameterKind { kUtf8, kHttpsUrl, kUnsignedDecimal, kWorkspaceRelative };
enum class ParameterPlacement { kArgv, kStdin, kClipboard };

struct FileIdentitySpec {
  std::string id;
  ExecutableClass executable_class = ExecutableClass::kProcessTool;
  std::wstring absolute_path;
  std::uint64_t size_bytes = 0;
  std::string sha256;
  std::uint64_t volume_serial = 0;
  std::string file_id_128;
};

struct DirectoryIdentitySpec {
  std::string id;
  std::wstring absolute_path;
  std::uint64_t volume_serial = 0;
  std::string file_id_128;
};

struct ParameterSpec {
  std::string name;
  ParameterKind kind = ParameterKind::kUtf8;
  ParameterPlacement placement = ParameterPlacement::kArgv;
  std::uint32_t max_bytes = 0;
  bool required = false;
  std::vector<std::string> allowed_values;
};

struct ArgumentPart {
  bool literal = true;
  std::string value;
};

struct ActionSpec {
  std::string action_id;
  ActionKind kind = ActionKind::kProcess;
  std::string executable_id;
  std::string cwd_id;
  std::string argv_template_id;
  std::string action_policy_id;
  std::string confinement_profile;
  std::vector<ArgumentPart> argv_template;
  std::vector<ParameterSpec> parameters;
  std::uint32_t timeout_ms = 0;
  std::uint32_t stdout_limit_bytes = 0;
  std::uint32_t stderr_limit_bytes = 0;
  std::uint32_t max_processes = 1;
  bool visible = false;
};

struct BrokerManifest {
  std::string manifest_id;
  std::string runtime_directory_id;
  std::map<std::string, FileIdentitySpec> executables;
  std::map<std::string, DirectoryIdentitySpec> directories;
  std::map<std::string, ActionSpec> actions;
};

// Parses an already authenticated manifest. The caller must compare the exact
// document bytes to the compiled trust anchor before calling this function.
// Product loading rejects mode != "product" even when the JSON schema accepts a
// fixture-mode document.
bool parse_product_manifest(const nlohmann::json& value, BrokerManifest& manifest,
                            std::string& error_code) noexcept;

bool action_arguments_match(const ActionSpec& action,
                            const nlohmann::json& arguments,
                            std::string& error_code) noexcept;

}  // namespace lae::windows_broker
