// Logging that matches the Python DUT's line format exactly, so the same
// evidence checks (and the same eyes) can read either log:
//
//   2026-08-11T04:12:33Z level=INFO message=dut_ready host=127.0.0.1 port=9000
#ifndef DUT_LOGGING_HPP
#define DUT_LOGGING_HPP

#include <string>

namespace dut {

// Debug records are emitted only under --verbose, which is what makes
// that flag observable — the Python DUT has the same pairing.
enum class Level { Debug, Info, Warning, Error };

// Opens the log file (appending) in addition to stderr, creating its parent
// directory the way the Python DUT does.  Returns false when the requested
// file could not be opened, which the caller must treat as fatal: serving
// while silently discarding the evidence an operator asked for is worse than
// not serving at all.
bool configure_logging(const std::string& log_file, bool verbose);

void log_message(Level level, const std::string& message);

}  // namespace dut

#endif  // DUT_LOGGING_HPP
