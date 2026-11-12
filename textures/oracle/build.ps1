# Builds bc7dec.exe (independent BC7 decoder) with clang-cl + Ninja into textures\build\oracle\.
# Run:  pwsh -File textures\oracle\build.ps1   (from the project root)
$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$build  = [System.IO.Path]::GetFullPath((Join-Path $here '..\build\oracle'))
$cmake  = 'C:\Program Files\CMake\bin\cmake.exe'
$clang  = 'C:\Program Files\LLVM\bin\clang-cl.exe'
$ninja  = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ninja.exe'
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat'

foreach ($p in @($cmake, $clang, $ninja, $vcvars)) {
    if (-not (Test-Path $p)) { throw "not found: $p" }
}

# import the VS developer environment (INCLUDE, LIB, PATH) into this session
$envdump = cmd /c "`"$vcvars`" >nul && set"
foreach ($line in $envdump) {
    $i = $line.IndexOf('=')
    if ($i -gt 0) { Set-Item -Path ("Env:" + $line.Substring(0, $i)) -Value $line.Substring($i + 1) }
}

& $cmake -S $here -B $build -G Ninja "-DCMAKE_MAKE_PROGRAM=$ninja" "-DCMAKE_CXX_COMPILER=$clang" -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { throw "configure failed (exit $LASTEXITCODE)" }
& $cmake --build $build
if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }
Write-Output "built: $(Join-Path $build 'bc7dec.exe')"
