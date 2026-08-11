// Logging that matches the Python DUT's line format exactly, so the same
// evidence checks (and the same eyes) can read either log:
//
//   2026-08-11T04:12:33Z level=INFO message=dut_ready host=127.0.0.1 port=9000
#ifndef DUT_LOGGING_HPP
#define DUT_LOGGING_HPP

#include <string>

namespace dut {

enum class Level { Info, Warning, Error };

// Opens the log file (appending) in addition to stderr.  An unwritable path is
// reported and then ignored: losing the file log must not stop the DUT, but it
// must not be silent either.
void configure_logging(const std::string& log_file, bool verbose);

void log_message(Level level, const std::string& message);

}  // namespace dut

#endif  // DUT_LOGGING_HPP
