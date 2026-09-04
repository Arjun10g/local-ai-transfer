add_library(lae_runtime
  backend/fixture_backend.cpp
  backend/llama_backend.cpp
  engine/engine.cpp
  model_validation/model_validator.cpp
  server/http_server.cpp
)
target_include_directories(lae_runtime PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(lae_runtime PUBLIC LAE_API_VERSION="0.1.0" LAE_ENGINE_VERSION="0.1.0" LAE_LLAMA_CPP_REVISION="3581ba0cf591b3f772fbb002de0f70e294bc0396")

option(LAE_ENABLE_LLAMA_CPP "Build against the pinned llama.cpp CPU backend" OFF)
if(LAE_ENABLE_LLAMA_CPP)
  set(LLAMA_BUILD_COMMON OFF CACHE BOOL "" FORCE)
  set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
  set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
  set(LLAMA_BUILD_APP OFF CACHE BOOL "" FORCE)
  set(LLAMA_BUILD_UI OFF CACHE BOOL "" FORCE)
  set(LLAMA_OPENSSL OFF CACHE BOOL "" FORCE)
  set(LLAMA_SUBPROCESS OFF CACHE BOOL "" FORCE)
  set(GGML_CPU ON CACHE BOOL "" FORCE)
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
  set(GGML_METAL OFF CACHE BOOL "" FORCE)
  set(GGML_BLAS OFF CACHE BOOL "" FORCE)
  add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../vendor/llama.cpp llama.cpp EXCLUDE_FROM_ALL)
  target_compile_definitions(lae_runtime PRIVATE LAE_ENABLE_LLAMA_CPP)
  target_include_directories(lae_runtime PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../vendor/llama.cpp/include)
  target_link_libraries(lae_runtime PRIVATE llama)
endif()
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

find_package(Python3 COMPONENTS Interpreter)
if(Python3_Interpreter_FOUND)
  add_test(NAME lae-http-negative-tests
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/../tests/native/http_negative_tests.py $<TARGET_FILE:lae-engine>)
  if(LAE_ENABLE_LLAMA_CPP)
    add_test(NAME lae-real-model-smoke
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/../tests/native/real_model_smoke.py $<TARGET_FILE:lae-engine>)
  endif()
endif()
