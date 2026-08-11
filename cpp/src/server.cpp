#include "dut/server.hpp"

#include <atomic>
#include <chrono>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#include "dut/logging.hpp"
#include "dut/protocol.hpp"

namespace dut {
namespace {

std::atomic<int> g_request_count{0};

// Held for the whole process lifetime by the Hang fault, so the handler thread
// that tries to take it never returns.  A deadlock, on purpose, with two named
// threads for GDB to show.
std::mutex g_hang_mutex;

void close_handle(socket_t handle) {
#ifdef _WIN32
  ::closesocket(handle);
#else
  ::close(handle);
#endif
}

std::string peer_label(const sockaddr_in& address) {
  char text[INET_ADDRSTRLEN] = {0};
  ::inet_ntop(AF_INET, &address.sin_addr, text, sizeof(text));
  return std::string(text) + ":" + std::to_string(ntohs(address.sin_port));
}

void trigger_crash() {
  log_message(Level::Warning, "fault_injected fault=crash");
  // A null dereference: the fastest way to a backtrace whose top frame names
  // this function.  volatile stops the optimiser from deleting it outright.
  volatile int* pointer = nullptr;
  *pointer = 42;
}

void trigger_bad_access() {
  log_message(Level::Warning, "fault_injected fault=bad-access");
  // Writes past the end of a heap allocation.  Unlike the null dereference
  // this may not fault immediately — which is the lesson: memory errors and
  // crashes are not the same event, and the gap between them is where
  // debugging gets hard.
  std::vector<int> samples(4, 0);
  int* raw = samples.data();
  for (int i = 0; i < 64; ++i) {
    raw[i] = i;  // out of bounds from i == 4
  }
  log_message(Level::Warning, "fault_injected fault=bad-access survived_write");
}

void trigger_hang() {
  log_message(Level::Warning, "fault_injected fault=hang");
  // The main thread already holds this mutex, so this lock never returns.
  std::lock_guard<std::mutex> guard(g_hang_mutex);
  log_message(Level::Error, "unreachable: hang fault acquired the mutex");
}

// Applies whichever fault is configured once the request counter reaches
// fault_after.  Returns true when the caller should skip its normal reply.
bool maybe_inject_fault(const ServerOptions& options, int request_number) {
  if (options.fault == Fault::None || request_number < options.fault_after) {
    return false;
  }
  switch (options.fault) {
    case Fault::Crash:
      trigger_crash();
      return false;
    case Fault::BadAccess:
      trigger_bad_access();
      return false;
    case Fault::Hang:
      trigger_hang();
      return true;  // never actually reached
    case Fault::Slow:
      log_message(Level::Warning,
                  "fault_injected fault=slow delay_ms=" +
                      std::to_string(options.fault_delay_ms));
      std::this_thread::sleep_for(
          std::chrono::milliseconds(options.fault_delay_ms));
      return false;
    case Fault::None:
    default:
      return false;
  }
}

// MSG_NOSIGNAL makes a write to a closed socket return EPIPE instead of
// raising SIGPIPE, whose default action would kill the whole process.  One
// client hanging up mid-conversation must cost one connection, not the DUT.
// Windows has no SIGPIPE and no such flag; main() also installs SIG_IGN for
// platforms (e.g. macOS) that lack MSG_NOSIGNAL.
#ifdef MSG_NOSIGNAL
constexpr int kSendFlags = MSG_NOSIGNAL;
#else
constexpr int kSendFlags = 0;
#endif

bool send_all(socket_t handle, const std::string& payload) {
  std::size_t sent = 0;
  while (sent < payload.size()) {
    const int chunk = ::send(handle, payload.data() + sent,
                             static_cast<int>(payload.size() - sent),
                             kSendFlags);
    if (chunk <= 0) {
      return false;
    }
    sent += static_cast<std::size_t>(chunk);
  }
  return true;
}

// One thread per connection, mirroring the Python ThreadingTCPServer.  The
// Socket argument is taken by value and moved in, so this thread owns the
// handle and closes it when the function returns by any path.
void handle_connection(Socket connection, std::string peer,
                       const ServerOptions options) {
  log_message(Level::Info, "client_connected peer=" + peer);
  std::string buffer;
  char chunk[1024];

  while (true) {
    const int received = ::recv(connection.get(), chunk, sizeof(chunk), 0);
    if (received <= 0) {
      break;
    }
    buffer.append(chunk, static_cast<std::size_t>(received));

    // TCP is a byte stream: one recv may carry several lines, or half of one.
    // The newline is the frame boundary, not the packet.
    std::size_t newline;
    while ((newline = buffer.find('\n')) != std::string::npos) {
      std::string line = buffer.substr(0, newline);
      buffer.erase(0, newline + 1);
      if (!line.empty() && line.back() == '\r') {
        line.pop_back();
      }

      log_message(Level::Info, "request peer=" + peer + " payload=" + line);
      const int request_number = ++g_request_count;
      if (maybe_inject_fault(options, request_number)) {
        return;
      }

      const std::string response = handle_request_line(line).dump();
      if (!send_all(connection.get(), response + "\n")) {
        log_message(Level::Warning, "send_failed peer=" + peer);
        return;
      }
      log_message(Level::Info, "response peer=" + peer + " payload=" + response);
    }
  }

  log_message(Level::Info, "client_disconnected peer=" + peer);
}

}  // namespace

Fault fault_from_string(const std::string& name) {
  if (name == "crash") return Fault::Crash;
  if (name == "bad-access") return Fault::BadAccess;
  if (name == "hang") return Fault::Hang;
  if (name == "slow") return Fault::Slow;
  return Fault::None;
}

WinsockGuard::WinsockGuard() {
#ifdef _WIN32
  WSADATA data;
  ok_ = ::WSAStartup(MAKEWORD(2, 2), &data) == 0;
#else
  ok_ = true;
#endif
}

WinsockGuard::~WinsockGuard() {
#ifdef _WIN32
  if (ok_) {
    ::WSACleanup();
  }
#endif
}

Socket::~Socket() { close(); }

Socket::Socket(Socket&& other) noexcept : handle_(other.handle_) {
  other.handle_ = kInvalidSocket;
}

Socket& Socket::operator=(Socket&& other) noexcept {
  if (this != &other) {
    close();
    handle_ = other.handle_;
    other.handle_ = kInvalidSocket;
  }
  return *this;
}

void Socket::close() {
  if (handle_ != kInvalidSocket) {
    close_handle(handle_);
    handle_ = kInvalidSocket;
  }
}

int run_server(const ServerOptions& options, std::atomic<bool>& stop_requested) {
  WinsockGuard winsock;
  if (!winsock.ok()) {
    log_message(Level::Error, "winsock_init_failed");
    return 1;
  }

  if (options.fault == Fault::Hang) {
    // Taken before any client can connect: the handler thread that tries to
    // lock it later will block forever, and both threads stay visible in GDB.
    g_hang_mutex.lock();
  }

  Socket listener(::socket(AF_INET, SOCK_STREAM, 0));
  if (!listener.valid()) {
    log_message(Level::Error, "socket_create_failed");
    return 1;
  }

  int reuse = 1;
  ::setsockopt(listener.get(), SOL_SOCKET, SO_REUSEADDR,
               reinterpret_cast<const char*>(&reuse), sizeof(reuse));

  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(static_cast<unsigned short>(options.port));
  if (::inet_pton(AF_INET, options.host.c_str(), &address.sin_addr) != 1) {
    log_message(Level::Error, "invalid_host host=" + options.host);
    return 1;
  }

  if (::bind(listener.get(), reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) != 0) {
    log_message(Level::Error, "bind_failed host=" + options.host + " port=" +
                                  std::to_string(options.port));
    return 1;
  }
  if (::listen(listener.get(), 16) != 0) {
    log_message(Level::Error, "listen_failed");
    return 1;
  }

  // A short receive timeout on the listener turns accept() into a poll, so a
  // shutdown request is noticed within ~200 ms instead of blocking forever.
#ifdef _WIN32
  DWORD accept_timeout_ms = 200;
  ::setsockopt(listener.get(), SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&accept_timeout_ms),
               sizeof(accept_timeout_ms));
#else
  timeval accept_timeout{};
  accept_timeout.tv_usec = 200000;
  ::setsockopt(listener.get(), SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&accept_timeout),
               sizeof(accept_timeout));
#endif

  log_message(Level::Info, "dut_ready host=" + options.host + " port=" +
                               std::to_string(options.port));

  // --fault-after 0 fires the fault immediately, with no client involved.
  // That makes a crash reproducible under `gdb -batch -ex run -ex bt` in one
  // command, instead of needing a second process to poke the server.
  if (options.fault_after == 0) {
    maybe_inject_fault(options, 0);
  }

  while (!stop_requested.load()) {
    sockaddr_in peer_address{};
#ifdef _WIN32
    int peer_length = sizeof(peer_address);
#else
    socklen_t peer_length = sizeof(peer_address);
#endif
    socket_t accepted = ::accept(
        listener.get(), reinterpret_cast<sockaddr*>(&peer_address),
        &peer_length);
    if (accepted == kInvalidSocket) {
      continue;  // timed out (the poll above) or interrupted — re-check the flag
    }
    Socket connection(accepted);
    std::thread(handle_connection, std::move(connection),
                peer_label(peer_address), options)
        .detach();
  }

  log_message(Level::Info, "dut_stopped requested=True");
  return 0;
}

}  // namespace dut
