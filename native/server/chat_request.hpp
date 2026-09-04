#pragma once

#include "../backend/engine_backend.hpp"

#include <string>

namespace lae {

// Strict, bounded native HTTP envelope. Parsing is intentionally independent
// of the upstream runtime and rejects unknown fields before generation.
struct ChatRequest {
  std::string model;
  std::string session_id;
  GenerationRequest generation;
  bool stream = false;
  std::string mode = "normal";
};

// error_code is one of invalid_json, invalid_request, or request_too_large.
bool parse_chat_request(const std::string& body, ChatRequest& request,
                        std::string& error_code);

}  // namespace lae
