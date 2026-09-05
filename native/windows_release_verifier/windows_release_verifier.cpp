#include "windows_release_verifier.hpp"

#include <aclapi.h>
#include <bcrypt.h>
#include <winioctl.h>
#include <winternl.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cwctype>
#include <limits>
#include <new>
#include <set>
#include <string_view>
#include <utility>
#include <vector>

namespace lae::windows_release_verifier {
namespace {

// These are intentionally non-overridable source constants. There is no
// environment, argument, registry, config, or test seam that can enable them.
constexpr bool kCompiledManifestIdentityTrusted = false;
constexpr bool kAuthenticodePolicyTrusted = false;
constexpr bool kCancellableNativeIoTrusted = false;

constexpr std::size_t kMaximumComponentCodeUnits = 255;
constexpr std::size_t kDirectoryBufferBytes = 65'536;
constexpr std::size_t kHashChunkBytes = 65'536;
constexpr std::size_t kMaximumDirectoryRecords = 512;
constexpr DWORD kObjectShare = FILE_SHARE_READ;  // deny WRITE and DELETE
constexpr ACCESS_MASK kDirectoryAccess =
    FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE;
constexpr ACCESS_MASK kFileAccess =
    FILE_READ_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE;
constexpr ULONG kRelativeOpenOptions =
    FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT;

constexpr char kCompiledManifestSha256[] =
    "fe9560fc1b31ccaa81e09ef33cb59a5cebde69bc0e882f05bf14a174b82168fd";

enum class SignerPolicy : std::uint8_t {
  kNotApplicable = 0,
  kOfflineAuthenticodeRequired = 1,
};

struct ManifestEntry {
  const wchar_t* path;
  std::uint64_t size;
  const char* sha256;
  SignerPolicy signer_policy;
  const char* signer_subject_sha256;
};

// This exact finite table mirrors the advisory source-skeleton names at the
// stated base. It is not a production package allowlist. Script signer
// identities are deliberately empty, so even the latent verifier cannot turn
// the source snapshot into a verified package.
constexpr std::array<ManifestEntry, 20> kCompiledManifest{{
    {L"backend-profiles.json", 2334,
     "524af868c7b8ac551d612288e4d5ada788f6cf914919794892279f80ee8782ef",
     SignerPolicy::kNotApplicable, ""},
    {L"backend-runtime.example.json", 2149,
     "f05fbb0f3a61ddfadef9e844eac4552ef2801ce713b7052095b7f4389f108109",
     SignerPolicy::kNotApplicable, ""},
    {L"Build-WindowsBackend.ps1", 882,
     "ae7d4582d8ec1338523e296e27b298e77b9ea7111dca5178ecf2ac8ab472ac01",
     SignerPolicy::kOfflineAuthenticodeRequired, ""},
    {L"CHECKSUMS.sha256", 93,
     "dfbd4ceea001296b4131c75240c4368b8af83573bccc6c8393d0b014fd9e076d",
     SignerPolicy::kNotApplicable, ""},
    {L"config.example.json", 117,
     "2e8dd408679949debfefe7353eda0b75c301ea2ca44c467a68798383486aa19b",
     SignerPolicy::kNotApplicable, ""},
    {L"host-config.example.json", 320,
     "5126af3805603c3e29f14c88c919a2cf33c4effef896f4d60d0c9d28f9f9f135",
     SignerPolicy::kNotApplicable, ""},
    {L"licenses\\README.md", 162,
     "f9c477e0d4c5b4c2d9251fe94923635825c5e0068812dbba5369e764ffb0f036",
     SignerPolicy::kNotApplicable, ""},
    {L"node-provenance.json", 495,
     "f7f36a2e17f7a59f53c3cd198a93b20fa5ea612995d2381212e02b0f6926f3da",
     SignerPolicy::kNotApplicable, ""},
    {L"README-OPERATOR.md", 1567,
     "be8f23c72c10660f8228741c00f6930f38a5ec24f9cdff40b8b974c04ea2388e",
     SignerPolicy::kNotApplicable, ""},
    {L"RELEASE_MANIFEST.json", 770,
     "a33924e2dcf83817f8340009644cb9f03600844b31b7f4622c48cd8f87d221fd",
     SignerPolicy::kNotApplicable, ""},
    {L"Run-WindowsBackend.ps1", 797,
     "457d798b2ddcbc88b894a17e8b7bc2f3b532b8b565311d3569c6c9ba25168167",
     SignerPolicy::kOfflineAuthenticodeRequired, ""},
    {L"SBOM.spdx.json", 356,
     "781416c6682c5f33693e7234463b2ffd846ba1d70b7c00c7078e46f58264c3bb",
     SignerPolicy::kNotApplicable, ""},
    {L"schemas\\README.md", 220,
     "1f5aff7285234865ac6436bcfdb8bbcac9b227874342fc2ce68ab5bb9ea0196c",
     SignerPolicy::kNotApplicable, ""},
    {L"Start-LocalAssistant.ps1", 698,
     "68575c9f68bde3f6efaa90efab91c66e36c38727c6d0dd5479a8f8430cb3193d",
     SignerPolicy::kOfflineAuthenticodeRequired, ""},
    {L"THIRD_PARTY_NOTICES.md", 529,
     "b8462b522d66fb12fbe926823e4adf85f1438e4ce1ffd46bc441daf280d8d333",
     SignerPolicy::kNotApplicable, ""},
    {L"ui\\app.css", 59,
     "2962b955b6555f16d7c88fb953cd750aa33acd50d57e528478970e3b531ad0cb",
     SignerPolicy::kNotApplicable, ""},
    {L"ui\\app.js", 124,
     "3f2ebdc105037c20d56b8851fdfb378ad8b06dd58f3352afd894835527c27c9a",
     SignerPolicy::kOfflineAuthenticodeRequired, ""},
    {L"ui\\index.html", 320,
     "5860e6e675b26da8cfece8f5c1b80c5bb73f9e2154fad493043f3b6d4ac9813f",
     SignerPolicy::kNotApplicable, ""},
    {L"Verify-Release.ps1", 425,
     "2430a092de34530ce3793e42e23898cabb99aa676dd7b668d688960d5e8a72ae",
     SignerPolicy::kOfflineAuthenticodeRequired, ""},
    {L"windows_backend_plan.py", 18016,
     "34cbc10ceb6215b8e254c6cdc5ef4b049840e9864ce8e2622d833a0ab83f3a0f",
     SignerPolicy::kOfflineAuthenticodeRequired, ""},
}};

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

struct ObjectFacts {
  ObjectIdentity identity{};
  std::uint64_t internal_file_id = 0;
  std::uint64_t size = 0;
  bool directory = false;
};

struct DirectoryEntry {
  std::wstring name;
  std::uint64_t file_id = 0;
  bool directory = false;
};

struct NativeDirectoryRecord {
  ULONG next_entry_offset;
  ULONG file_index;
  LARGE_INTEGER creation_time;
  LARGE_INTEGER last_access_time;
  LARGE_INTEGER last_write_time;
  LARGE_INTEGER change_time;
  LARGE_INTEGER end_of_file;
  LARGE_INTEGER allocation_size;
  ULONG file_attributes;
  ULONG file_name_length;
  ULONG ea_size;
  CCHAR short_name_length;
  WCHAR short_name[12];
  LARGE_INTEGER file_id;
  WCHAR file_name[1];
};

struct NativeStreamRecord {
  ULONG next_entry_offset;
  ULONG stream_name_length;
  LARGE_INTEGER stream_size;
  LARGE_INTEGER stream_allocation_size;
  WCHAR stream_name[1];
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

bool equal_ordinal(const std::wstring& left, const std::wstring& right,
                   bool ignore_case) noexcept {
  if (left.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      right.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  return CompareStringOrdinal(left.data(), static_cast<int>(left.size()),
                              right.data(), static_cast<int>(right.size()),
                              ignore_case ? TRUE : FALSE) == CSTR_EQUAL;
}

bool valid_utf16(const std::wstring& value) noexcept {
  if (value.empty() ||
      value.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  return WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
                             static_cast<int>(value.size()), nullptr, 0,
                             nullptr, nullptr) > 0;
}

bool reserved_component(const std::wstring& component) {
  std::wstring stem = component.substr(0, component.find(L'.'));
  std::transform(stem.begin(), stem.end(), stem.begin(), [](wchar_t value) {
    return static_cast<wchar_t>(towupper(value));
  });
  if (stem == L"CON" || stem == L"PRN" || stem == L"AUX" ||
      stem == L"NUL")
    return true;
  if (stem.size() == 4 && stem[3] >= L'1' && stem[3] <= L'9')
    return stem.rfind(L"COM", 0) == 0 || stem.rfind(L"LPT", 0) == 0;
  return false;
}

bool safe_component(const std::wstring& component) noexcept {
  if (component.empty() || component.size() > kMaximumComponentCodeUnits ||
      component == L"." || component == L".." || component.back() == L'.' ||
      component.back() == L' ' || component.find(L'~') != std::wstring::npos ||
      reserved_component(component) || !valid_utf16(component))
    return false;
  for (wchar_t value : component) {
    if (value < 0x20 || value == 0x7f || value == L':' || value == L'/' ||
        value == L'\\' || value == L'<' || value == L'>' || value == L'"' ||
        value == L'|' || value == L'?' || value == L'*')
      return false;
  }
  return true;
}

bool split_relative_path(const std::wstring& path,
                         std::vector<std::wstring>& components) {
  if (path.empty() || path.front() == L'\\' || path.front() == L'/' ||
      path.find(L'/') != std::wstring::npos ||
      path.find(L':') != std::wstring::npos ||
      path.find(L'\0') != std::wstring::npos)
    return false;
  std::size_t start = 0;
  while (start < path.size()) {
    const std::size_t end = path.find(L'\\', start);
    const std::wstring component = path.substr(
        start, end == std::wstring::npos ? std::wstring::npos : end - start);
    if (!safe_component(component) ||
        components.size() >= kMaximumPathComponents)
      return false;
    components.push_back(component);
    if (end == std::wstring::npos) break;
    start = end + 1;
  }
  return !components.empty();
}

bool valid_sha256_text(const char* value, bool allow_empty = false) noexcept {
  if (value == nullptr) return false;
  const std::string_view text(value);
  if (allow_empty && text.empty()) return true;
  if (text.size() != 64) return false;
  for (const unsigned char byte : text)
    if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f')))
      return false;
  return true;
}

bool hex_to_bytes(const char* text, std::uint8_t* output,
                  std::size_t output_size) noexcept {
  if (text == nullptr || output == nullptr) return false;
  const std::string_view value(text);
  if (value.size() != output_size * 2) return false;
  auto nibble = [](unsigned char byte) -> int {
    if (byte >= '0' && byte <= '9') return byte - '0';
    if (byte >= 'a' && byte <= 'f') return 10 + byte - 'a';
    return -1;
  };
  for (std::size_t index = 0; index < output_size; ++index) {
    const int high = nibble(static_cast<unsigned char>(value[index * 2]));
    const int low = nibble(static_cast<unsigned char>(value[index * 2 + 1]));
    if (high < 0 || low < 0) return false;
    output[index] = static_cast<std::uint8_t>((high << 4) | low);
  }
  return true;
}

std::string bytes_to_hex(const std::uint8_t* bytes, std::size_t count) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string output(count * 2, '0');
  for (std::size_t index = 0; index < count; ++index) {
    output[index * 2] = digits[bytes[index] >> 4];
    output[index * 2 + 1] = digits[bytes[index] & 0x0f];
  }
  return output;
}

Status checkpoint(const ExecutionContext& execution) noexcept {
  if (execution.monotonic_ms == nullptr || execution.is_cancelled == nullptr)
    return Status::kInvalidRequest;
  if (execution.is_cancelled(execution.opaque)) return Status::kCancelled;
  const std::uint64_t now = execution.monotonic_ms(execution.opaque);
  if (execution.deadline_monotonic_ms == 0 ||
      now >= execution.deadline_monotonic_ms)
    return Status::kDeadlineExceeded;
  return Status::kVerified;
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
  if (!GetTokenInformation(user.token.get(), TokenUser,
                           user.token_user.data(), size, &size))
    return false;
  user.sid = reinterpret_cast<TOKEN_USER*>(user.token_user.data())->User.Sid;
  if (user.sid == nullptr || !IsValidSid(user.sid) ||
      GetLengthSid(user.sid) > SECURITY_MAX_SID_SIZE)
    return false;
  constexpr WELL_KNOWN_SID_TYPE rejected[] = {
      WinNullSid, WinWorldSid, WinAnonymousSid, WinAuthenticatedUserSid,
      WinBuiltinUsersSid, WinBuiltinAdministratorsSid, WinLocalSystemSid,
      WinLocalServiceSid, WinNetworkServiceSid};
  for (const auto type : rejected) {
    std::array<std::uint8_t, SECURITY_MAX_SID_SIZE> buffer{};
    DWORD length = static_cast<DWORD>(buffer.size());
    if (!CreateWellKnownSid(type, nullptr, buffer.data(), &length) ||
        EqualSid(user.sid, buffer.data()))
      return false;
  }
  return true;
}

bool private_current_user_security(HANDLE handle, PSID current_sid) {
  PSID owner = nullptr;
  PACL dacl = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  const DWORD error = GetSecurityInfo(
      handle, SE_FILE_OBJECT,
      OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
      &owner, nullptr, &dacl, nullptr, &descriptor);
  if (error != ERROR_SUCCESS || descriptor == nullptr) return false;
  SECURITY_DESCRIPTOR_CONTROL control = 0;
  DWORD revision = 0;
  bool valid = owner != nullptr && IsValidSid(owner) &&
               EqualSid(owner, current_sid) && dacl != nullptr &&
               IsValidAcl(dacl) &&
               GetSecurityDescriptorControl(descriptor, &control, &revision) &&
               revision == SECURITY_DESCRIPTOR_REVISION &&
               (control & SE_DACL_PROTECTED) != 0 &&
               (control & SE_DACL_DEFAULTED) == 0 && dacl->AceCount == 1;
  if (valid) {
    void* raw_ace = nullptr;
    valid = GetAce(dacl, 0, &raw_ace) && raw_ace != nullptr;
    if (valid) {
      const auto* ace = static_cast<const ACCESS_ALLOWED_ACE*>(raw_ace);
      PSID sid = const_cast<DWORD*>(&ace->SidStart);
      valid = ace->Header.AceType == ACCESS_ALLOWED_ACE_TYPE &&
              ace->Header.AceFlags == 0 &&
              ace->Mask == FILE_ALL_ACCESS && IsValidSid(sid) &&
              EqualSid(sid, current_sid);
    }
  }
  LocalFree(descriptor);
  return valid;
}

bool object_identity(HANDLE handle, ObjectIdentity& identity) noexcept {
  FILE_ID_INFO info{};
  if (!GetFileInformationByHandleEx(handle, FileIdInfo, &info, sizeof(info)))
    return false;
  identity.volume_serial = info.VolumeSerialNumber;
  std::copy(std::begin(info.FileId.Identifier), std::end(info.FileId.Identifier),
            identity.file_id.begin());
  return identity.volume_serial != 0 &&
         !all_zero(identity.file_id.data(), identity.file_id.size());
}

Status inspect_object(HANDLE handle, std::uint64_t expected_volume,
                      PSID current_sid, bool expected_directory,
                      ObjectFacts& facts) {
  if (GetFileType(handle) != FILE_TYPE_DISK) return Status::kUnsafeObject;
  FILE_ATTRIBUTE_TAG_INFO tag{};
  FILE_STANDARD_INFO standard{};
  FILE_INTERNAL_INFO internal{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag,
                                    sizeof(tag)) ||
      !GetFileInformationByHandleEx(handle, FileStandardInfo, &standard,
                                    sizeof(standard)) ||
      !GetFileInformationByHandleEx(handle, FileInternalInfo, &internal,
                                    sizeof(internal)) ||
      !object_identity(handle, facts.identity))
    return Status::kIoFailed;
  constexpr DWORD rejected =
      FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE |
      FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_SPARSE_FILE |
      FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED |
      FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS |
      FILE_ATTRIBUTE_VIRTUAL;
  if ((tag.FileAttributes & rejected) != 0 || standard.DeletePending)
    return Status::kUnsafeObject;
  facts.directory = standard.Directory != FALSE ||
                    (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
  if (facts.directory != expected_directory ||
      standard.NumberOfLinks != 1 ||
      standard.EndOfFile.QuadPart < 0)
    return Status::kUnsafeObject;
  if (facts.identity.volume_serial != expected_volume)
    return Status::kIdentityMismatch;
  if (!private_current_user_security(handle, current_sid))
    return Status::kUnsafeSecurity;
  facts.internal_file_id = static_cast<std::uint64_t>(internal.IndexNumber.QuadPart);
  facts.size = facts.directory
                   ? 0
                   : static_cast<std::uint64_t>(standard.EndOfFile.QuadPart);
  return Status::kVerified;
}

Status map_nt_error(NTSTATUS status) noexcept {
  const ULONG error = RtlNtStatusToDosError(status);
  if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
    return Status::kInventoryMismatch;
  if (error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION)
    return Status::kUnsafeObject;
  return Status::kIoFailed;
}

Status open_relative(HANDLE parent, const std::wstring& component,
                     bool directory, UniqueHandle& output) {
  if (!safe_component(component)) return Status::kUnsafeName;
  if (component.size() >
      static_cast<std::size_t>(std::numeric_limits<USHORT>::max() /
                               sizeof(wchar_t)))
    return Status::kUnsafeName;
  UNICODE_STRING name{};
  name.Length = static_cast<USHORT>(component.size() * sizeof(wchar_t));
  name.MaximumLength = name.Length;
  name.Buffer = const_cast<PWSTR>(component.data());
  OBJECT_ATTRIBUTES attributes{};
  attributes.Length = sizeof(attributes);
  attributes.RootDirectory = parent;
  attributes.ObjectName = &name;
  attributes.Attributes = OBJ_CASE_INSENSITIVE;
  IO_STATUS_BLOCK io_status{};
  HANDLE raw = INVALID_HANDLE_VALUE;
  const ACCESS_MASK access = directory ? kDirectoryAccess : kFileAccess;
  const ULONG type = directory ? FILE_DIRECTORY_FILE : FILE_NON_DIRECTORY_FILE;
  const NTSTATUS status = NtOpenFile(&raw, access, &attributes, &io_status,
                                     kObjectShare,
                                     kRelativeOpenOptions | type);
  if (status < 0 || raw == INVALID_HANDLE_VALUE) return map_nt_error(status);
  output.reset(raw);
  return Status::kVerified;
}

Status no_alternate_streams(HANDLE handle, bool directory) {
  std::array<std::uint8_t, 4096> buffer{};
  IO_STATUS_BLOCK io_status{};
  const NTSTATUS status = NtQueryInformationFile(
      handle, &io_status, buffer.data(), static_cast<ULONG>(buffer.size()),
      FileStreamInformation);
  if (status < 0 || io_status.Information > buffer.size()) return Status::kIoFailed;
  std::size_t offset = 0;
  std::size_t records = 0;
  bool any_stream_seen = false;
  bool unnamed_data_seen = false;
  bool directory_index_seen = false;
  bool directory_bitmap_seen = false;
  while (offset < io_status.Information) {
    if (++records > 2 || io_status.Information - offset <
                             offsetof(NativeStreamRecord, stream_name))
      return Status::kUnsafeObject;
    const auto* record = reinterpret_cast<const NativeStreamRecord*>(
        buffer.data() + offset);
    const std::size_t record_bytes = record->next_entry_offset == 0
                                         ? io_status.Information - offset
                                         : record->next_entry_offset;
    if (record_bytes < offsetof(NativeStreamRecord, stream_name) ||
        record_bytes > io_status.Information - offset ||
        record->stream_name_length % sizeof(wchar_t) != 0 ||
        record->stream_name_length >
            record_bytes - offsetof(NativeStreamRecord, stream_name))
      return Status::kUnsafeObject;
    const std::wstring name(
        record->stream_name,
        record->stream_name_length / sizeof(wchar_t));
    if (name == L"::$DATA" && !unnamed_data_seen) {
      unnamed_data_seen = true;
    } else if (directory && name == L":$I30:$INDEX_ALLOCATION" &&
               !directory_index_seen) {
      directory_index_seen = true;
    } else if (directory && name == L":$I30:$BITMAP" &&
               !directory_bitmap_seen) {
      directory_bitmap_seen = true;
    } else {
      return Status::kUnsafeObject;
    }
    any_stream_seen = true;
    if (record->next_entry_offset == 0) break;
    offset += record->next_entry_offset;
  }
  return any_stream_seen ? Status::kVerified : Status::kUnsafeObject;
}

Status enumerate_directory(HANDLE directory,
                           const ExecutionContext& execution,
                           std::vector<DirectoryEntry>& entries) {
  std::array<std::uint8_t, kDirectoryBufferBytes> buffer{};
  bool restart = true;
  for (;;) {
    Status state = checkpoint(execution);
    if (state != Status::kVerified) return state;
    IO_STATUS_BLOCK io_status{};
    const NTSTATUS native = NtQueryDirectoryFile(
        directory, nullptr, nullptr, nullptr, &io_status, buffer.data(),
        static_cast<ULONG>(buffer.size()), FileIdBothDirectoryInformation,
        FALSE, nullptr, restart ? TRUE : FALSE);
    restart = false;
    state = checkpoint(execution);
    if (state != Status::kVerified) return state;
    if (native == STATUS_NO_MORE_FILES) break;
    if (native < 0 || io_status.Information == 0 ||
        io_status.Information > buffer.size())
      return Status::kIoFailed;
    std::size_t offset = 0;
    for (;;) {
      if (entries.size() >= kMaximumDirectoryRecords ||
          io_status.Information - offset <
              offsetof(NativeDirectoryRecord, file_name))
        return Status::kInventoryMismatch;
      const auto* record = reinterpret_cast<const NativeDirectoryRecord*>(
          buffer.data() + offset);
      const std::size_t record_bytes = record->next_entry_offset == 0
                                           ? io_status.Information - offset
                                           : record->next_entry_offset;
      if (record_bytes < offsetof(NativeDirectoryRecord, file_name) ||
          record_bytes > io_status.Information - offset ||
          record->file_name_length % sizeof(wchar_t) != 0 ||
          record->file_name_length >
              record_bytes - offsetof(NativeDirectoryRecord, file_name) ||
          record->short_name_length != 0)
        return Status::kUnsafeName;
      std::wstring name(record->file_name,
                        record->file_name_length / sizeof(wchar_t));
      if (name != L"." && name != L"..") {
        if (!safe_component(name)) return Status::kUnsafeName;
        entries.push_back(DirectoryEntry{
            std::move(name),
            static_cast<std::uint64_t>(record->file_id.QuadPart),
            (record->file_attributes & FILE_ATTRIBUTE_DIRECTORY) != 0});
      }
      if (record->next_entry_offset == 0) break;
      offset += record->next_entry_offset;
    }
  }
  for (std::size_t left = 0; left < entries.size(); ++left) {
    for (std::size_t right = left + 1; right < entries.size(); ++right) {
      if (equal_ordinal(entries[left].name, entries[right].name, true))
        return Status::kInventoryMismatch;
    }
  }
  return Status::kVerified;
}

std::wstring parent_path(const std::wstring& path) {
  const std::size_t separator = path.rfind(L'\\');
  return separator == std::wstring::npos ? L"" : path.substr(0, separator);
}

std::wstring base_name(const std::wstring& path) {
  const std::size_t separator = path.rfind(L'\\');
  return separator == std::wstring::npos ? path : path.substr(separator + 1);
}

std::vector<std::wstring> expected_directories() {
  std::vector<std::wstring> directories;
  for (const auto& entry : kCompiledManifest) {
    std::wstring path(entry.path);
    std::size_t offset = 0;
    while ((offset = path.find(L'\\', offset)) != std::wstring::npos) {
      const std::wstring directory = path.substr(0, offset);
      if (std::find(directories.begin(), directories.end(), directory) ==
          directories.end())
        directories.push_back(directory);
      ++offset;
    }
  }
  return directories;
}

bool expected_child(const std::wstring& directory, const DirectoryEntry& item,
                    const std::vector<std::wstring>& directories) {
  for (const auto& expected_directory : directories) {
    if (parent_path(expected_directory) == directory &&
        base_name(expected_directory) == item.name && item.directory)
      return true;
  }
  for (const auto& entry : kCompiledManifest) {
    const std::wstring path(entry.path);
    if (parent_path(path) == directory && base_name(path) == item.name &&
        !item.directory)
      return true;
  }
  return false;
}

std::size_t expected_child_count(
    const std::wstring& directory,
    const std::vector<std::wstring>& directories) {
  std::size_t count = 0;
  for (const auto& item : directories)
    if (parent_path(item) == directory) ++count;
  for (const auto& entry : kCompiledManifest)
    if (parent_path(entry.path) == directory) ++count;
  return count;
}

Status duplicate_handle(HANDLE source, UniqueHandle& output) {
  HANDLE raw = INVALID_HANDLE_VALUE;
  if (source == nullptr || source == INVALID_HANDLE_VALUE ||
      !DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(), &raw,
                       0, FALSE, DUPLICATE_SAME_ACCESS) ||
      raw == INVALID_HANDLE_VALUE)
    return Status::kRootAuthorityInvalid;
  output.reset(raw);
  return Status::kVerified;
}

Status open_path_from_root(HANDLE root, const std::wstring& path,
                           bool final_directory,
                           std::vector<UniqueHandle>& retained) {
  std::vector<std::wstring> components;
  if (!split_relative_path(path, components)) return Status::kUnsafeName;
  HANDLE parent = root;
  for (std::size_t index = 0; index < components.size(); ++index) {
    UniqueHandle next;
    const bool directory = index + 1 < components.size() || final_directory;
    const Status status = open_relative(parent, components[index], directory,
                                        next);
    if (status != Status::kVerified) return status;
    parent = next.get();
    retained.push_back(std::move(next));
  }
  return Status::kVerified;
}

Status inspect_directory_chain(const std::vector<UniqueHandle>& chain,
                               std::size_t count,
                               std::uint64_t expected_volume,
                               PSID current_sid) {
  if (count > chain.size()) return Status::kInternal;
  for (std::size_t index = 0; index < count; ++index) {
    ObjectFacts facts{};
    Status state = inspect_object(chain[index].get(), expected_volume,
                                  current_sid, true, facts);
    if (state != Status::kVerified) return state;
    state = no_alternate_streams(chain[index].get(), true);
    if (state != Status::kVerified) return state;
  }
  return Status::kVerified;
}

Status validate_local_ntfs(HANDLE root, std::uint64_t expected_volume) {
  std::array<wchar_t, 32> filesystem{};
  DWORD serial = 0;
  DWORD maximum_component = 0;
  DWORD flags = 0;
  if (!GetVolumeInformationByHandleW(
          root, nullptr, 0, &serial, &maximum_component, &flags,
          filesystem.data(), static_cast<DWORD>(filesystem.size())) ||
      serial == 0 || expected_volume == 0 ||
      serial != static_cast<DWORD>(expected_volume) ||
      CompareStringOrdinal(filesystem.data(), -1, L"NTFS", -1, TRUE) !=
          CSTR_EQUAL ||
      (flags & FILE_PERSISTENT_ACLS) == 0 ||
      (flags & (FILE_READ_ONLY_VOLUME | FILE_VOLUME_IS_COMPRESSED)) != 0)
    return Status::kUnsafeVolume;

  std::array<wchar_t, 32'768> final_path{};
  const DWORD count = GetFinalPathNameByHandleW(
      root, final_path.data(), static_cast<DWORD>(final_path.size()),
      FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (count < 7 || count >= final_path.size() ||
      std::wstring_view(final_path.data(), 4) != L"\\\\?\\" ||
      !iswalpha(final_path[4]) || final_path[5] != L':' ||
      final_path[6] != L'\\')
    return Status::kUnsafeVolume;
  wchar_t drive_root[4] = {final_path[4], L':', L'\\', L'\0'};
  if (GetDriveTypeW(drive_root) != DRIVE_FIXED) return Status::kUnsafeVolume;
  std::wstring volume_device = L"\\\\.\\";
  volume_device.push_back(final_path[4]);
  volume_device.push_back(L':');
  UniqueHandle volume(CreateFileW(
      volume_device.c_str(), 0,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
      OPEN_EXISTING, 0, nullptr));
  if (!volume) return Status::kUnsafeVolume;
  STORAGE_HOTPLUG_INFO hotplug{};
  hotplug.Size = sizeof(hotplug);
  DWORD returned = 0;
  if (!DeviceIoControl(volume.get(), IOCTL_STORAGE_GET_HOTPLUG_INFO, nullptr, 0,
                       &hotplug, sizeof(hotplug), &returned, nullptr) ||
      returned < sizeof(hotplug) || hotplug.MediaRemovable ||
      hotplug.MediaHotplug || hotplug.DeviceHotplug)
    return Status::kUnsafeVolume;
  return Status::kVerified;
}

Status sha256_handle(HANDLE handle, std::uint64_t expected_size,
                     const ExecutionContext& execution,
                     std::array<std::uint8_t, 32>& digest) {
  if (expected_size > kMaximumFileBytes) return Status::kSizeMismatch;
  LARGE_INTEGER zero{};
  if (!SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN))
    return Status::kIoFailed;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD digest_size = 0;
  DWORD returned = 0;
  if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
          &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(
          algorithm, BCRYPT_OBJECT_LENGTH,
          reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
          &returned, 0)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(
          algorithm, BCRYPT_HASH_LENGTH,
          reinterpret_cast<PUCHAR>(&digest_size), sizeof(digest_size),
          &returned, 0)) ||
      object_size == 0 || object_size > 65'536 || digest_size != digest.size()) {
    if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
    return Status::kIoFailed;
  }
  std::vector<std::uint8_t> object(object_size);
  if (!BCRYPT_SUCCESS(BCryptCreateHash(
          algorithm, &hash, object.data(), object_size, nullptr, 0, 0))) {
    BCryptCloseAlgorithmProvider(algorithm, 0);
    return Status::kIoFailed;
  }
  std::array<std::uint8_t, kHashChunkBytes> buffer{};
  std::uint64_t total = 0;
  Status result = Status::kVerified;
  while (total < expected_size) {
    result = checkpoint(execution);
    if (result != Status::kVerified) break;
    const DWORD requested = static_cast<DWORD>(std::min<std::uint64_t>(
        buffer.size(), expected_size - total));
    DWORD count = 0;
    if (!ReadFile(handle, buffer.data(), requested, &count, nullptr) ||
        count == 0 || count > requested || total + count > expected_size ||
        !BCRYPT_SUCCESS(BCryptHashData(hash, buffer.data(), count, 0))) {
      result = Status::kIoFailed;
      break;
    }
    total += count;
    result = checkpoint(execution);
    if (result != Status::kVerified) break;
  }
  std::uint8_t extra = 0;
  DWORD extra_count = 0;
  if (result == Status::kVerified &&
      (!ReadFile(handle, &extra, 1, &extra_count, nullptr) || extra_count != 0))
    result = Status::kSizeMismatch;
  if (result == Status::kVerified &&
      !BCRYPT_SUCCESS(BCryptFinishHash(hash, digest.data(),
                                       static_cast<ULONG>(digest.size()), 0)))
    result = Status::kIoFailed;
  BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  SecureZeroMemory(object.data(), object.size());
  SecureZeroMemory(buffer.data(), buffer.size());
  return result;
}

bool valid_manifest_table() {
  if (kCompiledManifest.empty() ||
      kCompiledManifest.size() > kMaximumManifestEntries ||
      !valid_sha256_text(kCompiledManifestSha256))
    return false;
  std::uint64_t total = 0;
  std::wstring previous;
  for (const auto& entry : kCompiledManifest) {
    std::vector<std::wstring> components;
    const std::wstring path(entry.path == nullptr ? L"" : entry.path);
    if (!split_relative_path(path, components) || entry.size > kMaximumFileBytes ||
        entry.size > kMaximumTreeBytes - total ||
        !valid_sha256_text(entry.sha256) ||
        path.size() >= 5 &&
            equal_ordinal(path.substr(path.size() - 5), L".gguf", true))
      return false;
    if (!previous.empty()) {
      const int order = CompareStringOrdinal(
          previous.data(), static_cast<int>(previous.size()), path.data(),
          static_cast<int>(path.size()), TRUE);
      if (order != CSTR_LESS_THAN) return false;
    }
    previous = path;
    total += entry.size;
    const std::wstring extension =
        path.substr(path.rfind(L'.') == std::wstring::npos
                        ? path.size()
                        : path.rfind(L'.'));
    const bool signed_type = equal_ordinal(extension, L".exe", true) ||
                             equal_ordinal(extension, L".dll", true) ||
                             equal_ordinal(extension, L".ps1", true) ||
                             equal_ordinal(extension, L".py", true) ||
                             equal_ordinal(extension, L".js", true) ||
                             equal_ordinal(extension, L".mjs", true);
    if (signed_type !=
        (entry.signer_policy == SignerPolicy::kOfflineAuthenticodeRequired))
      return false;
    if (entry.signer_policy == SignerPolicy::kNotApplicable &&
        !valid_sha256_text(entry.signer_subject_sha256, true))
      return false;
  }
  return true;
}

std::string canonical_receipt(const Receipt& receipt) {
  return std::string("{\"schema\":\"local_bmo.windows-release-verification-receipt.v0.1.0\",") +
         "\"status\":\"" + receipt.status + "\",\"reason\":\"" +
         receipt.reason + "\",\"manifest_sha256\":\"" +
         receipt.manifest_sha256 + "\",\"verified_files\":" +
         std::to_string(receipt.verified_files) +
         ",\"verified_directories\":" +
         std::to_string(receipt.verified_directories) +
         ",\"verified_bytes\":" + std::to_string(receipt.verified_bytes) +
         ",\"root_identity_retained\":" +
         (receipt.root_identity_retained ? "true" : "false") +
         ",\"object_identities_retained\":" +
         (receipt.object_identities_retained ? "true" : "false") +
         ",\"signatures_verified\":" +
         (receipt.signatures_verified ? "true" : "false") +
         ",\"model_external\":true,\"activated\":false}";
}

void set_receipt(VerificationResponse* response, Status status,
                 const Receipt* partial = nullptr) noexcept {
  if (response == nullptr) return;
  try {
    response->receipt = partial == nullptr ? Receipt{} : *partial;
    response->receipt.status =
        status == Status::kVerified ? "VERIFIED"
                                    : status == Status::kNotActivated
                                          ? "REFUSED_NOT_ACTIVATED"
                                          : "FAILED";
    response->receipt.reason =
        status == Status::kVerified ? "none" : status_name(status);
    response->receipt.manifest_sha256 = kCompiledManifestSha256;
    response->receipt.model_external = true;
    response->receipt.activated = false;
    response->canonical_receipt_json = canonical_receipt(response->receipt);
  } catch (...) {
    response->receipt = Receipt{};
    response->canonical_receipt_json.clear();
  }
}

}  // namespace

class VerifierCore final {
 public:
  static Status run(const VerificationRequest& request,
                    const ReleaseRootCapability& capability,
                    Receipt& receipt) noexcept {
    try {
      Status state = checkpoint(request.execution);
      if (state != Status::kVerified) return state;
      if (!valid_manifest_table() || !capability.valid())
        return Status::kRootAuthorityInvalid;
      std::array<std::uint8_t, 32> compiled_digest{};
      if (!hex_to_bytes(kCompiledManifestSha256, compiled_digest.data(),
                        compiled_digest.size()) ||
          !same_bytes(compiled_digest.data(), capability.manifest_sha256_.data(),
                      compiled_digest.size()))
        return Status::kManifestIdentityMismatch;

      UniqueHandle root;
      state = duplicate_handle(capability.retained_root_, root);
      if (state != Status::kVerified) return state;
      CurrentUser user;
      if (!current_user(user)) return Status::kUnsafeSecurity;
      ObjectFacts root_before{};
      state = inspect_object(root.get(), capability.root_identity_.volume_serial,
                             user.sid, true, root_before);
      if (state != Status::kVerified ||
          !same_identity(root_before.identity, capability.root_identity_))
        return state == Status::kVerified ? Status::kIdentityMismatch : state;
      state = no_alternate_streams(root.get(), true);
      if (state != Status::kVerified) return state;
      state = validate_local_ntfs(root.get(), root_before.identity.volume_serial);
      if (state != Status::kVerified) return state;
      receipt.root_identity_retained = true;

      const std::vector<std::wstring> directories = expected_directories();
      std::vector<std::pair<std::wstring, std::vector<DirectoryEntry>>> first;
      std::vector<std::pair<std::wstring, ObjectIdentity>> directory_identities;
      std::vector<std::wstring> scan_paths{L""};
      scan_paths.insert(scan_paths.end(), directories.begin(), directories.end());
      for (const auto& directory_path : scan_paths) {
        std::vector<UniqueHandle> chain;
        HANDLE directory_handle = root.get();
        if (!directory_path.empty()) {
          state = open_path_from_root(root.get(), directory_path, true, chain);
          if (state != Status::kVerified) return state;
          state = inspect_directory_chain(chain, chain.size(),
                                          root_before.identity.volume_serial,
                                          user.sid);
          if (state != Status::kVerified) return state;
          directory_handle = chain.back().get();
          ObjectFacts directory_facts{};
          state = inspect_object(directory_handle,
                                 root_before.identity.volume_serial, user.sid,
                                 true, directory_facts);
          if (state != Status::kVerified) return state;
          const std::wstring parent = parent_path(directory_path);
          const auto parent_snapshot = std::find_if(
              first.begin(), first.end(),
              [&](const auto& item) { return item.first == parent; });
          if (parent_snapshot == first.end()) return Status::kInternal;
          const auto enumerated = std::find_if(
              parent_snapshot->second.begin(), parent_snapshot->second.end(),
              [&](const DirectoryEntry& item) {
                return item.name == base_name(directory_path) && item.directory;
              });
          if (enumerated == parent_snapshot->second.end() ||
              enumerated->file_id != directory_facts.internal_file_id)
            return Status::kIdentityMismatch;
          directory_identities.emplace_back(directory_path,
                                            directory_facts.identity);
        }
        std::vector<DirectoryEntry> entries;
        state = enumerate_directory(directory_handle, request.execution, entries);
        if (state != Status::kVerified ||
            entries.size() != expected_child_count(directory_path, directories))
          return state == Status::kVerified ? Status::kInventoryMismatch : state;
        for (const auto& item : entries)
          if (!expected_child(directory_path, item, directories))
            return Status::kInventoryMismatch;
        first.emplace_back(directory_path, std::move(entries));
      }
      receipt.verified_directories =
          static_cast<std::uint32_t>(directories.size());

      std::set<std::array<std::uint8_t, 16>> retained_file_ids;
      for (const auto& entry : kCompiledManifest) {
        state = checkpoint(request.execution);
        if (state != Status::kVerified) return state;
        std::vector<UniqueHandle> chain;
        state = open_path_from_root(root.get(), entry.path, false, chain);
        if (state != Status::kVerified || chain.empty()) return state;
        state = inspect_directory_chain(chain, chain.size() - 1,
                                        root_before.identity.volume_serial,
                                        user.sid);
        if (state != Status::kVerified) return state;
        HANDLE file = chain.back().get();
        ObjectFacts before{};
        state = inspect_object(file, root_before.identity.volume_serial, user.sid,
                               false, before);
        if (state != Status::kVerified) return state;
        const std::wstring file_parent = parent_path(entry.path);
        const auto parent_snapshot = std::find_if(
            first.begin(), first.end(),
            [&](const auto& item) { return item.first == file_parent; });
        if (parent_snapshot == first.end()) return Status::kInternal;
        const auto enumerated = std::find_if(
            parent_snapshot->second.begin(), parent_snapshot->second.end(),
            [&](const DirectoryEntry& item) {
              return item.name == base_name(entry.path) && !item.directory;
            });
        if (enumerated == parent_snapshot->second.end() ||
            enumerated->file_id != before.internal_file_id)
          return Status::kIdentityMismatch;
        if (!retained_file_ids.insert(before.identity.file_id).second)
          return Status::kIdentityMismatch;
        if (before.size != entry.size) return Status::kSizeMismatch;
        state = no_alternate_streams(file, false);
        if (state != Status::kVerified) return state;
        std::array<std::uint8_t, 32> actual_digest{};
        state = sha256_handle(file, entry.size, request.execution, actual_digest);
        if (state != Status::kVerified ||
            bytes_to_hex(actual_digest.data(), actual_digest.size()) !=
                entry.sha256)
          return state == Status::kVerified ? Status::kHashMismatch : state;
        if (entry.signer_policy == SignerPolicy::kOfflineAuthenticodeRequired) {
          std::array<std::uint8_t, 32> signer_digest{};
          if (!valid_sha256_text(entry.signer_subject_sha256) ||
              !hex_to_bytes(entry.signer_subject_sha256, signer_digest.data(),
                            signer_digest.size()) ||
              capability.authenticode_verifier_ == nullptr)
            return Status::kSignatureUntrusted;
          state = capability.authenticode_verifier_->verify_retained_handle(
              file, before.identity, signer_digest, request.execution);
          SecureZeroMemory(signer_digest.data(), signer_digest.size());
          if (state != Status::kVerified) return state;
        }
        ObjectFacts after{};
        state = inspect_object(file, root_before.identity.volume_serial, user.sid,
                               false, after);
        if (state != Status::kVerified ||
            !same_identity(before.identity, after.identity) ||
            before.internal_file_id != after.internal_file_id ||
            before.size != after.size)
          return state == Status::kVerified ? Status::kIdentityMismatch : state;
        ++receipt.verified_files;
        receipt.verified_bytes += entry.size;
      }

      // A second full enumeration closes name/identity changes between the
      // first inventory snapshot and the per-file retained-handle checks.
      for (const auto& snapshot : first) {
        std::vector<UniqueHandle> chain;
        HANDLE directory_handle = root.get();
        if (!snapshot.first.empty()) {
          state = open_path_from_root(root.get(), snapshot.first, true, chain);
          if (state != Status::kVerified) return state;
          state = inspect_directory_chain(chain, chain.size(),
                                          root_before.identity.volume_serial,
                                          user.sid);
          if (state != Status::kVerified) return state;
          directory_handle = chain.back().get();
          ObjectFacts directory_after{};
          state = inspect_object(directory_handle,
                                 root_before.identity.volume_serial, user.sid,
                                 true, directory_after);
          if (state != Status::kVerified) return state;
          const auto prior = std::find_if(
              directory_identities.begin(), directory_identities.end(),
              [&](const auto& item) { return item.first == snapshot.first; });
          if (prior == directory_identities.end() ||
              !same_identity(prior->second, directory_after.identity))
            return Status::kIdentityMismatch;
        }
        std::vector<DirectoryEntry> second;
        state = enumerate_directory(directory_handle, request.execution, second);
        if (state != Status::kVerified || second.size() != snapshot.second.size())
          return state == Status::kVerified ? Status::kInventoryMismatch : state;
        for (const auto& expected : snapshot.second) {
          const auto found = std::find_if(
              second.begin(), second.end(), [&](const DirectoryEntry& item) {
                return item.name == expected.name && item.directory == expected.directory &&
                       item.file_id == expected.file_id;
              });
          if (found == second.end()) return Status::kIdentityMismatch;
        }
      }
      ObjectFacts root_after{};
      state = inspect_object(root.get(), root_before.identity.volume_serial,
                             user.sid, true, root_after);
      if (state != Status::kVerified ||
          !same_identity(root_before.identity, root_after.identity) ||
          root_before.internal_file_id != root_after.internal_file_id)
        return state == Status::kVerified ? Status::kIdentityMismatch : state;
      receipt.object_identities_retained = true;
      receipt.signatures_verified = true;
      return checkpoint(request.execution);
    } catch (...) {
      return Status::kInternal;
    }
  }
};

ReleaseRootCapability::~ReleaseRootCapability() {
  if (retained_root_ != nullptr && retained_root_ != INVALID_HANDLE_VALUE)
    CloseHandle(retained_root_);
  retained_root_ = INVALID_HANDLE_VALUE;
  SecureZeroMemory(manifest_sha256_.data(), manifest_sha256_.size());
  SecureZeroMemory(issuer_binding_.data(), issuer_binding_.size());
}

ReleaseRootCapability::ReleaseRootCapability(
    ReleaseRootCapability&& other) noexcept
    : retained_root_(other.retained_root_),
      root_identity_(other.root_identity_),
      manifest_sha256_(other.manifest_sha256_),
      issuer_binding_(other.issuer_binding_),
      authenticated_supervisor_issued_(other.authenticated_supervisor_issued_),
      root_opened_without_write_or_delete_share_(
          other.root_opened_without_write_or_delete_share_),
      authenticode_verifier_(other.authenticode_verifier_) {
  other.retained_root_ = INVALID_HANDLE_VALUE;
  other.authenticated_supervisor_issued_ = false;
  other.root_opened_without_write_or_delete_share_ = false;
  other.authenticode_verifier_ = nullptr;
  SecureZeroMemory(other.manifest_sha256_.data(),
                   other.manifest_sha256_.size());
  SecureZeroMemory(other.issuer_binding_.data(), other.issuer_binding_.size());
}

ReleaseRootCapability& ReleaseRootCapability::operator=(
    ReleaseRootCapability&& other) noexcept {
  if (this != &other) {
    this->~ReleaseRootCapability();
    new (this) ReleaseRootCapability(std::move(other));
  }
  return *this;
}

bool ReleaseRootCapability::valid() const noexcept {
  return retained_root_ != nullptr && retained_root_ != INVALID_HANDLE_VALUE &&
         authenticated_supervisor_issued_ &&
         root_opened_without_write_or_delete_share_ &&
         root_identity_.volume_serial != 0 &&
         !all_zero(root_identity_.file_id.data(), root_identity_.file_id.size()) &&
         !all_zero(manifest_sha256_.data(), manifest_sha256_.size()) &&
         !all_zero(issuer_binding_.data(), issuer_binding_.size()) &&
         authenticode_verifier_ != nullptr;
}

Status verify_release_tree(const VerificationRequest* request,
                           VerificationResponse* response) noexcept {
  // This ordering is the security boundary: do not validate or dereference the
  // request and do not inspect a handle/path/file while any gate is false.
  if (!kCompiledManifestIdentityTrusted || !kAuthenticodePolicyTrusted ||
      !kCancellableNativeIoTrusted) {
    set_receipt(response, Status::kNotActivated);
    return Status::kNotActivated;
  }
  if (request == nullptr || response == nullptr ||
      request->abi_version != kAbiVersion ||
      request->root_capability == nullptr) {
    set_receipt(response, Status::kInvalidRequest);
    return Status::kInvalidRequest;
  }
  Receipt receipt{};
  const Status status =
      VerifierCore::run(*request, *request->root_capability, receipt);
  set_receipt(response, status, &receipt);
  return status;
}

const char* status_name(Status status) noexcept {
  switch (status) {
    case Status::kVerified:
      return "none";
    case Status::kNotActivated:
      return "trust_anchor_unavailable";
    case Status::kInvalidRequest:
      return "invalid_request";
    case Status::kCancelled:
      return "cancelled";
    case Status::kDeadlineExceeded:
      return "deadline_exceeded";
    case Status::kManifestIdentityMismatch:
      return "manifest_identity_mismatch";
    case Status::kRootAuthorityInvalid:
      return "root_authority_invalid";
    case Status::kUnsafeVolume:
      return "unsafe_volume";
    case Status::kUnsafeSecurity:
      return "unsafe_security";
    case Status::kUnsafeName:
      return "unsafe_name";
    case Status::kInventoryMismatch:
      return "inventory_mismatch";
    case Status::kIdentityMismatch:
      return "identity_mismatch";
    case Status::kUnsafeObject:
      return "unsafe_object";
    case Status::kSizeMismatch:
      return "size_mismatch";
    case Status::kHashMismatch:
      return "hash_mismatch";
    case Status::kSignatureUntrusted:
      return "signature_untrusted";
    case Status::kIoFailed:
      return "io_failed";
    case Status::kInternal:
      return "io_failed";
  }
  return "io_failed";
}

}  // namespace lae::windows_release_verifier
