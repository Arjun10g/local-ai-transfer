#include "backend/fixture_backend.hpp"
#include "engine/engine.hpp"
#include "server/http_server.hpp"

#include <csignal>
#include <iostream>
#include <memory>
#include <string>
#include <chrono>
#include <thread>

namespace {
lae::Engine* active_engine = nullptr;
lae::HttpServer* active_server = nullptr;
volatile std::sig_atomic_t stop_requested = 0;
void on_signal(int) { stop_requested = 1; }
void usage() {
  std::cout << "lae-engine 0.1.0-fixture\ncommands: serve version print-build-info probe\n";
}
}  // namespace

int main(int argc, char** argv) {
  const std::string command = argc > 1 ? argv[1] : "help";
  if (command == "version") { std::cout << "0.1.0-fixture\n"; return 0; }
  if (command == "print-build-info") {
    std::cout << "{\"engine_version\":\"0.1.0-fixture\",\"api_version\":\"0.1.0\",\"backend\":\"fixture-cpu/0.1.0\",\"source_revision\":\"fixture-no-upstream\",\"model\":\"none\"}\n";
    return 0;
  }
  if (command == "probe") {
#ifdef _WIN32
    std::cout << "{\"bind\":\"127.0.0.1\",\"platform\":\"windows\",\"backend\":\"fixture-cpu/0.1.0\"}\n";
#else
    std::cout << "{\"bind\":\"127.0.0.1\",\"platform\":\"posix\",\"backend\":\"fixture-cpu/0.1.0\"}\n";
#endif
    return 0;
  }
  if (command != "serve") { usage(); return command == "help" ? 0 : 2; }

  unsigned port = 0; std::string token; bool token_seen = false;
  for (int i = 2; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--port" && i + 1 < argc) port = static_cast<unsigned>(std::stoul(argv[++i]));
    else if (arg == "--token" && i + 1 < argc) { token = argv[++i]; token_seen = true; }
    else { std::cerr << "unknown argument\n"; return 2; }
  }
  if (!token_seen || token.empty()) { std::cerr << "serve requires a non-empty --token\n"; return 2; }
  lae::Engine engine(std::make_unique<lae::FixtureBackend>());
  try { engine.initialize(); } catch (const std::exception& error) { std::cerr << error.what() << "\n"; return 1; }
  lae::HttpServer server(engine, token);
  try { server.start(port); } catch (const std::exception& error) { std::cerr << error.what() << "\n"; return 1; }
  active_engine = &engine; active_server = &server;
  std::signal(SIGINT, on_signal); std::signal(SIGTERM, on_signal);
  std::cout << "{\"event\":\"ready\",\"port\":" << server.port() << ",\"bind\":\"127.0.0.1\",\"token_required\":true}\n" << std::flush;
  while (!stop_requested) std::this_thread::sleep_for(std::chrono::milliseconds(50));
  server.stop();
  engine.stop();
  active_server = nullptr; active_engine = nullptr;
  return 0;
}
