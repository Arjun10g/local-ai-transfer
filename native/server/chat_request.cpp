#include "chat_request.hpp"

#include <cctype>
#include <cstdint>
#include <limits>
#include <map>
#include <stdexcept>
#include <vector>

namespace lae {
namespace {

constexpr size_t kMaxString = 32768;
constexpr size_t kMaxMessages = 64;
constexpr size_t kMaxMessageFields = 8;

struct JsonValue {
  enum class Type { Null, Bool, Number, String, Array, Object } type = Type::Null;
  bool boolean = false;
  uint64_t number = 0;
  std::string string;
  std::vector<JsonValue> array;
  std::map<std::string, JsonValue> object;
};

struct ParseFailure : std::runtime_error {
  explicit ParseFailure(const char* message, bool syntax_error = true)
      : std::runtime_error(message), syntax_error(syntax_error) {}
  bool syntax_error;
};

class Parser {
 public:
  explicit Parser(const std::string& input) : input_(input) {}

  JsonValue parse() {
    JsonValue value = value_at(0);
    whitespace();
    if (position_ != input_.size()) throw ParseFailure("trailing JSON data");
    return value;
  }

 private:
  void whitespace() {
    while (position_ < input_.size() && std::isspace(static_cast<unsigned char>(input_[position_]))) ++position_;
  }

  bool consume(char expected) {
    whitespace();
    if (position_ >= input_.size() || input_[position_] != expected) return false;
    ++position_;
    return true;
  }

  JsonValue value_at(size_t depth) {
    if (depth > 16) throw ParseFailure("JSON nesting limit exceeded");
    whitespace();
    if (position_ >= input_.size()) throw ParseFailure("truncated JSON");
    switch (input_[position_]) {
      case '{': return object_at(depth + 1);
      case '[': return array_at(depth + 1);
      case '"': { JsonValue value; value.type = JsonValue::Type::String; value.string = string_at(); return value; }
      case 't': literal("true"); { JsonValue value; value.type = JsonValue::Type::Bool; value.boolean = true; return value; }
      case 'f': literal("false"); { JsonValue value; value.type = JsonValue::Type::Bool; value.boolean = false; return value; }
      case 'n': literal("null"); return JsonValue{};
      default: return number_at();
    }
  }

  void literal(const char* literal_text) {
    for (const char* cursor = literal_text; *cursor; ++cursor) {
      if (position_ >= input_.size() || input_[position_++] != *cursor) throw ParseFailure("invalid JSON literal");
    }
  }

  static void append_codepoint(std::string& output, uint32_t codepoint) {
    if (codepoint >= 0xd800 && codepoint <= 0xdfff || codepoint > 0x10ffff) throw ParseFailure("invalid Unicode escape");
    if (codepoint < 0x80) output.push_back(static_cast<char>(codepoint));
    else if (codepoint < 0x800) { output.push_back(static_cast<char>(0xc0 | (codepoint >> 6))); output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f))); }
    else if (codepoint < 0x10000) { output.push_back(static_cast<char>(0xe0 | (codepoint >> 12))); output.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f))); output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f))); }
    else { output.push_back(static_cast<char>(0xf0 | (codepoint >> 18))); output.push_back(static_cast<char>(0x80 | ((codepoint >> 12) & 0x3f))); output.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f))); output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f))); }
  }

  std::string string_at() {
    if (!consume('"')) throw ParseFailure("expected JSON string");
    std::string output;
    while (position_ < input_.size()) {
      const unsigned char character = static_cast<unsigned char>(input_[position_++]);
      if (character == '"') return output;
      if (character < 0x20) throw ParseFailure("control character in JSON string");
      if (character != '\\') {
        output.push_back(static_cast<char>(character));
      } else {
        if (position_ >= input_.size()) throw ParseFailure("truncated JSON escape");
        const char escaped = input_[position_++];
        switch (escaped) {
          case '"': output.push_back('"'); break; case '\\': output.push_back('\\'); break;
          case '/': output.push_back('/'); break; case 'b': output.push_back('\b'); break;
          case 'f': output.push_back('\f'); break; case 'n': output.push_back('\n'); break;
          case 'r': output.push_back('\r'); break; case 't': output.push_back('\t'); break;
          case 'u': {
            if (position_ + 4 > input_.size()) throw ParseFailure("truncated Unicode escape");
            uint32_t codepoint = 0;
            for (unsigned i = 0; i < 4; ++i) {
              const char digit = input_[position_++];
              codepoint <<= 4;
              if (digit >= '0' && digit <= '9') codepoint += static_cast<unsigned>(digit - '0');
              else if (digit >= 'a' && digit <= 'f') codepoint += static_cast<unsigned>(digit - 'a' + 10);
              else if (digit >= 'A' && digit <= 'F') codepoint += static_cast<unsigned>(digit - 'A' + 10);
              else throw ParseFailure("invalid Unicode escape");
            }
            append_codepoint(output, codepoint);
            break;
          }
          default: throw ParseFailure("invalid JSON escape");
        }
      }
      if (output.size() > kMaxString) throw ParseFailure("JSON string too large");
    }
    throw ParseFailure("unterminated JSON string");
  }

  JsonValue object_at(size_t depth) {
    JsonValue value; value.type = JsonValue::Type::Object;
    if (!consume('{')) throw ParseFailure("expected object");
    whitespace();
    if (consume('}')) return value;
    while (true) {
      whitespace();
      if (position_ >= input_.size() || input_[position_] != '"') throw ParseFailure("object key must be a string");
      const std::string key = string_at();
      if (value.object.find(key) != value.object.end()) throw ParseFailure("duplicate object field", false);
      if (value.object.size() >= kMaxMessageFields) throw ParseFailure("object field limit exceeded", false);
      if (!consume(':')) throw ParseFailure("object key missing colon");
      auto inserted = value.object.emplace(key, value_at(depth));
      (void)inserted;
      whitespace();
      if (consume('}')) return value;
      if (!consume(',')) throw ParseFailure("object missing comma");
    }
  }

  JsonValue array_at(size_t depth) {
    JsonValue value; value.type = JsonValue::Type::Array;
    if (!consume('[')) throw ParseFailure("expected array");
    whitespace();
    if (consume(']')) return value;
    while (true) {
      if (value.array.size() >= kMaxMessages) throw ParseFailure("array too large");
      value.array.push_back(value_at(depth));
      whitespace();
      if (consume(']')) return value;
      if (!consume(',')) throw ParseFailure("array missing comma");
    }
  }

  JsonValue number_at() {
    whitespace();
    if (position_ >= input_.size() || input_[position_] < '0' || input_[position_] > '9') throw ParseFailure("invalid JSON value");
    number_start_ = position_;
    if (input_[position_] == '0') ++position_;
    else while (position_ < input_.size() && std::isdigit(static_cast<unsigned char>(input_[position_]))) ++position_;
    if (position_ < input_.size() && (input_[position_] == '.' || input_[position_] == 'e' || input_[position_] == 'E')) throw ParseFailure("only integer JSON numbers are accepted");
    JsonValue value; value.type = JsonValue::Type::Number;
    try {
      const size_t start = number_start_;
      value.number = std::stoull(input_.substr(start, position_ - start));
    } catch (...) { throw ParseFailure("JSON number out of range"); }
    return value;
  }

  const std::string& input_;
  size_t position_ = 0;
  size_t number_start_ = 0;
};

bool is_type(const JsonValue* value, JsonValue::Type type) { return value && value->type == type; }
const JsonValue* field(const JsonValue& object, const char* name) {
  const auto it = object.object.find(name);
  return it == object.object.end() ? nullptr : &it->second;
}
bool valid_text(const JsonValue* value, size_t max, std::string& output, bool allow_empty = true) {
  if (!is_type(value, JsonValue::Type::String) || (!allow_empty && value->string.empty()) || value->string.size() > max) return false;
  output = value->string;
  return true;
}

}  // namespace

bool parse_chat_request(const std::string& body, ChatRequest& request, std::string& error_code) {
  error_code.clear();
  try {
    Parser parser(body);
    const JsonValue root = parser.parse();
    if (root.type != JsonValue::Type::Object || root.object.size() > 8) throw ParseFailure("request must be an object", false);
    for (const auto& item : root.object) {
      if (item.first != "model" && item.first != "session_id" && item.first != "messages" && item.first != "stream" && item.first != "max_tokens" && item.first != "mode") throw ParseFailure("unknown request field", false);
    }
    if (!valid_text(field(root, "model"), 128, request.model, false)) throw ParseFailure("model is required", false);
    if (const JsonValue* session = field(root, "session_id")) if (!valid_text(session, 128, request.session_id)) throw ParseFailure("invalid session_id", false);
    const JsonValue* messages = field(root, "messages");
    if (!is_type(messages, JsonValue::Type::Array) || messages->array.empty() || messages->array.size() > kMaxMessages) throw ParseFailure("messages are required and bounded", false);
    request.generation.messages.clear();
    size_t total_content = 0;
    bool has_user_message = false;
    for (size_t message_index = 0; message_index < messages->array.size(); ++message_index) {
      const auto& message = messages->array[message_index];
      if (message.type != JsonValue::Type::Object || message.object.size() > kMaxMessageFields) throw ParseFailure("invalid message", false);
      for (const auto& item : message.object) if (item.first != "role" && item.first != "content" && item.first != "name" && item.first != "tool_call_id") throw ParseFailure("unknown message field", false);
      GenerationRequest::ChatMessage parsed;
      if (!valid_text(field(message, "role"), 16, parsed.role, false) || (parsed.role != "system" && parsed.role != "user" && parsed.role != "assistant" && parsed.role != "tool")) throw ParseFailure("unsupported message role", false);
      if (parsed.role == "system" && message_index != 0) throw ParseFailure("system message must be first", false);
      if (parsed.role == "user") has_user_message = true;
      if (!valid_text(field(message, "content"), kMaxString, parsed.content)) throw ParseFailure("message content is required and bounded", false);
      if (const JsonValue* name = field(message, "name")) if (!valid_text(name, 128, parsed.name, false)) throw ParseFailure("invalid message name", false);
      if (const JsonValue* id = field(message, "tool_call_id")) if (!valid_text(id, 128, parsed.tool_call_id, false)) throw ParseFailure("invalid tool_call_id", false);
      if (parsed.role == "tool" && parsed.name.empty()) throw ParseFailure("tool message name is required", false);
      total_content += parsed.content.size();
      if (total_content > 49152) throw ParseFailure("message history too large");
      request.generation.messages.push_back(std::move(parsed));
    }
    if (!has_user_message) throw ParseFailure("at least one user message is required", false);
    if (const JsonValue* stream = field(root, "stream")) {
      if (!is_type(stream, JsonValue::Type::Bool)) throw ParseFailure("stream must be boolean", false);
      request.stream = stream->boolean;
    } else request.stream = false;
    if (const JsonValue* max_tokens = field(root, "max_tokens")) {
      if (!is_type(max_tokens, JsonValue::Type::Number) || max_tokens->number < 1 || max_tokens->number > 64) throw ParseFailure("max_tokens out of range", false);
      request.generation.max_tokens = static_cast<unsigned>(max_tokens->number);
    } else request.generation.max_tokens = 8;
    if (const JsonValue* mode = field(root, "mode")) {
      if (!valid_text(mode, 16, request.mode, false) || (request.mode != "normal" && request.mode != "deep")) throw ParseFailure("invalid mode", false);
    } else request.mode = "normal";
    request.generation.enable_thinking = request.mode == "deep";
    return true;
  } catch (const ParseFailure& failure) {
    const std::string message = failure.what();
    if (message.find("too large") != std::string::npos) error_code = "request_too_large";
    else error_code = failure.syntax_error ? "invalid_json" : "invalid_request";
    return false;
  } catch (...) {
    error_code = "invalid_json";
    return false;
  }
}

}  // namespace lae
