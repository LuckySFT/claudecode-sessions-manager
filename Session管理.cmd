@echo off
rem Double-click to open the Claude Code Session Manager UI.
rem Starts the local server first if it is not already running.
rem
rem This file must be saved as ASCII with CRLF line endings.
rem   - No non-ASCII bytes: cmd.exe decodes .cmd with the console code page
rem     (cp950 here), so UTF-8 Chinese turns into mojibake AND its lead bytes
rem     swallow following bytes, which shifts "rem" off the line start and makes
rem     cmd try to execute the comment text.
rem   - CRLF, not LF: cmd.exe parses LF-only batch files unreliably, breaking
rem     rem lines and parenthesised blocks the same way.
rem   Hence %~nx0 below instead of writing the (Chinese) file name literally.
rem   All Chinese output lives in the PowerShell script, which is UTF-8 + BOM.
rem
rem Usage:
rem   %~nx0                          update the index, start if needed, open browser
rem   %~nx0 -Stop                    stop the server
rem   %~nx0 -Status                  show process tree and listener PID
rem   %~nx0 -Reindex                 force the index update (same as the default)
rem   %~nx0 -NoReindex               skip the index update, just open the UI
rem   %~nx0 -NoBrowser               start only, do not open the browser
rem   %~nx0 -UseConsole              use python.exe instead of pythonw.exe
rem   %~nx0 -UseConsole -ShowWindow  visible console, live log output
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open-ui.ps1" %*
if errorlevel 1 (
  echo.
  echo Failed. See logs\serve.err.log for details.
  pause
)
endlocal
