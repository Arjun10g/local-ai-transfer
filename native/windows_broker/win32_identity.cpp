#include "win32_identity.hpp"

#include "protocol.hpp"
#include "trust_anchor.hpp"

#include <bcrypt.h>

#include <algorithm>
#include <array>
#include <cwctype>
#include <limits>
#include <utility>
#include <vector>

namespace lae::windows_broker {
namespace {

constexpr std::size_t kMaxPathCharacters = 32767;
constexpr ULONGLONG kManifestReadDeadlineMs = 5000;
constexpr DWORD kFileLeaseShare = FILE_SHARE_READ;  // Denies WRITE and DELETE.
constexpr DWORD kDirectoryLeaseShare = FILE_SHARE_READ | FILE_SHARE_WRITE;

bool invalid_handle(HANDLE value) {
  return value == nullptr || value == INVALID_HANDLE_VALUE;
}

std::wstring strip_extended_prefix(std::wstring path) {
  if (path.rfind(L"\\\\?\\UNC\\", 0) == 0) return L"\\\\" + path.substr(8);
  if (path.rfind(L"\\\\?\\", 0) == 0) return path.substr(4);
  return path;
}

bool equal_path(const std::wstring& left, const std::wstring& right) {
  return CompareStringOrdinal(left.data(), static_cast<int>(left.size()),
                              right.data(), static_cast<int>(right.size()), TRUE) ==
         CSTR_EQUAL;
}

bool canonical_local_ntfs_path(const std::wstring& supplied,
                               std::wstring& canonical) {
  if (supplied.size() < 4 || supplied.size() > kMaxPathCharacters ||
      !std::iswalpha(supplied[0]) || supplied[1] != L':' ||
      (supplied[2] != L'\\' && supplied[2] != L'/') ||
      supplied.rfind(L"\\\\", 0) == 0 || supplied.rfind(L"\\\\?\\", 0) == 0 ||
      supplied.rfind(L"\\\\.\\", 0) == 0) return false;
  for (std::size_t index = 0; index < supplied.size(); ++index) {
    if (supplied[index] == L':' && index != 1) return false;
  }
  std::vector<wchar_t> buffer(kMaxPathCharacters + 1);
  const DWORD count = GetFullPathNameW(supplied.c_str(),
                                       static_cast<DWORD>(buffer.size()),
                                       buffer.data(), nullptr);
  if (count == 0 || count >= buffer.size()) return false;
  canonical.assign(buffer.data(), count);
  std::replace(canonical.begin(), canonical.end(), L'/', L'\\');
  if (!equal_path(canonical, supplied)) return false;  // Reject dot/alias normalization.

  const wchar_t root[] = {canonical[0], L':', L'\\', L'\0'};
  if (GetDriveTypeW(root) != DRIVE_FIXED) return false;
  std::array<wchar_t, 32> filesystem{};
  if (!GetVolumeInformationW(root, nullptr, 0, nullptr, nullptr, nullptr,
                             filesystem.data(),
                             static_cast<DWORD>(filesystem.size())) ||
      CompareStringOrdinal(filesystem.data(), -1, L"NTFS", -1, TRUE) != CSTR_EQUAL)
    return false;
  return true;
}

bool attributes_are_plain(HANDLE handle, bool directory) {
  FILE_ATTRIBUTE_TAG_INFO tag{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag,
                                    sizeof(tag))) return false;
  if ((tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) return false;
  return directory ? (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0
                   : (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

bool final_path(HANDLE handle, std::wstring& output) {
  std::vector<wchar_t> buffer(kMaxPathCharacters + 1);
  const DWORD count = GetFinalPathNameByHandleW(
      handle, buffer.data(), static_cast<DWORD>(buffer.size()),
      FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (count == 0 || count >= buffer.size()) return false;
  output = strip_extended_prefix(std::wstring(buffer.data(), count));
  return true;
}

bool identity(HANDLE handle, std::uint64_t& volume_serial,
              std::string& file_id) {
  FILE_ID_INFO info{};
  if (!GetFileInformationByHandleEx(handle, FileIdInfo, &info, sizeof(info)))
    return false;
  volume_serial = info.VolumeSerialNumber;
  static constexpr char hex[] = "0123456789abcdef";
  file_id.clear();
  file_id.reserve(32);
  for (const unsigned char value : info.FileId.Identifier) {
    file_id.push_back(hex[value >> 4]);
    file_id.push_back(hex[value & 0x0f]);
  }
  return true;
}

bool acquire_ancestors(const std::wstring& canonical, bool include_leaf,
                       std::vector<UniqueHandle>& leases) {
  std::size_t end = 3;  // Drive root.
  while (true) {
    const auto separator = canonical.find(L'\\', end);
    const bool at_leaf = separator == std::wstring::npos;
    const std::size_t prefix_end = at_leaf ? canonical.size() : separator;
    if (at_leaf && !include_leaf) break;
    std::wstring prefix = canonical.substr(0, prefix_end);
    if (prefix.size() == 2) prefix.push_back(L'\\');
    HANDLE raw = CreateFileW(prefix.c_str(), FILE_READ_ATTRIBUTES,
                             kDirectoryLeaseShare, nullptr, OPEN_EXISTING,
                             FILE_FLAG_BACKUP_SEMANTICS |
                                 FILE_FLAG_OPEN_REPARSE_POINT,
                             nullptr);
    if (invalid_handle(raw)) return false;
    UniqueHandle handle(raw);
    std::wstring observed;
    if (!attributes_are_plain(handle.get(), true) ||
        !final_path(handle.get(), observed) || !equal_path(prefix, observed))
      return false;
    leases.push_back(std::move(handle));
    if (at_leaf) break;
    end = separator + 1;
  }
  return true;
}

bool sha256_handle(HANDLE handle, std::uint64_t maximum_bytes,
                   std::string& digest, std::string* content,
                   const std::atomic<bool>* cancelled = nullptr,
                   std::uint64_t deadline_tick_ms = 0) {
  LARGE_INTEGER zero{};
  if (!SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN)) return false;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_length = 0;
  DWORD result_length = 0;
  bool success = false;
  // BCrypt owns pointers into this caller-provided object buffer until the hash
  // handle is destroyed. Keep it alive through the cleanup block.
  std::vector<unsigned char> object;
  if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM,
                                                   nullptr, 0)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                                        reinterpret_cast<PUCHAR>(&object_length),
                                        sizeof(object_length), &result_length, 0)) ||
      object_length == 0 || object_length > 1024 * 1024) goto cleanup;
  {
    object.resize(object_length);
    if (!BCRYPT_SUCCESS(BCryptCreateHash(algorithm, &hash, object.data(),
                                         object_length, nullptr, 0, 0))) goto cleanup;
    std::array<unsigned char, 64 * 1024> chunk{};
    std::uint64_t consumed = 0;
    if (content) content->clear();
    while (true) {
      if ((cancelled && cancelled->load(std::memory_order_acquire)) ||
          (deadline_tick_ms != 0 && GetTickCount64() >= deadline_tick_ms))
        goto cleanup;
      DWORD count = 0;
      if (!ReadFile(handle, chunk.data(), static_cast<DWORD>(chunk.size()), &count,
                    nullptr)) goto cleanup;
      if (count == 0) break;
      if (count > maximum_bytes || consumed > maximum_bytes - count) goto cleanup;
      consumed += count;
      if (!BCRYPT_SUCCESS(BCryptHashData(hash, chunk.data(), count, 0))) goto cleanup;
      if (content) content->append(reinterpret_cast<const char*>(chunk.data()), count);
    }
    std::array<unsigned char, 32> raw{};
    if (!BCRYPT_SUCCESS(BCryptFinishHash(hash, raw.data(),
                                         static_cast<ULONG>(raw.size()), 0)))
      goto cleanup;
    static constexpr char hex[] = "0123456789abcdef";
    digest.clear();
    digest.reserve(64);
    for (const unsigned char value : raw) {
      digest.push_back(hex[value >> 4]);
      digest.push_back(hex[value & 0x0f]);
    }
    success = true;
  }
cleanup:
  if (hash) BCryptDestroyHash(hash);
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN);
  return success;
}

bool acquire_object(const std::wstring& expected_path, bool directory,
                    IdentityLease& lease, std::string& error_code) {
  std::wstring canonical;
  if (!canonical_local_ntfs_path(expected_path, canonical)) {
    error_code = directory ? "cwd_mismatch" : "identity_mismatch";
    return false;
  }
  IdentityLease candidate;
  if (!acquire_ancestors(canonical, directory, candidate.ancestors)) {
    error_code = directory ? "cwd_mismatch" : "identity_mismatch";
    return false;
  }
  HANDLE raw = CreateFileW(canonical.c_str(), FILE_READ_ATTRIBUTES |
                                                (directory ? 0 : GENERIC_READ),
                           directory ? kDirectoryLeaseShare : kFileLeaseShare,
                           nullptr, OPEN_EXISTING,
                           FILE_FLAG_OPEN_REPARSE_POINT |
                               (directory ? FILE_FLAG_BACKUP_SEMANTICS
                                          : FILE_FLAG_SEQUENTIAL_SCAN),
                           nullptr);
  if (invalid_handle(raw)) {
    error_code = directory ? "cwd_mismatch" : "identity_mismatch";
    return false;
  }
  candidate.object = UniqueHandle(raw);
  std::wstring observed;
  if (!attributes_are_plain(candidate.object.get(), directory) ||
      !final_path(candidate.object.get(), observed) || !equal_path(canonical, observed) ||
      !identity(candidate.object.get(), candidate.volume_serial,
                candidate.file_id_128)) {
    error_code = directory ? "cwd_mismatch" : "identity_mismatch";
    return false;
  }
  candidate.normalized_path = observed;
  candidate.directory = directory;
  lease = std::move(candidate);
  return true;
}

std::wstring manifest_path() {
  std::vector<wchar_t> module(kMaxPathCharacters + 1);
  const DWORD count = GetModuleFileNameW(nullptr, module.data(),
                                         static_cast<DWORD>(module.size()));
  if (count == 0 || count >= module.size()) return {};
  std::wstring path(module.data(), count);
  const auto separator = path.find_last_of(L"\\/");
  if (separator == std::wstring::npos) return {};
  path.resize(separator + 1);
  path += L"windows-broker.manifest.json";
  return path;
}

}  // namespace

UniqueHandle::~UniqueHandle() {
  if (*this) CloseHandle(value_);
}

UniqueHandle::UniqueHandle(UniqueHandle&& other) noexcept : value_(other.release()) {}

UniqueHandle& UniqueHandle::operator=(UniqueHandle&& other) noexcept {
  if (this == &other) return *this;
  if (*this) CloseHandle(value_);
  value_ = other.release();
  return *this;
}

HANDLE UniqueHandle::release() {
  HANDLE value = value_;
  value_ = INVALID_HANDLE_VALUE;
  return value;
}

bool acquire_executable_lease(const FileIdentitySpec& expected,
                              IdentityLease& lease,
                              std::string& error_code,
                              const std::atomic<bool>* cancelled,
                              std::uint64_t deadline_tick_ms) noexcept {
  error_code = "identity_mismatch";
  try {
    IdentityLease candidate;
    if (!acquire_object(expected.absolute_path, false, candidate, error_code))
      return false;
    LARGE_INTEGER size{};
    std::string digest;
    if (!GetFileSizeEx(candidate.object.get(), &size) || size.QuadPart < 0 ||
        static_cast<std::uint64_t>(size.QuadPart) != expected.size_bytes ||
        candidate.volume_serial != expected.volume_serial ||
        candidate.file_id_128 != expected.file_id_128 ||
        !sha256_handle(candidate.object.get(), expected.size_bytes, digest, nullptr,
                       cancelled, deadline_tick_ms) ||
        digest != expected.sha256) return false;
    lease = std::move(candidate);
    error_code.clear();
    return true;
  } catch (...) {
    error_code = "identity_mismatch";
    return false;
  }
}

bool acquire_directory_lease(const DirectoryIdentitySpec& expected,
                             IdentityLease& lease,
                             std::string& error_code) noexcept {
  error_code = "cwd_mismatch";
  try {
    IdentityLease candidate;
    if (!acquire_object(expected.absolute_path, true, candidate, error_code) ||
        candidate.volume_serial != expected.volume_serial ||
        candidate.file_id_128 != expected.file_id_128) return false;
    lease = std::move(candidate);
    error_code.clear();
    return true;
  } catch (...) {
    error_code = "cwd_mismatch";
    return false;
  }
}

bool load_compiled_manifest(ManifestLease& lease,
                            std::string& error_code) noexcept {
  error_code = "broker_not_activated";
  try {
    if (!release_activation_prerequisites_configured()) return false;
    const std::wstring path = manifest_path();
    if (path.empty()) {
      error_code = "manifest_untrusted";
      return false;
    }
    IdentityLease document;
    if (!acquire_object(path, false, document, error_code)) {
      error_code = "manifest_untrusted";
      return false;
    }
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(document.object.get(), &size) || size.QuadPart <= 0 ||
        static_cast<std::uint64_t>(size.QuadPart) > kMaxManifestBytes) {
      error_code = "manifest_untrusted";
      return false;
    }
    std::string digest;
    std::string content;
    if (!sha256_handle(document.object.get(), kMaxManifestBytes, digest, &content,
                       nullptr, GetTickCount64() + kManifestReadDeadlineMs) ||
        digest != kCompiledManifestSha256) {
      error_code = "manifest_untrusted";
      return false;
    }
    nlohmann::json json;
    if (!parse_strict_document(content, kMaxManifestBytes, json)) {
      error_code = "manifest_invalid";
      return false;
    }
    BrokerManifest parsed;
    if (!parse_product_manifest(json, parsed, error_code)) return false;
    const auto runtime = parsed.directories.find(parsed.runtime_directory_id);
    if (runtime == parsed.directories.end()) {
      error_code = "manifest_invalid";
      return false;
    }
    IdentityLease runtime_lease;
    if (!acquire_directory_lease(runtime->second, runtime_lease, error_code))
      return false;
    ManifestLease candidate;
    candidate.manifest = std::move(parsed);
    candidate.document = std::move(document);
    candidate.runtime_directory = std::move(runtime_lease);
    candidate.manifest_sha256 = digest;
    lease = std::move(candidate);
    error_code.clear();
    return true;
  } catch (...) {
    error_code = "manifest_untrusted";
    return false;
  }
}

bool same_identity(const IdentityLease& lease) noexcept {
  try {
    if (!lease.object) return false;
    std::uint64_t volume_serial = 0;
    std::string file_id;
    std::wstring path;
    return identity(lease.object.get(), volume_serial, file_id) &&
           volume_serial == lease.volume_serial && file_id == lease.file_id_128 &&
           final_path(lease.object.get(), path) && equal_path(path, lease.normalized_path) &&
           attributes_are_plain(lease.object.get(), lease.directory);
  } catch (...) {
    return false;
  }
}

}  // namespace lae::windows_broker
