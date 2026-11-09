@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
echo ================================================
echo Ledger Engine V3.0 - isolated acceptance test
echo ================================================
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\verify_ledger_engine.py"
) else (
  python "scripts\verify_ledger_engine.py"
)
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo ALL CHECKS PASSED.
) else (
  echo TEST FAILED. Please send the full output for diagnosis.
)
pause
exit /b %RC%
