@echo off
echo ============================================
echo  Configurar la clave de API de Anthropic
echo ============================================
echo.
echo Pega aqui tu clave (empieza por sk-ant-...) y pulsa Enter.
echo Ojo: pegar con click derecho o Ctrl+V, sin espacios antes o despues.
echo.
set /p CLAVE="Clave: "
setx ANTHROPIC_API_KEY "%CLAVE%"
echo.
echo Listo. La clave ha quedado guardada de forma permanente en tu usuario de Windows.
echo A partir de ahora no hace falta que la vuelvas a escribir.
echo (Si tenias una ventana de arrancar.bat abierta, cierrala y abrela de nuevo
echo  para que coja el cambio.)
echo.
pause
