# Memoricorde — sistema interno de créditos (fase previa a Stripe)

Fecha: 2 de noviembre de 2026

## Alcance de esta entrega

Esta versión incorpora la estructura comercial y el motor interno de créditos, pero no conecta todavía Stripe ni descuenta créditos automáticamente de las llamadas de IA. Las operaciones actuales de entrevista, recuerdos y autobiografía siguen funcionando como antes. Esta separación es deliberada: primero se valida el libro contable y la migración; después se conectará cada operación de IA y, en una fase posterior, los pagos.

## Política de cuentas

- Las cuentas que ya existían al ejecutar por primera vez la migración reciben el plan `legacy` y conservan sus modelos existentes.
- Los nuevos registros públicos reciben el plan `free` y el modelo estándar fijado por el servidor. Los modelos enviados por el navegador se ignoran.
- Las cuentas creadas desde `/admin.html` también reciben `free`.
- Los planes disponibles en la base de datos son `free`, `premium` y `legacy`.
- La cuota gratuita y Premium se configuran mediante variables de entorno. Ambas quedan a cero por defecto hasta decidir cantidades comerciales.

## Tablas añadidas

- `planes`: definición de las modalidades y sus límites.
- `cuentas_comerciales`: plan, estado, bolsas de créditos y periodo vigente.
- `suscripciones`: historial y estado de suscripciones (preparada para el proveedor futuro).
- `pagos`: registro de pagos y recargas (preparada para el proveedor futuro).
- `movimientos_creditos`: libro de movimientos, con saldos posteriores a cada movimiento.
- `reservas_creditos`: reservas, liquidaciones, liberaciones y posibles déficits.
- `eventos_pago`: identificadores de eventos del proveedor para futura idempotencia de webhooks.

Las tablas se crean mediante `CREATE TABLE IF NOT EXISTS`. La migración no elimina ni modifica las tablas biográficas existentes.

## Motor interno

Funciones disponibles en `main.py`:

- `obtener_saldo_creditos(usuario)`
- `conceder_creditos(usuario, cantidad, bolsa, ...)`
- `renovar_creditos_mensuales(usuario, periodo_inicio, periodo_fin, ...)`
- `reservar_creditos(usuario, operacion, cantidad, ...)`
- `liquidar_reserva_creditos(reserva_id, coste_real)`
- `liberar_reserva_creditos(reserva_id, motivo)`
- `regularizar_incidencia_creditos(reserva_id)`
- `coste_creditos_desde_usd(coste_usd)`

Reglas: se consumen primero los créditos mensuales y después los adicionales; las renovaciones extinguen únicamente el saldo mensual anterior; los adicionales no caducan; las reservas y movimientos se procesan en transacciones SQLite; las claves de idempotencia evitan duplicar concesiones y reservas. Si el coste real supera la reserva y no existe saldo para cubrirlo, queda registrada una incidencia y se bloquean nuevas reservas hasta regularizarla.

## Endpoints incorporados

Usuario autenticado:

- `GET /api/creditos`: plan, saldo, reservas pendientes y últimos movimientos.
- `GET /api/creditos/movimientos?limite=50`: historial paginado por límite (máximo 200).

Administración (requieren `admin_password`):

- `POST /api/admin/comercial/usuarios`: lista de usuarios y sus saldos.
- `POST /api/admin/comercial/ajustar-creditos`: concesión manual de créditos con motivo obligatorio.
- `POST /api/admin/comercial/cambiar-plan`: cambio manual de plan y apertura de un nuevo periodo de 30 días con la cuota configurada.
- `POST /api/admin/comercial/regularizar-incidencia`: regulariza un déficit si ya existe saldo suficiente.

El panel `/admin.html` incluye consulta de cuentas comerciales, cambio de plan y concesión manual de créditos. La sección Cuenta de `/app.html` muestra el plan, los saldos y los últimos movimientos.

## Variables de entorno nuevas

```ini
CREDITOS_GRATUITOS_MENSUALES=0
CREDITOS_PREMIUM_MENSUALES=0
CREDITOS_FACTOR_COSTE=1.0
CREDITOS_USD_POR_UNIDAD=0.01
```

La conversión de coste estimado de Anthropic a créditos es configurable. No deben fijarse cuotas ni valor económico definitivo hasta analizar los costes históricos de `uso_ia` y decidir precios y márgenes.

## Lo que queda expresamente para la siguiente fase

1. Integrar reservas y liquidaciones en cada llamada de IA (entrevista, cierre, aportaciones, reconstrucción, reprocesamiento y autobiografía).
2. Definir cuotas, límites y reglas comerciales definitivas.
3. Incorporar la renovación periódica automática de la cuota gratuita y la recuperación de reservas expiradas.
4. Integrar Stripe Checkout, Billing y webhooks firmados e idempotentes.
5. Definir la política fiscal, de reembolsos y retención de documentos de facturación.

## Pruebas realizadas en esta entrega

- Compilación sintáctica de `main.py` con `py_compile`.
- Validación de sintaxis JavaScript de `app.html`, `registro.html` y `admin.html` con Node.js.
- Ejecución de las sentencias DDL de las siete tablas comerciales sobre SQLite.
- Pruebas aisladas del motor de créditos: concesión, reserva mensual-primero, liquidación con devolución a la bolsa correcta, renovación idempotente, liberación, rechazo por saldo insuficiente, incidencia por exceso y regularización.

No se ha podido ejecutar una prueba de extremo a extremo del servidor FastAPI en este entorno porque no están instaladas aquí las dependencias de producción `sqlcipher3-binary`, `anthropic`, `bcrypt` y `slowapi`. Por tanto, antes de producción debe hacerse una prueba de arranque con `requirements.txt` y una copia de la base de datos real.

## Despliegue seguro

1. Descargar un backup completo de la base de datos cifrada de Railway.
2. Conservar el ZIP de la versión anterior.
3. Desplegar esta versión sin sustituir el volumen ni copiar una base local sobre producción.
4. Revisar los logs y comprobar que la aplicación arranca.
5. Verificar que los usuarios existentes aparecen como `legacy` y mantienen sus recuerdos, sesiones y modelos.
6. Crear una cuenta de prueba y comprobar que aparece como `free`.
7. Revisar `/api/creditos` y el apartado Cuenta.
8. No activar todavía cobros ni consumo automático de créditos.
