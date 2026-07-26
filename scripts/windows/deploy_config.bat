@echo off
REM Deploy all EuroPi config JSON files to the connected Pico/Pico 2 via mpremote.
REM Target directory: /config/
REM   EuroPiConfig.json      -> board model "pico 2", CPU "normal" (no overclock)
REM   ExperimentalConfig.json -> empty {} (use defaults)
REM   Diagnostic.json        -> empty {} (use defaults)
REM Prerequisites:
REM   1. Pico 2 flashed with RPI_PICO2 MicroPython firmware and connected via USB.
REM   2. mpremote installed (pip install mpremote).

echo Creating /config directory (ignore error if it already exists)...
mpremote fs mkdir :/config 2>nul

echo Copying config files to /config/...
mpremote fs cp "%~dp0EuroPiConfig.json" :/config/EuroPiConfig.json
mpremote fs cp "%~dp0ExperimentalConfig.json" :/config/ExperimentalConfig.json
mpremote fs cp "%~dp0Diagnostic.json" :/config/Diagnostic.json

if errorlevel 1 (
    echo.
    echo FAILED: could not copy config files.
    echo Check that the board is connected and running MicroPython.
    pause
    exit /b 1
)

echo.
echo Done. Verify with:
echo   mpremote fs cat :/config/EuroPiConfig.json
echo   mpremote fs cat :/config/ExperimentalConfig.json
echo   mpremote fs cat :/config/Diagnostic.json
pause
