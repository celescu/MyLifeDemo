@echo off
cd /d "%~dp0"

if "%ANTHROPIC_API_KEY%"=="" (
    echo No se encuentra la clave de API configurada.
    echo Ejecuta primero configurar_clave.bat con doble clic.
    echo.
    pause
    exit
)

echo Arrancando el servidor...
start "Servidor - no cerrar mientras uses la demo" cmd /k "python -m uvicorn main:app --reload"

echo Esperando a que arranque...
timeout /t 4 >nul

echo Abriendo el navegador...
start "" http://localhost:8000

echo.
echo Listo. Cuando termines, ve a la ventana llamada
echo "Servidor - no cerrar mientras uses la demo" y cierrala
echo (o pulsa Ctrl+C dentro de ella) para apagar el servidor.
echo.
pause
