# Demo: entrevistador biográfico con memoria progresiva

## Qué es esto

Un demo mínimo para probar la idea contigo mismo como primer usuario:
- Chat en el navegador donde una IA te entrevista sobre tu vida.
- Puedes cerrar la sesión, volver otro día, y la IA recuerda (mediante un
  resumen estructurado, no la conversación completa) por dónde ibas.
- Sin base de datos externa, sin servidor en la nube: todo corre en tu
  ordenador con SQLite.

## Cómo ponerlo en marcha

1. Necesitas Python 3.10+ instalado.

2. Instala las dependencias:

   pip install -r requirements.txt

3. Consigue una clave de API de Anthropic en https://console.anthropic.com/
   (creas una cuenta, generas una API key). Anthropic te da algo de crédito
   gratis al empezar; para este demo el gasto será de céntimos.

4. Exporta la clave como variable de entorno:

   Linux/macOS:
     export ANTHROPIC_API_KEY="tu-clave-aqui"

   Windows (PowerShell):
     $env:ANTHROPIC_API_KEY="tu-clave-aqui"

5. Arranca el servidor:

   uvicorn main:app --reload

6. Abre el navegador en:

   http://localhost:8000

7. Escribe cualquier cosa para arrancar. Habla con la entrevistadora todo
   lo que quieras. Cuando quieras parar por hoy, pulsa "Cerrar sesión de
   hoy" (esto genera el resumen de memoria). Al volver a abrir la página
   y escribir de nuevo, seguirá desde donde lo dejaste.

8. "Ver memoria acumulada" te enseña el JSON de resumen tal cual lo ve la
   IA — útil para depurar y ver si está capturando bien lo importante.

## Qué mirar mientras lo pruebas

- ¿Las preguntas se sienten naturales o repetitivas?
- ¿La entrevistadora retoma bien los temas pendientes en la sesión 2?
- ¿El resumen de memoria (botón "ver memoria") refleja fielmente lo que
  contaste, o pierde matices importantes?
- ¿Cuánto tiempo aguantas hablando antes de cansarte? Eso te dice la
  duración real de sesión a diseñar.

## Siguiente paso natural

Si esto funciona bien contigo, el siguiente paso sería:
- Pulir el system prompt de la entrevistadora según lo que veas que falla.
- Añadir un pequeño "resumen visible" al reabrir sesión ("la última vez
  hablamos de tu infancia..."), para dar continuidad de cara al usuario.
- Solo después, pensar en múltiples usuarios, login, y cobro.
