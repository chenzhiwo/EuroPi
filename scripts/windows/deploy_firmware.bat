@echo off
:: FIRMWARE
set "SRC=%~dp0..\..\software\firmware"
mpremote fs mkdir :/lib
for %%f in ("%SRC%\*.py") do mpremote fs cp "%%f" :/lib/

::: TOOLS (diagnostic, calibrate, about, conf_edit, experimental_conf_edit, __init__)
mpremote fs mkdir :/lib/tools
for %%f in ("%SRC%\tools\*.py") do mpremote fs cp "%%f" :/lib/tools/
