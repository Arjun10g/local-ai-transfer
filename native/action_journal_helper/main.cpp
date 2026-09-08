#include "pipe_server.hpp"

// This source file is absent from the default/product build. The optional
// compile-check static library never creates a runnable helper or package;
// production registration still requires reviewed Windows evidence.
int wmain(int argc, wchar_t** argv) {
  if (argc != 1 || argv == nullptr) return 64;
  // No standalone owner/default storage path exists. The co-located
  // supervisor must provide a borrowed owner in a separately reviewed seam.
  return 70;
}
