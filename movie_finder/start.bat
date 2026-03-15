@echo off
echo 映画上映時間チェッカーを起動します...
cd /d "%~dp0"
start http://localhost:5000
python app.py
pause
