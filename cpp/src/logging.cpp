#include "dut/logging.hpp"

#include <chrono>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>

namespace dut {
namespace {

std::mutex g_log_mutex;
std::ofstream g_log_file;
bool g_verbose = false;

const char* level_name(Level level) {
  switch (level) {
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

void configure_logging(const std::string& log_file, bool verbose) {
  std::lock_guard<std::mutex> guard(g_log_mutex);
  g_verbose = verbose;
  if (log_file.empty()) {
    return;
  }
  g_log_file.open(log_file, std::ios::app);
  if (!g_log_file.is_open()) {
    std::cerr << utc_timestamp()
              << " level=ERROR message=log_file_unavailable path=" << log_file
              << '\n';
  }
}

void log_message(Level level, const std::string& message) {
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
