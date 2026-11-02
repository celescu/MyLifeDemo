# Memoricorde — sistema de créditos (fase previa a Stripe)

Fecha: 2 de noviembre de 2026

## Política de créditos

- Cada usuario tiene un único saldo entero (`creditos_saldo`) y una única fecha global (`creditos_caducidad`). No hay bolsas separadas por origen.
- Cualquier concesión positiva —compra futura, ajuste administrativo, promoción o cuota de plan— se suma al saldo y renueva la fecha global a `ahora + CREDITOS_DIAS_CADUCIDAD` (365 días por defecto).
- La nueva fecha se aplica a todo el saldo acumulado. Ejemplo: 700 créditos existentes + compra de 500 = 1.200 créditos y una nueva fecha global de caducidad.
- Al alcanzar la fecha, caduca el saldo completo. Un proceso de fondo lo revisa cada hora y cada consulta/reserva también comprueba la caducidad para impedir el uso de créditos vencidos.
- Las cuentas que ya tenían saldo en las dos antiguas bolsas se migran sumando ambas cantidades. Se asigna una fecha global inicial de 365 días al saldo migrado.

## Concesión, consumo y fallos

- La administración puede conceder créditos desde `/admin.html`; la operación registra motivo, movimiento y nueva fecha de caducidad.
- `conceder_creditos()` es la función que deberá invocar el futuro webhook de Stripe cuando un pago quede confirmado. Admite una clave de idempotencia para impedir conceder dos veces los créditos de un mismo pago.
- Antes de cada llamada de IA, el servidor reserva la tarifa configurada en una transacción SQLite `BEGIN IMMEDIATE`. Si el saldo es insuficiente, devuelve HTTP 402 y no llama a Anthropic.
- Si la llamada a Anthropic falla, la reserva se libera. Si la API responde, se liquida la tarifa. El coste real de Anthropic se registra separadamente en `uso_ia`.
- Las reservas abandonadas tienen una expiración de seguridad de 120 minutos y se liberan si los créditos siguen vigentes.
- En una reconstrucción global, se cobra por cada conversación procesada. Si se agota el saldo, el proceso se detiene en estado `sin_creditos`, conserva el progreso y puede reanudarse después de conceder más créditos.
- Las operaciones manuales que no llaman a la IA (editar un recuerdo, editar texto, exportar datos, etc.) no consumen créditos.

## Tarifas iniciales orientativas

Se usa como hipótesis de trabajo un valor interno de referencia de **1 crédito ≈ 0,01 €**. No es todavía un precio de venta aprobado ni sustituye la decisión posterior sobre paquetes. Las tarifas están pensadas para cubrir el coste de API con un margen prudente; son importes fijos por operación, por lo que deben revisarse con los consumos reales de `uso_ia`.

| Operación | Haiku 4.5 | Sonnet 4.6 |
|---|---:|---:|
| Turno de entrevista | 5 | 15 |
| Turno de aportación externa | 5 | 15 |
| Cerrar sesión y actualizar recuerdos | 15 | 45 |
| Reprocesar una sesión | 15 | 45 |
| Reconstruir recuerdos | 15 por conversación | 45 por conversación |
| Proponer estructura autobiográfica | 15 | 45 |
| Generar un capítulo | 25 | 75 |
| Mejorar un capítulo | 15 | 45 |
| Generar autobiografía completa | 80 | 240 |

Sonnet 4.6 aplica un factor ×3 porque su precio estándar de API es tres veces el de Haiku 4.5 tanto en entrada como en salida. La tabla es editable desde `admin.html`; el importe que se edita es la tarifa base Haiku y el servidor aplica el factor del modelo asignado.

## Cobertura de operaciones de IA

Todas las llamadas de la aplicación a Anthropic se realizan mediante `llamar_a_claude_facturable`:

- Entrevista con Lira.
- Aportaciones externas.
- Cierre de sesión y actualización de recuerdos.
- Reprocesamiento de una sesión cerrada.
- Reconstrucción global, por conversación procesada.
- Propuesta/previsualización de estructura autobiográfica.
- Generación de capítulos.
- Mejora de capítulos.
- Generación/unificación de autobiografía completa.

El saldo y el historial se muestran en Cuenta. El saldo resumido también aparece en la cabecera y se actualiza tras operaciones facturables.

## Tablas y endpoints

Tablas comerciales: `planes`, `cuentas_comerciales`, `suscripciones`, `pagos`, `movimientos_creditos`, `reservas_creditos`, `eventos_pago` y `tarifas_servicios`.

Usuario autenticado:
- `GET /api/creditos`: plan, saldo, caducidad global, movimientos recientes.
- `GET /api/creditos/movimientos?limite=50`: historial.

Administración (requiere `admin_password`):
- `POST /api/admin/comercial/usuarios`: saldos y caducidad de las cuentas.
- `POST /api/admin/comercial/ajustar-creditos`: concede créditos al saldo único.
- `POST /api/admin/comercial/cambiar-plan`: cambia el plan.
- `POST /api/admin/comercial/tarifas`: consulta tarifas.
- `POST /api/admin/comercial/tarifas/guardar`: guarda tarifas.
- `POST /api/admin/comercial/regularizar-incidencia`: regulariza un déficit excepcional.

## Variables de entorno

```ini
CREDITOS_GRATUITOS_MENSUALES=0
CREDITOS_PREMIUM_MENSUALES=0
CREDITOS_DIAS_CADUCIDAD=365
```

Las cuotas de plan siguen en cero por defecto. La compra en línea, Stripe Checkout, Billing y webhooks no están incluidos todavía. Hasta integrar Stripe, los créditos se conceden desde administración para probar el circuito.

## Pruebas

Consultar `PRUEBAS_CREDITOS.md`. Se han realizado comprobaciones estáticas y pruebas aisladas del motor; no se ha podido ejecutar en este entorno una prueba de arranque completa con SQLCipher, Anthropic y el resto de dependencias de producción. Antes de actualizar Railway, hacer backup cifrado y prueba de arranque con `requirements.txt` y una copia de la base de datos.
