@echo off
title Douyin Analyzer Pro - Login
cd /d %~dp0
echo 在弹出的窗口输入账号名（回车=default），扫码登录最长60秒。
python -c "import sys; sys.path.insert(0,'app'); import accounts; print('当前账号:', accounts.active_name() or 'default')"
set /p ACC=账号名(回车=default):
python -c "import sys; sys.path.insert(0,'app'); import subprocess; acc=sys.argv[1] if len(sys.argv)>1 else 'default'; subprocess.Popen([sys.executable,'app/login_worker.py',acc],creationflags=subprocess.CREATE_NEW_CONSOLE)" %ACC%
pause
