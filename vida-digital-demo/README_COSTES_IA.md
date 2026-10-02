# Cambio: registro detallado de costes de IA

Archivos modificados:
- `main(1).py`
- `app.html`

## Qué añade

- Tabla `uso_ia` para registrar cada llamada real a Anthropic.
- Modelo realmente usado en cada llamada.
- Tokens de entrada/salida y coste estimado en USD.
- Operaciones separadas: entrevista, cierre de sesión, reconstrucción, aportación externa y operaciones de autobiografía.
- Coste de cada reconstrucción consultable durante y después del proceso.
- Coste de cada generación completa de autobiografía devuelto por su API.
- Coste de capítulos, mejoras e índice de autobiografía.
- La pestaña Métricas muestra el coste acumulado de reconstrucciones y otras operaciones.
- Las nuevas reconstrucciones ya no contaminan los tokens/coste de la sesión que están procesando.

## Nota sobre datos anteriores

Los costes de sesiones y autobiografías que ya estaban almacenados siguen pudiéndose calcular con sus tokens y modelo. Las operaciones anteriores a esta modificación no tienen un registro individual en `uso_ia`, por lo que no se pueden separar retrospectivamente por operación (especialmente reconstrucciones antiguas).
