#include "windows_storage.hpp"

#include <aclapi.h>
#include <bcrypt.h>
#include <winioctl.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cstring>
#include <cwctype>
#include <iterator>
#include <limits>
#include <new>
#include <utility>
#include <vector>

namespace lae::action_journal_storage {
namespace {

constexpr std::size_t kMaxPathCharacters = 32'767;
constexpr std::size_t kMaxAncestorHandles = 256;
constexpr std::size_t kIoChunkBytes = 65'536;
constexpr std::size_t kHeaderChecksumOffset = 4'064;
constexpr std::size_t kProtocolOffset = 84;
constexpr std::size_t kProtocolBytes = 64;
constexpr std::size_t kContainerIdOffset = 152;
constexpr DWORD kDirectoryShare = FILE_SHARE_READ | FILE_SHARE_WRITE;
constexpr DWORD kFileShare = FILE_SHARE_READ;  // Deny WRITE and DELETE.
constexpr DWORD kPrivateAccess = FILE_ALL_ACCESS;
constexpr char kProtocol[] = "lae.action-journal.v0.1.0";
constexpr char kHashDomain[] = "lae.action-journal.container.v0.1.0";
constexpr char kHeaderLabel[] = "header";
constexpr std::array<std::uint8_t, 16> kHeaderMagic = {
    'L', 'A', 'E', 'J', 'R', 'N', 'L', 'C',
    'O', 'N', 'T', 'A', 'I', 'N', 'E', 'R'};

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

struct FileIdentity {
  std::uint64_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
};

struct HeldDirectory {
  UniqueHandle handle;
  std::wstring final_path;
  FileIdentity identity;
};

struct CurrentUser {
  UniqueHandle token;
  std::vector<std::uint8_t> token_user;
  PSID sid = nullptr;
};

struct PrivateSecurityDescriptor {
  SECURITY_DESCRIPTOR descriptor{};
  std::vector<std::uint8_t> acl;
  SECURITY_ATTRIBUTES attributes{};
};

bool all_zero(const std::uint8_t* bytes, std::size_t count) noexcept {
  std::uint8_t value = 0;
  for (std::size_t index = 0; index < count; ++index) value |= bytes[index];
  return value == 0;
}

bool equal_bytes(const std::uint8_t* left, const std::uint8_t* right,
                 std::size_t count) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < count; ++index)
    difference |= static_cast<std::uint8_t>(left[index] ^ right[index]);
  return difference == 0;
}

bool same_identity(const FileIdentity& left,
                   const FileIdentity& right) noexcept {
  return left.volume_serial == right.volume_serial &&
         equal_bytes(left.file_id.data(), right.file_id.data(),
                     left.file_id.size());
}

bool same_identity(const FileIdentity& left,
                   const StorageIdentity& right) noexcept {
  return left.volume_serial == right.volume_serial &&
         equal_bytes(left.file_id.data(), right.file_id.data(),
                     left.file_id.size());
}

bool equal_path(const std::wstring& left, const std::wstring& right) noexcept {
  if (left.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      right.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  return CompareStringOrdinal(left.data(), static_cast<int>(left.size()),
                              right.data(), static_cast<int>(right.size()),
                              TRUE) == CSTR_EQUAL;
}

std::wstring strip_extended_prefix(std::wstring path) {
  if (path.rfind(L"\\\\?\\UNC\\", 0) == 0) return L"\\\\" + path.substr(8);
  if (path.rfind(L"\\\\?\\", 0) == 0) return path.substr(4);
  return path;
}

bool final_path(HANDLE handle, std::wstring& value) {
  std::vector<wchar_t> buffer(kMaxPathCharacters + 1);
  const DWORD count = GetFinalPathNameByHandleW(
      handle, buffer.data(), static_cast<DWORD>(buffer.size()),
      FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (count == 0 || count >= buffer.size()) return false;
  value = strip_extended_prefix(std::wstring(buffer.data(), count));
  return value.find(L'\0') == std::wstring::npos;
}

bool reserved_component(const std::wstring& component) {
  std::wstring stem = component.substr(0, component.find(L'.'));
  std::transform(stem.begin(), stem.end(), stem.begin(),
                 [](wchar_t value) { return static_cast<wchar_t>(towupper(value)); });
  if (stem == L"CON" || stem == L"PRN" || stem == L"AUX" || stem == L"NUL")
    return true;
  if (stem.size() == 4 && stem[3] >= L'1' && stem[3] <= L'9') {
    return stem.rfind(L"COM", 0) == 0 || stem.rfind(L"LPT", 0) == 0;
  }
  return false;
}

bool safe_component(const std::wstring& component) {
  if (component.empty() || component == L"." || component == L".." ||
      component.back() == L'.' || component.back() == L' ' ||
      reserved_component(component)) return false;
  for (wchar_t value : component) {
    if (value < 0x20 || value == 0x7f || value == L':' || value == L'/' ||
        value == L'\\' || value == L'<' || value == L'>' || value == L'"' ||
        value == L'|' || value == L'?' || value == L'*') return false;
  }
  return true;
}

bool canonical_directory(const std::wstring& supplied, std::wstring& output,
                         wchar_t (&root)[4]) {
  if (supplied.size() <= 3 || supplied.size() > kMaxPathCharacters ||
      supplied.find(L'\0') != std::wstring::npos || supplied.find(L'/') != std::wstring::npos ||
      supplied.rfind(L"\\\\", 0) == 0 || supplied.rfind(L"\\\\?\\", 0) == 0 ||
      supplied.rfind(L"\\\\.\\", 0) == 0 ||
      !iswalpha(supplied[0]) || supplied[1] != L':' || supplied[2] != L'\\' ||
      supplied.back() == L'\\') return false;
  for (std::size_t index = 0; index < supplied.size(); ++index)
    if (supplied[index] == L':' && index != 1) return false;

  std::size_t start = 3;
  std::size_t components = 0;
  while (start < supplied.size()) {
    const std::size_t end = supplied.find(L'\\', start);
    const std::wstring component = supplied.substr(
        start, end == std::wstring::npos ? std::wstring::npos : end - start);
    if (!safe_component(component) || ++components > kMaxAncestorHandles - 1)
      return false;
    if (end == std::wstring::npos) break;
    start = end + 1;
  }

  std::vector<wchar_t> buffer(kMaxPathCharacters + 1);
  const DWORD count = GetFullPathNameW(supplied.c_str(),
                                       static_cast<DWORD>(buffer.size()),
                                       buffer.data(), nullptr);
  if (count == 0 || count >= buffer.size()) return false;
  output.assign(buffer.data(), count);
  if (!equal_path(output, supplied)) return false;
  root[0] = output[0]; root[1] = L':'; root[2] = L'\\'; root[3] = L'\0';
  return true;
}

bool get_identity(HANDLE handle, FileIdentity& identity) {
  FILE_ID_INFO info{};
  if (!GetFileInformationByHandleEx(handle, FileIdInfo, &info, sizeof(info)))
    return false;
  identity.volume_serial = info.VolumeSerialNumber;
  std::copy(std::begin(info.FileId.Identifier), std::end(info.FileId.Identifier),
            identity.file_id.begin());
  return !all_zero(identity.file_id.data(), identity.file_id.size());
}

bool current_user(CurrentUser& user) {
  HANDLE token = INVALID_HANDLE_VALUE;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
  user.token.reset(token);
  DWORD size = 0;
  GetTokenInformation(user.token.get(), TokenUser, nullptr, 0, &size);
  if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0 || size > 65'536)
    return false;
  user.token_user.resize(size);
  if (!GetTokenInformation(user.token.get(), TokenUser, user.token_user.data(),
                           size, &size)) return false;
  user.sid = reinterpret_cast<TOKEN_USER*>(user.token_user.data())->User.Sid;
  if (!IsValidSid(user.sid) || GetLengthSid(user.sid) > SECURITY_MAX_SID_SIZE)
    return false;

  constexpr WELL_KNOWN_SID_TYPE rejected[] = {
      WinNullSid, WinWorldSid, WinAnonymousSid, WinAuthenticatedUserSid,
      WinBuiltinUsersSid, WinBuiltinAdministratorsSid, WinLocalSystemSid,
      WinLocalServiceSid, WinNetworkServiceSid};
  for (const auto type : rejected) {
    std::array<std::uint8_t, SECURITY_MAX_SID_SIZE> buffer{};
    DWORD length = static_cast<DWORD>(buffer.size());
    if (!CreateWellKnownSid(type, nullptr, buffer.data(), &length)) return false;
    if (EqualSid(user.sid, buffer.data())) return false;
  }
  return true;
}

bool build_private_security(PSID sid, PrivateSecurityDescriptor& result) {
  const DWORD sid_length = GetLengthSid(sid);
  const DWORD acl_length = sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) - sizeof(DWORD) + sid_length;
  if (sid_length == 0 || acl_length > 65'536) return false;
  result.acl.resize(acl_length);
  PACL acl = reinterpret_cast<PACL>(result.acl.data());
  if (!InitializeAcl(acl, acl_length, ACL_REVISION) ||
      !AddAccessAllowedAceEx(acl, ACL_REVISION, 0, kPrivateAccess, sid) ||
      !InitializeSecurityDescriptor(&result.descriptor, SECURITY_DESCRIPTOR_REVISION) ||
      !SetSecurityDescriptorOwner(&result.descriptor, sid, FALSE) ||
      !SetSecurityDescriptorDacl(&result.descriptor, TRUE, acl, FALSE) ||
      !SetSecurityDescriptorControl(&result.descriptor, SE_DACL_PROTECTED,
                                    SE_DACL_PROTECTED)) return false;
  result.attributes.nLength = sizeof(result.attributes);
  result.attributes.lpSecurityDescriptor = &result.descriptor;
  result.attributes.bInheritHandle = FALSE;
  return IsValidSecurityDescriptor(&result.descriptor) != FALSE;
}

bool private_security(HANDLE handle, PSID expected_user) {
  PSID owner = nullptr;
  PACL dacl = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  const DWORD status = GetSecurityInfo(
      handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
      &owner, nullptr, &dacl, nullptr, &descriptor);
  if (status != ERROR_SUCCESS || descriptor == nullptr) return false;
  const auto release = [&descriptor]() {
    LocalFree(descriptor);
    descriptor = nullptr;
  };
  bool valid = owner != nullptr && IsValidSid(owner) && EqualSid(owner, expected_user);
  SECURITY_DESCRIPTOR_CONTROL control = 0;
  DWORD revision = 0;
  valid = valid && GetSecurityDescriptorControl(descriptor, &control, &revision) &&
          revision == SECURITY_DESCRIPTOR_REVISION &&
          (control & SE_DACL_PROTECTED) != 0 &&
          (control & SE_DACL_DEFAULTED) == 0 && dacl != nullptr && IsValidAcl(dacl) &&
          dacl->AceCount == 1;
  if (valid) {
    void* raw_ace = nullptr;
    valid = GetAce(dacl, 0, &raw_ace) && raw_ace != nullptr;
    if (valid) {
      const auto* ace = static_cast<const ACCESS_ALLOWED_ACE*>(raw_ace);
      PSID ace_sid = const_cast<DWORD*>(&ace->SidStart);
      valid = ace->Header.AceType == ACCESS_ALLOWED_ACE_TYPE &&
              ace->Header.AceFlags == 0 && ace->Mask == kPrivateAccess &&
              IsValidSid(ace_sid) && EqualSid(ace_sid, expected_user);
    }
  }
  release();
  return valid;
}

bool plain_attributes(HANDLE handle, bool directory) {
  if (GetFileType(handle) != FILE_TYPE_DISK) return false;
  FILE_ATTRIBUTE_TAG_INFO tag{};
  FILE_STANDARD_INFO standard{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag, sizeof(tag)) ||
      !GetFileInformationByHandleEx(handle, FileStandardInfo, &standard, sizeof(standard)))
    return false;
  constexpr DWORD rejected =
      FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE | FILE_ATTRIBUTE_OFFLINE |
      FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED |
      FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS |
      FILE_ATTRIBUTE_VIRTUAL;
  if ((tag.FileAttributes & rejected) != 0 || standard.DeletePending) return false;
  const bool is_directory = (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0 ||
                            standard.Directory != FALSE;
  return directory == is_directory;
}

StorageStatus file_shape(HANDLE handle, std::uint64_t expected_size,
                         bool require_size) {
  if (GetFileType(handle) != FILE_TYPE_DISK) return StorageStatus::kUnsupportedFileType;
  FILE_ATTRIBUTE_TAG_INFO tag{};
  FILE_STANDARD_INFO standard{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag, sizeof(tag)) ||
      !GetFileInformationByHandleEx(handle, FileStandardInfo, &standard, sizeof(standard)))
    return StorageStatus::kIoFailed;
  if ((tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
    return StorageStatus::kReparseRefused;
  constexpr DWORD rejected =
      FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_DEVICE | FILE_ATTRIBUTE_OFFLINE |
      FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED |
      FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS |
      FILE_ATTRIBUTE_VIRTUAL;
  if ((tag.FileAttributes & rejected) != 0 || standard.Directory)
    return StorageStatus::kUnsupportedFileType;
  if (standard.DeletePending) return StorageStatus::kDeletePending;
  if (standard.NumberOfLinks != 1) return StorageStatus::kLinkCountRefused;
  if (standard.EndOfFile.QuadPart < 0 ||
      (require_size && static_cast<std::uint64_t>(standard.EndOfFile.QuadPart) != expected_size))
    return StorageStatus::kSizeMismatch;
  return StorageStatus::kOkOpened;
}

StorageStatus validate_volume(HANDLE file, HANDLE volume,
                              const wchar_t (&root)[4],
                              DWORD expected_serial,
                              bool require_expected_serial,
                              std::string& filesystem,
                              DWORD& filesystem_serial) {
  if (GetDriveTypeW(root) != DRIVE_FIXED) return StorageStatus::kUnsafeVolume;
  STORAGE_HOTPLUG_INFO hotplug{};
  hotplug.Size = sizeof(hotplug);
  DWORD returned = 0;
  if (!DeviceIoControl(volume, IOCTL_STORAGE_GET_HOTPLUG_INFO,
                       nullptr, 0, &hotplug, sizeof(hotplug), &returned, nullptr) ||
      returned < sizeof(hotplug) || hotplug.MediaRemovable || hotplug.MediaHotplug ||
      hotplug.DeviceHotplug) return StorageStatus::kUnsafeVolume;

  std::array<wchar_t, 32> name{};
  DWORD component_length = 0;
  DWORD flags = 0;
  if (!GetVolumeInformationByHandleW(file, nullptr, 0, &filesystem_serial,
                                     &component_length, &flags, name.data(),
                                     static_cast<DWORD>(name.size())))
    return StorageStatus::kUnsafeVolume;
  if (CompareStringOrdinal(name.data(), -1, L"NTFS", -1, TRUE) != CSTR_EQUAL ||
      (flags & FILE_PERSISTENT_ACLS) == 0)
    return StorageStatus::kUnsupportedFilesystem;
  if ((flags & (FILE_READ_ONLY_VOLUME | FILE_VOLUME_IS_COMPRESSED)) != 0)
    return StorageStatus::kUnsafeVolume;
  if (require_expected_serial && filesystem_serial != expected_serial)
    return StorageStatus::kIdentityMismatch;
  filesystem = "NTFS";
  return StorageStatus::kOkOpened;
}

StorageStatus filesystem_policy(HANDLE file, const wchar_t (&root)[4],
                                UniqueHandle& volume, std::string& filesystem,
                                DWORD& filesystem_serial) {
  if (GetDriveTypeW(root) != DRIVE_FIXED) return StorageStatus::kUnsafeVolume;
  std::wstring volume_path = L"\\\\.\\";
  volume_path.push_back(root[0]);
  volume_path.push_back(L':');
  HANDLE raw_volume = CreateFileW(volume_path.c_str(), 0,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  nullptr, OPEN_EXISTING, 0, nullptr);
  if (raw_volume == INVALID_HANDLE_VALUE) return StorageStatus::kUnsafeVolume;
  volume.reset(raw_volume);
  return validate_volume(file, volume.get(), root, 0, false, filesystem,
                         filesystem_serial);
}

bool acquire_directories(const std::wstring& canonical, PSID current_sid,
                         std::vector<HeldDirectory>& held,
                         FileIdentity& volume_identity) {
  std::vector<std::wstring> paths;
  paths.emplace_back(canonical.substr(0, 3));
  std::size_t offset = 3;
  while (offset < canonical.size()) {
    const std::size_t separator = canonical.find(L'\\', offset);
    paths.emplace_back(canonical.substr(
        0, separator == std::wstring::npos ? canonical.size() : separator));
    if (separator == std::wstring::npos) break;
    offset = separator + 1;
  }
  if (paths.size() > kMaxAncestorHandles) return false;
  held.reserve(paths.size());
  for (const auto& path : paths) {
    HANDLE raw = CreateFileW(path.c_str(), FILE_READ_ATTRIBUTES | READ_CONTROL,
                             kDirectoryShare, nullptr, OPEN_EXISTING,
                             FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                             nullptr);
    if (raw == INVALID_HANDLE_VALUE) return false;
    HeldDirectory entry;
    entry.handle.reset(raw);
    if (!plain_attributes(entry.handle.get(), true) ||
        !final_path(entry.handle.get(), entry.final_path) ||
        !equal_path(entry.final_path, path) ||
        !get_identity(entry.handle.get(), entry.identity)) return false;
    if (held.empty()) volume_identity = entry.identity;
    else if (entry.identity.volume_serial != volume_identity.volume_serial) return false;
    held.push_back(std::move(entry));
  }
  return !held.empty() && private_security(held.back().handle.get(), current_sid);
}

bool directories_stable(const std::vector<HeldDirectory>& held,
                        PSID current_sid) {
  if (held.empty() || !private_security(held.back().handle.get(), current_sid))
    return false;
  for (const auto& entry : held) {
    FileIdentity identity;
    std::wstring path;
    if (!plain_attributes(entry.handle.get(), true) ||
        !get_identity(entry.handle.get(), identity) ||
        !same_identity(identity, entry.identity) ||
        !final_path(entry.handle.get(), path) || !equal_path(path, entry.final_path))
      return false;
  }
  return true;
}

bool seek(HANDLE handle, std::uint64_t offset) {
  if (offset > static_cast<std::uint64_t>(std::numeric_limits<LONGLONG>::max()))
    return false;
  LARGE_INTEGER position{};
  position.QuadPart = static_cast<LONGLONG>(offset);
  return SetFilePointerEx(handle, position, nullptr, FILE_BEGIN) != FALSE;
}

bool write_exact(HANDLE handle, std::uint64_t offset, const std::uint8_t* data,
                 std::size_t size) {
  if (!seek(handle, offset)) return false;
  std::size_t written_total = 0;
  while (written_total < size) {
    const DWORD requested = static_cast<DWORD>(std::min<std::size_t>(
        size - written_total, std::numeric_limits<DWORD>::max()));
    DWORD written = 0;
    if (!WriteFile(handle, data + written_total, requested, &written, nullptr) ||
        written == 0 || written > requested) return false;
    written_total += written;
  }
  return written_total == size;
}

bool read_exact(HANDLE handle, std::uint64_t offset, std::uint8_t* data,
                std::size_t size) {
  if (!seek(handle, offset)) return false;
  std::size_t read_total = 0;
  while (read_total < size) {
    const DWORD requested = static_cast<DWORD>(std::min<std::size_t>(
        size - read_total, std::numeric_limits<DWORD>::max()));
    DWORD read = 0;
    if (!ReadFile(handle, data + read_total, requested, &read, nullptr) ||
        read == 0 || read > requested) return false;
    read_total += read;
  }
  return read_total == size;
}

struct HashPart {
  const std::uint8_t* data;
  std::size_t size;
};

bool sha256(const std::vector<HashPart>& parts,
            std::array<std::uint8_t, 32>& output) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_length = 0;
  DWORD hash_length = 0;
  DWORD copied = 0;
  std::vector<std::uint8_t> object;
  bool ok = BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
      &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0));
  if (ok) ok = BCRYPT_SUCCESS(BCryptGetProperty(
      algorithm, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&object_length),
      sizeof(object_length), &copied, 0));
  if (ok) ok = BCRYPT_SUCCESS(BCryptGetProperty(
      algorithm, BCRYPT_HASH_LENGTH, reinterpret_cast<PUCHAR>(&hash_length),
      sizeof(hash_length), &copied, 0));
  if (!ok || object_length == 0 || object_length > 1'048'576 ||
      hash_length != output.size()) {
    if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
    return false;
  }
  object.resize(object_length);
  ok = BCRYPT_SUCCESS(BCryptCreateHash(algorithm, &hash, object.data(),
                                      object_length, nullptr, 0, 0));
  for (const auto& part : parts) {
    if (!ok || part.size > std::numeric_limits<ULONG>::max()) { ok = false; break; }
    ok = BCRYPT_SUCCESS(BCryptHashData(
        hash, const_cast<PUCHAR>(part.data), static_cast<ULONG>(part.size), 0));
  }
  if (ok) ok = BCRYPT_SUCCESS(BCryptFinishHash(
      hash, output.data(), static_cast<ULONG>(output.size()), 0));
  if (hash != nullptr) BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  SecureZeroMemory(object.data(), object.size());
  return ok;
}

void write_u32(std::array<std::uint8_t, kHeaderBytes>& bytes,
               std::size_t offset, std::uint32_t value) {
  for (std::size_t index = 0; index < 4; ++index)
    bytes[offset + index] = static_cast<std::uint8_t>(value >> (index * 8));
}

void write_u64(std::array<std::uint8_t, kHeaderBytes>& bytes,
               std::size_t offset, std::uint64_t value) {
  for (std::size_t index = 0; index < 8; ++index)
    bytes[offset + index] = static_cast<std::uint8_t>(value >> (index * 8));
}

std::uint32_t read_u32(const std::array<std::uint8_t, kHeaderBytes>& bytes,
                       std::size_t offset) {
  std::uint32_t value = 0;
  for (std::size_t index = 0; index < 4; ++index)
    value |= static_cast<std::uint32_t>(bytes[offset + index]) << (index * 8);
  return value;
}

std::uint64_t read_u64(const std::array<std::uint8_t, kHeaderBytes>& bytes,
                       std::size_t offset) {
  std::uint64_t value = 0;
  for (std::size_t index = 0; index < 8; ++index)
    value |= static_cast<std::uint64_t>(bytes[offset + index]) << (index * 8);
  return value;
}

bool header_digest(const std::array<std::uint8_t, kHeaderBytes>& header,
                   const std::array<std::uint8_t, 32>& container_id,
                   std::array<std::uint8_t, 32>& digest) {
  return sha256({
      {reinterpret_cast<const std::uint8_t*>(kHashDomain), sizeof(kHashDomain)},
      {reinterpret_cast<const std::uint8_t*>(kHeaderLabel), sizeof(kHeaderLabel)},
      {container_id.data(), container_id.size()},
      {header.data(), kHeaderChecksumOffset}}, digest);
}

StorageStatus encode_header(const std::array<std::uint8_t, 32>& container_id,
                            std::array<std::uint8_t, kHeaderBytes>& header) {
  header.fill(0);
  std::copy(kHeaderMagic.begin(), kHeaderMagic.end(), header.begin());
  write_u32(header, 16, 1);
  write_u32(header, 20, 0x01020304);
  write_u32(header, 24, kHeaderBytes);
  write_u32(header, 28, 1'024);
  write_u32(header, 32, 2);
  write_u32(header, 36, 16'384);
  write_u32(header, 40, 15'360);
  write_u32(header, 44, 1'024);
  write_u32(header, 48, 1'024);
  write_u32(header, 52, 16);
  write_u32(header, 56, 896);
  write_u32(header, 60, 768);
  write_u32(header, 64, 256);
  write_u32(header, 68, 768);
  write_u64(header, 72, kContainerBytes);
  write_u32(header, 80, static_cast<std::uint32_t>(sizeof(kProtocol) - 1));
  std::copy_n(reinterpret_cast<const std::uint8_t*>(kProtocol),
              sizeof(kProtocol) - 1, header.begin() + kProtocolOffset);
  write_u32(header, 148, 1);
  std::copy(container_id.begin(), container_id.end(),
            header.begin() + kContainerIdOffset);
  std::array<std::uint8_t, 32> digest{};
  if (!header_digest(header, container_id, digest)) return StorageStatus::kHashFailed;
  std::copy(digest.begin(), digest.end(), header.begin() + kHeaderChecksumOffset);
  return StorageStatus::kOkCreated;
}

StorageStatus decode_header(const std::array<std::uint8_t, kHeaderBytes>& header,
                            std::array<std::uint8_t, 32>& container_id) {
  if (!equal_bytes(header.data(), kHeaderMagic.data(), kHeaderMagic.size())) {
    return all_zero(header.data(), kHeaderMagic.size())
               ? StorageStatus::kContainerUnformatted
               : StorageStatus::kContainerCorruptHeader;
  }
  constexpr std::array<std::pair<std::size_t, std::uint32_t>, 14> fields = {{
      {16, 1}, {20, 0x01020304}, {24, kHeaderBytes}, {28, 1'024},
      {32, 2}, {36, 16'384}, {40, 15'360}, {44, 1'024}, {48, 1'024},
      {52, 16}, {56, 896}, {60, 768}, {64, 256}, {68, 768}}};
  for (const auto& [offset, expected] : fields)
    if (read_u32(header, offset) != expected)
      return StorageStatus::kContainerCorruptHeader;
  if (read_u64(header, 72) != kContainerBytes ||
      read_u32(header, 80) != sizeof(kProtocol) - 1 ||
      read_u32(header, 148) != 1 ||
      !equal_bytes(header.data() + kProtocolOffset,
                   reinterpret_cast<const std::uint8_t*>(kProtocol),
                   sizeof(kProtocol) - 1) ||
      !all_zero(header.data() + kProtocolOffset + sizeof(kProtocol) - 1,
                kProtocolBytes - (sizeof(kProtocol) - 1)))
    return StorageStatus::kContainerCorruptHeader;
  std::copy_n(header.begin() + kContainerIdOffset, container_id.size(),
              container_id.begin());
  if (all_zero(container_id.data(), container_id.size()) ||
      !all_zero(header.data() + kContainerIdOffset + container_id.size(),
                kHeaderChecksumOffset - kContainerIdOffset - container_id.size()))
    return StorageStatus::kContainerCorruptHeader;
  std::array<std::uint8_t, 32> expected{};
  if (!header_digest(header, container_id, expected)) return StorageStatus::kHashFailed;
  if (!equal_bytes(expected.data(), header.data() + kHeaderChecksumOffset,
                   expected.size())) return StorageStatus::kContainerCorruptHeader;
  return StorageStatus::kOkOpened;
}

StorageStatus zero_initialize(HANDLE file) {
  std::array<std::uint8_t, kIoChunkBytes> zero{};
  for (std::uint64_t offset = 0; offset < kContainerBytes; offset += zero.size()) {
    const auto count = static_cast<std::size_t>(
        std::min<std::uint64_t>(zero.size(), kContainerBytes - offset));
    if (!write_exact(file, offset, zero.data(), count))
      return StorageStatus::kGenesisIncomplete;
  }
  if (!seek(file, kContainerBytes) || !SetEndOfFile(file) || !FlushFileBuffers(file))
    return StorageStatus::kGenesisIncomplete;
  std::array<std::uint8_t, kIoChunkBytes> readback{};
  for (std::uint64_t offset = 0; offset < kContainerBytes; offset += readback.size()) {
    const auto count = static_cast<std::size_t>(
        std::min<std::uint64_t>(readback.size(), kContainerBytes - offset));
    if (!read_exact(file, offset, readback.data(), count) ||
        !all_zero(readback.data(), count)) return StorageStatus::kGenesisIncomplete;
  }
  return StorageStatus::kOkCreated;
}

StorageStatus initialize_header(HANDLE file,
                                std::array<std::uint8_t, 32>& container_id) {
  if (!BCRYPT_SUCCESS(BCryptGenRandom(nullptr, container_id.data(),
                                      static_cast<ULONG>(container_id.size()),
                                      BCRYPT_USE_SYSTEM_PREFERRED_RNG)) ||
      all_zero(container_id.data(), container_id.size()))
    return StorageStatus::kRngFailed;
  std::array<std::uint8_t, kHeaderBytes> header{};
  StorageStatus status = encode_header(container_id, header);
  if (status != StorageStatus::kOkCreated) return status;
  if (!write_exact(file, 0, header.data(), header.size()) || !FlushFileBuffers(file))
    return StorageStatus::kGenesisIncomplete;
  std::array<std::uint8_t, kHeaderBytes> readback{};
  if (!read_exact(file, 0, readback.data(), readback.size()) ||
      !equal_bytes(header.data(), readback.data(), header.size()))
    return StorageStatus::kGenesisIncomplete;
  std::array<std::uint8_t, 32> decoded{};
  status = decode_header(readback, decoded);
  if (status != StorageStatus::kOkOpened ||
      !equal_bytes(container_id.data(), decoded.data(), container_id.size()))
    return StorageStatus::kGenesisIncomplete;
  return StorageStatus::kOkCreated;
}

StorageStatus load_header(HANDLE file,
                          std::array<std::uint8_t, 32>& container_id) {
  std::array<std::uint8_t, kHeaderBytes> header{};
  if (!read_exact(file, 0, header.data(), header.size())) return StorageStatus::kIoFailed;
  return decode_header(header, container_id);
}

StorageStatus open_error(bool create) {
  const DWORD error = GetLastError();
  if (create && (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS))
    return StorageStatus::kAlreadyExists;
  if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
    return StorageStatus::kNotFound;
  if (error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION ||
      error == ERROR_PRIVILEGE_NOT_HELD)
    return StorageStatus::kAccessDenied;
  return StorageStatus::kIoFailed;
}

std::string hex(const std::uint8_t* data, std::size_t count) {
  constexpr char alphabet[] = "0123456789abcdef";
  std::string output;
  output.reserve(count * 2);
  for (std::size_t index = 0; index < count; ++index) {
    output.push_back(alphabet[data[index] >> 4]);
    output.push_back(alphabet[data[index] & 0x0f]);
  }
  return output;
}

std::string hex_u64(std::uint64_t value) {
  std::array<std::uint8_t, 8> bytes{};
  for (std::size_t index = 0; index < bytes.size(); ++index)
    bytes[bytes.size() - index - 1] = static_cast<std::uint8_t>(value >> (index * 8));
  return hex(bytes.data(), bytes.size());
}

bool expected_identity_valid(const StorageIdentity& value) {
  return value.volume_serial != 0 && !all_zero(value.file_id.data(), value.file_id.size()) &&
         !all_zero(value.container_id.data(), value.container_id.size());
}

}  // namespace

struct JournalStorageLease::Impl {
  UniqueHandle file;
  UniqueHandle path_reopen;
  UniqueHandle volume;
  std::vector<HeldDirectory> directories;
  FileIdentity identity;
  std::array<std::uint8_t, 32> container_id{};
};

JournalStorageLease::JournalStorageLease() noexcept = default;
JournalStorageLease::~JournalStorageLease() = default;
JournalStorageLease::JournalStorageLease(JournalStorageLease&&) noexcept = default;
JournalStorageLease& JournalStorageLease::operator=(JournalStorageLease&&) noexcept = default;
bool JournalStorageLease::valid() const noexcept {
  return impl_ && static_cast<bool>(impl_->file) &&
         static_cast<bool>(impl_->path_reopen) &&
         !impl_->directories.empty();
}
HANDLE JournalStorageLease::retained_file_handle() const noexcept {
  return valid() ? impl_->file.get() : INVALID_HANDLE_VALUE;
}
void JournalStorageLease::reset() noexcept { impl_.reset(); }

const char* status_name(StorageStatus status) noexcept {
  switch (status) {
    case StorageStatus::kOkCreated: return "ok_created";
    case StorageStatus::kOkOpened: return "ok_opened";
    case StorageStatus::kPlatformUnavailable: return "platform_unavailable";
    case StorageStatus::kInvalidRequest: return "invalid_request";
    case StorageStatus::kUnsafePath: return "unsafe_path";
    case StorageStatus::kUnsafeVolume: return "unsafe_volume";
    case StorageStatus::kUnsupportedFilesystem: return "unsupported_filesystem";
    case StorageStatus::kPrivateDirectoryRequired: return "private_directory_required";
    case StorageStatus::kAlreadyExists: return "already_exists";
    case StorageStatus::kNotFound: return "not_found";
    case StorageStatus::kAccessDenied: return "access_denied";
    case StorageStatus::kSecurityUnavailable: return "security_unavailable";
    case StorageStatus::kReparseRefused: return "reparse_refused";
    case StorageStatus::kUnsupportedFileType: return "unsupported_file_type";
    case StorageStatus::kLinkCountRefused: return "link_count_refused";
    case StorageStatus::kDeletePending: return "delete_pending";
    case StorageStatus::kSizeMismatch: return "size_mismatch";
    case StorageStatus::kIdentityMismatch: return "identity_mismatch";
    case StorageStatus::kReopenIdentityMismatch: return "reopen_identity_mismatch";
    case StorageStatus::kRngFailed: return "rng_failed";
    case StorageStatus::kHashFailed: return "hash_failed";
    case StorageStatus::kIoFailed: return "io_failed";
    case StorageStatus::kGenesisIncomplete: return "genesis_incomplete";
    case StorageStatus::kContainerUnformatted: return "container_unformatted";
    case StorageStatus::kContainerCorruptHeader: return "container_corrupt_header";
    case StorageStatus::kInternal: return "internal";
  }
  return "internal";
}

StorageStatus acquire_storage(const StorageRequest& request,
                              JournalStorageLease& lease,
                              StorageReceipt& receipt) noexcept {
  lease.reset();
  receipt = {};
  auto fail = [&receipt](StorageStatus status) {
    receipt.status = status_name(status);
    return status;
  };
  try {
    const bool create = request.mode == OpenMode::kCreateNew;
    const bool open = request.mode == OpenMode::kOpenExisting;
    if ((!create && !open) || (create && request.has_expected_identity) ||
        (open && (!request.has_expected_identity ||
                  !expected_identity_valid(request.expected_identity))))
      return fail(StorageStatus::kInvalidRequest);

    std::wstring directory;
    wchar_t root[4]{};
    if (!canonical_directory(request.absolute_directory, directory, root))
      return fail(StorageStatus::kUnsafePath);
    if (directory.size() + 1 + (std::size(kFixedLeafName) - 1) > kMaxPathCharacters)
      return fail(StorageStatus::kUnsafePath);
    const std::wstring path = directory + L"\\" + kFixedLeafName;

    CurrentUser user;
    if (!current_user(user)) return fail(StorageStatus::kSecurityUnavailable);
    auto candidate = std::make_unique<JournalStorageLease::Impl>();
    FileIdentity directory_volume;
    if (!acquire_directories(directory, user.sid, candidate->directories,
                             directory_volume))
      return fail(StorageStatus::kPrivateDirectoryRequired);

    PrivateSecurityDescriptor security;
    if (!build_private_security(user.sid, security))
      return fail(StorageStatus::kSecurityUnavailable);
    HANDLE raw = CreateFileW(
        path.c_str(), GENERIC_READ | GENERIC_WRITE | READ_CONTROL, kFileShare,
        create ? &security.attributes : nullptr, create ? CREATE_NEW : OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH,
        nullptr);
    if (raw == INVALID_HANDLE_VALUE) return fail(open_error(create));
    candidate->file.reset(raw);

    StorageStatus status = file_shape(candidate->file.get(), create ? 0 : kContainerBytes, true);
    if (status != StorageStatus::kOkOpened) return fail(status);
    std::wstring observed_path;
    if (!final_path(candidate->file.get(), observed_path) || !equal_path(path, observed_path))
      return fail(StorageStatus::kIdentityMismatch);
    if (!private_security(candidate->file.get(), user.sid))
      return fail(StorageStatus::kSecurityUnavailable);
    if (!get_identity(candidate->file.get(), candidate->identity) ||
        candidate->identity.volume_serial != directory_volume.volume_serial)
      return fail(StorageStatus::kIdentityMismatch);
    DWORD filesystem_serial = 0;
    std::string filesystem;
    status = filesystem_policy(candidate->file.get(), root, candidate->volume,
                               filesystem, filesystem_serial);
    if (status != StorageStatus::kOkOpened) return fail(status);
    if (static_cast<DWORD>(candidate->identity.volume_serial) != filesystem_serial)
      return fail(StorageStatus::kIdentityMismatch);
    if (open && !same_identity(candidate->identity, request.expected_identity))
      return fail(StorageStatus::kIdentityMismatch);

    if (create) {
      status = zero_initialize(candidate->file.get());
      if (status != StorageStatus::kOkCreated) return fail(status);
      status = initialize_header(candidate->file.get(), candidate->container_id);
      if (status != StorageStatus::kOkCreated) return fail(status);
    } else {
      status = load_header(candidate->file.get(), candidate->container_id);
      if (status != StorageStatus::kOkOpened) return fail(status);
      if (!equal_bytes(candidate->container_id.data(),
                       request.expected_identity.container_id.data(),
                       candidate->container_id.size()))
        return fail(StorageStatus::kIdentityMismatch);
    }

    status = file_shape(candidate->file.get(), kContainerBytes, true);
    FileIdentity final_identity;
    if (status != StorageStatus::kOkOpened) return fail(status);
    if (!get_identity(candidate->file.get(), final_identity) ||
        !same_identity(final_identity, candidate->identity) ||
        !private_security(candidate->file.get(), user.sid) ||
        !directories_stable(candidate->directories, user.sid))
      return fail(StorageStatus::kIdentityMismatch);
    DWORD rechecked_serial = 0;
    std::string rechecked_filesystem;
    status = validate_volume(candidate->file.get(), candidate->volume.get(), root,
                             filesystem_serial, true, rechecked_filesystem,
                             rechecked_serial);
    if (status != StorageStatus::kOkOpened || rechecked_filesystem != filesystem)
      return fail(status == StorageStatus::kOkOpened
                      ? StorageStatus::kIdentityMismatch
                      : status);

    HANDLE reopen = CreateFileW(path.c_str(), FILE_READ_ATTRIBUTES | READ_CONTROL,
                                FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (reopen == INVALID_HANDLE_VALUE)
      return fail(StorageStatus::kReopenIdentityMismatch);
    candidate->path_reopen.reset(reopen);
    FileIdentity reopened_identity;
    std::wstring reopened_path;
    if (file_shape(candidate->path_reopen.get(), kContainerBytes, true) !=
            StorageStatus::kOkOpened ||
        !get_identity(candidate->path_reopen.get(), reopened_identity) ||
        !same_identity(reopened_identity, candidate->identity) ||
        !final_path(candidate->path_reopen.get(), reopened_path) ||
        !equal_path(reopened_path, path) ||
        !private_security(candidate->path_reopen.get(), user.sid))
      return fail(StorageStatus::kReopenIdentityMismatch);

    receipt.abi_version = kStorageAbiVersion;
    receipt.status = status_name(create ? StorageStatus::kOkCreated
                                        : StorageStatus::kOkOpened);
    receipt.container_bytes = kContainerBytes;
    receipt.container_id_hex = hex(candidate->container_id.data(),
                                   candidate->container_id.size());
    receipt.volume_serial_hex = hex_u64(candidate->identity.volume_serial);
    receipt.file_id_hex = hex(candidate->identity.file_id.data(),
                             candidate->identity.file_id.size());
    receipt.filesystem = filesystem;
    receipt.dacl_profile = "current_user_only_protected_v1";
    receipt.identity_reopened = true;
    receipt.handle_retained = true;
    const StorageStatus success = create ? StorageStatus::kOkCreated
                                         : StorageStatus::kOkOpened;
    lease.impl_ = std::move(candidate);
    return success;
  } catch (const std::bad_alloc&) {
    return fail(StorageStatus::kInternal);
  } catch (...) {
    return fail(StorageStatus::kInternal);
  }
}

}  // namespace lae::action_journal_storage
