// Unit tests for the protocol core, run by CTest.
//
// No socket is opened here.  build_response() is a pure function, so its whole
// contract — including every error_code — is testable in microseconds.  That
// separation is why the Python harness only has to test the parts that really
// need a live process.
//
// The binary takes one test name and runs just that case, so CMake can
// register each case as its own CTest test and a failure names itself.
#include <iostream>
#include <string>
#include <vector>

#include "dut/json.hpp"
#include "dut/protocol.hpp"

namespace {

int g_failures = 0;

void check_equal(const std::string& actual, const std::string& expected,
                 const std::string& what) {
  if (actual == expected) {
    std::cout << "  ok: " << what << '\n';
    return;
  }
  ++g_failures;
  std::cout << "  FAILED: " << what << "\n    expected: " << expected
            << "\n    actual:   " << actual << '\n';
}

void check_true(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok: " << what << '\n';
    return;
  }
  ++g_failures;
  std::cout << "  FAILED: " << what << '\n';
}

std::string respond(const std::string& line) {
  return dut::handle_request_line(line).dump();
}

// REQ-COM-001: a valid status request is answered with state READY.
void test_status_ok() {
  check_equal(respond(R"({"command": "status", "sequence": 1})"),
              R"({"sequence": 1, "state": "READY", "status": "ok"})",
              "status request returns READY with the sequence echoed");
}

// REQ-PROTO-001: an unsupported command is rejected, not fatal.
void test_unsupported_command() {
  check_equal(respond(R"({"command": "launch", "sequence": 7})"),
              R"({"command": "launch", "error_code": "UNSUPPORTED_COMMAND", )"
              R"("sequence": 7, "status": "error"})",
              "unknown command returns UNSUPPORTED_COMMAND");
}

void test_missing_command() {
  check_equal(respond(R"({"sequence": 2})"),
              R"({"error_code": "MISSING_COMMAND", "sequence": 2, )"
              R"("status": "error"})",
              "missing command returns MISSING_COMMAND");
  check_equal(respond(R"({"command": null, "sequence": 2})"),
              R"({"error_code": "MISSING_COMMAND", "sequence": 2, )"
              R"("status": "error"})",
              "explicit null command is treated as missing");
}

// REQ-PROTO-002: malformed JSON is rejected without terminating.
void test_invalid_json() {
  for (const std::string& bad : {"{not json", "", "{\"a\": }", "[1, 2"}) {
    check_equal(respond(bad),
                R"({"error_code": "INVALID_JSON", "status": "error"})",
                "malformed input rejected: " + (bad.empty() ? "<empty>" : bad));
  }
}

void test_non_object_request() {
  check_equal(respond("[1, 2, 3]"),
              R"({"error_code": "INVALID_MESSAGE_TYPE", )"
              R"("message": "Request must be a JSON object", )"
              R"("status": "error"})",
              "a JSON array is not a valid request");
}

void test_json_round_trip() {
  const std::optional<dut::Value> parsed =
      dut::parse(R"({"b": "two", "a": [1, true, null], "c": {"d": "e"}})");
  check_true(parsed.has_value(), "nested document parses");
  if (parsed) {
    check_equal(parsed->dump(),
                R"({"a": [1, true, null], "b": "two", "c": {"d": "e"}})",
                "keys are sorted on output, matching Python's sort_keys=True");
  }
  const std::optional<dut::Value> escapes =
      dut::parse(R"({"text": "line\nbreak \"quoted\" é"})");
  check_true(escapes.has_value(), "escape sequences parse");
  if (escapes) {
    // ensure_ascii=True, as Python's json.dumps defaults to: é is emitted as
    // the six characters \u00e9 rather than as raw UTF-8 bytes.
    check_equal(escapes->dump(),
                "{\"text\": \"line\\nbreak \\\"quoted\\\" \\u00e9\"}",
                "escapes survive a round trip and non-ASCII is escaped");
  }

  // Astral characters become a UTF-16 surrogate pair, exactly as Python does.
  const std::optional<dut::Value> astral = dut::parse(R"({"text": "😀"})");
  check_true(astral.has_value(), "surrogate pair parses");
  if (astral) {
    check_equal(astral->dump(), "{\"text\": \"\\ud83d\\ude00\"}",
                "astral characters round trip as surrogate pairs");
  }
}

// REQ-PROTO-002: JSON forbids raw control characters inside strings.
void test_control_characters_rejected() {
  const std::string with_tab = "{\"command\": \"status\", \"sequence\": \"a\tb\"}";
  check_equal(respond(with_tab),
              R"({"error_code": "INVALID_JSON", "status": "error"})",
              "a literal tab inside a string is rejected");

  const std::string with_ctrl = "{\"command\": \"status\", \"sequence\": \"a\x01"
                                "b\"}";
  check_equal(respond(with_ctrl),
              R"({"error_code": "INVALID_JSON", "status": "error"})",
              "a literal control character inside a string is rejected");

  // The escaped forms remain valid and round trip.
  check_equal(respond(R"({"command": "status", "sequence": "a\tb"})"),
              R"({"sequence": "a\tb", "state": "READY", "status": "ok"})",
              "an escaped tab is accepted and re-escaped on output");
}

struct TestCase {
  const char* name;
  void (*run)();
};

const std::vector<TestCase>& all_tests() {
  static const std::vector<TestCase> tests = {
      {"status_ok", test_status_ok},
      {"unsupported_command", test_unsupported_command},
      {"missing_command", test_missing_command},
      {"invalid_json", test_invalid_json},
      {"non_object_request", test_non_object_request},
      {"json_round_trip", test_json_round_trip},
      {"control_characters", test_control_characters_rejected},
  };
  return tests;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--list") {
    for (const TestCase& test : all_tests()) {
      std::cout << test.name << '\n';
    }
    return 0;
  }

  const std::string requested = argc > 1 ? argv[1] : "";
  bool ran = false;
  for (const TestCase& test : all_tests()) {
    if (!requested.empty() && requested != test.name) {
      continue;
    }
    std::cout << test.name << ":\n";
    test.run();
    ran = true;
  }

  if (!ran) {
    std::cerr << "no such test: " << requested << '\n';
    return 2;
  }
  return g_failures == 0 ? 0 : 1;
}
