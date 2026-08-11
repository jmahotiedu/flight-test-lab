#include "dut/protocol.hpp"

namespace dut {
namespace {

// Copies the request's sequence value into the response.  An absent key and an
// explicit null are the same case here, exactly as in Python where
// message.get("sequence") returns None for both.
Value sequence_of(const Value& request) {
  const Value* sequence = request.find("sequence");
  return sequence != nullptr ? *sequence : Value(nullptr);
}

}  // namespace

Value build_response(const Value& request) {
  if (!request.is_object()) {
    return Object{{"status", Value("error")},
                  {"error_code", Value("INVALID_MESSAGE_TYPE")},
                  {"message", Value("Request must be a JSON object")}};
  }

  const Value* command = request.find("command");
  if (command == nullptr || command->is_null()) {
    return Object{{"status", Value("error")},
                  {"error_code", Value("MISSING_COMMAND")},
                  {"sequence", sequence_of(request)}};
  }

  if (command->is_string() && command->as_string() == "status") {
    return Object{{"status", Value("ok")},
                  {"state", Value("READY")},
                  {"sequence", sequence_of(request)}};
  }

  return Object{{"status", Value("error")},
                {"error_code", Value("UNSUPPORTED_COMMAND")},
                {"command", *command},
                {"sequence", sequence_of(request)}};
}

Value handle_request_line(const std::string& line) {
  std::optional<Value> request = parse(line);
  if (!request) {
    return Object{{"status", Value("error")},
                  {"error_code", Value("INVALID_JSON")}};
  }
  return build_response(*request);
}

}  // namespace dut
