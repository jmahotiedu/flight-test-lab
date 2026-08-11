#include "dut/server.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netdb.h>
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

// How long shutdown waits for handlers to finish before leaving
// without static destruction.  Long enough for a client that is
// merely slow, short enough that Ctrl+C still feels immediate.
constexpr auto kShutdownGrace = std::chrono::seconds(2);

// Maximum bytes in one request line, matching simulator.MAX_LINE_BYTES.  A
// client that streams without ever sending a newline otherwise grows this
// buffer until allocation fails — and an exception escaping a thread entry
// point calls std::terminate, so one malformed request would take the whole
// DUT down instead of costing one connection.
constexpr std::size_t kMaxLineBytes = 1048576;

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
  // Equality, not >=. The option is documented as "fire the fault on request
  // N", and >= turned `--fault slow --fault-after 2` into every request from
  // the second onwards being slow — measured 422, 406, 405 ms for requests
  // 2, 3 and 4. A fault injector that keeps firing is not producing the one
  // controlled event an experiment is built around. With --fault-after 0 the
  // startup call passes 0 and requests start at 1, so it also stops repeating
  // there.
  if (options.fault == Fault::None || request_number != options.fault_after) {
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
// What to do after one frame: keep reading, or stop this connection.
enum class Next { Continue, Stop };

// Answer one complete request line.  Extracted so the same path serves a line
// terminated by a newline and the final fragment left in the buffer when the
// client half-closes without one.
Next handle_line(const Socket& connection, const std::string& peer,
                 const ServerOptions& options, std::string line) {
  // Every trailing '\r', not just one: the Python DUT builds its request with
  // rstrip("\r\n"), so stopping at the first would leave the two DUTs parsing
  // different bytes for a payload that ends in more than one.
  while (!line.empty() && line.back() == '\r') {
    line.pop_back();
  }

  // The bound is on the payload, measured after the delimiter is gone — the
  // same number the Python DUT checks. Applying it to the raw buffer instead
  // rejected a payload of exactly kMaxLineBytes that Python's readline
  // accepted, and let a terminated line through at whatever size the last
  // read happened to deliver.
  if (line.size() > kMaxLineBytes) {
    log_message(Level::Warning, "request_too_long peer=" + peer + " bytes=" +
                                    std::to_string(line.size()) + " limit=" +
                                    std::to_string(kMaxLineBytes));
    send_all(connection.get(),
             std::string(R"({"error_code": "INVALID_JSON", "status": "error"})") +
                 "\n");
    return Next::Stop;
  }

  // Log the sanitised form: the raw bytes may not be valid UTF-8, and the
  // Python DUT decodes with errors="replace" before logging. Writing raw
  // bytes here would leave dut.log undecodable on native runs only, so an
  // evidence reader would fail depending on which DUT produced it.
  log_message(Level::Info,
              "request peer=" + peer + " payload=" + sanitize_utf8(line));
  // Byte-level detail, only under --verbose, mirroring the Python DUT's
  // DEBUG records: what you want when a request looks wrong on the wire
  // but fine in the summary line above.
  log_message(Level::Debug, "request_bytes peer=" + peer + " length=" +
                                std::to_string(line.size()));
  const int request_number = ++g_request_count;
  if (maybe_inject_fault(options, request_number)) {
    return Next::Stop;
  }

  const std::string response = handle_request_line(line).dump();
  if (!send_all(connection.get(), response + "\n")) {
    log_message(Level::Warning, "send_failed peer=" + peer);
    return Next::Stop;
  }
  log_message(Level::Info, "response peer=" + peer + " payload=" + response);
  return Next::Continue;
}

void handle_connection(Socket connection, std::string peer,
                       const ServerOptions options) {
  log_message(Level::Info, "client_connected peer=" + peer);
  std::string buffer;
  char chunk[1024];

  while (true) {
    const int received = ::recv(connection.get(), chunk, sizeof(chunk), 0);
    if (received <= 0) {
      // An orderly half-close leaves whatever arrived without a trailing
      // newline still buffered. The Python DUT's readline() returns that
      // fragment and answers it, so discarding it here made the same request
      // succeed against one implementation and vanish against the other.
      if (received == 0 && !buffer.empty()) {
        handle_line(connection, peer, options, buffer);
      }
      break;
    }
    buffer.append(chunk, static_cast<std::size_t>(received));

    // TCP is a byte stream: one recv may carry several lines, or half of one.
    // The newline is the frame boundary, not the packet.
    std::size_t newline;
    bool stop = false;
    while ((newline = buffer.find('\n')) != std::string::npos) {
      std::string line = buffer.substr(0, newline);
      buffer.erase(0, newline + 1);
      if (handle_line(connection, peer, options, std::move(line)) ==
          Next::Stop) {
        stop = true;
        break;
      }
    }
    if (stop) {
      return;
    }

    // The cap applies to what is left *after* extraction: the unterminated
    // frame. Checking the whole buffer first rejected a legal near-limit
    // frame whose successor happened to arrive in the same recv — a
    // divergence, since the Python DUT reads one line at a time and answers
    // both. What the bound is for is a client that never sends a newline: the
    // buffer would grow until allocation fails, and an exception escaping a
    // thread entry point calls std::terminate.
    //
    // Measured the way handle_line measures: trailing CRs are delimiter, not
    // payload. Counting them here rejected a full-size frame ending "\r" that
    // the EOF path would have answered — and that Python answers, since its
    // readline strips before comparing.
    std::size_t pending = buffer.size();
    while (pending > 0 && buffer[pending - 1] == '\r') {
      --pending;
    }
    if (pending > kMaxLineBytes) {
      log_message(Level::Warning, "request_too_long peer=" + peer + " bytes=" +
                                      std::to_string(pending) + " limit=" +
                                      std::to_string(kMaxLineBytes));
      send_all(connection.get(),
               std::string(R"({"error_code": "INVALID_JSON", "status": "error"})") +
                   "\n");
      break;
    }
  }

  log_message(Level::Info, "client_disconnected peer=" + peer);
}

// A running handler plus a flag it sets on the way out.  std::thread cannot
// be asked whether it has finished, and joining one that has not would block
// the accept loop, so the flag is what makes incremental reaping possible.
struct Handler {
  std::thread thread;
  std::shared_ptr<std::atomic<bool>> done;
};

void reap_finished(std::vector<Handler>& handlers) {
  const auto finished = std::remove_if(
      handlers.begin(), handlers.end(), [](Handler& handler) {
        if (!handler.done->load()) {
          return false;
        }
        if (handler.thread.joinable()) {
          handler.thread.join();
        }
        return true;
      });
  handlers.erase(finished, handlers.end());
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
  // inet_pton takes numeric literals only, so `--host localhost` bound fine
  // under the Python DUT and failed here — the same harness configuration
  // working or not depending on which implementation it pointed at, which is
  // the one thing a second implementation must not introduce.
  if (::inet_pton(AF_INET, options.host.c_str(), &address.sin_addr) != 1) {
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* resolved = nullptr;
    if (::getaddrinfo(options.host.c_str(), nullptr, &hints, &resolved) != 0 ||
        resolved == nullptr) {
      log_message(Level::Error, "invalid_host host=" + options.host);
      return 1;
    }
    address.sin_addr =
        reinterpret_cast<sockaddr_in*>(resolved->ai_addr)->sin_addr;
    ::freeaddrinfo(resolved);
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

  std::vector<Handler> handlers;
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
    // Clear the receive timeout the listener carries. An accepted socket
    // inherits the listener's options, and that 200 ms poll — which exists so
    // shutdown is noticed promptly — was being applied to *client* traffic:
    // any connection idle for longer than that had recv() return an error,
    // which this handler read as EOF and hung up. Measured against the Python
    // DUT, a 300 ms pause before sending a request was answered there and
    // dropped here. Handlers block until their client speaks or leaves.
#ifdef _WIN32
    DWORD no_timeout = 0;
    ::setsockopt(connection.get(), SOL_SOCKET, SO_RCVTIMEO,
                 reinterpret_cast<const char*>(&no_timeout),
                 sizeof(no_timeout));
#else
    timeval no_timeout{};
    ::setsockopt(connection.get(), SOL_SOCKET, SO_RCVTIMEO,
                 reinterpret_cast<const char*>(&no_timeout),
                 sizeof(no_timeout));
#endif
    // Kept, not detached. A detached handler outlives run_server, so on Ctrl+C
    // with a client still connected the WinsockGuard below would call
    // WSACleanup — and the process-wide logging globals would begin
    // destruction — while that thread was still inside recv() or writing a log
    // line. Every handler now has a lifetime that ends before the resources it
    // uses do.
    auto done = std::make_shared<std::atomic<bool>>(false);
    handlers.push_back(Handler{
        std::thread(
            [done](Socket socket, std::string peer, ServerOptions settings) {
              handle_connection(std::move(socket), std::move(peer), settings);
              done->store(true);
            },
            std::move(connection), peer_label(peer_address), options),
        done});
    // Reap as we go, so a long session does not accumulate one thread handle
    // per connection ever made.
    reap_finished(handlers);
  }

  // Close the listener first: no new handlers start, and one blocked in recv()
  // wakes as soon as its client goes away.
  listener.close();
  const auto deadline = std::chrono::steady_clock::now() + kShutdownGrace;
  for (Handler& handler : handlers) {
    while (!handler.done->load() &&
           std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (handler.done->load() && handler.thread.joinable()) {
      handler.thread.join();
    }
  }
  // Second pass, and the decision is "did we join it", not "does it say it is
  // done". A handler that finished between its own timed-out check and this
  // point would otherwise report done, satisfy the scan, and be destroyed
  // while still joinable — which is std::terminate, i.e. a crash produced by
  // the code meant to make shutdown graceful.
  bool all_joined = true;
  for (Handler& handler : handlers) {
    if (handler.done->load() && handler.thread.joinable()) {
      handler.thread.join();
    }
    if (handler.thread.joinable()) {
      all_joined = false;
    }
  }

  log_message(Level::Info, "dut_stopped requested=True");
  if (!all_joined) {
    // The hang fault blocks a handler on purpose and forever, so waiting is
    // not an option and neither is destroying Winsock underneath it. Leaving
    // without running static destructors is the only ending that races
    // nothing; every log line is already flushed as it is written.
    log_message(Level::Warning,
                "handler_still_running exiting_without_static_destruction");
    std::_Exit(0);
  }
  return 0;
}

}  // namespace dut
