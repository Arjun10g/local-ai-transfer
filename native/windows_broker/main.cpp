#include "broker.hpp"
#include "protocol.hpp"
#include "win32_identity.hpp"

#include <fcntl.h>
#include <io.h>
#include <cstdio>
#include <utility>

namespace {

void startup_failure(HANDLE output, const char* error_code) {
  lae::windows_broker::Response response{
      "broker_startup_0001", "failed", error_code, nlohmann::json::object(),
      std::nullopt};
  lae::windows_broker::write_response_frame(output, response);
}

}  // namespace

int wmain(int argc, wchar_t**) {
  // There is intentionally no CLI surface. Configuration comes only from the
  // fixed adjacent manifest authenticated by the compiled trust anchor.
  if (argc != 1) return 64;
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
  HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
  HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
  if (input == INVALID_HANDLE_VALUE || output == INVALID_HANDLE_VALUE ||
      input == nullptr || output == nullptr) return 65;

  lae::windows_broker::ManifestLease manifest;
  std::string error_code;
  if (!lae::windows_broker::load_compiled_manifest(manifest, error_code)) {
    startup_failure(output, error_code.c_str());
    return 66;
  }
  lae::windows_broker::Broker broker(std::move(manifest));
  return broker.serve(input, output);
}
