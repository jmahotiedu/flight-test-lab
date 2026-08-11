# Build definition for the C++ device under test.
#
#   cmake -S cpp -B cpp/build
#   cmake --build cpp/build
#   ctest --test-dir cpp/build --output-on-failure
#
# The layout is deliberately conventional: a library target holding the logic,
# a thin executable that wires it to argv, and a test executable that links the
# library without going near the network.

cmake_minimum_required(VERSION 3.20)
project(flight_test_lab_dut LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Debug is the useful default here: the debugging lessons need symbols, and an
# optimised build inlines the very frames those exercises ask you to find.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Debug CACHE STRING "Build type" FORCE)
endif()

# Predictable output locations, so the Python harness can find the binary
# without knowing which generator produced it.
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
foreach(config DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
  set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${config} ${CMAKE_BINARY_DIR}/bin)
endforeach()

add_library(dut_core
  src/json.cpp
  src/logging.cpp
  src/protocol.cpp
  src/server.cpp
)
target_include_directories(dut_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

find_package(Threads REQUIRED)
target_link_libraries(dut_core PUBLIC Threads::Threads)

if(WIN32)
  # Winsock is not linked by default; forgetting this is the classic first
  # Windows link error (unresolved external symbol __imp_socket).
  target_link_libraries(dut_core PUBLIC ws2_32)
endif()

if(MSVC)
  target_compile_options(dut_core PRIVATE /W4)
else()
  target_compile_options(dut_core PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(dut src/main.cpp)
target_link_libraries(dut PRIVATE dut_core)

# MinGW links libstdc++/libgcc/winpthread dynamically by default, so the
# binary resolves those DLLs from PATH at startup — and on a typical Windows
# box PATH also contains Git's older bundled MinGW. Picking up the wrong
# libstdc++-6.dll makes the DUT die with STATUS_ENTRYPOINT_NOT_FOUND before
# main() runs, which looks like a broken build rather than a DLL collision.
# The harness launches this binary from pytest, the learning server and CI, so
# it is linked statically and depends on nothing but the OS.
if(MINGW)
  target_link_options(dut PRIVATE -static)
endif()

enable_testing()

add_executable(dut_tests tests/test_protocol.cpp)
target_link_libraries(dut_tests PRIVATE dut_core)
if(MINGW)
  target_link_options(dut_tests PRIVATE -static)
endif()

# One CTest entry per case, so a failure reports which contract broke rather
# than just "the test binary exited 1".
set(DUT_TEST_CASES
  status_ok
  unsupported_command
  missing_command
  invalid_json
  non_object_request
  json_round_trip
  control_characters
)
foreach(case ${DUT_TEST_CASES})
  add_test(NAME protocol.${case} COMMAND dut_tests ${case})
endforeach()

# A smoke test that the executable starts and answers --help: cheap proof that
# the link succeeded and the binary is runnable on this machine.
add_test(NAME dut.help COMMAND dut --help)
set_tests_properties(dut.help PROPERTIES
  PASS_REGULAR_EXPRESSION "line-delimited JSON device-under-test")
