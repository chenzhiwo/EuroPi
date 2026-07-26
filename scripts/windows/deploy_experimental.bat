@echo off
:: EXPERIMENTAL
set "SRC=%~dp0..\..\software\firmware\experimental"
mpremote fs mkdir :/lib/experimental
for %%f in ("%SRC%\*.py") do mpremote fs cp "%%f" :/lib/experimental/
