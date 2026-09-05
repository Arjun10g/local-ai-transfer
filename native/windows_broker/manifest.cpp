#include "manifest.hpp"

#include <algorithm>
#include <cctype>
#include <charconv>
#include <cwctype>
#include <initializer_list>
#include <limits>
#include <set>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#error "The Windows process broker manifest parser is Windows-only"
#endif

namespace lae::windows_broker {
namespace {

bool exact_keys(const nlohmann::json& value,
                std::initializer_list<std::string_view> allowed,
                std::initializer_list<std::string_view> required) {
  if (!value.is_object()) return false;
  for (const auto& item : value.items()) {
    if (std::find(allowed.begin(), allowed.end(), item.key()) == allowed.end())
      return false;
  }
  for (const auto key : required) {
    if (!value.contains(std::string(key))) return false;
  }
  return true;
}

bool ascii_id(const std::string& value, std::size_t maximum, bool dotted = false) {
  if (value.empty() || value.size() > maximum) return false;
  return std::all_of(value.begin(), value.end(), [dotted](unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '_' || c == '-' ||
           (dotted && c == '.');
  });
}

bool lower_hex(const std::string& value, std::size_t length) {
  return value.size() == length &&
         std::all_of(value.begin(), value.end(), [](unsigned char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

bool bounded_control_free_text(const std::string& value, std::size_t maximum) {
  return !value.empty() && value.size() <= maximum &&
         std::none_of(value.begin(), value.end(), [](unsigned char c) {
           return c < 0x20 || c == 0x7f;
         });
}

bool local_absolute_windows_path(const std::string& value) {
  if (value.size() < 4 || value.size() > 32767 ||
      !std::isalpha(static_cast<unsigned char>(value[0])) || value[1] != ':' ||
      (value[2] != '\\' && value[2] != '/')) return false;
  if (value.rfind("\\\\", 0) == 0 || value.rfind("//", 0) == 0 ||
      value.rfind("\\\\?\\", 0) == 0 || value.rfind("\\\\.\\", 0) == 0)
    return false;
  for (std::size_t index = 0; index < value.size(); ++index) {
    const unsigned char c = static_cast<unsigned char>(value[index]);
    if (c < 0x20 || c == 0x7f || c == '"' || c == '<' || c == '>' || c == '|')
      return false;
    if (c == ':' && index != 1) return false;  // Alternate data stream/device syntax.
  }
  return true;
}

bool explicit_exe_path(const std::string& value) {
  if (value.size() < 4) return false;
  const auto suffix = value.substr(value.size() - 4);
  return suffix[0] == '.' &&
         std::tolower(static_cast<unsigned char>(suffix[1])) == 'e' &&
         std::tolower(static_cast<unsigned char>(suffix[2])) == 'x' &&
         std::tolower(static_cast<unsigned char>(suffix[3])) == 'e';
}

std::wstring widen_utf8(const std::string& value) {
#ifdef _WIN32
  if (value.empty()) return {};
  const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                        value.data(), static_cast<int>(value.size()),
                                        nullptr, 0);
  if (count <= 0) return {};
  std::wstring output(static_cast<std::size_t>(count), L'\0');
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                          static_cast<int>(value.size()), output.data(), count) != count)
    return {};
  return output;
#else
  (void)value;
  return {};
#endif
}

bool unsigned_value(const nlohmann::json& value, std::uint64_t maximum,
                    std::uint64_t& output) {
  if (!value.is_number_unsigned()) return false;
  output = value.get<std::uint64_t>();
  return output <= maximum;
}

std::optional<ActionKind> action_kind(const std::string& value) {
  if (value == "process") return ActionKind::kProcess;
  if (value == "application") return ActionKind::kApplication;
  if (value == "browser") return ActionKind::kBrowser;
  if (value == "copilot") return ActionKind::kCopilot;
  if (value == "clipboard_read") return ActionKind::kClipboardRead;
  if (value == "clipboard_write") return ActionKind::kClipboardWrite;
  return std::nullopt;
}

std::optional<ExecutableClass> executable_class(const std::string& value) {
  if (value == "process_tool") return ExecutableClass::kProcessTool;
  if (value == "application") return ExecutableClass::kApplication;
  if (value == "browser") return ExecutableClass::kBrowser;
  if (value == "copilot") return ExecutableClass::kCopilot;
  return std::nullopt;
}

std::optional<ParameterKind> parameter_kind(const std::string& value) {
  if (value == "utf8") return ParameterKind::kUtf8;
  if (value == "https_url") return ParameterKind::kHttpsUrl;
  if (value == "unsigned_decimal") return ParameterKind::kUnsignedDecimal;
  if (value == "workspace_relative") return ParameterKind::kWorkspaceRelative;
  return std::nullopt;
}

std::optional<ParameterPlacement> parameter_placement(const std::string& value) {
  if (value == "argv") return ParameterPlacement::kArgv;
  if (value == "stdin") return ParameterPlacement::kStdin;
  if (value == "clipboard") return ParameterPlacement::kClipboard;
  return std::nullopt;
}

bool valid_https_url(const std::string& value);
bool valid_workspace_relative(const std::string& value);

bool valid_unsigned_decimal(const std::string& value) {
  if (value.empty() || value.size() > 10 ||
      (value.size() > 1 && value.front() == '0') ||
      !std::all_of(value.begin(), value.end(), [](unsigned char c) {
        return c >= '0' && c <= '9';
      })) return false;
  std::uint64_t parsed = 0;
  const auto result =
      std::from_chars(value.data(), value.data() + value.size(), parsed);
  return result.ec == std::errc{} && result.ptr == value.data() + value.size() &&
         parsed <= 0x7fffffffu;
}

bool valid_declared_value(ParameterKind kind, const std::string& value) {
  if (!bounded_control_free_text(value, 64 * 1024)) return false;
  if (kind == ParameterKind::kUnsignedDecimal)
    return valid_unsigned_decimal(value);
  if (kind == ParameterKind::kHttpsUrl) return valid_https_url(value);
  if (kind == ParameterKind::kWorkspaceRelative)
    return valid_workspace_relative(value);
  return true;
}

bool parse_directory(const nlohmann::json& value, DirectoryIdentitySpec& output) {
  if (!exact_keys(value,
                  {"id", "absolute_path", "volume_serial", "file_id_128"},
                  {"id", "absolute_path", "volume_serial", "file_id_128"}) ||
      !value["id"].is_string() || !value["absolute_path"].is_string() ||
      !value["file_id_128"].is_string()) return false;
  output.id = value["id"].get<std::string>();
  const auto path = value["absolute_path"].get<std::string>();
  output.file_id_128 = value["file_id_128"].get<std::string>();
  if (!ascii_id(output.id, 64) || !local_absolute_windows_path(path) ||
      !lower_hex(output.file_id_128, 32) ||
      !unsigned_value(value["volume_serial"],
                      std::numeric_limits<std::uint64_t>::max(),
                      output.volume_serial)) return false;
  output.absolute_path = widen_utf8(path);
  return !output.absolute_path.empty();
}

bool parse_executable(const nlohmann::json& value, FileIdentitySpec& output) {
  if (!exact_keys(value,
                  {"id", "class", "absolute_path", "size_bytes", "sha256",
                   "volume_serial", "file_id_128"},
                  {"id", "class", "absolute_path", "size_bytes", "sha256",
                   "volume_serial", "file_id_128"}) ||
      !value["id"].is_string() || !value["class"].is_string() ||
      !value["absolute_path"].is_string() ||
      !value["sha256"].is_string() || !value["file_id_128"].is_string())
    return false;
  output.id = value["id"].get<std::string>();
  const auto parsed_class =
      executable_class(value["class"].get<std::string>());
  const std::string path = value["absolute_path"].get<std::string>();
  output.file_id_128 = value["file_id_128"].get<std::string>();
  output.sha256 = value["sha256"].get<std::string>();
  if (!ascii_id(output.id, 64) || !parsed_class ||
      !local_absolute_windows_path(path) || !explicit_exe_path(path) ||
      !lower_hex(output.file_id_128, 32) || !lower_hex(output.sha256, 64) ||
      !unsigned_value(value["volume_serial"],
                      std::numeric_limits<std::uint64_t>::max(),
                      output.volume_serial) ||
      !unsigned_value(value["size_bytes"], kMaxExecutableBytes,
                      output.size_bytes) || output.size_bytes == 0)
    return false;
  output.executable_class = *parsed_class;
  output.absolute_path = widen_utf8(path);
  return !output.absolute_path.empty() &&
         output.size_bytes <= kMaxExecutableBytes;
}

bool parse_parameter(const nlohmann::json& value, ParameterSpec& output) {
  if (!exact_keys(value,
                  {"name", "kind", "placement", "max_bytes", "required",
                   "allowed_values"},
                  {"name", "kind", "placement", "max_bytes", "required"}) ||
      !value["name"].is_string() || !value["kind"].is_string() ||
      !value["placement"].is_string() || !value["required"].is_boolean())
    return false;
  output.name = value["name"].get<std::string>();
  const auto kind = parameter_kind(value["kind"].get<std::string>());
  const auto placement =
      parameter_placement(value["placement"].get<std::string>());
  std::uint64_t max_bytes = 0;
  if (!ascii_id(output.name, 64, true) || !kind || !placement ||
      !unsigned_value(value["max_bytes"], 64 * 1024, max_bytes) || max_bytes == 0)
    return false;
  output.kind = *kind;
  output.placement = *placement;
  output.max_bytes = static_cast<std::uint32_t>(max_bytes);
  output.required = value["required"].get<bool>();
  if (value.contains("allowed_values")) {
    if (!value["allowed_values"].is_array() ||
        value["allowed_values"].empty() || value["allowed_values"].size() > 32)
      return false;
    std::set<std::string> unique;
    for (const auto& allowed : value["allowed_values"]) {
      if (!allowed.is_string()) return false;
      const auto candidate = allowed.get<std::string>();
      if (candidate.empty() || candidate.size() > output.max_bytes ||
          !valid_declared_value(output.kind, candidate) ||
          !unique.insert(candidate).second) return false;
      output.allowed_values.push_back(candidate);
    }
  }
  return true;
}

bool launch_kind(ActionKind kind) {
  return kind == ActionKind::kProcess || kind == ActionKind::kApplication ||
         kind == ActionKind::kBrowser || kind == ActionKind::kCopilot;
}

bool forbidden_executable_host(const std::wstring& path) {
  const auto slash = path.find_last_of(L"\\/");
  std::wstring name = path.substr(slash == std::wstring::npos ? 0 : slash + 1);
  std::transform(name.begin(), name.end(), name.begin(),
                 [](wchar_t c) { return static_cast<wchar_t>(std::towlower(c)); });
  static const std::set<std::wstring> forbidden = {
      L"at.exe",        L"bash.exe",       L"bitsadmin.exe", L"certreq.exe",
      L"certutil.exe",  L"cmd.exe",        L"cmstp.exe",     L"conhost.exe",
      L"control.exe",   L"cscript.exe",    L"csi.exe",       L"curl.exe",
      L"dfsvc.exe",     L"dllhost.exe",    L"dotnet.exe",    L"esentutl.exe",
      L"expand.exe",    L"explorer.exe",   L"extrac32.exe",  L"forfiles.exe",
      L"ftp.exe",       L"git.exe",        L"hh.exe",        L"ieexec.exe",
      L"installutil.exe", L"java.exe",     L"makecab.exe",   L"mavinject.exe",
      L"msbuild.exe",   L"msdt.exe",       L"mshta.exe",     L"msiexec.exe",
      L"node.exe",      L"odbcconf.exe",   L"pcalua.exe",    L"powershell.exe",
      L"presentationhost.exe", L"pwsh.exe", L"python.exe",   L"pythonw.exe",
      L"reg.exe",       L"regasm.exe",     L"regsvcs.exe",   L"regsvr32.exe",
      L"rundll32.exe",  L"sc.exe",         L"schtasks.exe",  L"sh.exe",
      L"ssh.exe",       L"syncappvpublishingserver.exe",      L"tar.exe",
      L"verclsid.exe",  L"wevtutil.exe",   L"wmic.exe",      L"wscript.exe",
  };
  return forbidden.find(name) != forbidden.end();
}

bool parse_action(const nlohmann::json& value, ActionSpec& output) {
  if (!exact_keys(
          value,
          {"action_id", "kind", "executable_id", "cwd_id",
           "argv_template_id", "action_policy_id", "confinement_profile",
           "argv_template", "parameters", "timeout_ms",
           "stdout_limit_bytes", "stderr_limit_bytes", "max_processes",
           "visible", "environment_profile"},
          {"action_id", "kind", "parameters", "timeout_ms",
           "stdout_limit_bytes", "stderr_limit_bytes", "max_processes",
           "visible", "environment_profile"}) ||
      !value["action_id"].is_string() || !value["kind"].is_string() ||
      !value["visible"].is_boolean() ||
      !value["environment_profile"].is_string() ||
      value["environment_profile"] != "systemroot-broker-temp-v1") return false;
  output.action_id = value["action_id"].get<std::string>();
  const auto kind = action_kind(value["kind"].get<std::string>());
  if (!ascii_id(output.action_id, 96, true) || !kind) return false;
  output.kind = *kind;
  output.visible = value["visible"].get<bool>();

  std::uint64_t number = 0;
  if (!unsigned_value(value["timeout_ms"], 120000, number) || number < 100)
    return false;
  output.timeout_ms = static_cast<std::uint32_t>(number);
  if (!unsigned_value(value["stdout_limit_bytes"], kMaxCapturedStreamBytes, number))
    return false;
  output.stdout_limit_bytes = static_cast<std::uint32_t>(number);
  if (!unsigned_value(value["stderr_limit_bytes"], kMaxCapturedStreamBytes, number))
    return false;
  output.stderr_limit_bytes = static_cast<std::uint32_t>(number);
  if (!unsigned_value(value["max_processes"], 32, number) || number == 0)
    return false;
  output.max_processes = static_cast<std::uint32_t>(number);

  if (!value["parameters"].is_array() || value["parameters"].size() > 16)
    return false;
  std::set<std::string> parameter_names;
  std::size_t stdin_parameters = 0;
  std::size_t clipboard_parameters = 0;
  for (const auto& parameter_value : value["parameters"]) {
    ParameterSpec parameter;
    if (!parse_parameter(parameter_value, parameter) ||
        !parameter.required || !parameter_names.insert(parameter.name).second)
      return false;
    stdin_parameters += parameter.placement == ParameterPlacement::kStdin;
    clipboard_parameters += parameter.placement == ParameterPlacement::kClipboard;
    output.parameters.push_back(std::move(parameter));
  }
  if (stdin_parameters > 1 || clipboard_parameters > 1) return false;

  if (launch_kind(output.kind)) {
    for (const char* field : {"executable_id", "cwd_id", "argv_template_id",
                              "action_policy_id", "confinement_profile",
                              "argv_template"}) {
      if (!value.contains(field)) return false;
    }
    if (!value["executable_id"].is_string() || !value["cwd_id"].is_string() ||
        !value["argv_template_id"].is_string() ||
        !value["action_policy_id"].is_string() ||
        !value["confinement_profile"].is_string() ||
        !value["argv_template"].is_array() ||
        value["argv_template"].empty() || value["argv_template"].size() > 64)
      return false;
    output.executable_id = value["executable_id"].get<std::string>();
    output.cwd_id = value["cwd_id"].get<std::string>();
    output.argv_template_id = value["argv_template_id"].get<std::string>();
    output.action_policy_id = value["action_policy_id"].get<std::string>();
    output.confinement_profile =
        value["confinement_profile"].get<std::string>();
    if (!ascii_id(output.executable_id, 64) || !ascii_id(output.cwd_id, 64) ||
        !ascii_id(output.argv_template_id, 64, true) ||
        !ascii_id(output.action_policy_id, 64, true) ||
        output.confinement_profile != "appcontainer-low-integrity-v1")
      return false;
    std::map<std::string, std::size_t> used_parameters;
    for (const auto& part : value["argv_template"]) {
      if (!part.is_object() || part.size() != 1) return false;
      ArgumentPart parsed;
      if (part.contains("literal") && part["literal"].is_string()) {
        parsed.literal = true;
        parsed.value = part["literal"].get<std::string>();
        if (!bounded_control_free_text(parsed.value, 4096)) return false;
      } else if (part.contains("parameter") && part["parameter"].is_string()) {
        parsed.literal = false;
        parsed.value = part["parameter"].get<std::string>();
        const auto parameter = std::find_if(
            output.parameters.begin(), output.parameters.end(), [&](const auto& item) {
              return item.name == parsed.value &&
                     item.placement == ParameterPlacement::kArgv;
            });
        if (parameter == output.parameters.end()) return false;
        ++used_parameters[parsed.value];
      } else {
        return false;
      }
      output.argv_template.push_back(std::move(parsed));
    }
    for (const auto& parameter : output.parameters)
      if (parameter.placement == ParameterPlacement::kArgv &&
          used_parameters[parameter.name] != 1)
        return false;

    if (output.kind == ActionKind::kApplication) {
      if (!output.parameters.empty()) return false;
    } else if (output.kind == ActionKind::kBrowser) {
      if (output.parameters.size() != 1 || output.parameters[0].name != "url" ||
          output.parameters[0].kind != ParameterKind::kHttpsUrl ||
          output.parameters[0].placement != ParameterPlacement::kArgv ||
          output.parameters[0].max_bytes > 2048 || !output.visible)
        return false;
    } else if (output.kind == ActionKind::kCopilot) {
      if (output.parameters.size() != 1 ||
          output.parameters[0].name != "prompt" ||
          output.parameters[0].kind != ParameterKind::kUtf8 ||
          output.parameters[0].placement != ParameterPlacement::kStdin ||
          output.parameters[0].max_bytes > 64 * 1024 || output.visible)
        return false;
    } else if (output.kind == ActionKind::kProcess) {
      for (const auto& parameter : output.parameters) {
        if (parameter.placement != ParameterPlacement::kArgv ||
            parameter.kind == ParameterKind::kWorkspaceRelative ||
            parameter.allowed_values.empty())
          return false;
      }
    }
  } else {
    for (const char* forbidden : {"executable_id", "cwd_id", "argv_template_id",
                                  "action_policy_id", "confinement_profile",
                                  "argv_template"}) {
      if (value.contains(forbidden)) return false;
    }
    if (output.kind == ActionKind::kClipboardRead && !output.parameters.empty())
      return false;
    if (output.kind == ActionKind::kClipboardWrite &&
        (clipboard_parameters != 1 || output.parameters.size() != 1 ||
         output.parameters[0].kind != ParameterKind::kUtf8)) return false;
    if (output.kind == ActionKind::kClipboardRead &&
        (output.stdout_limit_bytes == 0 || output.stdout_limit_bytes > 64 * 1024 ||
         output.stderr_limit_bytes != 0 || output.timeout_ms > 5000 ||
         output.max_processes != 1 || output.visible))
      return false;
    if (output.kind == ActionKind::kClipboardWrite &&
        (output.stdout_limit_bytes != 0 || output.stderr_limit_bytes != 0 ||
         output.parameters[0].max_bytes > 64 * 1024 ||
         output.timeout_ms > 5000 || output.max_processes != 1 ||
         output.visible))
      return false;
  }
  return true;
}

bool valid_https_url(const std::string& value) {
  // This is intentionally conservative and only a first lexical gate. A later
  // browser integration must use WinHTTP/URI parsing and policy-check the final
  // destination; this parser never accepts credentials, fragments, or controls.
  if (value.size() < 9 || value.size() > 2048 || value.rfind("https://", 0) != 0)
    return false;
  const auto authority_end = value.find_first_of("/?#", 8);
  const auto authority = value.substr(8, authority_end == std::string::npos
                                             ? std::string::npos
                                             : authority_end - 8);
  std::string lower_authority = authority;
  std::transform(lower_authority.begin(), lower_authority.end(),
                 lower_authority.begin(), [](unsigned char c) {
                   return static_cast<char>(std::tolower(c));
                 });
  if (authority.empty() || authority.size() > 253 ||
      authority.find('@') != std::string::npos ||
      authority.find(':') != std::string::npos || value.find('#') != std::string::npos ||
      lower_authority == "localhost" || authority.front() == '.' ||
      authority.back() == '.' || authority.find("..") != std::string::npos ||
      !std::all_of(authority.begin(), authority.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
               (c >= '0' && c <= '9') || c == '-' || c == '.';
      }))
    return false;
  std::size_t label_start = 0;
  while (label_start < authority.size()) {
    const auto label_end = authority.find('.', label_start);
    const auto label = authority.substr(
        label_start, label_end == std::string::npos
                         ? std::string::npos
                         : label_end - label_start);
    if (label.empty() || label.size() > 63 || label.front() == '-' ||
        label.back() == '-') return false;
    if (label_end == std::string::npos) break;
    label_start = label_end + 1;
  }
  return std::none_of(value.begin(), value.end(), [](unsigned char c) {
    return c <= 0x20 || c == 0x7f || c == '\\' || c == '"' || c == '<' ||
           c == '>' || c == '{' || c == '}' || c == '|' || c == '^' ||
           c == '`';
  });
}

bool valid_workspace_relative(const std::string& value) {
  if (value.empty() || value.size() > 4096 || value.front() == '/' ||
      value.front() == '\\' || value.find(':') != std::string::npos)
    return false;
  std::size_t start = 0;
  while (start <= value.size()) {
    const auto end = value.find_first_of("/\\", start);
    const auto part = value.substr(start, end == std::string::npos
                                             ? std::string::npos
                                             : end - start);
    if (part.empty() || part == "." || part == "..") return false;
    if (end == std::string::npos) break;
    start = end + 1;
  }
  return true;
}

}  // namespace

bool parse_product_manifest(const nlohmann::json& value, BrokerManifest& manifest,
                            std::string& error_code) noexcept {
  error_code = "manifest_invalid";
  try {
    if (!exact_keys(value,
                    {"schema", "mode", "manifest_id", "runtime_directory_id",
                     "executables", "directories", "actions"},
                    {"schema", "mode", "manifest_id", "runtime_directory_id",
                     "executables", "directories", "actions"}) ||
        value["schema"] != "lae.windows-broker.manifest.v1" ||
        value["mode"] != "product" || !value["manifest_id"].is_string() ||
        !value["runtime_directory_id"].is_string() ||
        !value["executables"].is_array() || !value["directories"].is_array() ||
        !value["actions"].is_array() || value["executables"].empty() ||
        value["executables"].size() > kMaxExecutables ||
        value["directories"].empty() ||
        value["directories"].size() > kMaxDirectories ||
        value["actions"].empty() || value["actions"].size() > kMaxActions)
      return false;

    BrokerManifest parsed;
    parsed.manifest_id = value["manifest_id"].get<std::string>();
    parsed.runtime_directory_id = value["runtime_directory_id"].get<std::string>();
    if (!ascii_id(parsed.manifest_id, 64, true) ||
        !ascii_id(parsed.runtime_directory_id, 64)) return false;
    for (const auto& item : value["executables"]) {
      FileIdentitySpec spec;
      if (!parse_executable(item, spec) ||
          !parsed.executables.emplace(spec.id, spec).second) return false;
    }
    for (const auto& item : value["directories"]) {
      DirectoryIdentitySpec spec;
      if (!parse_directory(item, spec) ||
          !parsed.directories.emplace(spec.id, spec).second) return false;
    }
    if (parsed.directories.find(parsed.runtime_directory_id) ==
        parsed.directories.end()) return false;
    for (const auto& item : value["actions"]) {
      ActionSpec spec;
      if (!parse_action(item, spec) || !parsed.actions.emplace(spec.action_id, spec).second)
        return false;
      const auto& inserted = parsed.actions.at(spec.action_id);
      if (launch_kind(inserted.kind) &&
          (parsed.executables.find(inserted.executable_id) ==
               parsed.executables.end() ||
           parsed.directories.find(inserted.cwd_id) == parsed.directories.end() ||
           forbidden_executable_host(
               parsed.executables.at(inserted.executable_id).absolute_path)))
        return false;
      if (launch_kind(inserted.kind)) {
        const auto executable_class =
            parsed.executables.at(inserted.executable_id).executable_class;
        const bool class_matches =
            (inserted.kind == ActionKind::kProcess &&
             executable_class == ExecutableClass::kProcessTool) ||
            (inserted.kind == ActionKind::kApplication &&
             executable_class == ExecutableClass::kApplication) ||
            (inserted.kind == ActionKind::kBrowser &&
             executable_class == ExecutableClass::kBrowser) ||
            (inserted.kind == ActionKind::kCopilot &&
             executable_class == ExecutableClass::kCopilot);
        if (!class_matches) return false;
      }
    }
    manifest = std::move(parsed);
    error_code.clear();
    return true;
  } catch (...) {
    error_code = "manifest_invalid";
    return false;
  }
}

bool action_arguments_match(const ActionSpec& action,
                            const nlohmann::json& arguments,
                            std::string& error_code) noexcept {
  error_code = "arguments_rejected";
  try {
    if (!arguments.is_object() || arguments.size() > action.parameters.size())
      return false;
    for (const auto& item : arguments.items()) {
      if (std::none_of(action.parameters.begin(), action.parameters.end(),
                       [&](const auto& parameter) { return parameter.name == item.key(); }))
        return false;
    }
    for (const auto& parameter : action.parameters) {
      if (!arguments.contains(parameter.name)) {
        if (parameter.required) return false;
        continue;
      }
      const auto& value = arguments.at(parameter.name);
      if (parameter.kind == ParameterKind::kUnsignedDecimal) {
        if (!value.is_number_unsigned() || value.get<std::uint64_t>() > 0x7fffffffu)
          return false;
        const auto rendered = std::to_string(value.get<std::uint64_t>());
        if (rendered.size() > parameter.max_bytes ||
            (!parameter.allowed_values.empty() &&
                std::find(parameter.allowed_values.begin(),
                          parameter.allowed_values.end(), rendered) ==
                    parameter.allowed_values.end()))
          return false;
        continue;
      }
      if (!value.is_string()) return false;
      const auto& text = value.get_ref<const std::string&>();
      if (text.empty() || text.size() > parameter.max_bytes ||
          std::any_of(text.begin(), text.end(), [](unsigned char c) {
            return c < 0x20 || c == 0x7f;
          })) return false;
      if (!parameter.allowed_values.empty() &&
          std::find(parameter.allowed_values.begin(), parameter.allowed_values.end(), text) ==
              parameter.allowed_values.end()) return false;
      if (parameter.kind == ParameterKind::kHttpsUrl && !valid_https_url(text))
        return false;
      if (parameter.kind == ParameterKind::kWorkspaceRelative &&
          !valid_workspace_relative(text)) return false;
    }
    error_code.clear();
    return true;
  } catch (...) {
    error_code = "arguments_rejected";
    return false;
  }
}

}  // namespace lae::windows_broker
