@echo off
call "%~dp0deploy_firmware.bat"
call "%~dp0deploy_experimental.bat"
call "%~dp0deploy_contrib.bat"

:: Install menu as the boot program (root main.py)
mpremote fs cp "%~dp0..\..\software\contrib\menu.py" :/main.py
