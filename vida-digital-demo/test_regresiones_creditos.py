from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main


@pytest.fixture
def cuenta(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "memoricorde-test.db"))
    main.init_db()
    usuario = "pytest_usuario"
    ahora = datetime.utcnow().isoformat()
    with main.db() as conn:
        conn.execute(
            """INSERT INTO cuentas_comerciales
               (usuario, plan_id, estado, creditos_mensuales, creditos_adicionales,
                creditos_saldo, creditos_caducidad, periodo_inicio, periodo_fin, creado, actualizado)
               VALUES (?, 'legacy', 'activo', 0, 0, 0, ?, NULL, NULL, ?, ?)""",
            (usuario, (datetime.utcnow() + timedelta(days=30)).isoformat(), ahora, ahora),
        )
    return usuario


def test_conceder_creditos_idempotente(cuenta):
    main.conceder_creditos(cuenta, 100, clave_idempotencia="concesion-unica")
    main.conceder_creditos(cuenta, 100, clave_idempotencia="concesion-unica")
    assert main.obtener_saldo_creditos(cuenta)["total"] == 100


def test_reservas_concurrentes_no_permiten_saldo_negativo(cuenta):
    main.conceder_creditos(cuenta, 100, clave_idempotencia="saldo-inicial")

    def reservar():
        try:
            return main.reservar_creditos(cuenta, "prueba", 100)
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: reservar(), range(2)))
    assert sum(isinstance(x, str) for x in resultados) == 1
    assert 402 in resultados
    assert main.obtener_saldo_creditos(cuenta)["total"] == 0


def test_caducidad_global_no_reintegra_reserva_activa(cuenta):
    main.conceder_creditos(cuenta, 100, clave_idempotencia="saldo-inicial")
    reserva = main.reservar_creditos(cuenta, "prueba", 40)
    vencida = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    with main.db() as conn:
        conn.execute("UPDATE cuentas_comerciales SET creditos_caducidad=? WHERE usuario=?", (vencida, cuenta))
    saldo = main.obtener_saldo_creditos(cuenta)
    with main.db() as conn:
        estado = conn.execute("SELECT estado FROM reservas_creditos WHERE id=?", (reserva,)).fetchone()["estado"]
    assert saldo["total"] == 0
    assert estado == "reservado"


def test_fallo_liquidacion_intenta_liberar_reserva(monkeypatch):
    liberadas = []
    respuesta = SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(main, "comprobar_creditos_servicio", lambda *a, **k: 10)
    monkeypatch.setattr(main, "reservar_creditos", lambda *a, **k: "reserva-test")
    monkeypatch.setattr(main, "llamar_a_claude", lambda **k: respuesta)
    monkeypatch.setattr(main, "liquidar_reserva_creditos", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fallo simulado")))
    monkeypatch.setattr(main, "liberar_reserva_creditos", lambda reserva, motivo: liberadas.append((reserva, motivo)))
    with pytest.raises(RuntimeError, match="fallo simulado"):
        main.llamar_a_claude_facturable("usuario", "servicio", "modelo", model="modelo")
    assert liberadas == [("reserva-test", "Fallo al liquidar")]


def test_comparador_detecta_texto_nuevo_sin_cambios_dato_y_cronologia():
    anterior = {"bloques": {"aficiones": "Le gusta leer novelas."}, "anio_nacimiento": None, "cronologia": []}
    actual = {"bloques": {"aficiones": "Le gusta leer novelas. También disfruta de la fotografía."},
              "anio_nacimiento": 1980,
              "cronologia": [{"_id": "evento-1", "anio": 2001, "momento": "juventud", "evento": "Se trasladó a Madrid"}]}
    cambios = main._comparar_resumenes_memoria(anterior, actual)
    assert any(c.get("seccion") == "bloque" and c.get("clave") == "aficiones" and c.get("añadidas") for c in cambios)
    assert any(c.get("seccion") == "dato" and c.get("nombre") == "Año de nacimiento" for c in cambios)
    assert any(c.get("seccion") == "cronologia" and c.get("tipo") == "añadido" for c in cambios)
    assert main._comparar_resumenes_memoria(actual, actual) == []
