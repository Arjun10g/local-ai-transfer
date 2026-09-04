add_library(lae_runtime
  backend/fixture_backend.cpp
  engine/engine.cpp
  server/http_server.cpp
)
target_include_directories(lae_runtime PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(lae_runtime PUBLIC LAE_API_VERSION="0.1.0" LAE_ENGINE_VERSION="0.1.0-fixture")
if(WIN32)
  target_link_libraries(lae_runtime PUBLIC ws2_32)
else()
  target_link_libraries(lae_runtime PUBLIC pthread)
endif()

add_executable(lae-engine main.cpp)
target_link_libraries(lae-engine PRIVATE lae_runtime)

add_executable(lae-runtime-tests ../tests/native/runtime_tests.cpp)
target_link_libraries(lae-runtime-tests PRIVATE lae_runtime)
add_test(NAME lae-runtime-tests COMMAND lae-runtime-tests)
