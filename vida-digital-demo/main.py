"""
Demo: entrevistador biográfico con memoria progresiva entre sesiones.

Cómo funciona:
- Cada usuario tiene sesiones de conversación.
- Al terminar una sesión, se genera/actualiza un "resumen de memoria" (JSON).
- La sesión siguiente arranca con ese resumen como contexto, no con el
  historial completo de horas anteriores -> coste y contexto controlados.

Para el demo, hay un único usuario fijo ("yo") para simplificar.
"""

import os
import sqlite3
import json
import secrets
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import anthropic

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "memoria.db"))
MODEL = "claude-sonnet-4-6"

# Usuario y contraseña para proteger el acceso a toda la app una vez esté
# publicada en internet. Se leen de variables de entorno; si no existen,
# la app no arranca protegida (solo pensado para uso local en tu propio PC).
APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

client = anthropic.Anthropic()  # usa la variable de entorno ANTHROPIC_API_KEY

app = FastAPI()


@app.middleware("http")
async def proteger_con_password(request: Request, call_next):
    # Si no se han configurado APP_USER/APP_PASSWORD, no se aplica protección
    # (esto es lo que pasa ahora mismo en tu PC local).
    if not APP_USER or not APP_PASSWORD:
        return await call_next(request)

    auth = request.headers.get("Authorization")
    if auth:
        try:
            tipo, credenciales = auth.split(" ", 1)
            import base64
            usuario, password = base64.b64decode(credenciales).decode().split(":", 1)
            if tipo == "Basic" and secrets.compare_digest(usuario, APP_USER) and secrets.compare_digest(password, APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": "Basic"},
        content="Acceso restringido",
    )

SYSTEM_PROMPT_ENTREVISTA = """Eres una entrevistadora biográfica cálida, curiosa y paciente.
Tu objetivo es ayudar a la persona a contar su vida con el máximo detalle posible,
a lo largo de muchas sesiones (no tienes que cubrir todo hoy).

Bloques temáticos a cubrir con el tiempo: infancia, familia, lugares donde vivió,
estudios, primeros trabajos, amistades, relaciones de pareja, momentos de cambio
importantes, valores y creencias, aficiones, pérdidas, logros, cómo se ve a sí misma hoy.

Reglas:
- Haz UNA pregunta a la vez, nunca varias juntas.
- Si la respuesta anterior tiene carga emocional o abre un hilo interesante, profundiza
  ahí antes de saltar a otro bloque.
- Si llevas varios turnos en un bloque, puedes pasar a otro con una transición natural.
- No inventes ni completes huecos: si no lo sabes, pregúntalo.
- Habla en un tono cercano, como una conversación real, no como un formulario.
- Ten en cuenta el resumen de memoria previo (si existe) para no repetir preguntas
  ya respondidas en sesiones anteriores, y para retomar los "temas pendientes".

Si la persona se va por las ramas pero sigue hablando de su vida o de algo relacionado
(aunque no responda exactamente a tu pregunta), síguele el hilo con naturalidad, como
haría cualquier buen conversador — no lo corrijas ni insistas en volver a tu pregunta
original si lo que cuenta es interesante.

Si en cambio la persona te pide algo que no tiene nada que ver con contar su vida
(resolver un problema de matemáticas, escribir código, traducir un texto, hacerle
de asistente general, etc.), no lo hagas. Dile con amabilidad que tu papel aquí es
acompañarla a contar su historia, no resolver ese tipo de tareas, y retoma la
conversación biográfica con una pregunta relacionada con lo último que sí contó.
"""

SYSTEM_PROMPT_RESUMEN = """Vas a recibir un resumen de memoria previo (puede estar vacío)
y la transcripción de una nueva sesión de entrevista biográfica.

Actualiza y amplía el resumen previo con lo nuevo de esta sesión, no lo sustituyas
por completo: conserva lo anterior y añade/enriquece. Usa la herramienta que tienes
disponible para guardar el resultado."""

HERRAMIENTA_RESUMEN = {
    "name": "guardar_resumen_memoria",
    "description": "Guarda el resumen actualizado de la memoria biográfica de la persona, organizado por bloques temáticos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bloques": {
                "type": "object",
                "properties": {
                    "infancia": {"type": "string", "description": "Texto narrativo con lo que se sabe hasta ahora, o vacío"},
                    "familia": {"type": "string"},
                    "lugares": {"type": "string"},
                    "estudios": {"type": "string"},
                    "trabajo": {"type": "string"},
                    "relaciones": {"type": "string"},
                    "momentos_de_cambio": {"type": "string"},
                    "valores": {"type": "string"},
                    "aficiones": {"type": "string"},
                    "otros": {"type": "string"},
                },
                "required": [
                    "infancia", "familia", "lugares", "estudios", "trabajo",
                    "relaciones", "momentos_de_cambio", "valores", "aficiones", "otros",
                ],
            },
            "temas_pendientes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista breve de temas apenas tocados o sin tocar todavía",
            },
        },
        "required": ["bloques", "temas_pendientes"],
    },
}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sesiones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT NOT NULL,
                fecha TEXT NOT NULL,
                mensajes TEXT NOT NULL,
                cerrada INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memoria (
                usuario TEXT PRIMARY KEY,
                resumen TEXT NOT NULL
            )
        """)


init_db()


class MensajeIn(BaseModel):
    usuario: str = "yo"
    mensaje: str
    sesion_id: int | None = None


class CerrarSesionIn(BaseModel):
    usuario: str = "yo"
    sesion_id: int


def cargar_resumen(usuario: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT resumen FROM memoria WHERE usuario = ?", (usuario,)
        ).fetchone()
    if row:
        return json.loads(row["resumen"])
    return {"bloques": {}, "temas_pendientes": []}


def guardar_resumen(usuario: str, resumen: dict):
    with db() as conn:
        conn.execute(
            "INSERT INTO memoria (usuario, resumen) VALUES (?, ?) "
            "ON CONFLICT(usuario) DO UPDATE SET resumen = excluded.resumen",
            (usuario, json.dumps(resumen, ensure_ascii=False)),
        )


def obtener_o_crear_sesion(usuario: str, sesion_id: int | None) -> tuple[int, list]:
    with db() as conn:
        if sesion_id is not None:
            row = conn.execute(
                "SELECT id, mensajes FROM sesiones WHERE id = ? AND usuario = ?",
                (sesion_id, usuario),
            ).fetchone()
            if row:
                return row["id"], json.loads(row["mensajes"])
        cur = conn.execute(
            "INSERT INTO sesiones (usuario, fecha, mensajes) VALUES (?, ?, ?)",
            (usuario, datetime.utcnow().isoformat(), json.dumps([])),
        )
        return cur.lastrowid, []


def guardar_mensajes(sesion_id: int, mensajes: list):
    with db() as conn:
        conn.execute(
            "UPDATE sesiones SET mensajes = ? WHERE id = ?",
            (json.dumps(mensajes, ensure_ascii=False), sesion_id),
        )


@app.post("/api/mensaje")
def enviar_mensaje(payload: MensajeIn):
    sesion_id, mensajes = obtener_o_crear_sesion(payload.usuario, payload.sesion_id)
    resumen = cargar_resumen(payload.usuario)

    if not mensajes:
        # primer mensaje de la sesión: inyectamos el resumen previo como contexto
        contexto = (
            f"Resumen de memoria acumulado hasta ahora (JSON):\n"
            f"{json.dumps(resumen, ensure_ascii=False)}\n\n"
            f"Empieza la sesión de hoy. Si hay temas_pendientes, prioriza uno de ellos "
            f"con una pregunta natural; si el resumen está vacío, empieza por la infancia."
        )
        mensajes.append({"role": "user", "content": contexto})

    mensajes.append({"role": "user", "content": payload.mensaje})

    respuesta = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT_ENTREVISTA,
        messages=mensajes,
    )
    texto = respuesta.content[0].text
    mensajes.append({"role": "assistant", "content": texto})
    guardar_mensajes(sesion_id, mensajes)

    return {"sesion_id": sesion_id, "respuesta": texto}


@app.post("/api/cerrar_sesion")
def cerrar_sesion(payload: CerrarSesionIn):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ?",
            (payload.sesion_id, payload.usuario),
        ).fetchone()
    if not row:
        return {"error": "sesión no encontrada"}

    mensajes = json.loads(row["mensajes"])
    resumen_previo = cargar_resumen(payload.usuario)

    transcripcion = "\n".join(
        f"{m['role']}: {m['content']}" for m in mensajes if m["role"] in ("user", "assistant")
    )

    # el primer mensaje "user" de la sesión es el contexto interno inyectado por el
    # sistema (el resumen previo), no algo que haya escrito la persona de verdad;
    # si no hay nada más que eso, no hay conversación real que resumir todavía
    turnos_reales = [m for m in mensajes if m["role"] == "assistant"]
    if not turnos_reales:
        return {"error": "esta sesión no tiene ninguna respuesta todavía, no hay nada que resumir"}

    respuesta = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT_RESUMEN,
        tools=[HERRAMIENTA_RESUMEN],
        tool_choice={"type": "tool", "name": "guardar_resumen_memoria"},
        messages=[{
            "role": "user",
            "content": (
                f"Resumen previo:\n{json.dumps(resumen_previo, ensure_ascii=False)}\n\n"
                f"Transcripción de la nueva sesión:\n{transcripcion}"
            ),
        }],
    )

    bloque_herramienta = next(
        (b for b in respuesta.content if b.type == "tool_use"), None
    )
    if bloque_herramienta is None:
        print(f"[AVISO] El modelo no devolvió una llamada a la herramienta para {payload.usuario}")
        print(f"[AVISO] stop_reason: {respuesta.stop_reason}, contenido: {respuesta.content}")
        return {"error": "no se pudo generar el resumen esta vez, la conversación sigue guardada íntegra"}

    nuevo_resumen = bloque_herramienta.input

    guardar_resumen(payload.usuario, nuevo_resumen)

    with db() as conn:
        conn.execute("UPDATE sesiones SET cerrada = 1 WHERE id = ?", (payload.sesion_id,))

    return {"resumen": nuevo_resumen}


@app.get("/api/sesiones")
def listar_sesiones(usuario: str = "yo"):
    with db() as conn:
        filas = conn.execute(
            "SELECT id, fecha, cerrada, mensajes FROM sesiones WHERE usuario = ? ORDER BY id DESC",
            (usuario,),
        ).fetchall()
    resultado = []
    for f in filas:
        mensajes = json.loads(f["mensajes"])
        # contamos solo turnos reales (excluyendo el mensaje de contexto inicial)
        num_turnos = len([m for m in mensajes if m["role"] == "assistant"])
        resultado.append({
            "id": f["id"],
            "fecha": f["fecha"],
            "cerrada": bool(f["cerrada"]),
            "num_turnos": num_turnos,
        })
    return resultado


@app.get("/api/sesion/{sesion_id}")
def ver_sesion(sesion_id: int, usuario: str = "yo"):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ?",
            (sesion_id, usuario),
        ).fetchone()
    if not row:
        return {"error": "sesión no encontrada"}
    return json.loads(row["mensajes"])


@app.get("/api/memoria")
def ver_memoria(usuario: str = "yo"):
    return cargar_resumen(usuario)


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
