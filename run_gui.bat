@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pythonw "%~dp0agy_session_manager.py"
) else (
    python "%~dp0agy_session_manager.py"
)
