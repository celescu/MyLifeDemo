# Demo: entrevistador biográfico con memoria progresiva

## Qué es esto

Un demo para probar la idea con varias personas de confianza, cada una con
su propio usuario y contraseña:
- Chat en el navegador donde una IA entrevista a cada persona sobre su vida.
- Cada usuario puede cerrar su sesión, volver otro día, y la IA recuerda
  (mediante un resumen estructurado, no la conversación completa) por dónde iba.
- Los datos de cada usuario están separados; nadie puede ver la biografía
  de otra persona.
- Pensado para desplegarse en un hosting sencillo (ej. Railway) con SQLite;
  no necesita infraestructura compleja para un puñado de usuarios de prueba.
- Incluye cuestionario de autopercepción, métricas de uso y coste, y
  generación de una autobiografía narrativa a partir de lo contado.

⚠️ IMPORTANTE si despliegas en Railway: NO añadas un archivo `Procfile` a
este proyecto. Railway usa Railpack para detectar automáticamente que es
una app de Python/FastAPI, y un Procfile presente confunde esa detección
(ya nos pasó una vez: el despliegue fallaba en silencio y la app se quedaba
congelada sirviendo una versión antigua). Deja que Railway lo detecte solo.

## Cifrado de la base de datos

Desde esta versión, `memoria.db` se cifra en reposo con SQLCipher usando una
clave maestra (`DB_ENCRYPTION_KEY`). Esto protege el archivo si alguien
accediera a él sin pasar por la aplicación (un volumen filtrado, una copia
de seguridad robada). **No** protege frente al administrador, que conoce la
clave — para eso está el sistema de exportar/restaurar/borrar por usuario
que ya tienes.

- Si `memoria.db` ya existe y está sin cifrar, la primera vez que arranque
  esta versión se migra automáticamente a cifrado, sin perder nada.
- Se guarda una copia sin cifrar como `memoria.db.sin_cifrar.backup` durante
  la migración — **bórrala manualmente en cuanto confirmes que todo
  funciona bien**, si no la borras, el propósito del cifrado queda anulado
  mientras esa copia exista en el mismo disco.
- Si pierdes `DB_ENCRYPTION_KEY`, **pierdes todos los datos para siempre** —
  no hay forma de recuperarlos sin ella. Guárdala en un sitio seguro,
  separado de donde guardes las copias de `memoria.db`.

## Variables de entorno necesarias

     ANTHROPIC_API_KEY=sk-ant-...
     ADMIN_PASSWORD=una-contraseña-solo-tuya-para-crear-cuentas
     ADMIN_USERNAME=yo
     DB_ENCRYPTION_KEY=una-clave-larga-y-aleatoria

Genera `DB_ENCRYPTION_KEY` con este comando y pégala en Railway (Variables):

     python -c "import secrets; print(secrets.token_urlsafe(48))"

ADMIN_PASSWORD no es la contraseña de ningún usuario — es una contraseña
aparte que solo tú conoces, y que se usa únicamente para dar de alta
cuentas nuevas desde /admin.html y para operaciones de administración.
ADMIN_USERNAME debe coincidir con el nombre de usuario que uses tú mismo
(recomendado: "yo", así se conecta con todos tus datos ya guardados).

## Cómo ponerlo en marcha

1. Necesitas Python 3.10+ instalado.

2. Instala las dependencias:

   pip install -r requirements.txt

3. Consigue una clave de API de Anthropic en https://console.anthropic.com/
   (creas una cuenta, generas una API key). Anthropic te da algo de crédito
   gratis al empezar; para este demo el gasto será de céntimos.

4. Configura las variables de entorno de la sección anterior (en Railway:
   pestaña "Variables" del servicio).

5. Crea tu propia cuenta de usuario: con el servidor arrancado (paso 6),
   entra en /admin.html, pon como "nombre de usuario" exactamente "yo"
   (para conservar tu historial ya guardado), elige una contraseña, y
   pon tu ADMIN_PASSWORD en el tercer campo. Repite este paso por cada
   persona nueva a la que quieras dar acceso, con un nombre de usuario
   distinto para cada una.

6. Arranca el servidor:

   uvicorn main:app --reload

7. Abre el navegador en:

   http://localhost:8000/login.html

   Inicia sesión con el usuario y contraseña que creaste en el paso 5.

8. Escribe cualquier cosa para arrancar. Habla con la entrevistadora todo
   lo que quieras. Cuando quieras parar por hoy, pulsa "Cerrar sesión de
   hoy" (esto genera el resumen de memoria). Al volver a abrir la página
   y escribir de nuevo, seguirá desde donde lo dejaste.

9. "Ver memoria acumulada" te enseña el JSON de resumen tal cual lo ve la
   IA — útil para depurar y ver si está capturando bien lo importante.

10. El enlace "Descargar copia de seguridad" es visible para cualquier
    usuario, pero solo funciona para la cuenta que coincide con
    ADMIN_USERNAME — cualquier otra persona recibirá un error de acceso
    denegado al pulsarlo, ya que descarga los datos de TODOS los usuarios.

11. "Mi autobiografía" genera, a partir de la memoria acumulada, un texto
    narrativo en primera persona ordenado cronológicamente. Es una
    "instantánea": se genera bajo demanda y se puede volver a generar
    cuantas veces quieras para que incluya las sesiones nuevas (cada
    regeneración sustituye a la anterior, no se guarda un histórico).

## Qué mirar mientras lo pruebas

- ¿Las preguntas se sienten naturales o repetitivas?
- ¿La entrevistadora retoma bien los temas pendientes en la sesión 2?
- ¿El resumen de memoria (botón "ver memoria") refleja fielmente lo que
  contaste, o pierde matices importantes?
- ¿Cuánto tiempo aguantas hablando antes de cansarte? Eso te dice la
  duración real de sesión a diseñar.
- ¿La autobiografía generada suena natural y respeta el orden cronológico,
  o se nota artificial / repite cosas de los bloques temáticos?

## Pruebas obligatorias antes de dar por bueno este cambio

Hazlas en este orden, sin saltarte ninguna, porque un fallo aquí puede
significar pérdida de datos:

1. **Antes de desplegar nada**: descarga un backup de tu `memoria.db`
   actual (sin cifrar) desde la versión que ya tenías funcionando. Guárdalo
   aparte, con fecha, por si hay que volver atrás.

2. **Despliega esta versión** con las 4 variables de entorno puestas
   (sobre todo `DB_ENCRYPTION_KEY`, sin ella la app ni arranca).

3. **Revisa los logs de Railway** justo después del despliegue. Deberías
   ver la línea `[cifrado] Detectada base de datos sin cifrar. Migrando a
   SQLCipher...` seguida de `[cifrado] Migración completada.`. Si no
   aparece nada de esto, algo no se ha disparado — dímelo antes de seguir.

4. **Entra en la app con tu usuario de siempre** y comprueba que tu memoria,
   sesiones y autobiografía siguen exactamente igual que antes de migrar.
   Si algo falta aquí, NO borres el backup del paso 1 todavía.

5. **Descarga la base de datos** desde `/admin.html` ("Backup total") y
   comprueba con un editor de texto que el archivo ya NO empieza por el
   texto legible `SQLite format 3` — debería verse como datos binarios sin
   sentido. Eso confirma que el cifrado está activo de verdad.

6. **Prueba el ciclo de restauración total**: sube ese mismo archivo recién
   descargado (ya cifrado) de vuelta con "Restaurar TODO" en /admin.html,
   y confirma que todo sigue funcionando después. Esto verifica que un
   backup cifrado se puede restaurar sin problemas, no solo generar.

7. **Reinicia el servicio en Railway manualmente** (sin cambiar código) y
   comprueba que la app arranca bien y tus datos siguen ahí. Esto confirma
   que la clave de cifrado persiste correctamente entre reinicios.

8. Solo cuando 1-7 hayan ido bien: en `/admin.html`, sección "Copia
   residual sin cifrar", pulsa "Comprobar si existe" y luego "Borrar copia
   sin cifrar". Borra también el backup sin cifrar del paso 1 de tu propio
   ordenador, ya que a partir de ahí solo deberías conservar backups ya
   cifrados.

## Siguiente paso natural

- Validar con 2-3 personas de confianza además de ti mismo.
- Pensar en algún sistema de pago sencillo si la validación va bien
  (sin necesidad todavía de base legal/empresarial formal).
- Una vez el cifrado esté confirmado y estable, añadir en una tanda aparte
  las mejoras de menor riesgo que quedaron pendientes: límite de peticiones
  (rate limiting), cabeceras de seguridad, y validación de contraseñas
  fuertes al crear cuentas.
