#include "windows_readonly_fs.hpp"

#include <aclapi.h>
#include <winioctl.h>
#include <winternl.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cwctype>
#include <limits>
#include <new>
#include <utility>
#include <vector>

namespace lae::windows_readonly_fs {
namespace {

constexpr std::size_t kMaxPathCharacters = 32'767;
constexpr std::size_t kMaxComponentCharacters = 255;
constexpr std::size_t kEnumerationBufferBytes = 65'536;
constexpr std::size_t kReadChunkBytes = 16'384;
constexpr DWORD kObjectShare = FILE_SHARE_READ;  // Deny WRITE and DELETE.
constexpr DWORD kPrivateAccess = FILE_ALL_ACCESS;
constexpr ULONG kRelativeOpenOptions =
    FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT;

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

struct CurrentUser {
  UniqueHandle token;
  std::vector<std::uint8_t> token_user;
  PSID sid = nullptr;
};

struct HeldObject {
  UniqueHandle handle;
  ObjectIdentity identity{};
  ObjectKind kind = ObjectKind::kFile;
  bool require_private = true;
};

bool all_zero(const std::uint8_t* bytes, std::size_t count) noexcept {
  std::uint8_t value = 0;
  for (std::size_t index = 0; index < count; ++index) value |= bytes[index];
  return value == 0;
}

bool same_bytes(const std::uint8_t* left, const std::uint8_t* right,
                std::size_t count) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < count; ++index)
    difference |= static_cast<std::uint8_t>(left[index] ^ right[index]);
  return difference == 0;
}

bool same_identity(const ObjectIdentity& left,
                   const ObjectIdentity& right) noexcept {
  return left.volume_serial == right.volume_serial &&
         same_bytes(left.file_id.data(), right.file_id.data(),
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

bool final_path(HANDLE handle, std::wstring& output) {
  std::vector<wchar_t> buffer(kMaxPathCharacters + 1);
  const DWORD count = GetFinalPathNameByHandleW(
      handle, buffer.data(), static_cast<DWORD>(buffer.size()),
      FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (count == 0 || count >= buffer.size()) return false;
  output = strip_extended_prefix(std::wstring(buffer.data(), count));
  return output.find(L'\0') == std::wstring::npos;
}

bool reserved_component(const std::wstring& component) {
  std::wstring stem = component.substr(0, component.find(L'.'));
  std::transform(stem.begin(), stem.end(), stem.begin(), [](wchar_t value) {
    return static_cast<wchar_t>(towupper(value));
  });
  if (stem == L"CON" || stem == L"PRN" || stem == L"AUX" || stem == L"NUL")
    return true;
  if (stem.size() == 4 && stem[3] >= L'1' && stem[3] <= L'9')
    return stem.rfind(L"COM", 0) == 0 || stem.rfind(L"LPT", 0) == 0;
  return false;
}

bool valid_utf16(const std::wstring& value) {
  if (value.empty() ||
      value.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  return WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
                             static_cast<int>(value.size()), nullptr, 0,
                             nullptr, nullptr) > 0;
}

bool safe_component(const std::wstring& component) {
  if (component.empty() || component.size() > kMaxComponentCharacters ||
      component == L"." || component == L".." || component.back() == L'.' ||
      component.back() == L' ' || reserved_component(component) ||
      !valid_utf16(component))
    return false;
  for (const wchar_t value : component) {
    if (value < 0x20 || value == 0x7f || value == L':' || value == L'/' ||
        value == L'\\' || value == L'<' || value == L'>' || value == L'"' ||
        value == L'|' || value == L'?' || value == L'*')
      return false;
  }
  return true;
}

bool canonical_root(const std::wstring& supplied, std::wstring& canonical,
                    wchar_t (&drive_root)[4]) {
  if (supplied.size() <= 3 || supplied.size() > kMaxPathCharacters ||
      supplied.find(L'\0') != std::wstring::npos ||
      supplied.find(L'/') != std::wstring::npos ||
      supplied.rfind(L"\\\\", 0) == 0 || supplied.rfind(L"\\\\?\\", 0) == 0 ||
      supplied.rfind(L"\\\\.\\", 0) == 0 || !iswalpha(supplied[0]) ||
      supplied[1] != L':' || supplied[2] != L'\\' || supplied.back() == L'\\')
    return false;
  for (std::size_t index = 0; index < supplied.size(); ++index)
    if (supplied[index] == L':' && index != 1) return false;

  std::size_t start = 3;
  std::size_t count = 0;
  while (start < supplied.size()) {
    const std::size_t end = supplied.find(L'\\', start);
    const std::wstring component = supplied.substr(
        start, end == std::wstring::npos ? std::wstring::npos : end - start);
    if (!safe_component(component) || ++count > kMaxComponents) return false;
    if (end == std::wstring::npos) break;
    start = end + 1;
  }

  std::vector<wchar_t> buffer(kMaxPathCharacters + 1);
  const DWORD length = GetFullPathNameW(supplied.c_str(),
                                        static_cast<DWORD>(buffer.size()),
                                        buffer.data(), nullptr);
  if (length == 0 || length >= buffer.size()) return false;
  canonical.assign(buffer.data(), length);
  if (!equal_path(canonical, supplied)) return false;
  drive_root[0] = canonical[0];
  drive_root[1] = L':';
  drive_root[2] = L'\\';
  drive_root[3] = L'\0';
  return true;
}

bool valid_grant_id(const std::string& value) {
  if (value.empty() || value.size() > 64) return false;
  for (std::size_t index = 0; index < value.size(); ++index) {
    const unsigned char byte = static_cast<unsigned char>(value[index]);
    const bool alpha = byte >= 'a' && byte <= 'z';
    const bool digit = byte >= '0' && byte <= '9';
    if (!alpha && !digit && (index == 0 || (byte != '_' && byte != '-')))
      return false;
  }
  return true;
}

Status checkpoint(const ExecutionContext& execution) noexcept {
  if (execution.monotonic_ms == nullptr || execution.is_cancelled == nullptr)
    return Status::kInvalidRequest;
  if (execution.is_cancelled(execution.opaque)) return Status::kCancelled;
  const std::uint64_t now = execution.monotonic_ms(execution.opaque);
  if (execution.deadline_monotonic_ms == 0 ||
      now >= execution.deadline_monotonic_ms)
    return Status::kDeadlineExceeded;
  return Status::kOk;
}

bool current_user(CurrentUser& user) {
  HANDLE raw = INVALID_HANDLE_VALUE;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw)) return false;
  user.token.reset(raw);
  DWORD size = 0;
  GetTokenInformation(user.token.get(), TokenUser, nullptr, 0, &size);
  if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0 || size > 65'536)
    return false;
  user.token_user.resize(size);
  if (!GetTokenInformation(user.token.get(), TokenUser, user.token_user.data(),
                           size, &size))
    return false;
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

bool private_security(HANDLE handle, PSID expected_user) {
  PSID owner = nullptr;
  PACL dacl = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  const DWORD result = GetSecurityInfo(
      handle, SE_FILE_OBJECT,
      OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
      &owner, nullptr, &dacl, nullptr, &descriptor);
  if (result != ERROR_SUCCESS || descriptor == nullptr) return false;
  bool valid = owner != nullptr && IsValidSid(owner) &&
               EqualSid(owner, expected_user);
  SECURITY_DESCRIPTOR_CONTROL control = 0;
  DWORD revision = 0;
  valid = valid &&
          GetSecurityDescriptorControl(descriptor, &control, &revision) &&
          revision == SECURITY_DESCRIPTOR_REVISION &&
          (control & SE_DACL_PROTECTED) != 0 &&
          (control & SE_DACL_DEFAULTED) == 0 && dacl != nullptr &&
          IsValidAcl(dacl) && dacl->AceCount == 1;
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
  LocalFree(descriptor);
  return valid;
}

bool get_identity(HANDLE handle, ObjectIdentity& identity) {
  FILE_ID_INFO info{};
  if (!GetFileInformationByHandleEx(handle, FileIdInfo, &info, sizeof(info)))
    return false;
  identity.volume_serial = info.VolumeSerialNumber;
  std::copy(std::begin(info.FileId.Identifier), std::end(info.FileId.Identifier),
            identity.file_id.begin());
  return identity.volume_serial != 0 &&
         !all_zero(identity.file_id.data(), identity.file_id.size());
}

Status inspect_object(HANDLE handle, PSID expected_user,
                      std::uint64_t expected_volume, bool require_private,
                      ObjectKind* kind,
                      std::uint64_t* size, ObjectIdentity& identity) {
  if (GetFileType(handle) != FILE_TYPE_DISK)
    return Status::kUnsupportedFileType;
  FILE_ATTRIBUTE_TAG_INFO tag{};
  FILE_STANDARD_INFO standard{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag,
                                    sizeof(tag)) ||
      !GetFileInformationByHandleEx(handle, FileStandardInfo, &standard,
                                    sizeof(standard)) ||
      !get_identity(handle, identity))
    return Status::kIoFailed;
  if ((tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
    return Status::kReparseRefused;
  constexpr DWORD rejected =
      FILE_ATTRIBUTE_DEVICE | FILE_ATTRIBUTE_OFFLINE |
      FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED |
      FILE_ATTRIBUTE_ENCRYPTED | FILE_ATTRIBUTE_RECALL_ON_OPEN |
      FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_VIRTUAL;
  if ((tag.FileAttributes & rejected) != 0)
    return Status::kUnsupportedFileType;
  if (standard.DeletePending) return Status::kDeletePending;
  const bool directory = standard.Directory != FALSE ||
                         (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
  if (!directory && standard.NumberOfLinks != 1)
    return Status::kLinkCountRefused;
  if (standard.EndOfFile.QuadPart < 0) return Status::kBoundsExceeded;
  if (identity.volume_serial != expected_volume)
    return Status::kIdentityMismatch;
  if (require_private && !private_security(handle, expected_user))
    return Status::kSecurityUnavailable;
  if (kind != nullptr)
    *kind = directory ? ObjectKind::kDirectory : ObjectKind::kFile;
  if (size != nullptr)
    *size = directory ? 0 : static_cast<std::uint64_t>(standard.EndOfFile.QuadPart);
  return Status::kOk;
}

Status validate_volume(HANDLE root_handle, const wchar_t (&drive_root)[4],
                       std::uint64_t expected_serial) {
  if (GetDriveTypeW(drive_root) != DRIVE_FIXED) return Status::kUnsafeVolume;
  std::wstring volume_path = L"\\\\.\\";
  volume_path.push_back(drive_root[0]);
  volume_path.push_back(L':');
  UniqueHandle volume(CreateFileW(volume_path.c_str(), 0,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE |
                                      FILE_SHARE_DELETE,
                                  nullptr, OPEN_EXISTING, 0, nullptr));
  if (!volume) return Status::kUnsafeVolume;
  STORAGE_HOTPLUG_INFO hotplug{};
  hotplug.Size = sizeof(hotplug);
  DWORD returned = 0;
  if (!DeviceIoControl(volume.get(), IOCTL_STORAGE_GET_HOTPLUG_INFO, nullptr, 0,
                       &hotplug, sizeof(hotplug), &returned, nullptr) ||
      returned < sizeof(hotplug) || hotplug.MediaRemovable ||
      hotplug.MediaHotplug || hotplug.DeviceHotplug)
    return Status::kUnsafeVolume;

  std::array<wchar_t, 32> filesystem{};
  DWORD serial = 0;
  DWORD max_component = 0;
  DWORD flags = 0;
  if (!GetVolumeInformationByHandleW(
          root_handle, nullptr, 0, &serial, &max_component, &flags,
          filesystem.data(), static_cast<DWORD>(filesystem.size())))
    return Status::kUnsafeVolume;
  if (serial == 0 || expected_serial == 0) return Status::kIdentityMismatch;
  if (CompareStringOrdinal(filesystem.data(), -1, L"NTFS", -1, TRUE) !=
          CSTR_EQUAL ||
      (flags & FILE_PERSISTENT_ACLS) == 0)
    return Status::kUnsupportedFilesystem;
  if ((flags & (FILE_READ_ONLY_VOLUME | FILE_VOLUME_IS_COMPRESSED)) != 0)
    return Status::kUnsafeVolume;
  return Status::kOk;
}

Status map_open_error(NTSTATUS native_status) noexcept {
  const ULONG error = RtlNtStatusToDosError(native_status);
  if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
    return Status::kNotFound;
  if (error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION)
    return Status::kAccessDenied;
  return Status::kIoFailed;
}

Status open_relative(HANDLE parent, const std::wstring& component,
                     ACCESS_MASK desired_access, ULONG type_option,
                     UniqueHandle& output) {
  if (!safe_component(component)) return Status::kUnsafeComponent;
  if (component.size() >
      static_cast<std::size_t>(std::numeric_limits<USHORT>::max() /
                               sizeof(wchar_t)))
    return Status::kBoundsExceeded;
  UNICODE_STRING name{};
  name.Length = static_cast<USHORT>(component.size() * sizeof(wchar_t));
  name.MaximumLength = name.Length;
  name.Buffer = const_cast<PWSTR>(component.data());
  OBJECT_ATTRIBUTES attributes{};
  attributes.Length = sizeof(attributes);
  attributes.RootDirectory = parent;
  attributes.ObjectName = &name;
  attributes.Attributes = OBJ_CASE_INSENSITIVE;
  attributes.SecurityDescriptor = nullptr;
  attributes.SecurityQualityOfService = nullptr;
  IO_STATUS_BLOCK io_status{};
  HANDLE raw = INVALID_HANDLE_VALUE;
  const NTSTATUS status = NtOpenFile(
      &raw, desired_access | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE,
      &attributes, &io_status, kObjectShare,
      kRelativeOpenOptions | type_option);
  if (status < 0 || raw == INVALID_HANDLE_VALUE) return map_open_error(status);
  output.reset(raw);
  return Status::kOk;
}

Status open_drive_root(const Request& request, const wchar_t (&drive_root)[4],
                       PSID current_sid, HeldObject& root) {
  const DWORD access = FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES |
                       READ_CONTROL | SYNCHRONIZE;
  UniqueHandle handle(CreateFileW(
      drive_root, access, kObjectShare, nullptr, OPEN_EXISTING,
      FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
  if (!handle) {
    const DWORD error = GetLastError();
    if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
      return Status::kNotFound;
    if (error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION)
      return Status::kAccessDenied;
    return Status::kIoFailed;
  }
  ObjectKind kind{};
  std::uint64_t ignored_size = 0;
  ObjectIdentity identity{};
  Status status = inspect_object(handle.get(), current_sid,
                                 request.expected_root_identity.volume_serial,
                                 false, &kind, &ignored_size, identity);
  if (status != Status::kOk) return status;
  if (kind != ObjectKind::kDirectory) return Status::kUnsupportedFileType;
  root.handle = std::move(handle);
  root.identity = identity;
  root.kind = kind;
  root.require_private = false;
  return Status::kOk;
}

Status open_grant_components(const Request& request,
                             const std::wstring& canonical, PSID current_sid,
                             std::vector<HeldObject>& held) {
  std::size_t start = 3;
  while (start < canonical.size()) {
    Status status = checkpoint(request.execution);
    if (status != Status::kOk) return status;
    const std::size_t end = canonical.find(L'\\', start);
    const std::wstring component = canonical.substr(
        start, end == std::wstring::npos ? std::wstring::npos : end - start);
    UniqueHandle handle;
    status = open_relative(held.back().handle.get(), component,
                           FILE_LIST_DIRECTORY, FILE_DIRECTORY_FILE, handle);
    if (status != Status::kOk) return status;
    ObjectKind kind{};
    std::uint64_t ignored_size = 0;
    ObjectIdentity identity{};
    status = inspect_object(handle.get(), current_sid,
                            request.expected_root_identity.volume_serial, true,
                            &kind, &ignored_size, identity);
    if (status != Status::kOk) return status;
    if (kind != ObjectKind::kDirectory)
      return Status::kUnsupportedFileType;
    held.push_back(HeldObject{std::move(handle), identity, kind, true});
    if (end == std::wstring::npos) break;
    start = end + 1;
  }
  if (held.size() < 2 ||
      !same_identity(held.back().identity, request.expected_root_identity))
    return Status::kIdentityMismatch;
  std::wstring resolved;
  if (!final_path(held.back().handle.get(), resolved) ||
      !equal_path(resolved, canonical))
    return Status::kIdentityMismatch;
  return checkpoint(request.execution);
}

Status validate_held(const std::vector<HeldObject>& held, PSID current_sid,
                     std::uint64_t expected_volume) {
  for (const auto& object : held) {
    ObjectKind kind{};
    std::uint64_t ignored_size = 0;
    ObjectIdentity identity{};
    Status status = inspect_object(object.handle.get(), current_sid,
                                   expected_volume, object.require_private,
                                   &kind, &ignored_size, identity);
    if (status != Status::kOk) return status;
    if (kind != object.kind || !same_identity(identity, object.identity))
      return Status::kIdentityMismatch;
  }
  return Status::kOk;
}

Status open_request_target(const Request& request, PSID current_sid,
                           std::vector<HeldObject>& held) {
  for (std::size_t index = 0; index < request.relative_components.size(); ++index) {
    Status status = checkpoint(request.execution);
    if (status != Status::kOk) return status;
    const bool final = index + 1 == request.relative_components.size();
    const bool require_directory = !final || request.operation == Operation::kList;
    const bool require_file = final && request.operation == Operation::kRead;
    ACCESS_MASK access = require_directory ? FILE_LIST_DIRECTORY : 0;
    if (require_file) access |= FILE_READ_DATA;
    ULONG type_option = require_directory
                            ? FILE_DIRECTORY_FILE
                            : (require_file ? FILE_NON_DIRECTORY_FILE : 0);
    UniqueHandle handle;
    status = open_relative(held.back().handle.get(),
                           request.relative_components[index], access,
                           type_option, handle);
    if (status != Status::kOk) return status;
    ObjectKind kind{};
    std::uint64_t ignored_size = 0;
    ObjectIdentity identity{};
    status = inspect_object(handle.get(), current_sid,
                            held.front().identity.volume_serial, true, &kind,
                            &ignored_size, identity);
    if (status != Status::kOk) return status;
    if (require_directory && kind != ObjectKind::kDirectory)
      return Status::kUnsupportedFileType;
    if (require_file && kind != ObjectKind::kFile)
      return Status::kUnsupportedFileType;
    held.push_back(HeldObject{std::move(handle), identity, kind, true});
    status = checkpoint(request.execution);
    if (status != Status::kOk) return status;
  }
  return Status::kOk;
}

Status wide_to_utf8(const std::wstring& value, std::string& output) {
  if (value.empty()) return Status::kUnsafeComponent;
  if (value.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return Status::kBoundsExceeded;
  const int required = WideCharToMultiByte(
      CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
      nullptr, 0, nullptr, nullptr);
  if (required <= 0 || required > static_cast<int>(kMaxListNameBytes))
    return Status::kUtf8Required;
  output.resize(static_cast<std::size_t>(required));
  if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
                          static_cast<int>(value.size()), output.data(), required,
                          nullptr, nullptr) != required)
    return Status::kUtf8Required;
  for (const unsigned char byte : output)
    if (byte < 0x20 || byte == 0x7f) return Status::kUnsafeComponent;
  return Status::kOk;
}

Status stat_target(const Request& request, PSID current_sid,
                   std::vector<HeldObject>& held, Response& response) {
  Status status = checkpoint(request.execution);
  if (status != Status::kOk) return status;
  ObjectKind kind{};
  std::uint64_t size = 0;
  ObjectIdentity identity{};
  status = inspect_object(held.back().handle.get(), current_sid,
                          held.front().identity.volume_serial, true, &kind,
                          &size, identity);
  if (status != Status::kOk || !same_identity(identity, held.back().identity))
    return status == Status::kOk ? Status::kIdentityMismatch : status;
  response.has_stat = true;
  response.stat.name = request.relative_components.empty()
                           ? std::string(".")
                           : std::string();
  if (!request.relative_components.empty()) {
    status = wide_to_utf8(request.relative_components.back(), response.stat.name);
    if (status != Status::kOk) return status;
  }
  response.stat.kind = kind;
  response.stat.size = size;
  return Status::kOk;
}

Status list_target(const Request& request, PSID current_sid,
                   std::vector<HeldObject>& held, Response& response) {
  if (held.back().kind != ObjectKind::kDirectory)
    return Status::kUnsupportedFileType;
  const HANDLE directory_handle = held.back().handle.get();
  std::array<std::uint8_t, kEnumerationBufferBytes> buffer{};
  bool restart = true;
  std::size_t total_name_bytes = 0;
  for (;;) {
    Status status = checkpoint(request.execution);
    if (status != Status::kOk) return status;
    const FILE_INFO_BY_HANDLE_CLASS info_class =
        restart ? FileIdBothDirectoryRestartInfo : FileIdBothDirectoryInfo;
    restart = false;
    if (!GetFileInformationByHandleEx(directory_handle, info_class,
                                      buffer.data(),
                                      static_cast<DWORD>(buffer.size()))) {
      if (GetLastError() == ERROR_NO_MORE_FILES) break;
      return Status::kIoFailed;
    }
    std::size_t offset = 0;
    for (;;) {
      if (offset > buffer.size() - offsetof(FILE_ID_BOTH_DIR_INFO, FileName))
        return Status::kIoFailed;
      const auto* item = reinterpret_cast<const FILE_ID_BOTH_DIR_INFO*>(
          buffer.data() + offset);
      if ((item->FileNameLength % sizeof(wchar_t)) != 0 ||
          item->FileNameLength == 0 ||
          item->FileNameLength > kMaxComponentCharacters * sizeof(wchar_t) ||
          item->FileNameLength >
              buffer.size() - offset - offsetof(FILE_ID_BOTH_DIR_INFO, FileName))
        return Status::kBoundsExceeded;
      const std::wstring name(item->FileName,
                              item->FileNameLength / sizeof(wchar_t));
      if (name != L"." && name != L"..") {
        if (!safe_component(name)) return Status::kUnsafeComponent;
        if (response.entries.size() >= kMaxListEntries)
          return Status::kBoundsExceeded;
        status = checkpoint(request.execution);
        if (status != Status::kOk) return status;
        UniqueHandle child;
        status = open_relative(directory_handle, name, 0, 0, child);
        if (status != Status::kOk) return status;
        ObjectKind kind{};
        std::uint64_t size = 0;
        ObjectIdentity identity{};
        status = inspect_object(child.get(), current_sid,
                                held.front().identity.volume_serial, true,
                                &kind, &size, identity);
        if (status != Status::kOk) return status;
        std::string utf8_name;
        status = wide_to_utf8(name, utf8_name);
        if (status != Status::kOk) return status;
        if (utf8_name.size() > kMaxListNameBytes - total_name_bytes)
          return Status::kBoundsExceeded;
        total_name_bytes += utf8_name.size();
        response.entries.push_back(Entry{std::move(utf8_name), kind, size});
        held.push_back(HeldObject{std::move(child), identity, kind, true});
      }
      if (item->NextEntryOffset == 0) break;
      if ((item->NextEntryOffset % sizeof(void*)) != 0 ||
          item->NextEntryOffset < offsetof(FILE_ID_BOTH_DIR_INFO, FileName) ||
          item->NextEntryOffset > buffer.size() - offset)
        return Status::kIoFailed;
      offset += item->NextEntryOffset;
    }
  }
  response.receipt.entries_returned =
      static_cast<std::uint32_t>(response.entries.size());
  return Status::kOk;
}

Status validate_utf8_text(const std::string& bytes) {
  for (const unsigned char byte : bytes) {
    if (byte == 0 || (byte < 0x20 && byte != '\t' && byte != '\n' &&
                      byte != '\r') || byte == 0x7f)
      return Status::kBinaryRefused;
  }
  if (bytes.empty()) return Status::kOk;
  if (bytes.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return Status::kBoundsExceeded;
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, bytes.data(),
                          static_cast<int>(bytes.size()), nullptr, 0) <= 0)
    return Status::kUtf8Required;
  return Status::kOk;
}

Status read_target(const Request& request, PSID current_sid,
                   std::vector<HeldObject>& held, Response& response) {
  if (held.back().kind != ObjectKind::kFile)
    return Status::kUnsupportedFileType;
  ObjectKind kind{};
  std::uint64_t file_size = 0;
  ObjectIdentity identity{};
  Status status = inspect_object(held.back().handle.get(), current_sid,
                                 held.front().identity.volume_serial, true,
                                 &kind, &file_size, identity);
  if (status != Status::kOk) return status;
  if (!same_identity(identity, held.back().identity))
    return Status::kIdentityMismatch;
  if (file_size > kMaxReadableFileBytes || request.offset > file_size)
    return Status::kBoundsExceeded;
  const std::uint64_t available = file_size - request.offset;
  const std::size_t target = static_cast<std::size_t>(
      std::min<std::uint64_t>(available, request.max_bytes));
  response.text.clear();
  response.text.resize(target);
  LARGE_INTEGER position{};
  position.QuadPart = static_cast<LONGLONG>(request.offset);
  if (!SetFilePointerEx(held.back().handle.get(), position, nullptr, FILE_BEGIN))
    return Status::kIoFailed;
  std::size_t offset = 0;
  while (offset < target) {
    status = checkpoint(request.execution);
    if (status != Status::kOk) return status;
    const DWORD chunk = static_cast<DWORD>(
        std::min<std::size_t>(kReadChunkBytes, target - offset));
    DWORD transferred = 0;
    if (!ReadFile(held.back().handle.get(), response.text.data() + offset, chunk,
                  &transferred, nullptr) || transferred == 0 ||
        transferred > chunk)
      return Status::kIoFailed;
    offset += transferred;
  }
  status = checkpoint(request.execution);
  if (status != Status::kOk) return status;
  status = validate_utf8_text(response.text);
  if (status != Status::kOk) return status;
  response.receipt.bytes_returned = static_cast<std::uint32_t>(target);
  response.receipt.truncated = target < available;
  return Status::kOk;
}

bool valid_request(const Request& request) {
  if (request.abi_version != kAbiVersion ||
      !valid_grant_id(request.grant_id) ||
      request.expected_root_identity.volume_serial == 0 ||
      all_zero(request.expected_root_identity.file_id.data(),
               request.expected_root_identity.file_id.size()) ||
      request.relative_components.size() > kMaxComponents ||
      request.execution.monotonic_ms == nullptr ||
      request.execution.is_cancelled == nullptr)
    return false;
  for (const auto& component : request.relative_components)
    if (!safe_component(component)) return false;
  switch (request.operation) {
    case Operation::kStat:
    case Operation::kList:
      return request.offset == 0 && request.max_bytes == 0;
    case Operation::kRead:
      return !request.relative_components.empty() &&
             request.max_bytes > 0 && request.max_bytes <= kMaxReadBytes;
  }
  return false;
}

void initialize_receipt(const Request& request, Status status,
                        Response& response) {
  response.receipt = Receipt{};
  response.receipt.status = status_name(status);
  response.receipt.operation = operation_name(request.operation);
  if (valid_grant_id(request.grant_id)) response.receipt.grant_id = request.grant_id;
  response.receipt.component_count = static_cast<std::uint32_t>(
      std::min<std::size_t>(request.relative_components.size(), kMaxComponents));
}

}  // namespace

struct ReadLease::Impl {
  std::vector<HeldObject> held;
};

ReadLease::ReadLease() noexcept = default;
ReadLease::~ReadLease() = default;
ReadLease::ReadLease(ReadLease&&) noexcept = default;
ReadLease& ReadLease::operator=(ReadLease&&) noexcept = default;
bool ReadLease::valid() const noexcept {
  return impl_ != nullptr && !impl_->held.empty();
}
void ReadLease::reset() noexcept { impl_.reset(); }

Status execute_readonly(const Request& request, ReadLease& lease,
                        Response& response) noexcept {
  lease.reset();
  response = Response{};
  Status status = Status::kInternal;
  try {
    if (!valid_request(request)) {
      status = Status::kInvalidRequest;
      initialize_receipt(request, status, response);
      return status;
    }
    status = checkpoint(request.execution);
    if (status != Status::kOk) {
      initialize_receipt(request, status, response);
      return status;
    }
    std::wstring canonical;
    wchar_t drive_root[4]{};
    if (!canonical_root(request.absolute_grant_root, canonical, drive_root)) {
      status = Status::kUnsafeGrant;
      initialize_receipt(request, status, response);
      return status;
    }
    CurrentUser user;
    if (!current_user(user)) {
      status = Status::kSecurityUnavailable;
      initialize_receipt(request, status, response);
      return status;
    }
    auto candidate = std::make_unique<ReadLease::Impl>();
    HeldObject root;
    status = open_drive_root(request, drive_root, user.sid, root);
    if (status != Status::kOk) {
      initialize_receipt(request, status, response);
      return status;
    }
    candidate->held.push_back(std::move(root));
    status = validate_volume(candidate->held.front().handle.get(), drive_root,
                             request.expected_root_identity.volume_serial);
    if (status != Status::kOk) {
      initialize_receipt(request, status, response);
      return status;
    }
    status = open_grant_components(request, canonical, user.sid,
                                   candidate->held);
    if (status != Status::kOk) {
      initialize_receipt(request, status, response);
      return status;
    }
    status = checkpoint(request.execution);
    if (status == Status::kOk)
      status = open_request_target(request, user.sid, candidate->held);
    if (status == Status::kOk) {
      switch (request.operation) {
        case Operation::kStat:
          status = stat_target(request, user.sid, candidate->held, response);
          break;
        case Operation::kList:
          status = list_target(request, user.sid, candidate->held, response);
          break;
        case Operation::kRead:
          status = read_target(request, user.sid, candidate->held, response);
          break;
      }
    }
    if (status == Status::kOk)
      status = validate_held(candidate->held, user.sid,
                             request.expected_root_identity.volume_serial);
    if (status == Status::kOk) status = checkpoint(request.execution);
    if (status != Status::kOk) {
      response = Response{};
      initialize_receipt(request, status, response);
      return status;
    }
    const bool truncated = response.receipt.truncated;
    initialize_receipt(request, Status::kOk, response);
    response.receipt.entries_returned =
        static_cast<std::uint32_t>(response.entries.size());
    response.receipt.bytes_returned =
        static_cast<std::uint32_t>(response.text.size());
    response.receipt.truncated = truncated;
    response.receipt.root_identity_bound = true;
    response.receipt.handles_retained = true;
    lease.impl_ = std::move(candidate);
    return Status::kOk;
  } catch (const std::bad_alloc&) {
    status = Status::kBoundsExceeded;
  } catch (...) {
    status = Status::kInternal;
  }
  lease.reset();
  response = Response{};
  initialize_receipt(request, status, response);
  return status;
}

const char* status_name(Status status) noexcept {
  switch (status) {
    case Status::kOk: return "ok";
    case Status::kPlatformUnavailable: return "platform_unavailable";
    case Status::kInvalidRequest: return "invalid_request";
    case Status::kUnsafeGrant: return "unsafe_grant";
    case Status::kUnsafeComponent: return "unsafe_component";
    case Status::kUnsafeVolume: return "unsafe_volume";
    case Status::kUnsupportedFilesystem: return "unsupported_filesystem";
    case Status::kNotFound: return "not_found";
    case Status::kAccessDenied: return "access_denied";
    case Status::kSecurityUnavailable: return "security_unavailable";
    case Status::kReparseRefused: return "reparse_refused";
    case Status::kUnsupportedFileType: return "unsupported_file_type";
    case Status::kLinkCountRefused: return "link_count_refused";
    case Status::kDeletePending: return "delete_pending";
    case Status::kIdentityMismatch: return "identity_mismatch";
    case Status::kBoundsExceeded: return "bounds_exceeded";
    case Status::kUtf8Required: return "utf8_required";
    case Status::kBinaryRefused: return "binary_refused";
    case Status::kCancelled: return "cancelled";
    case Status::kDeadlineExceeded: return "deadline_exceeded";
    case Status::kIoFailed: return "io_failed";
    case Status::kInternal: return "internal";
  }
  return "internal";
}

const char* operation_name(Operation operation) noexcept {
  switch (operation) {
    case Operation::kStat: return "stat";
    case Operation::kList: return "list";
    case Operation::kRead: return "read";
  }
  return "invalid";
}

}  // namespace lae::windows_readonly_fs
