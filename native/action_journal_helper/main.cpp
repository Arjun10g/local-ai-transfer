#include "pipe_server.hpp"

// This source file is intentionally absent from native/CMakeLists.txt. A later
// reviewed build/packaging slice may register it only after Windows evidence.
int wmain(int argc, wchar_t** argv) {
  if (argc != 1 || argv == nullptr) return 64;
  const auto status =
      lae::action_journal_helper::run_foreground_helper_from_inherited_stdin();
  return status == lae::action_journal_helper::HelperStatus::kOk ? 0 : 70;
}
