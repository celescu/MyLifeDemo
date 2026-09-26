@echo off
echo ============================================
echo  Configurar claves y contraseñas de la app
echo ============================================
echo.
echo Pega aqui tu clave de Anthropic (empieza por sk-ant-...) y pulsa Enter.
echo Ojo: pegar con click derecho o Ctrl+V, sin espacios antes o despues.
echo.
set /p CLAVE="Clave de Anthropic: "
setx ANTHROPIC_API_KEY "%CLAVE%"

echo.
echo Ahora elige una contraseña de ADMINISTRADOR (solo para ti, sirve para
echo crear cuentas de usuario nuevas en /admin.html, no es la contraseña
echo de ningun usuario concreto):
echo.
set /p ADMINPASS="Contraseña de administrador: "
setx ADMIN_PASSWORD "%ADMINPASS%"
setx ADMIN_USERNAME "yo"

echo.
echo Listo. Todo ha quedado guardado de forma permanente en tu usuario de
echo Windows. A partir de ahora no hace falta que lo vuelvas a escribir.
echo (Si tenias una ventana de arrancar.bat abierta, cierrala y abrela de nuevo
echo  para que coja el cambio.)
echo.
pause
