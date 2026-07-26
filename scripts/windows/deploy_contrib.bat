@echo off
:: CONTRIB
set "SRC=%~dp0..\..\software\contrib"
mpremote fs mkdir :/lib/contrib
for %%f in ("%SRC%\*.py") do mpremote fs cp "%%f" :/lib/contrib/
