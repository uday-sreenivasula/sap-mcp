@echo off
rem Starts the SAP MCP server. First run (or a new plugin version, or the Python it was
rem set up with has gone): runs setup.cmd, which finds/installs Python and the packages.
rem stdout belongs to the MCP protocol, so setup output goes to %LOCALAPPDATA%\<NAME>\setup.log.
rem NAME and ENTRY differ between the sap and sap-write plugins (set by tools\build_plugin.py).
setlocal
set "ROOT=%~dp0"
set "NAME=sap-mcp-plugin"
set "ENTRY=run.py"
set "VER=0.7.8"
set "DATA=%LOCALAPPDATA%\%NAME%"
if not exist "%DATA%" mkdir "%DATA%"

set "PY="
if exist "%DATA%\python.txt" set /p PY=<"%DATA%\python.txt"
if not exist "%DATA%\ready-%VER%" goto setup
if not defined PY goto setup
if not exist "%PY%" goto setup
goto run

:setup
call "%ROOT%setup.cmd" "%VER%" "%DATA%" "%ENTRY%" >> "%DATA%\setup.log" 2>&1
if errorlevel 1 (
  echo SAP connector setup failed - see %DATA%\setup.log 1>&2
  exit /b 1
)
set /p PY=<"%DATA%\python.txt"

:run
set "PYTHONIOENCODING=utf-8"
set "SAP_MCP_LIB=%DATA%\lib-%VER%"
"%PY%" -s "%ROOT%server\%ENTRY%"
