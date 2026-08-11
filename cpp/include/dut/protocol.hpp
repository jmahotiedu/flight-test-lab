// The protocol core: a pure function from request to response.
//
// This is the C++ twin of simulator/simulator.py::build_response.  It touches
// no sockets, no clock and no globals, which is why it can be unit-tested by
// CTest without starting a server — the same reason the Python version is
// testable without one.
#ifndef DUT_PROTOCOL_HPP
#define DUT_PROTOCOL_HPP

#include <string>

#include "dut/json.hpp"

namespace dut {

// Mirrors build_response() in the Python DUT, including which inputs map to
// which error_code.  Given the same request, both implementations must emit
// byte-identical JSON.
Value build_response(const Value& request);

// Parses one request line and builds the response.  A line that is not valid
// JSON becomes {"error_code": "INVALID_JSON", "status": "error"} — the parse
// failure never propagates as an exception, because a malformed request must
// not take the connection (or the process) down.
Value handle_request_line(const std::string& line);

}  // namespace dut

#endif  // DUT_PROTOCOL_HPP
