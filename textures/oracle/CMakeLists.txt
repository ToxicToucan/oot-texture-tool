cmake_minimum_required(VERSION 3.20)
# Static MSVC runtime (/MT): players need no Visual C++ Redistributable. Set before project() so every target below, third-party code
# included, uses it; the libraries this one links must agree (a mismatch is a link error).
if(NOT DEFINED CMAKE_MSVC_RUNTIME_LIBRARY)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()
project(bc7dec LANGUAGES CXX)

# Standalone BC7 decoder for the oracle. Uses the reference decoder (bc7decomp_ref.cpp) from the
# bc7enc_rdo third-party tree; no encoder code is built.
# Configure with Ninja + clang-cl; output lands directly in the build directory (build\oracle).

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

set(BC7_THIRD_PARTY "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/bc7enc_rdo")

add_executable(bc7dec
  bc7dec.cpp
  "${BC7_THIRD_PARTY}/bc7decomp_ref.cpp"
)

target_include_directories(bc7dec PRIVATE "${BC7_THIRD_PARTY}")
target_compile_definitions(bc7dec PRIVATE _CRT_SECURE_NO_WARNINGS)
set_target_properties(bc7dec PROPERTIES
  CXX_STANDARD 17
  CXX_STANDARD_REQUIRED ON
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
  MSVC_RUNTIME_LIBRARY "MultiThreaded"
)

if(MSVC)
  target_compile_options(bc7dec PRIVATE /W3 /EHsc)
endif()
