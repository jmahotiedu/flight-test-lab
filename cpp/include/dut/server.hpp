// The TCP server, and the RAII types that keep its OS handles honest.
//
// Winsock and BSD sockets both hand you a raw integer handle that must be
// released exactly once.  Socket owns that handle; WinsockGuard owns the
// library initialisation.  Neither has a "cleanup()" you can forget to call:
// leaving the scope is the cleanup, including when an exception unwinds it.
#ifndef DUT_SERVER_HPP
#define DUT_SERVER_HPP

#include <atomic>
#include <string>

#ifdef _WIN32
#include <winsock2.h>
using socket_t = SOCKET;
inline constexpr socket_t kInvalidSocket = INVALID_SOCKET;
#else
using socket_t = int;
inline constexpr socket_t kInvalidSocket = -1;
#endif

namespace dut {

// Deliberate failure modes for the debugging lessons.  None of them can fire
// unless a flag asks for it, so the default build behaves exactly like the
// Python DUT.
enum class Fault {
  None,
  Crash,       // null dereference — a segfault with a readable backtrace
  BadAccess,   // out-of-bounds write past a heap buffer
  Hang,        // blocks forever on a mutex another thread already holds
  Slow,        // answers, but late
};

Fault fault_from_string(const std::string& name);

struct ServerOptions {
  std::string host = "127.0.0.1";
  int port = 9000;
  Fault fault = Fault::None;
  int fault_after = 1;    // fire on the Nth request (1-based)
  int fault_delay_ms = 400;
};

// Initialises Winsock for the process and shuts it down at scope exit.
// A no-op on POSIX, which keeps the call site free of #ifdef.
class WinsockGuard {
 public:
  WinsockGuard();
  ~WinsockGuard();

  WinsockGuard(const WinsockGuard&) = delete;
  WinsockGuard& operator=(const WinsockGuard&) = delete;

  bool ok() const { return ok_; }

 private:
  bool ok_ = false;
};

// Move-only owner of one socket handle.  Copying is deleted because two owners
// would close the same handle twice; moving transfers the obligation.
class Socket {
 public:
  Socket() = default;
  explicit Socket(socket_t handle) : handle_(handle) {}
  ~Socket();

  Socket(const Socket&) = delete;
  Socket& operator=(const Socket&) = delete;
  Socket(Socket&& other) noexcept;
  Socket& operator=(Socket&& other) noexcept;

  socket_t get() const { return handle_; }
  bool valid() const { return handle_ != kInvalidSocket; }
  void close();

 private:
  socket_t handle_ = kInvalidSocket;
};

// Runs the accept loop until stop_requested becomes true.  Returns a process
// exit status: 0 for a clean shutdown, 1 if the listener could not be created.
int run_server(const ServerOptions& options, std::atomic<bool>& stop_requested);

}  // namespace dut

#endif  // DUT_SERVER_HPP
