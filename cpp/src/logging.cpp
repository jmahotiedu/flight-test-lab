#include "dut/logging.hpp"

#include <chrono>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <system_error>

namespace dut {
namespace {

std::mutex g_log_mutex;
std::ofstream g_log_file;
bool g_verbose = false;

const char* level_name(Level level) {
  switch (level) {
    case Level::Debug:
      return "DEBUG";
    case Level::Warning:
      return "WARNING";
    case Level::Error:
      return "ERROR";
    case Level::Info:
    default:
      return "INFO";
  }
}

std::string utc_timestamp() {
  const std::time_t now = std::chrono::system_clock::to_time_t(
      std::chrono::system_clock::now());
  std::tm parts{};
#ifdef _WIN32
  gmtime_s(&parts, &now);
#else
  gmtime_r(&now, &parts);
#endif
  char buffer[32];
  std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S", &parts);
  return std::string(buffer) + "Z";
}

}  // namespace

bool configure_logging(const std::string& log_file, bool verbose) {
  std::lock_guard<std::mutex> guard(g_log_mutex);
  g_verbose = verbose;
  if (log_file.empty()) {
    return true;
  }

  // Create the parent directory, as the Python DUT does — a harness that
  // passes evidence/logs/dut.log should not have to pre-create the tree.
  std::error_code error;
  const std::filesystem::path parent =
      std::filesystem::path(log_file).parent_path();
  if (!parent.empty()) {
    std::filesystem::create_directories(parent, error);
  }

  g_log_file.open(log_file, std::ios::app);
  if (!g_log_file.is_open()) {
    // Continuing without the log would serve requests happily while losing
    // the evidence the operator explicitly asked for, so this is fatal —
    // matching the Python DUT, whose FileHandler raises.
    std::cerr << utc_timestamp()
              << " level=ERROR message=log_file_unavailable path=" << log_file
              << '\n';
    return false;
  }
  return true;
}

void log_message(Level level, const std::string& message) {
  if (level == Level::Debug && !g_verbose) {
    return;  // --verbose is the only thing that makes these visible
  }
  const std::string line = utc_timestamp() + " level=" + level_name(level) +
                           " message=" + message;
  std::lock_guard<std::mutex> guard(g_log_mutex);
  std::cerr << line << '\n';
  if (g_log_file.is_open()) {
    g_log_file << line << '\n';
    g_log_file.flush();  // evidence must survive a kill -9, so never buffer it
  }
}

}  // namespace dut
