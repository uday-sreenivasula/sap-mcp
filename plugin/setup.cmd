@echo off
rem One-time setup for the SAP connector (called by launch.cmd; output goes to setup.log).
rem 1. Find Python 3.10+ (not a uv-managed one: uv renames its folders on update);
rem    install Python 3.12 for this user with winget if there is none.
rem 2. Install the server's packages into <DATA>\lib (private to this connector).
rem Arguments from launch.cmd: version, data folder, entry script.
setlocal EnableDelayedExpansion
set "VER=%~1"
set "DATA=%~2"
set "ENTRY=%~3"
rem One package folder per version: a running Claude may still hold files of the
rem previous version open, so that folder is never deleted or overwritten here.
set "LIB=%DATA%\lib-%VER%"
set "CHECK=import sys, os; e = sys.executable; print(e if sys.version_info >= (3, 10) and 'uv' not in e.lower().split(os.sep) else '')"
echo === SAP connector setup %VER% %DATE% %TIME% ===

call :findpy
if not defined PY (
  echo Python 3.10+ not found - installing Python 3.12 for this user with winget
  where winget >nul 2>&1 || (echo winget not available - install Python 3.12 from python.org, then restart Claude & exit /b 1)
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
  call :findpy
  if not defined PY (echo Python install did not complete & exit /b 1)
)
echo python: !PY!

"!PY!" -m pip --version >nul 2>&1 || "!PY!" -m ensurepip --user
rem Fresh folder for this version (a leftover from a failed attempt is removed).
if exist "%LIB%" rmdir /s /q "%LIB%"
mkdir "%LIB%"
"!PY!" -m pip install --disable-pip-version-check --no-warn-script-location --target "%LIB%" ^
  "mcp>=2.3.0,<3" "httpx>=0.27.0" "pydantic>=2.6.0" "keyring>=24.0.0" "openpyxl>=3.1,<4" || (echo pip install failed & exit /b 1)
set "SAP_MCP_LIB=%LIB%"
"!PY!" -s "%~dp0server\%ENTRY%" --check || (echo import check failed & exit /b 1)

rem Other versions' ready markers stay: a Claude still running an older version must not
rem re-run its setup (that would delete the package folder it is using).
> "%DATA%\python.txt" (echo|set /p="!PY!")
echo %DATE% %TIME%> "%DATA%\ready-%VER%"
echo setup complete
exit /b 0

:findpy
set "PY="
rem py launcher first, then python on PATH (skipping the Microsoft Store alias), then default
rem folders. CHECK itself rejects uv-managed interpreters (a "uv" folder in their path).
for /f "delims=" %%E in ('py -3 -c "%CHECK%" 2^>nul') do if not defined PY set "PY=%%E"
if defined PY exit /b 0
for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
  if not defined PY for /f "delims=" %%E in ('"%%P" -c "%CHECK%" 2^>nul') do set "PY=%%E"
)
if defined PY exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
  if not defined PY if exist "%%D\python.exe" for /f "delims=" %%E in ('"%%D\python.exe" -c "%CHECK%" 2^>nul') do set "PY=%%E"
)
exit /b 0
