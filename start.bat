@echo off
chcp 65001 >nul
title Douyin Analyzer Pro
cd /d %~dp0
python app\main.py
echo.
echo Service exited. Press any key to close.
pause >nul