# Pruebas del sistema de créditos

Realiza primero estas pruebas en local con una copia de la base de datos. No fuerces caducidades ni manipules saldos en producción.

## Tarifas iniciales

Hipótesis de cálculo provisional: 1 crédito ≈ 0,01 €. No es todavía un precio de paquete aprobado. Las tarifas son fijas por operación, basadas en costes típicos aproximados; los costes reales varían con la longitud del contexto y la respuesta.

| Operación | Haiku 4.5 | Sonnet 4.6 |
|---|---:|---:|
| Turno de entrevista | 5 | 15 |
| Turno de aportación externa | 5 | 15 |
| Cerrar sesión | 15 | 45 |
| Reprocesar sesión | 15 | 45 |
| Reconstruir recuerdos | 15 por conversación | 45 por conversación |
| Proponer estructura autobiográfica | 15 | 45 |
| Generar capítulo | 25 | 75 |
| Mejorar capítulo | 15 | 45 |
| Generar autobiografía completa | 80 | 240 |

El servidor aplica el factor ×3 para Sonnet 4.6. Cambia los importes base desde `admin.html` y revisa `uso_ia` tras cada prueba.

## Prueba funcional

1. Entra en `admin.html`, carga la situación comercial y selecciona una cuenta de prueba.
2. Concede 1.000 créditos. Debe aparecer un saldo único de 1.000 y una fecha global de caducidad a 365 días.
3. Concede otros 500. El saldo debe pasar a 1.500 y la fecha global debe renovarse desde la segunda concesión.
4. Envía un mensaje a Lira con Haiku. Debe descontar 5 créditos; con Sonnet, 15. El indicador de la cabecera y el saldo de Cuenta deben actualizarse.
5. Cierra una sesión: 15/45 créditos según el modelo.
6. Reprocesa una sesión cerrada: 15/45 créditos.
7. Ejecuta una reconstrucción con varias conversaciones: 15/45 por conversación procesada. Al agotarse el saldo, el trabajo debe detenerse, conservar el progreso y permitir reanudarlo tras conceder más créditos.
8. Prueba estructura, generación de capítulos, mejora y autobiografía completa con las tarifas de la tabla.
9. Deja al usuario con menos créditos que el coste de una operación. La API debe devolver HTTP 402, la interfaz debe mostrar el aviso y la llamada a Anthropic no debe ejecutarse.
10. Simula un error de Anthropic. La reserva debe liberarse y el saldo debe quedar como antes.
11. Comprueba que las ediciones manuales de recuerdos y capítulos no consumen créditos.
12. Verifica en `movimientos_creditos` que cada reserva, consumo/liberación y caducidad queda registrada sin duplicados.

## Prueba de caducidad global (solo en una copia de la base de datos)

1. Concede créditos a una cuenta de prueba.
2. En la copia SQLite/SQLCipher, establece `creditos_caducidad` de esa cuenta en una fecha pasada.
3. Consulta `/api/creditos`: el saldo debe pasar a cero, la fecha global debe quedar vacía y el historial debe registrar un movimiento `caducidad`.
4. Concede créditos otra vez: debe aparecer un saldo nuevo y una fecha global futura.
5. Comprueba que el proceso de fondo también detecta vencimientos sin que el usuario consulte manualmente su saldo (el barrido se ejecuta cada hora; las operaciones comprueban la fecha en línea).
6. Comprueba la renovación: si hay 700 créditos y se conceden 500, el saldo queda en 1.200 y la fecha global se fija a 365 días desde la nueva concesión.

## Alcance pendiente

- No hay todavía compra directa, Stripe Checkout, webhooks ni renovación automática de suscripciones.
- Los créditos se conceden desde administración para probar. El futuro webhook de pago deberá invocar `conceder_creditos` con una clave idempotente derivada del identificador del pago.
- Las tarifas son fijas por operación y aproximadas; hay que contrastarlas con muestras reales de `uso_ia`, especialmente entrevistas largas y autobiografías extensas.
- No se pudo realizar en este entorno una prueba completa de arranque con las dependencias de producción y la base de datos cifrada real.
