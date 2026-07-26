@echo off

:: Install menu as the boot program (root main.py)
mpremote fs cp "%~dp0..\..\software\contrib\menu.py" :/main.py
