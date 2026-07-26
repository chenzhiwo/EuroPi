@echo off
REM Install the ssd1306 OLED driver onto the connected Pico/Pico 2 via mpremote.
REM Uses mip (NOT pypi), so the package name is "ssd1306", not "micropython-ssd1306".
REM Prerequisites:
REM   1. Pico 2 flashed with RPI_PICO2 MicroPython firmware and connected via USB.
REM   2. mpremote installed (pip install mpremote).

echo Installing ssd1306 via mpremote mip...
mpremote mip install ssd1306

if errorlevel 1 (
    echo.
    echo FAILED: could not install ssd1306.
    echo Check that the board is connected and running MicroPython.
    pause
    exit /b 1
)

echo.
echo Done. ssd1306 installed to /lib/ssd1306.mpy
pause
