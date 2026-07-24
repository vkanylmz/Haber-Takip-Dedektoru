@echo off
echo Finansal Haber Toplayici arka planda baslatiliyor...
cd /d "%~dp0"
powershell -Command "Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList 'main.py' -WindowStyle Hidden"
echo.
echo Basariyla baslatildi!
echo (Durumu kontrol etmek icin http://localhost:8000 adresini ziyaret edebilirsiniz)
timeout /t 5
