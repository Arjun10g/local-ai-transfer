# Compile/static-analysis graph for already-merged inert Windows boundaries.
# This file is included only by LAE_ENABLE_INERT_WINDOWS_COMPILE_CHECKS=ON
# after native/CMakeLists.txt has required WIN32 and an MSVC-compatible driver.
# It must never be included from an install, package, host, or product target.

set(_LAE_INERT_NATIVE_ROOT "${CMAKE_CURRENT_LIST_DIR}/..")
set(_LAE_INERT_REPOSITORY_ROOT "${_LAE_INERT_NATIVE_ROOT}/..")

function(_lae_configure_inert_compile_check target)
  set_target_properties(${target} PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED YES
    CXX_EXTENSIONS NO)
  target_compile_definitions(${target} PRIVATE
    LAE_INERT_COMPILE_CHECK_ONLY=1
    NOMINMAX
    UNICODE
    WIN32_LEAN_AND_MEAN
    _UNICODE)
  target_compile_options(${target} PRIVATE
    /W4
    /WX
    /EHsc
    /GS
    /permissive-
    /sdl
    /utf-8
    /Zc:__cplusplus
    /Zc:preprocessor
    /analyze)
endfunction()

add_library(lae_compilecheck_windows_readonly_fs STATIC
  "${_LAE_INERT_NATIVE_ROOT}/windows_readonly_fs/windows_readonly_fs.cpp")
_lae_configure_inert_compile_check(lae_compilecheck_windows_readonly_fs)
target_link_libraries(lae_compilecheck_windows_readonly_fs PRIVATE
  advapi32
  ntdll)

add_library(lae_compilecheck_windows_hardware_attestor STATIC
  "${_LAE_INERT_NATIVE_ROOT}/windows_hardware_attestor/windows_hardware_attestor.cpp")
_lae_configure_inert_compile_check(lae_compilecheck_windows_hardware_attestor)
target_link_libraries(lae_compilecheck_windows_hardware_attestor PRIVATE
  advapi32
  bcrypt
  crypt32
  dxgi
  ntdll
  setupapi
  wintrust)

add_library(lae_compilecheck_action_journal_helper STATIC
  "${_LAE_INERT_NATIVE_ROOT}/action_journal_helper/main.cpp"
  "${_LAE_INERT_NATIVE_ROOT}/action_journal_helper/pipe_server.cpp"
  "${_LAE_INERT_NATIVE_ROOT}/action_journal_helper/protocol_codec.cpp"
  "${_LAE_INERT_NATIVE_ROOT}/action_journal_helper/store_codec.cpp"
  "${_LAE_INERT_NATIVE_ROOT}/action_journal_storage/windows_storage.cpp")
_lae_configure_inert_compile_check(lae_compilecheck_action_journal_helper)
target_include_directories(lae_compilecheck_action_journal_helper SYSTEM PRIVATE
  "${_LAE_INERT_REPOSITORY_ROOT}/vendor/llama.cpp/vendor")
target_link_libraries(lae_compilecheck_action_journal_helper PRIVATE
  advapi32
  bcrypt)

add_library(lae_compilecheck_windows_release_verifier STATIC
  "${_LAE_INERT_NATIVE_ROOT}/windows_release_verifier/windows_release_verifier.cpp")
_lae_configure_inert_compile_check(lae_compilecheck_windows_release_verifier)
target_link_libraries(lae_compilecheck_windows_release_verifier PRIVATE
  advapi32
  bcrypt
  ntdll)

# A finite convenience target for remote evidence commands. It has no product
# consumer and is unreachable when the enclosing option is OFF.
add_custom_target(lae_inert_windows_compile_checks
  DEPENDS
    lae_compilecheck_action_journal_helper
    lae_compilecheck_windows_hardware_attestor
    lae_compilecheck_windows_readonly_fs
    lae_compilecheck_windows_release_verifier)

unset(_LAE_INERT_NATIVE_ROOT)
unset(_LAE_INERT_REPOSITORY_ROOT)
