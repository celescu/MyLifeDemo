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

## Cómo ponerlo en marcha

1. Necesitas Python 3.10+ instalado.

2. Instala las dependencias:

   pip install -r requirements.txt

3. Consigue una clave de API de Anthropic en https://console.anthropic.com/
   (creas una cuenta, generas una API key). Anthropic te da algo de crédito
   gratis al empezar; para este demo el gasto será de céntimos.

4. Exporta la clave como variable de entorno, y añade también las nuevas
   variables de administración:

   Linux/macOS:
     export ANTHROPIC_API_KEY="tu-clave-aqui"
     export ADMIN_PASSWORD="una-contraseña-solo-tuya-para-crear-cuentas"
     export ADMIN_USERNAME="yo"

   Windows (PowerShell):
     $env:ANTHROPIC_API_KEY="tu-clave-aqui"
     $env:ADMIN_PASSWORD="una-contraseña-solo-tuya-para-crear-cuentas"
     $env:ADMIN_USERNAME="yo"

   ADMIN_PASSWORD no es la contraseña de ningún usuario — es una contraseña
   aparte que solo tú conoces, y que se usa únicamente para dar de alta
   cuentas nuevas desde /admin.html y para poder descargar la base de datos
   completa. ADMIN_USERNAME debe coincidir con el nombre de usuario que uses
   tú mismo (recomendado: "yo", así se conecta con todos tus datos ya
   guardados de las pruebas anteriores).

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

## Siguiente paso natural

- Validar con 2-3 personas de confianza además de ti mismo.
- Pensar en algún sistema de pago sencillo si la validación va bien
  (sin necesidad todavía de base legal/empresarial formal).
