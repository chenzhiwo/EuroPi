@echo off
:: FIRMWARE
set "SRC=%~dp0..\..\software\firmware"
mpremote fs mkdir :/lib
for %%f in ("%SRC%\*.py") do mpremote fs cp "%%f" :/lib/
