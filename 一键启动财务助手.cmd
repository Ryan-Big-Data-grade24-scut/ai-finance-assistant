@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto setup
if not exist "frontend\node_modules" goto setup_done
goto launch

:setup
echo Project environment is missing. Running setup...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 goto failed
goto launch

:setup_done
echo Frontend dependencies are missing. Running setup...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 goto failed

:launch
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch-app.ps1" %*
if errorlevel 1 goto failed

echo.
echo AI Finance Assistant started successfully.
echo You may close this window. The app will keep running in the background.
echo Double-click the stop file when you want to stop it.
pause
exit /b 0

:failed
echo.
echo Startup failed. Please check the error message and the .runtime log folder.
pause
exit /b 1
