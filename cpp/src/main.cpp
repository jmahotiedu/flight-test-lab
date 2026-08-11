// Entry point for the C++ DUT.
//
// The shape mirrors simulator/simulator.py on purpose: parse arguments,
// validate before acquiring anything, configure logging, run the server,
// return an exit status.  main() returning int is the C++ equivalent of
// `raise SystemExit(main())` — the same contract CI reads.
#include <atomic>
#include <csignal>
#include <iostream>
#include <string>
#include <vector>

#if defined(__linux__)
#include <sys/prctl.h>
#endif

#include "dut/logging.hpp"
#include "dut/server.hpp"

namespace {

std::atomic<bool> g_stop_requested{false};

extern "C" void handle_signal(int signal_number) {
  // Signal handlers may only touch async-signal-safe state; an atomic flag
  // that the accept loop polls is the safe way to ask for a shutdown.
  (void)signal_number;
  g_stop_requested.store(true);
}

void print_usage() {
  std::cout
      << "Synthetic line-delimited JSON device-under-test server (C++).\n\n"
      << "options:\n"
      << "  --host HOST            interface to bind (default 127.0.0.1)\n"
      << "  --port PORT            port to bind (default 9000)\n"
      << "  --log-file PATH        append log lines to PATH as well as stderr\n"
      << "  --verbose              log at debug level\n"
      << "  --fault NAME           crash | bad-access | hang | slow\n"
      << "  --fault-after N        fire the fault on request N (default 1)\n"
      << "  --fault-delay-ms MS    delay used by --fault slow (default 400)\n"
      << "  -h, --help             show this message\n";
}

// Returns false when the value is missing or not an integer, so the caller can
// exit with a message instead of silently binding port 0.
bool parse_int(const std::string& text, int& out) {
  try {
    std::size_t consumed = 0;
    const int value = std::stoi(text, &consumed);
    if (consumed != text.size()) {
      return false;
    }
    out = value;
    return true;
  } catch (const std::exception&) {
    return false;
  }
}

}  // namespace

int main(int argc, char** argv) {
  dut::ServerOptions options;
  std::string log_file;
  bool verbose = false;

  const std::vector<std::string> args(argv + 1, argv + argc);
  for (std::size_t i = 0; i < args.size(); ++i) {
    const std::string& flag = args[i];
    const bool has_value = i + 1 < args.size();

    if (flag == "-h" || flag == "--help") {
      print_usage();
      return 0;
    }
    if (flag == "--verbose") {
      verbose = true;
    } else if (flag == "--host" && has_value) {
      options.host = args[++i];
    } else if (flag == "--port" && has_value) {
      if (!parse_int(args[++i], options.port)) {
        std::cerr << "--port must be an integer\n";
        return 2;
      }
    } else if (flag == "--log-file" && has_value) {
      log_file = args[++i];
    } else if (flag == "--fault" && has_value) {
      const std::string name = args[++i];
      options.fault = dut::fault_from_string(name);
      if (options.fault == dut::Fault::None) {
        std::cerr << "--fault must be crash, bad-access, hang or slow\n";
        return 2;
      }
    } else if (flag == "--fault-after" && has_value) {
      if (!parse_int(args[++i], options.fault_after)) {
        std::cerr << "--fault-after must be an integer\n";
        return 2;
      }
    } else if (flag == "--fault-delay-ms" && has_value) {
      if (!parse_int(args[++i], options.fault_delay_ms)) {
        std::cerr << "--fault-delay-ms must be an integer\n";
        return 2;
      }
    } else {
      std::cerr << "unrecognised argument: " << flag << "\n";
      return 2;
    }
  }

  // Validation before acquisition, same ordering as the Python DUT: a bad port
  // must fail before a socket, a thread or a log file exists.
  if (options.port < 1 || options.port > 65535) {
    std::cerr << "--port must be between 1 and 65535\n";
    return 1;
  }

  dut::configure_logging(log_file, verbose);
  std::signal(SIGINT, handle_signal);
  std::signal(SIGTERM, handle_signal);

#ifdef SIGPIPE
  // A client that disconnects mid-write must not kill the DUT.  The send path
  // uses MSG_NOSIGNAL where it exists; this covers the platforms that do not.
  std::signal(SIGPIPE, SIG_IGN);
#endif

#if defined(__linux__) && defined(PR_SET_PTRACER)
  // The debugging lessons attach GDB from a sibling process, which Linux
  // denies under the default Yama ptrace_scope=1.  This synthetic DUT opts
  // in to being traced so the exercise works for an ordinary user without
  // sudo or a system-wide sysctl change.
  ::prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
#endif

  return dut::run_server(options, g_stop_requested);
}
