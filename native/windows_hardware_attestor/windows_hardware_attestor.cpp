#include "windows_hardware_attestor.hpp"
#include "trust_anchor.hpp"

#include <bcrypt.h>
#include <devguid.h>
#include <dxgi1_6.h>
#include <intrin.h>
#include <setupapi.h>
#include <softpub.h>
#include <wincrypt.h>
#include <winternl.h>
#include <wintrust.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cstddef>
#include <cstdio>
#include <cwchar>
#include <cstring>
#include <iterator>
#include <limits>
#include <memory>
#include <new>
#include <numeric>
#include <set>
#include <string_view>
#include <utility>
#include <vector>

namespace lae::windows_hardware_attestor {
namespace {

constexpr std::size_t kMaximumPathCharacters = 32'767;
constexpr std::size_t kMaximumStringBytes = 512;
constexpr std::size_t kHashChunkBytes = 65'536;
constexpr DWORD kIdentityShare = FILE_SHARE_READ;  // deny write/delete
constexpr char kSchema[] = "lae.windows-native-hardware-attestation.v1";
constexpr char kProfile[] = "dell-039nng-a00-core-ultra-7-vpro-intel-graphics";
constexpr wchar_t kManifestLeafWide[] = L"windows-hardware-attestor.manifest.json";
constexpr wchar_t kDiagnosticScriptLeaf[] = L"Get-HardwareReceipt.ps1";
constexpr std::array<char, 16> kHex = {'0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f'};

class UniqueHandle final {
 public:
  UniqueHandle() noexcept = default;
  explicit UniqueHandle(HANDLE value) noexcept : value_(value) {}
  ~UniqueHandle() { reset(); }
  UniqueHandle(const UniqueHandle&) = delete;
  UniqueHandle& operator=(const UniqueHandle&) = delete;
  UniqueHandle(UniqueHandle&& other) noexcept : value_(other.release()) {}
  UniqueHandle& operator=(UniqueHandle&& other) noexcept {
    if (this != &other) reset(other.release());
    return *this;
  }
  HANDLE get() const noexcept { return value_; }
  explicit operator bool() const noexcept {
    return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
  }
  HANDLE release() noexcept {
    HANDLE value = value_;
    value_ = INVALID_HANDLE_VALUE;
    return value;
  }
  void reset(HANDLE value = INVALID_HANDLE_VALUE) noexcept {
    if (*this) CloseHandle(value_);
    value_ = value;
  }
 private:
  HANDLE value_ = INVALID_HANDLE_VALUE;
};

template <typename T>
class ComPtr final {
 public:
  ComPtr() noexcept = default;
  ~ComPtr() { reset(); }
  ComPtr(const ComPtr&) = delete;
  ComPtr& operator=(const ComPtr&) = delete;
  T* get() const noexcept { return value_; }
  T** put() noexcept { reset(); return &value_; }
  T* operator->() const noexcept { return value_; }
  explicit operator bool() const noexcept { return value_ != nullptr; }
  void reset() noexcept { if (value_) value_->Release(); value_ = nullptr; }
 private:
  T* value_ = nullptr;
};

class DeviceInfoSet final {
 public:
  explicit DeviceInfoSet(HDEVINFO value) noexcept : value_(value) {}
  ~DeviceInfoSet() {
    if (value_ != INVALID_HANDLE_VALUE) SetupDiDestroyDeviceInfoList(value_);
  }
  DeviceInfoSet(const DeviceInfoSet&) = delete;
  DeviceInfoSet& operator=(const DeviceInfoSet&) = delete;
 private:
  HDEVINFO value_ = INVALID_HANDLE_VALUE;
};

struct FileProof {
  UniqueHandle handle;
  std::uint64_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
  std::uint64_t size = 0;
  std::array<std::uint8_t, 32> sha256{};
};

struct CpuEvidence {
  std::string vendor;
  std::string brand;
  std::uint32_t family = 0;
  std::uint32_t model = 0;
  std::uint32_t stepping = 0;
  std::uint32_t physical_cores = 0;
  std::uint32_t logical_processors = 0;
  std::uint32_t performance_cores = 0;
  std::uint32_t efficiency_cores = 0;
  bool topology_complete = false;
  bool aes = false;
  bool avx = false;
  bool avx2 = false;
  bool bmi1 = false;
  bool bmi2 = false;
  bool fma = false;
  bool popcnt = false;
  bool sse42 = false;
};

struct MemoryEvidence {
  std::uint32_t slot_index = 0;
  std::uint64_t capacity_bytes = 0;
  std::uint32_t speed_mts = 0;
  std::uint32_t configured_speed_mts = 0;
  std::uint32_t memory_type = 0;
  std::uint32_t data_width = 0;
};

struct FirmwareEvidence {
  std::string bios_vendor;
  std::string bios_version;
  std::string bios_date;
  std::string system_manufacturer;
  std::string system_product;
  std::string board_manufacturer;
  std::string board_product;
  std::string board_revision;
  std::vector<MemoryEvidence> memory;
};

struct DisplayDeviceEvidence {
  std::string hardware_tuple;
  std::string hardware_tuple_sha256;
  std::uint32_t vendor_id = 0;
  std::uint32_t device_id = 0;
  std::uint32_t subsystem_id = 0;
  std::uint32_t revision = 0;
  std::string driver_version;
  std::string driver_date;
  std::string provider;
  std::string driver_signature_status = "unproven";
};

struct AdapterEvidence {
  std::uint32_t ordinal = 0;
  std::string name;
  std::uint32_t vendor_id = 0;
  std::uint32_t device_id = 0;
  std::uint32_t subsystem_id = 0;
  std::uint32_t revision = 0;
  std::uint64_t dedicated_video_bytes = 0;
  std::uint64_t dedicated_system_bytes = 0;
  std::uint64_t shared_system_bytes = 0;
  std::uint32_t luid_high = 0;
  std::uint32_t luid_low = 0;
  bool software = false;
  std::string integrated_classification = "unproven";
  std::string pnp_hardware_tuple_sha256;
  std::string driver_version;
  bool correlation_exact = false;
};

struct Evidence {
  std::string generated_at_utc;
  bool windows_x64 = false;
  bool self_identity = false;
  bool manifest_identity = false;
  bool script_identity = false;
  bool authenticode_valid = false;
  std::string executable_sha256;
  std::string manifest_sha256;
  std::string script_sha256;
  std::string signer_certificate_sha256;
  std::string windows_version;
  std::string windows_build;
  bool secure_boot = false;
  bool secure_boot_known = false;
  std::uint64_t total_physical_memory = 0;
  CpuEvidence cpu;
  FirmwareEvidence firmware;
  std::vector<DisplayDeviceEvidence> display_devices;
  std::vector<AdapterEvidence> adapters;
  bool vulkan_present = false;
  std::string vulkan_sha256;
  std::string vpro_status = "unproven";
  std::vector<std::string> reasons;
};

bool cancelled_or_expired(const CollectionContext& context) noexcept {
  return context.cancelled == nullptr ||
         context.cancelled->load(std::memory_order_acquire) ||
         context.started_tick_ms == 0 || context.deadline_tick_ms == 0 ||
         context.deadline_tick_ms <= context.started_tick_ms ||
         context.deadline_tick_ms - context.started_tick_ms > kGlobalDeadlineMs ||
         GetTickCount64() >= context.deadline_tick_ms;
}

bool all_zero(const std::uint8_t* value, std::size_t size) noexcept {
  std::uint8_t aggregate = 0;
  for (std::size_t i = 0; i < size; ++i) aggregate |= value[i];
  return aggregate == 0;
}

bool equal_bytes(const std::uint8_t* left, const std::uint8_t* right,
                 std::size_t size) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t i = 0; i < size; ++i) difference |= left[i] ^ right[i];
  return difference == 0;
}

std::string hex(const std::uint8_t* value, std::size_t size) {
  std::string output;
  output.reserve(size * 2);
  for (std::size_t i = 0; i < size; ++i) {
    output.push_back(kHex[value[i] >> 4]);
    output.push_back(kHex[value[i] & 0x0f]);
  }
  return output;
}

bool safe_text(std::string& value, std::size_t maximum = kMaximumStringBytes) {
  if (value.empty() || value.size() > maximum) return false;
  for (unsigned char byte : value) {
    // Hardware identity fields are intentionally restricted to printable
    // ASCII. This is a valid UTF-8 subset and avoids bidi/format controls.
    if (byte < 0x20 || byte >= 0x7f || byte == '\\' || byte == '@')
      return false;
  }
  // Permit punctuation used by firmware dates and adapter names, but never a
  // drive-qualified, UNC-like, or rooted path-shaped value.
  if (value.front() == '/' || value.rfind("//", 0) == 0 ||
      (value.size() >= 2 && std::isalpha(
          static_cast<unsigned char>(value[0])) && value[1] == ':') ||
      value.find(":/") != std::string::npos)
    return false;
  return true;
}

bool wide_utf8(const wchar_t* value, std::size_t length, std::string& output,
               std::size_t maximum = kMaximumStringBytes) {
  if (value == nullptr || length == 0 || length > 4096 ||
      length > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  const int required = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
      value, static_cast<int>(length), nullptr, 0, nullptr, nullptr);
  if (required <= 0 || static_cast<std::size_t>(required) > maximum) return false;
  output.assign(static_cast<std::size_t>(required), '\0');
  if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value,
      static_cast<int>(length), output.data(), required, nullptr, nullptr) != required)
    return false;
  return safe_text(output, maximum);
}

void append_json_string(std::string& output, std::string_view value) {
  output.push_back('"');
  for (unsigned char byte : value) {
    if (byte == '"' || byte == '\\') output.push_back('\\');
    output.push_back(static_cast<char>(byte));
  }
  output.push_back('"');
}

void add_reason(Evidence& evidence, std::string reason) {
  if (reason.empty() || reason.size() > 64) return;
  if (std::find(evidence.reasons.begin(), evidence.reasons.end(), reason) ==
      evidence.reasons.end()) evidence.reasons.push_back(std::move(reason));
}

bool file_shape(HANDLE handle, std::size_t maximum, std::uint64_t& size,
                std::uint64_t& volume_serial,
                std::array<std::uint8_t, 16>& file_id) {
  if (GetFileType(handle) != FILE_TYPE_DISK) return false;
  FILE_ATTRIBUTE_TAG_INFO tag{};
  FILE_STANDARD_INFO standard{};
  FILE_ID_INFO identity{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag, sizeof(tag)) ||
      !GetFileInformationByHandleEx(handle, FileStandardInfo, &standard, sizeof(standard)) ||
      !GetFileInformationByHandleEx(handle, FileIdInfo, &identity, sizeof(identity)))
    return false;
  constexpr DWORD rejected = FILE_ATTRIBUTE_DIRECTORY |
      FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE |
      FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_SPARSE_FILE |
      FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED |
      FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS |
      FILE_ATTRIBUTE_VIRTUAL;
  if ((tag.FileAttributes & rejected) != 0 || standard.Directory ||
      standard.DeletePending || standard.NumberOfLinks != 1 ||
      standard.EndOfFile.QuadPart <= 0 ||
      static_cast<std::uint64_t>(standard.EndOfFile.QuadPart) > maximum)
    return false;
  size = static_cast<std::uint64_t>(standard.EndOfFile.QuadPart);
  volume_serial = identity.VolumeSerialNumber;
  std::copy(std::begin(identity.FileId.Identifier),
            std::end(identity.FileId.Identifier), file_id.begin());
  return volume_serial != 0 && !all_zero(file_id.data(), file_id.size());
}

bool sha256_handle(HANDLE handle, std::uint64_t exact_size,
                   const CollectionContext& context,
                   std::array<std::uint8_t, 32>& output) {
  LARGE_INTEGER zero{};
  if (!SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN)) return false;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD returned = 0;
  std::vector<std::uint8_t> object;
  bool ok = false;
  if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
          &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(
          algorithm, BCRYPT_OBJECT_LENGTH,
          reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
          &returned, 0)) || object_size == 0 || object_size > 1'048'576)
    goto cleanup;
  object.resize(object_size);
  if (!BCRYPT_SUCCESS(BCryptCreateHash(algorithm, &hash, object.data(),
                                       object_size, nullptr, 0, 0)))
    goto cleanup;
  {
    std::array<std::uint8_t, kHashChunkBytes> buffer{};
    std::uint64_t consumed = 0;
    while (consumed < exact_size) {
      if (cancelled_or_expired(context)) goto cleanup;
      const DWORD wanted = static_cast<DWORD>(
          std::min<std::uint64_t>(buffer.size(), exact_size - consumed));
      DWORD count = 0;
      if (!ReadFile(handle, buffer.data(), wanted, &count, nullptr) ||
          count == 0 || count > wanted || consumed > exact_size - count)
        goto cleanup;
      if (!BCRYPT_SUCCESS(BCryptHashData(hash, buffer.data(), count, 0)))
        goto cleanup;
      consumed += count;
    }
    std::uint8_t extra = 0;
    DWORD count = 0;
    if (!ReadFile(handle, &extra, 1, &count, nullptr) || count != 0)
      goto cleanup;
    if (!BCRYPT_SUCCESS(BCryptFinishHash(
            hash, output.data(), static_cast<ULONG>(output.size()), 0)))
      goto cleanup;
    ok = true;
  }
cleanup:
  if (hash) BCryptDestroyHash(hash);
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN);
  return ok;
}

bool final_path(HANDLE handle, std::wstring& output) {
  std::vector<wchar_t> buffer(kMaximumPathCharacters + 1);
  const DWORD count = GetFinalPathNameByHandleW(
      handle, buffer.data(), static_cast<DWORD>(buffer.size()),
      FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (count == 0 || count >= buffer.size()) return false;
  output.assign(buffer.data(), count);
  return output.rfind(L"\\\\?\\", 0) == 0;
}

bool open_proof(const std::wstring& path, std::size_t maximum,
                const CollectionContext& context, FileProof& proof) {
  UniqueHandle handle(CreateFileW(
      path.c_str(), GENERIC_READ | FILE_READ_ATTRIBUTES | READ_CONTROL,
      kIdentityShare, nullptr, OPEN_EXISTING,
      FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
  if (!handle || !file_shape(handle.get(), maximum, proof.size,
                             proof.volume_serial, proof.file_id) ||
      !sha256_handle(handle.get(), proof.size, context, proof.sha256))
    return false;
  proof.handle = std::move(handle);
  return true;
}

bool sibling_path(const std::wstring& executable, const wchar_t* leaf,
                  std::wstring& output) {
  if (executable.empty() || executable.size() > kMaximumPathCharacters ||
      leaf == nullptr || std::wcschr(leaf, L'\\') != nullptr ||
      std::wcschr(leaf, L'/') != nullptr || std::wcschr(leaf, L':') != nullptr)
    return false;
  const std::size_t separator = executable.find_last_of(L"\\/");
  if (separator == std::wstring::npos) return false;
  output.assign(executable.data(), separator + 1);
  output.append(leaf);
  return output.size() <= kMaximumPathCharacters;
}

bool verify_authenticode_offline(const std::wstring& executable,
                                 std::array<std::uint8_t, 32>& certificate_hash) {
  WINTRUST_FILE_INFO file{};
  file.cbStruct = sizeof(file);
  file.pcwszFilePath = executable.c_str();
  GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
  WINTRUST_DATA data{};
  data.cbStruct = sizeof(data);
  data.dwUIChoice = WTD_UI_NONE;
  data.fdwRevocationChecks = WTD_REVOKE_NONE;
  data.dwUnionChoice = WTD_CHOICE_FILE;
  data.pFile = &file;
  data.dwStateAction = WTD_STATEACTION_VERIFY;
  data.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL;
  const LONG trust = WinVerifyTrust(INVALID_HANDLE_VALUE, &action, &data);
  data.dwStateAction = WTD_STATEACTION_CLOSE;
  WinVerifyTrust(INVALID_HANDLE_VALUE, &action, &data);
  if (trust != ERROR_SUCCESS) return false;

  HCERTSTORE store = nullptr;
  HCRYPTMSG message = nullptr;
  DWORD encoding = 0;
  DWORD content = 0;
  DWORD format = 0;
  if (!CryptQueryObject(CERT_QUERY_OBJECT_FILE, executable.c_str(),
                        CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
                        CERT_QUERY_FORMAT_FLAG_BINARY, 0, &encoding, &content,
                        &format, &store, &message, nullptr))
    return false;
  DWORD signer_size = 0;
  bool ok = false;
  std::vector<std::uint8_t> signer;
  PCCERT_CONTEXT certificate = nullptr;
  if (!CryptMsgGetParam(message, CMSG_SIGNER_INFO_PARAM, 0, nullptr,
                        &signer_size) || signer_size == 0 ||
      signer_size > 65'536)
    goto cleanup;
  signer.resize(signer_size);
  if (!CryptMsgGetParam(message, CMSG_SIGNER_INFO_PARAM, 0, signer.data(),
                        &signer_size))
    goto cleanup;
  {
    const auto* info = reinterpret_cast<const CMSG_SIGNER_INFO*>(signer.data());
    CERT_INFO search{};
    search.Issuer = info->Issuer;
    search.SerialNumber = info->SerialNumber;
    certificate = CertFindCertificateInStore(
        store, encoding, 0, CERT_FIND_SUBJECT_CERT, &search, nullptr);
    if (!certificate) goto cleanup;
    DWORD hash_size = static_cast<DWORD>(certificate_hash.size());
    if (!CertGetCertificateContextProperty(certificate,
          CERT_SHA256_HASH_PROP_ID, certificate_hash.data(), &hash_size) ||
        hash_size != certificate_hash.size())
      goto cleanup;
    ok = true;
  }
cleanup:
  if (certificate) CertFreeCertificateContext(certificate);
  if (message) CryptMsgClose(message);
  if (store) CertCloseStore(store, 0);
  return ok;
}

bool verify_self_and_manifest(const CollectionContext& context,
                              Evidence& evidence, FileProof& executable,
                              FileProof& manifest, FileProof& script) {
  std::vector<wchar_t> module(kMaximumPathCharacters + 1);
  const DWORD count = GetModuleFileNameW(nullptr, module.data(),
                                         static_cast<DWORD>(module.size()));
  if (count == 0 || count >= module.size()) return false;
  const std::wstring executable_path(module.data(), count);
  std::wstring manifest_path;
  std::wstring script_path;
  if (!sibling_path(executable_path, kManifestLeafWide, manifest_path) ||
      !sibling_path(executable_path, kDiagnosticScriptLeaf, script_path) ||
      !open_proof(executable_path, kMaximumExecutableBytes, context, executable) ||
      !open_proof(manifest_path, kMaximumManifestBytes, context, manifest) ||
      !open_proof(script_path, kMaximumManifestBytes, context, script))
    return false;
  std::wstring final_executable;
  std::wstring final_manifest;
  std::wstring final_script;
  if (!final_path(executable.handle.get(), final_executable) ||
      !final_path(manifest.handle.get(), final_manifest) ||
      !final_path(script.handle.get(), final_script))
    return false;
  const auto parent = [](const std::wstring& value) {
    const auto index = value.find_last_of(L"\\/");
    return index == std::wstring::npos ? std::wstring() : value.substr(0, index);
  };
  if (parent(final_executable) != parent(final_manifest) ||
      parent(final_executable) != parent(final_script))
    return false;
  evidence.executable_sha256 = hex(executable.sha256.data(), executable.sha256.size());
  evidence.manifest_sha256 = hex(manifest.sha256.data(), manifest.sha256.size());
  evidence.script_sha256 = hex(script.sha256.data(), script.sha256.size());
  evidence.self_identity = executable.size == trust_anchor::kExpectedExecutableSize &&
      equal_bytes(executable.sha256.data(),
                  trust_anchor::kExpectedExecutableSha256.data(), 32);
  evidence.manifest_identity = equal_bytes(
      manifest.sha256.data(), trust_anchor::kExpectedManifestSha256.data(), 32);
  evidence.script_identity = equal_bytes(
      script.sha256.data(), trust_anchor::kExpectedDiagnosticScriptSha256.data(), 32);
  std::array<std::uint8_t, 32> signer{};
  evidence.authenticode_valid = verify_authenticode_offline(executable_path, signer) &&
      equal_bytes(signer.data(),
                  trust_anchor::kExpectedSignerCertificateSha256.data(), 32);
  FileProof reopened_executable;
  if (evidence.authenticode_valid &&
      (!open_proof(executable_path, kMaximumExecutableBytes, context,
                   reopened_executable) ||
       reopened_executable.volume_serial != executable.volume_serial ||
       reopened_executable.file_id != executable.file_id ||
       reopened_executable.size != executable.size ||
       !equal_bytes(reopened_executable.sha256.data(), executable.sha256.data(),
                    executable.sha256.size())))
    evidence.authenticode_valid = false;
  evidence.signer_certificate_sha256 = hex(signer.data(), signer.size());
  return evidence.self_identity && evidence.manifest_identity &&
         evidence.script_identity && evidence.authenticode_valid;
}

bool collect_os(Evidence& evidence) {
  RTL_OSVERSIONINFOW version{};
  version.dwOSVersionInfoSize = sizeof(version);
  if (RtlGetVersion(&version) != 0) return false;
  char text[64]{};
  const int version_count = std::snprintf(
      text, sizeof(text), "%lu.%lu.%lu", version.dwMajorVersion,
      version.dwMinorVersion, version.dwBuildNumber);
  if (version_count <= 0 || static_cast<std::size_t>(version_count) >= sizeof(text))
    return false;
  evidence.windows_version.assign(text, static_cast<std::size_t>(version_count));
  const int build_count = std::snprintf(text, sizeof(text), "%lu",
                                        version.dwBuildNumber);
  if (build_count <= 0 || static_cast<std::size_t>(build_count) >= sizeof(text))
    return false;
  evidence.windows_build.assign(text, static_cast<std::size_t>(build_count));
  SYSTEM_INFO system{};
  GetNativeSystemInfo(&system);
  evidence.windows_x64 =
      system.wProcessorArchitecture == PROCESSOR_ARCHITECTURE_AMD64;
  DWORD secure_boot = 0;
  DWORD size = sizeof(secure_boot);
  const LSTATUS status = RegGetValueW(
      HKEY_LOCAL_MACHINE,
      L"SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State",
      L"UEFISecureBootEnabled", RRF_RT_REG_DWORD, nullptr, &secure_boot, &size);
  if (status == ERROR_SUCCESS && size == sizeof(secure_boot) &&
      (secure_boot == 0 || secure_boot == 1)) {
    evidence.secure_boot_known = true;
    evidence.secure_boot = secure_boot == 1;
  }
  MEMORYSTATUSEX memory{};
  memory.dwLength = sizeof(memory);
  if (!GlobalMemoryStatusEx(&memory)) return false;
  evidence.total_physical_memory = memory.ullTotalPhys;
  return evidence.windows_x64;
}

std::uint32_t population_count(ULONG_PTR value) {
#if defined(_WIN64)
  return static_cast<std::uint32_t>(__popcnt64(value));
#else
  return static_cast<std::uint32_t>(__popcnt(value));
#endif
}

bool collect_cpu(Evidence& evidence) {
  int registers[4]{};
  __cpuidex(registers, 0, 0);
  const std::uint32_t maximum_leaf = static_cast<std::uint32_t>(registers[0]);
  if (maximum_leaf < 7) return false;
  char vendor[13]{};
  std::memcpy(vendor, &registers[1], 4);
  std::memcpy(vendor + 4, &registers[3], 4);
  std::memcpy(vendor + 8, &registers[2], 4);
  evidence.cpu.vendor.assign(vendor, 12);
  if (evidence.cpu.vendor != "GenuineIntel") return false;
  __cpuidex(registers, 1, 0);
  const std::uint32_t eax = static_cast<std::uint32_t>(registers[0]);
  const std::uint32_t ecx = static_cast<std::uint32_t>(registers[2]);
  const std::uint32_t base_family = (eax >> 8) & 0xf;
  const std::uint32_t base_model = (eax >> 4) & 0xf;
  const std::uint32_t ext_family = (eax >> 20) & 0xff;
  const std::uint32_t ext_model = (eax >> 16) & 0xf;
  evidence.cpu.family = base_family == 0xf ? base_family + ext_family : base_family;
  evidence.cpu.model = (base_family == 0x6 || base_family == 0xf)
      ? base_model + (ext_model << 4) : base_model;
  evidence.cpu.stepping = eax & 0xf;
  evidence.cpu.sse42 = (ecx & (1u << 20)) != 0;
  evidence.cpu.popcnt = (ecx & (1u << 23)) != 0;
  evidence.cpu.aes = (ecx & (1u << 25)) != 0;
  evidence.cpu.avx = (ecx & (1u << 28)) != 0;
  evidence.cpu.fma = (ecx & (1u << 12)) != 0;
  __cpuidex(registers, 7, 0);
  const std::uint32_t ebx = static_cast<std::uint32_t>(registers[1]);
  evidence.cpu.bmi1 = (ebx & (1u << 3)) != 0;
  evidence.cpu.avx2 = (ebx & (1u << 5)) != 0;
  evidence.cpu.bmi2 = (ebx & (1u << 8)) != 0;

  __cpuidex(registers, static_cast<int>(0x80000000u), 0);
  const std::uint32_t maximum_extended = static_cast<std::uint32_t>(registers[0]);
  if (maximum_extended >= 0x80000004u) {
    std::array<int, 12> brand{};
    for (std::uint32_t leaf = 0; leaf < 3; ++leaf)
      __cpuidex(brand.data() + leaf * 4,
                static_cast<int>(0x80000002u + leaf), 0);
    std::string raw(reinterpret_cast<const char*>(brand.data()), 48);
    while (!raw.empty() && (raw.back() == '\0' || raw.back() == ' ')) raw.pop_back();
    while (!raw.empty() && raw.front() == ' ') raw.erase(raw.begin());
    if (safe_text(raw, 96)) evidence.cpu.brand = std::move(raw);
  }

  DWORD bytes = 0;
  GetLogicalProcessorInformationEx(RelationProcessorCore, nullptr, &bytes);
  if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || bytes == 0 ||
      bytes > kMaximumTopologyBytes) return false;
  std::vector<std::uint8_t> topology(bytes);
  if (!GetLogicalProcessorInformationEx(
          RelationProcessorCore,
          reinterpret_cast<PSYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX>(
              topology.data()), &bytes) || bytes != topology.size())
    return false;
  std::size_t offset = 0;
  while (offset < topology.size()) {
    if (topology.size() - offset <
        offsetof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX, Processor))
      return false;
    const auto* item = reinterpret_cast<
        const SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX*>(topology.data() + offset);
    if (item->Size < offsetof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX,
                              Processor.GroupMask) ||
        item->Size > topology.size() - offset ||
        item->Relationship != RelationProcessorCore ||
        item->Processor.GroupCount == 0 ||
        item->Processor.GroupCount > 64)
      return false;
    const std::size_t required =
        offsetof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX,
                 Processor.GroupMask) +
        static_cast<std::size_t>(item->Processor.GroupCount) *
            sizeof(GROUP_AFFINITY);
    if (required > item->Size) return false;
    ++evidence.cpu.physical_cores;
    if (item->Processor.EfficiencyClass == 0)
      ++evidence.cpu.efficiency_cores;
    else
      ++evidence.cpu.performance_cores;
    for (WORD group = 0; group < item->Processor.GroupCount; ++group)
      evidence.cpu.logical_processors +=
          population_count(item->Processor.GroupMask[group].Mask);
    offset += item->Size;
  }
  evidence.cpu.topology_complete = offset == topology.size() &&
      evidence.cpu.physical_cores > 0 &&
      evidence.cpu.logical_processors >= evidence.cpu.physical_cores;
  return evidence.cpu.topology_complete && !evidence.cpu.brand.empty();
}

std::string smbios_string(const std::uint8_t* structure,
                          std::size_t structure_bytes,
                          std::size_t formatted_length,
                          std::uint8_t index) {
  if (index == 0 || formatted_length >= structure_bytes) return {};
  const char* cursor = reinterpret_cast<const char*>(structure + formatted_length);
  const char* end = reinterpret_cast<const char*>(structure + structure_bytes);
  std::uint8_t current = 1;
  while (cursor < end && *cursor != '\0') {
    const char* terminator = std::find(cursor, end, '\0');
    if (terminator == end) return {};
    if (current == index) {
      std::string value(cursor, terminator);
      return safe_text(value, 128) ? value : std::string();
    }
    cursor = terminator + 1;
    ++current;
  }
  return {};
}

std::uint16_t read_u16(const std::uint8_t* value) {
  return static_cast<std::uint16_t>(value[0]) |
         static_cast<std::uint16_t>(value[1] << 8);
}

std::uint32_t read_u32(const std::uint8_t* value) {
  return static_cast<std::uint32_t>(value[0]) |
         (static_cast<std::uint32_t>(value[1]) << 8) |
         (static_cast<std::uint32_t>(value[2]) << 16) |
         (static_cast<std::uint32_t>(value[3]) << 24);
}

bool collect_smbios(Evidence& evidence) {
  const UINT32 provider = 'R' | ('S' << 8) | ('M' << 16) | ('B' << 24);
  const UINT bytes = GetSystemFirmwareTable(provider, 0, nullptr, 0);
  if (bytes < 8 || bytes > kMaximumFirmwareBytes) return false;
  std::vector<std::uint8_t> firmware(bytes);
  if (GetSystemFirmwareTable(provider, 0, firmware.data(), bytes) != bytes)
    return false;
  // RawSMBIOSData: four one-byte version/method fields plus uint32 length.
  const std::uint32_t table_bytes = read_u32(firmware.data() + 4);
  if (table_bytes == 0 || table_bytes != bytes - 8) return false;
  const std::uint8_t* table = firmware.data() + 8;
  std::size_t offset = 0;
  bool saw_end = false;
  std::set<std::uint16_t> handles;
  while (offset < table_bytes) {
    if (table_bytes - offset < 4) return false;
    const std::uint8_t type = table[offset];
    const std::size_t formatted = table[offset + 1];
    const std::uint16_t handle = read_u16(table + offset + 2);
    if (formatted < 4 || formatted > table_bytes - offset ||
        !handles.insert(handle).second) return false;
    std::size_t end = offset + formatted;
    while (end + 1 < table_bytes &&
           !(table[end] == 0 && table[end + 1] == 0)) ++end;
    if (end + 1 >= table_bytes) return false;
    const std::size_t structure_bytes = end + 2 - offset;
    const std::uint8_t* item = table + offset;
    if (type == 0 && formatted >= 9) {
      evidence.firmware.bios_vendor = smbios_string(item, structure_bytes, formatted, item[4]);
      evidence.firmware.bios_version = smbios_string(item, structure_bytes, formatted, item[5]);
      evidence.firmware.bios_date = smbios_string(item, structure_bytes, formatted, item[8]);
    } else if (type == 1 && formatted >= 6) {
      evidence.firmware.system_manufacturer = smbios_string(item, structure_bytes, formatted, item[4]);
      evidence.firmware.system_product = smbios_string(item, structure_bytes, formatted, item[5]);
    } else if (type == 2 && formatted >= 7) {
      evidence.firmware.board_manufacturer = smbios_string(item, structure_bytes, formatted, item[4]);
      evidence.firmware.board_product = smbios_string(item, structure_bytes, formatted, item[5]);
      evidence.firmware.board_revision = smbios_string(item, structure_bytes, formatted, item[6]);
    } else if (type == 17 && formatted >= 27) {
      if (evidence.firmware.memory.size() >= kMaximumMemoryDevices) return false;
      const std::uint16_t encoded_size = read_u16(item + 12);
      if (encoded_size != 0 && encoded_size != 0xffff) {
        MemoryEvidence memory{};
        memory.slot_index = static_cast<std::uint32_t>(evidence.firmware.memory.size());
        if (encoded_size == 0x7fff) {
          if (formatted < 32) return false;
          memory.capacity_bytes = static_cast<std::uint64_t>(read_u32(item + 28)) * 1'048'576;
        } else if ((encoded_size & 0x8000) != 0) {
          memory.capacity_bytes = static_cast<std::uint64_t>(encoded_size & 0x7fff) * 1024;
        } else {
          memory.capacity_bytes = static_cast<std::uint64_t>(encoded_size) * 1'048'576;
        }
        memory.data_width = read_u16(item + 10);
        memory.memory_type = item[18];
        memory.speed_mts = formatted >= 23 ? read_u16(item + 21) : 0;
        memory.configured_speed_mts = formatted >= 34 ? read_u16(item + 32) : 0;
        if (memory.capacity_bytes == 0) return false;
        evidence.firmware.memory.push_back(memory);
      }
    }
    offset += structure_bytes;
    if (type == 127) {
      saw_end = true;
      break;
    }
  }
  return saw_end && offset == table_bytes &&
      !evidence.firmware.bios_version.empty() &&
      !evidence.firmware.system_manufacturer.empty() &&
      !evidence.firmware.system_product.empty() &&
      !evidence.firmware.board_product.empty() &&
      !evidence.firmware.board_revision.empty() &&
      !evidence.firmware.memory.empty();
}

bool sha256_bytes(std::string_view input,
                  std::array<std::uint8_t, 32>& output) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD returned = 0;
  std::vector<std::uint8_t> object;
  bool ok = false;
  if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
          &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(
          algorithm, BCRYPT_OBJECT_LENGTH,
          reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
          &returned, 0)) || object_size == 0 || object_size > 1'048'576)
    goto cleanup;
  object.resize(object_size);
  if (!BCRYPT_SUCCESS(BCryptCreateHash(algorithm, &hash, object.data(),
                                       object_size, nullptr, 0, 0)) ||
      !BCRYPT_SUCCESS(BCryptHashData(
          hash, reinterpret_cast<PUCHAR>(const_cast<char*>(input.data())),
          static_cast<ULONG>(input.size()), 0)) ||
      !BCRYPT_SUCCESS(BCryptFinishHash(
          hash, output.data(), static_cast<ULONG>(output.size()), 0)))
    goto cleanup;
  ok = true;
cleanup:
  if (hash) BCryptDestroyHash(hash);
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  return ok;
}

bool parse_hex(std::wstring_view value, std::uint32_t& output) {
  if (value.empty() || value.size() > 8) return false;
  std::uint32_t result = 0;
  for (const wchar_t character : value) {
    std::uint32_t digit = 0;
    if (character >= L'0' && character <= L'9')
      digit = static_cast<std::uint32_t>(character - L'0');
    else if (character >= L'A' && character <= L'F')
      digit = static_cast<std::uint32_t>(character - L'A') + 10;
    else
      return false;
    if (result > (std::numeric_limits<std::uint32_t>::max() - digit) / 16)
      return false;
    result = result * 16 + digit;
  }
  output = result;
  return true;
}

bool token_value(std::wstring_view hardware_id, std::wstring_view prefix,
                 std::size_t digits, std::uint32_t& output) {
  const std::size_t position = hardware_id.find(prefix);
  if (position == std::wstring_view::npos ||
      position + prefix.size() + digits > hardware_id.size())
    return false;
  const std::size_t end = position + prefix.size() + digits;
  if (end < hardware_id.size() && hardware_id[end] != L'&') return false;
  return parse_hex(hardware_id.substr(position + prefix.size(), digits), output);
}

bool normalized_pci_tuple(std::wstring_view input,
                          DisplayDeviceEvidence& evidence) {
  if (input.size() < 20 || input.size() > 256) return false;
  std::wstring upper;
  upper.reserve(input.size());
  for (const wchar_t character : input) {
    if (character > 0x7f) return false;
    upper.push_back(character >= L'a' && character <= L'z'
        ? static_cast<wchar_t>(character - L'a' + L'A') : character);
  }
  if (upper.rfind(L"PCI\\VEN_", 0) != 0 ||
      !token_value(upper, L"VEN_", 4, evidence.vendor_id) ||
      !token_value(upper, L"DEV_", 4, evidence.device_id) ||
      !token_value(upper, L"SUBSYS_", 8, evidence.subsystem_id) ||
      !token_value(upper, L"REV_", 2, evidence.revision))
    return false;
  char tuple[96]{};
  const int count = std::snprintf(
      tuple, sizeof(tuple), "PCI;DEV=%04X;REV=%02X;SUBSYS=%08X;VEN=%04X",
      evidence.device_id, evidence.revision, evidence.subsystem_id,
      evidence.vendor_id);
  if (count <= 0 || static_cast<std::size_t>(count) >= sizeof(tuple))
    return false;
  evidence.hardware_tuple.assign(tuple, static_cast<std::size_t>(count));
  std::array<std::uint8_t, 32> digest{};
  constexpr char domain_separator[] = "lae.hardware.pnp-tuple.v1\0";
  std::string domain(domain_separator, sizeof(domain_separator) - 1);
  domain.append(evidence.hardware_tuple);
  if (!sha256_bytes(domain, digest)) return false;
  evidence.hardware_tuple_sha256 = hex(digest.data(), digest.size());
  return true;
}

bool registry_string(HKEY key, const wchar_t* name, std::string& output) {
  std::array<wchar_t, 512> buffer{};
  DWORD bytes = static_cast<DWORD>(buffer.size() * sizeof(wchar_t));
  DWORD type = 0;
  const LSTATUS status = RegGetValueW(
      key, nullptr, name, RRF_RT_REG_SZ | RRF_ZEROONFAILURE, &type,
      buffer.data(), &bytes);
  if (status != ERROR_SUCCESS || type != REG_SZ || bytes < sizeof(wchar_t) ||
      bytes > buffer.size() * sizeof(wchar_t) ||
      bytes % sizeof(wchar_t) != 0)
    return false;
  const std::size_t characters = bytes / sizeof(wchar_t);
  if (buffer[characters - 1] != L'\0') return false;
  std::size_t length = 0;
  while (length < characters && buffer[length] != L'\0') ++length;
  if (length == 0 || length + 1 != characters) return false;
  return wide_utf8(buffer.data(), length, output, 128);
}

bool display_hardware_tuple(HDEVINFO devices, SP_DEVINFO_DATA& item,
                            DisplayDeviceEvidence& output) {
  std::array<std::uint8_t, 4096> bytes{};
  DWORD type = 0;
  DWORD required = 0;
  if (!SetupDiGetDeviceRegistryPropertyW(
          devices, &item, SPDRP_HARDWAREID, &type, bytes.data(),
          static_cast<DWORD>(bytes.size()), &required) ||
      type != REG_MULTI_SZ || required < 2 * sizeof(wchar_t) ||
      required > bytes.size() || required % sizeof(wchar_t) != 0)
    return false;
  const auto* values = reinterpret_cast<const wchar_t*>(bytes.data());
  const std::size_t characters = required / sizeof(wchar_t);
  if (values[characters - 1] != L'\0' || values[characters - 2] != L'\0')
    return false;
  std::size_t offset = 0;
  bool found = false;
  while (offset + 1 < characters && values[offset] != L'\0') {
    std::size_t end = offset;
    while (end < characters && values[end] != L'\0') ++end;
    if (end == characters) return false;
    DisplayDeviceEvidence candidate;
    if (normalized_pci_tuple(
            std::wstring_view(values + offset, end - offset), candidate)) {
      if (found && candidate.hardware_tuple != output.hardware_tuple)
        return false;
      output = std::move(candidate);
      found = true;
    }
    offset = end + 1;
  }
  return found;
}

bool collect_display_devices(const CollectionContext& context,
                             Evidence& evidence) {
  HDEVINFO raw = SetupDiGetClassDevsW(
      &GUID_DEVCLASS_DISPLAY, nullptr, nullptr, DIGCF_PRESENT);
  if (raw == INVALID_HANDLE_VALUE) return false;
  DeviceInfoSet devices(raw);
  std::set<std::string> tuple_digests;
  for (DWORD index = 0; index <= kMaximumDisplayDevices; ++index) {
    if (cancelled_or_expired(context)) return false;
    SP_DEVINFO_DATA item{};
    item.cbSize = sizeof(item);
    if (!SetupDiEnumDeviceInfo(raw, index, &item)) {
      if (GetLastError() == ERROR_NO_MORE_ITEMS) break;
      return false;
    }
    if (index == kMaximumDisplayDevices) return false;
    DisplayDeviceEvidence device;
    if (!display_hardware_tuple(raw, item, device) ||
        !tuple_digests.insert(device.hardware_tuple_sha256).second)
      return false;
    HKEY key = SetupDiOpenDevRegKey(
        raw, &item, DICS_FLAG_GLOBAL, 0, DIREG_DRV, KEY_READ);
    if (key == INVALID_HANDLE_VALUE) return false;
    const bool properties = registry_string(key, L"DriverVersion",
                                             device.driver_version) &&
                            registry_string(key, L"DriverDate",
                                             device.driver_date) &&
                            registry_string(key, L"ProviderName",
                                             device.provider);
    RegCloseKey(key);
    if (!properties) return false;
    // SetupAPI registry metadata does not itself prove package signature.
    device.driver_signature_status = "unproven";
    evidence.display_devices.push_back(std::move(device));
  }
  return !evidence.display_devices.empty();
}

bool collect_dxgi(const CollectionContext& context, Evidence& evidence) {
  ComPtr<IDXGIFactory1> factory;
  if (FAILED(CreateDXGIFactory1(
          __uuidof(IDXGIFactory1),
          reinterpret_cast<void**>(factory.put()))))
    return false;
  std::set<std::pair<std::uint32_t, std::uint32_t>> luids;
  std::set<std::size_t> matched_display_devices;
  for (UINT index = 0; index <= kMaximumAdapters; ++index) {
    if (cancelled_or_expired(context)) return false;
    ComPtr<IDXGIAdapter1> adapter;
    const HRESULT enumerate = factory->EnumAdapters1(index, adapter.put());
    if (enumerate == DXGI_ERROR_NOT_FOUND) break;
    if (FAILED(enumerate) || index == kMaximumAdapters) return false;
    DXGI_ADAPTER_DESC1 description{};
    if (FAILED(adapter->GetDesc1(&description))) return false;
    AdapterEvidence result;
    result.ordinal = index;
    std::size_t name_length = 0;
    while (name_length < std::size(description.Description) &&
           description.Description[name_length] != L'\0') ++name_length;
    if (name_length == 0 || name_length == std::size(description.Description) ||
        !wide_utf8(description.Description, name_length, result.name, 256))
      return false;
    result.vendor_id = description.VendorId;
    result.device_id = description.DeviceId;
    result.subsystem_id = description.SubSysId;
    result.revision = description.Revision;
    result.dedicated_video_bytes = description.DedicatedVideoMemory;
    result.dedicated_system_bytes = description.DedicatedSystemMemory;
    result.shared_system_bytes = description.SharedSystemMemory;
    result.luid_high = static_cast<std::uint32_t>(description.AdapterLuid.HighPart);
    result.luid_low = description.AdapterLuid.LowPart;
    result.software = (description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
    if (!luids.insert({result.luid_high, result.luid_low}).second) return false;

    if (!result.software) {
      std::vector<std::size_t> matches;
      for (std::size_t display = 0;
           display < evidence.display_devices.size(); ++display) {
        const auto& candidate = evidence.display_devices[display];
        if (candidate.vendor_id == result.vendor_id &&
            candidate.device_id == result.device_id &&
            candidate.subsystem_id == result.subsystem_id &&
            candidate.revision == result.revision)
          matches.push_back(display);
      }
      if (matches.size() != 1 ||
          !matched_display_devices.insert(matches.front()).second)
        return false;
      const auto& match = evidence.display_devices[matches.front()];
      result.pnp_hardware_tuple_sha256 = match.hardware_tuple_sha256;
      result.driver_version = match.driver_version;
      result.correlation_exact = true;
    }
    // DXGI 1.6 memory fields do not authoritatively classify UMA/integrated.
    result.integrated_classification = "unproven";
    evidence.adapters.push_back(std::move(result));
  }
  return !evidence.adapters.empty();
}

bool collect_vulkan_presence(const CollectionContext& context,
                             Evidence& evidence) {
  std::vector<wchar_t> system(kMaximumPathCharacters + 1);
  const UINT count = GetSystemDirectoryW(
      system.data(), static_cast<UINT>(system.size()));
  if (count == 0 || count >= system.size()) return false;
  std::wstring path(system.data(), count);
  path.append(L"\\vulkan-1.dll");
  if (path.size() > kMaximumPathCharacters) return false;
  FileProof runtime;
  if (!open_proof(path, kMaximumRuntimeDllBytes, context, runtime)) return false;
  evidence.vulkan_present = true;
  evidence.vulkan_sha256 = hex(runtime.sha256.data(), runtime.sha256.size());
  return true;
}

bool utc_now(std::string& output) {
  SYSTEMTIME time{};
  GetSystemTime(&time);
  char text[32]{};
  const int count = std::snprintf(
      text, sizeof(text), "%04u-%02u-%02uT%02u:%02u:%02uZ", time.wYear,
      time.wMonth, time.wDay, time.wHour, time.wMinute, time.wSecond);
  if (count != 20) return false;
  output.assign(text, 20);
  return true;
}

void append_nullable_string(std::string& output, const std::string& value) {
  if (value.empty()) output.append("null");
  else append_json_string(output, value);
}

void append_boolean(std::string& output, bool value) {
  output.append(value ? "true" : "false");
}

void append_cpu(std::string& output, const CpuEvidence& cpu) {
  output.append("{\"aes\":"); append_boolean(output, cpu.aes);
  output.append(",\"avx\":"); append_boolean(output, cpu.avx);
  output.append(",\"avx2\":"); append_boolean(output, cpu.avx2);
  output.append(",\"bmi1\":"); append_boolean(output, cpu.bmi1);
  output.append(",\"bmi2\":"); append_boolean(output, cpu.bmi2);
  output.append(",\"brand\":"); append_nullable_string(output, cpu.brand);
  output.append(",\"efficiency_cores\":"); output.append(std::to_string(cpu.efficiency_cores));
  output.append(",\"family\":"); output.append(std::to_string(cpu.family));
  output.append(",\"fma\":"); append_boolean(output, cpu.fma);
  output.append(",\"logical_processors\":"); output.append(std::to_string(cpu.logical_processors));
  output.append(",\"model\":"); output.append(std::to_string(cpu.model));
  output.append(",\"performance_cores\":"); output.append(std::to_string(cpu.performance_cores));
  output.append(",\"physical_cores\":"); output.append(std::to_string(cpu.physical_cores));
  output.append(",\"popcnt\":"); append_boolean(output, cpu.popcnt);
  output.append(",\"sse42\":"); append_boolean(output, cpu.sse42);
  output.append(",\"stepping\":"); output.append(std::to_string(cpu.stepping));
  output.append(",\"topology_complete\":"); append_boolean(output, cpu.topology_complete);
  output.append(",\"vendor\":"); append_nullable_string(output, cpu.vendor);
  output.push_back('}');
}

void append_memory(std::string& output,
                   const std::vector<MemoryEvidence>& memory) {
  output.push_back('[');
  for (std::size_t index = 0; index < memory.size(); ++index) {
    if (index != 0) output.push_back(',');
    const auto& item = memory[index];
    output.append("{\"capacity_bytes\":"); output.append(std::to_string(item.capacity_bytes));
    output.append(",\"configured_speed_mts\":"); output.append(std::to_string(item.configured_speed_mts));
    output.append(",\"data_width\":"); output.append(std::to_string(item.data_width));
    output.append(",\"memory_type\":"); output.append(std::to_string(item.memory_type));
    output.append(",\"slot_index\":"); output.append(std::to_string(item.slot_index));
    output.append(",\"speed_mts\":"); output.append(std::to_string(item.speed_mts));
    output.push_back('}');
  }
  output.push_back(']');
}

void append_display_devices(
    std::string& output, const std::vector<DisplayDeviceEvidence>& devices) {
  output.push_back('[');
  for (std::size_t index = 0; index < devices.size(); ++index) {
    if (index != 0) output.push_back(',');
    const auto& item = devices[index];
    output.append("{\"device_id\":"); output.append(std::to_string(item.device_id));
    output.append(",\"driver_date\":"); append_nullable_string(output, item.driver_date);
    output.append(",\"driver_signature_status\":"); append_json_string(output, item.driver_signature_status);
    output.append(",\"driver_version\":"); append_nullable_string(output, item.driver_version);
    output.append(",\"hardware_tuple_sha256\":"); append_nullable_string(output, item.hardware_tuple_sha256);
    output.append(",\"provider\":"); append_nullable_string(output, item.provider);
    output.append(",\"revision\":"); output.append(std::to_string(item.revision));
    output.append(",\"subsystem_id\":"); output.append(std::to_string(item.subsystem_id));
    output.append(",\"vendor_id\":"); output.append(std::to_string(item.vendor_id));
    output.push_back('}');
  }
  output.push_back(']');
}

void append_adapters(std::string& output,
                     const std::vector<AdapterEvidence>& adapters) {
  output.push_back('[');
  for (std::size_t index = 0; index < adapters.size(); ++index) {
    if (index != 0) output.push_back(',');
    const auto& item = adapters[index];
    output.append("{\"correlation_exact\":"); append_boolean(output, item.correlation_exact);
    output.append(",\"dedicated_system_bytes\":"); output.append(std::to_string(item.dedicated_system_bytes));
    output.append(",\"dedicated_video_bytes\":"); output.append(std::to_string(item.dedicated_video_bytes));
    output.append(",\"device_id\":"); output.append(std::to_string(item.device_id));
    output.append(",\"driver_version\":"); append_nullable_string(output, item.driver_version);
    output.append(",\"integrated_classification\":"); append_json_string(output, item.integrated_classification);
    output.append(",\"luid_high\":"); output.append(std::to_string(item.luid_high));
    output.append(",\"luid_low\":"); output.append(std::to_string(item.luid_low));
    output.append(",\"name\":"); append_nullable_string(output, item.name);
    output.append(",\"ordinal\":"); output.append(std::to_string(item.ordinal));
    output.append(",\"pnp_hardware_tuple_sha256\":"); append_nullable_string(output, item.pnp_hardware_tuple_sha256);
    output.append(",\"revision\":"); output.append(std::to_string(item.revision));
    output.append(",\"shared_system_bytes\":"); output.append(std::to_string(item.shared_system_bytes));
    output.append(",\"software\":"); append_boolean(output, item.software);
    output.append(",\"subsystem_id\":"); output.append(std::to_string(item.subsystem_id));
    output.append(",\"vendor_id\":"); output.append(std::to_string(item.vendor_id));
    output.push_back('}');
  }
  output.push_back(']');
}

bool build_receipt(Evidence& evidence, bool executed_on_target,
                   AttestationResult& result) {
  std::sort(evidence.reasons.begin(), evidence.reasons.end());
  const bool exact_board = evidence.firmware.board_product == "039NNG" &&
      evidence.firmware.board_revision == "A00";
  const std::uint64_t installed_memory = std::accumulate(
      evidence.firmware.memory.begin(), evidence.firmware.memory.end(),
      std::uint64_t{0}, [](std::uint64_t total, const MemoryEvidence& item) {
        return item.capacity_bytes > std::numeric_limits<std::uint64_t>::max() - total
            ? std::numeric_limits<std::uint64_t>::max()
            : total + item.capacity_bytes;
      });
  bool exact_memory = installed_memory == 34'359'738'368ULL &&
      evidence.firmware.memory.size() > 0;
  for (const auto& item : evidence.firmware.memory)
    exact_memory = exact_memory && item.memory_type == 34 &&
        item.configured_speed_mts == 5600;
  bool exact_display = false;
  bool exact_correlation = true;
  for (const auto& item : evidence.adapters) {
    if (!item.software && item.vendor_id == 0x8086 &&
        item.driver_version == "32.0.101.8247") exact_display = true;
    if (!item.software) exact_correlation = exact_correlation && item.correlation_exact;
  }
  exact_correlation = exact_correlation && !evidence.adapters.empty();
  const bool cpu_exact = evidence.cpu.vendor == "GenuineIntel" &&
      evidence.cpu.brand.find("Intel(R) Core(TM) Ultra 7") != std::string::npos;

  std::string output;
  output.reserve(16'384);
  output.append("{\"checks\":{\"authenticode_valid\":"); append_boolean(output, evidence.authenticode_valid);
  output.append(",\"board_exact\":"); append_boolean(output, exact_board);
  output.append(",\"cpu_exact\":"); append_boolean(output, cpu_exact);
  output.append(",\"deadline_supervised\":"); append_boolean(output, trust_anchor::kSupervisedGlobalDeadlineAvailable);
  output.append(",\"display_driver_exact\":"); append_boolean(output, exact_display);
  output.append(",\"dxgi_pnp_correlation_exact\":"); append_boolean(output, exact_correlation);
  output.append(",\"manifest_identity\":"); append_boolean(output, evidence.manifest_identity);
  output.append(",\"memory_exact\":"); append_boolean(output, exact_memory);
  output.append(",\"operating_system_supported\":"); append_boolean(output, evidence.windows_x64);
  output.append(",\"script_identity\":"); append_boolean(output, evidence.script_identity);
  output.append(",\"secure_boot_enabled\":"); append_boolean(output, evidence.secure_boot_known && evidence.secure_boot);
  output.append(",\"self_identity\":"); append_boolean(output, evidence.self_identity);
  output.append(",\"vpro_authoritative\":false");
  output.append(",\"vulkan_runtime_present\":"); append_boolean(output, evidence.vulkan_present);
  output.append("},\"collector\":{\"abi_version\":1,\"configured\":"); append_boolean(output, trust_anchor::kConfigured);
  output.append(",\"offline_authenticode\":true,\"profile\":"); append_json_string(output, kProfile);
  output.append(",\"source_only\":true},\"executed_on_target\":"); append_boolean(output, executed_on_target);
  output.append(",\"expected\":{\"board_product\":\"039NNG\",\"board_revision\":\"A00\",\"cpu_description\":\"Intel Core Ultra 7 vPro Enterprise\",\"cpu_exact_sku\":null,\"display_driver\":\"32.0.101.8247\",\"display_vendor_id\":32902,\"memory_bytes\":34359738368,\"memory_configured_speed_mts\":5600,\"memory_type\":34,\"windows_architecture\":\"x64\"}");
  output.append(",\"fixture\":false,\"generated_at_utc\":"); append_nullable_string(output, evidence.generated_at_utc);
  output.append(",\"observed\":{\"adapters\":"); append_adapters(output, evidence.adapters);
  output.append(",\"bios\":{\"board_manufacturer\":"); append_nullable_string(output, evidence.firmware.board_manufacturer);
  output.append(",\"board_product\":"); append_nullable_string(output, evidence.firmware.board_product);
  output.append(",\"board_revision\":"); append_nullable_string(output, evidence.firmware.board_revision);
  output.append(",\"release_date\":"); append_nullable_string(output, evidence.firmware.bios_date);
  output.append(",\"system_manufacturer\":"); append_nullable_string(output, evidence.firmware.system_manufacturer);
  output.append(",\"system_product\":"); append_nullable_string(output, evidence.firmware.system_product);
  output.append(",\"vendor\":"); append_nullable_string(output, evidence.firmware.bios_vendor);
  output.append(",\"version\":"); append_nullable_string(output, evidence.firmware.bios_version);
  output.append("},\"computer\":{\"total_physical_memory\":"); output.append(std::to_string(evidence.total_physical_memory));
  output.append("},\"cpu\":"); append_cpu(output, evidence.cpu);
  output.append(",\"display_devices\":"); append_display_devices(output, evidence.display_devices);
  output.append(",\"memory_modules\":"); append_memory(output, evidence.firmware.memory);
  output.append(",\"operating_system\":{\"architecture\":");
  if (evidence.windows_x64) output.append("\"x64\""); else output.append("null");
  output.append(",\"build\":"); append_nullable_string(output, evidence.windows_build);
  output.append(",\"secure_boot\":");
  if (!evidence.secure_boot_known) output.append("null"); else append_boolean(output, evidence.secure_boot);
  output.append(",\"version\":"); append_nullable_string(output, evidence.windows_version);
  output.append("},\"self\":{\"authenticode_valid\":"); append_boolean(output, evidence.authenticode_valid);
  output.append(",\"executable_sha256\":"); append_nullable_string(output, evidence.executable_sha256);
  output.append(",\"manifest_sha256\":"); append_nullable_string(output, evidence.manifest_sha256);
  output.append(",\"script_sha256\":"); append_nullable_string(output, evidence.script_sha256);
  output.append(",\"signer_certificate_sha256\":"); append_nullable_string(output, evidence.signer_certificate_sha256);
  output.append("},\"vpro\":{\"authoritative\":false,\"status\":\"unproven\"},\"vulkan\":{\"present\":"); append_boolean(output, evidence.vulkan_present);
  output.append(",\"sha256\":"); append_nullable_string(output, evidence.vulkan_sha256);
  output.append("}},\"reason_codes\":[");
  for (std::size_t index = 0; index < evidence.reasons.size(); ++index) {
    if (index != 0) output.push_back(',');
    append_json_string(output, evidence.reasons[index]);
  }
  output.append("],\"receipt_kind\":\"diagnostic_only\",\"schema_version\":"); append_json_string(output, kSchema);
  output.append(",\"target_evidence_accepted\":false,\"verdict\":\"NOT_READY\"}");
  if (output.size() > kMaximumReceiptBytes) return false;
  result.canonical_json = std::move(output);
  result.hardware_match = false;
  result.target_evidence_accepted = false;
  return true;
}

bool basic_context_valid(const CollectionContext& context) {
  return context.cancelled != nullptr && context.started_tick_ms != 0 &&
      context.deadline_tick_ms > context.started_tick_ms &&
      context.deadline_tick_ms - context.started_tick_ms <= kGlobalDeadlineMs;
}

AttestorStatus collect_full(const CollectionContext& context,
                            Evidence& evidence) {
  FileProof executable;
  FileProof manifest;
  FileProof script;
  if (!verify_self_and_manifest(context, evidence, executable, manifest, script))
    return AttestorStatus::kSelfIdentityMismatch;
  if (cancelled_or_expired(context)) return AttestorStatus::kDeadlineExceeded;
  if (!collect_os(evidence)) return AttestorStatus::kInternal;
  if (cancelled_or_expired(context)) return AttestorStatus::kDeadlineExceeded;
  if (!collect_cpu(evidence)) return AttestorStatus::kInternal;
  if (cancelled_or_expired(context)) return AttestorStatus::kDeadlineExceeded;
  if (!collect_smbios(evidence)) return AttestorStatus::kFirmwareMalformed;
  if (cancelled_or_expired(context)) return AttestorStatus::kDeadlineExceeded;
  if (!collect_display_devices(context, evidence))
    return AttestorStatus::kDriverAmbiguous;
  if (!collect_dxgi(context, evidence)) return AttestorStatus::kAdapterAmbiguous;
  if (!collect_vulkan_presence(context, evidence))
    add_reason(evidence, "vulkan_runtime_unavailable");
  if (!utc_now(evidence.generated_at_utc)) return AttestorStatus::kInternal;
  add_reason(evidence, "cpu_exact_sku_unconfigured");
  add_reason(evidence, "display_driver_signature_unproven");
  add_reason(evidence, "integrated_gpu_classification_unproven");
  add_reason(evidence, "vpro_status_unproven");
  return AttestorStatus::kNotReady;
}

}  // namespace

AttestorStatus collect_attestation(const CollectionContext& context,
                                   AttestationResult& result) noexcept {
  try {
    result = AttestationResult{};
    Evidence evidence;
    AttestorStatus status = AttestorStatus::kNotReady;
    bool executed_on_target = false;
    if (!basic_context_valid(context)) {
      add_reason(evidence, "invalid_collection_context");
      status = AttestorStatus::kInvalidRequest;
    } else if (!trust_anchor::kConfigured ||
               trust_anchor::kExpectedExecutableSize == 0 ||
               all_zero(trust_anchor::kExpectedExecutableSha256.data(), 32) ||
               all_zero(trust_anchor::kExpectedManifestSha256.data(), 32) ||
               all_zero(trust_anchor::kExpectedDiagnosticScriptSha256.data(), 32) ||
               all_zero(trust_anchor::kExpectedSignerCertificateSha256.data(), 32)) {
      add_reason(evidence, "target_trust_anchor_unavailable");
      status = AttestorStatus::kTrustAnchorUnavailable;
    } else if (!trust_anchor::kOfflineAuthenticodePolicyReviewed) {
      add_reason(evidence, "offline_authenticode_policy_unreviewed");
      status = AttestorStatus::kSignatureUnproven;
    } else if (!trust_anchor::kSupervisedGlobalDeadlineAvailable) {
      add_reason(evidence, "supervised_global_deadline_unavailable");
      status = AttestorStatus::kSupervisionUnavailable;
    } else {
      // This branch is unreachable in the checked-in source. It may be enabled
      // only by a separately reviewed, signed trust-anchor build and a
      // process-level supervisor that can terminate blocking Win32 calls.
      executed_on_target = true;
      status = collect_full(context, evidence);
    }
    if (!build_receipt(evidence, executed_on_target, result)) {
      result = AttestationResult{};
      return AttestorStatus::kBoundsExceeded;
    }
    result.status = status;
    return status;
  } catch (...) {
    result = AttestationResult{};
    return AttestorStatus::kInternal;
  }
}

AttestorStatus write_receipt_stdout(const AttestationResult& result) noexcept {
  // WriteFile on arbitrary inherited handles is not synchronously cancellable.
  // The inert source therefore refuses output until an independently accepted
  // process-level global-deadline supervisor is compiled into the trust anchor.
  if (!trust_anchor::kSupervisedGlobalDeadlineAvailable)
    return AttestorStatus::kSupervisionUnavailable;
  if (result.canonical_json.empty() ||
      result.canonical_json.size() > kMaximumReceiptBytes ||
      result.canonical_json.front() != '{' ||
      result.canonical_json.back() != '}')
    return AttestorStatus::kInvalidRequest;
  for (const unsigned char byte : result.canonical_json)
    if (byte == 0 || byte == '\r' || byte == '\n')
      return AttestorStatus::kInvalidRequest;
  HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
  const DWORD type = output == nullptr || output == INVALID_HANDLE_VALUE
      ? FILE_TYPE_UNKNOWN : GetFileType(output);
  if (type != FILE_TYPE_CHAR && type != FILE_TYPE_PIPE)
    return AttestorStatus::kOutputFailed;
  std::string bytes = result.canonical_json;
  bytes.push_back('\n');
  std::size_t offset = 0;
  while (offset < bytes.size()) {
    DWORD written = 0;
    const DWORD wanted = static_cast<DWORD>(std::min<std::size_t>(
        bytes.size() - offset, 65'536));
    if (!WriteFile(output, bytes.data() + offset, wanted, &written, nullptr) ||
        written == 0 || written > wanted)
      return AttestorStatus::kOutputFailed;
    offset += written;
  }
  return AttestorStatus::kOk;
}

const char* status_name(AttestorStatus status) noexcept {
  switch (status) {
    case AttestorStatus::kNotReady: return "not_ready";
    case AttestorStatus::kOk: return "ok";
    case AttestorStatus::kInvalidRequest: return "invalid_request";
    case AttestorStatus::kTrustAnchorUnavailable: return "trust_anchor_unavailable";
    case AttestorStatus::kSupervisionUnavailable: return "supervision_unavailable";
    case AttestorStatus::kSelfIdentityMismatch: return "self_identity_mismatch";
    case AttestorStatus::kManifestMismatch: return "manifest_mismatch";
    case AttestorStatus::kSignatureUnproven: return "signature_unproven";
    case AttestorStatus::kDeadlineExceeded: return "deadline_exceeded";
    case AttestorStatus::kCancelled: return "cancelled";
    case AttestorStatus::kFirmwareMalformed: return "firmware_malformed";
    case AttestorStatus::kBoundsExceeded: return "bounds_exceeded";
    case AttestorStatus::kAdapterAmbiguous: return "adapter_ambiguous";
    case AttestorStatus::kDriverAmbiguous: return "driver_ambiguous";
    case AttestorStatus::kOutputFailed: return "output_failed";
    case AttestorStatus::kInternal: return "internal";
  }
  return "internal";
}

}  // namespace lae::windows_hardware_attestor
