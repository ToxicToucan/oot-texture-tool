@echo off
rem Builds texconv.exe (the texture tool's codec helper) next to this file, against the astc2bc7 library in textures\.
rem   1. powershell -File textures\scripts\fetch_third_party.ps1    (astc-encoder, bc7enc_rdo, ISPC into textures\third_party)
rem   2. pwsh -File textures\scripts\build.ps1                      (builds textures\build\release)
rem   3. build-texconv.cmd [astc2bc7 build folder] [output folder]
rem Needs Visual Studio (C++ tools) and LLVM clang-cl (set LLVM_DIR if LLVM is not in Program Files\LLVM).
setlocal
set "LIBDIR=%~1"
set "OUT=%~2"
if "%LIBDIR%"=="" set "LIBDIR=%~dp0textures\build\release"
if "%OUT%"=="" set "OUT=%~dp0."
if not exist "%LIBDIR%\astc2bc7.lib" (echo astc2bc7.lib not found in %LIBDIR%: run steps 1 and 2 first & exit /b 1)
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSDIR="
if exist "%VSWHERE%" "%VSWHERE%" -latest -products * -property installationPath > "%TEMP%\texconv-vs.txt"
if exist "%TEMP%\texconv-vs.txt" set /p VSDIR=<"%TEMP%\texconv-vs.txt"
if not defined VSDIR (echo Visual Studio with the C++ tools was not found & exit /b 1)
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
if not defined LLVM_DIR set "LLVM_DIR=%ProgramFiles%\LLVM"
set "PATH=%LLVM_DIR%\bin;%PATH%"
if not defined RGBCX_DIR set "RGBCX_DIR=%~dp0textures\third_party\bc7enc_rdo"
if not exist "%RGBCX_DIR%\rgbcx.cpp" (echo rgbcx.cpp not found in %RGBCX_DIR%: run step 1, or set RGBCX_DIR & exit /b 1)
if not exist "%OUT%" mkdir "%OUT%"
clang-cl /nologo /O2 /EHsc /std:c++17 /MD /I"%~dp0textures\include" /I"%RGBCX_DIR%" "%~dp0texconv.cpp" "%RGBCX_DIR%\rgbcx.cpp" ^
  /Fe"%OUT%\texconv.exe" /Fo"%OUT%\\" /link "%LIBDIR%\astc2bc7.lib" "%LIBDIR%\bc7e_ispc.lib" || exit /b 1
copy /y "%LIBDIR%\astcdec-*-shared.dll" "%OUT%\" >nul
echo built %OUT%\texconv.exe
