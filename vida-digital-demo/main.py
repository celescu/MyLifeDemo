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

Si tienes disponible su autopercepción declarada (cómo se describe a sí misma en
un cuestionario), puedes usarla ocasionalmente para pedir un ejemplo concreto que
la ilustre o la contraste ("dijiste que te mantienes calmado ante un conflicto,
¿me cuentas alguna vez que lo vivieras así?"), pero no la menciones constantemente
ni la trates como un hecho — es solo cómo ella misma se ve, no una verdad
verificada.
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


CUESTIONARIO_AUTOPERCEPCION = [
    {"id": "conflicto", "pregunta": "Ante un conflicto con otra persona, ¿cómo reaccionas habitualmente?",
     "opciones": ["Confronto directamente y a veces pierdo los nervios", "Me mantengo calmado y trato el problema con raciocinio", "Evito el conflicto siempre que puedo", "Cedo para que se resuelva cuanto antes"]},
    {"id": "decisiones", "pregunta": "Cuando tienes que tomar una decisión importante, ¿qué haces normalmente?",
     "opciones": ["Decido rápido, siguiendo la intuición", "Analizo mucho antes de decidir", "Pido consejo a otras personas", "Tiendo a posponer la decisión"]},
    {"id": "energia_social", "pregunta": "¿De dónde sacas energía habitualmente?",
     "opciones": ["De estar rodeado de gente y socializar", "De pasar tiempo a solas", "De ambas por igual, depende del momento", "Me cuesta identificarlo"]},
    {"id": "incertidumbre", "pregunta": "¿Cómo te llevas con el cambio o la incertidumbre?",
     "opciones": ["Lo abrazo, me estimula", "Lo tolero razonablemente bien", "Prefiero evitarlo si puedo", "Me genera bastante ansiedad"]},
    {"id": "expresion_emocional", "pregunta": "¿Cómo expresas tus emociones habitualmente?",
     "opciones": ["Las expreso abiertamente, sin filtro", "Las guardo mayormente para mí mismo", "Las comparto solo con muy pocas personas de confianza", "A veces me cuesta identificar lo que siento"]},
    {"id": "riesgo", "pregunta": "¿Cómo te describirías respecto al riesgo?",
     "opciones": ["Busco el riesgo, me atrae", "Tomo riesgos calculados", "Soy prudente por naturaleza", "Soy muy cauteloso, evito el riesgo"]},
    {"id": "control", "pregunta": "¿Sientes que controlas el rumbo de tu vida?",
     "opciones": ["Sí, siento que depende sobre todo de mí", "Creo que depende bastante de las circunstancias", "Un poco de ambas cosas", "Depende mucho del área de mi vida de la que hablemos"]},
    {"id": "optimismo", "pregunta": "¿Cómo ves el futuro, en general?",
     "opciones": ["Con mucho optimismo", "Con optimismo cauto", "De forma neutral o realista", "Tiendo a preocuparme por lo que pueda venir"]},
    {"id": "estructura", "pregunta": "¿Cómo te llevas con la planificación y el orden?",
     "opciones": ["Me gusta planificarlo prácticamente todo", "Prefiero cierta rutina pero con flexibilidad", "Improviso la mayoría de las veces", "Evito planificar en general"]},
    {"id": "empatia", "pregunta": "A la hora de priorizar, ¿qué es más habitual en ti?",
     "opciones": ["Priorizo las necesidades de los demás", "Busco un equilibrio entre mis necesidades y las de otros", "Priorizo mis propias necesidades", "Me cuesta conectar con lo que sienten los demás"]},
    {"id": "ambicion", "pregunta": "¿Cómo describirías tu relación con las metas y el logro?",
     "opciones": ["Estoy muy orientado a conseguir metas concretas", "Tengo metas pero sin obsesionarme", "Prefiero disfrutar el presente antes que perseguir metas", "No tengo grandes metas en este momento"]},
    {"id": "novedad", "pregunta": "¿Prefieres la estabilidad o la novedad en tu día a día?",
     "opciones": ["Prefiero la estabilidad y la rutina", "Me gusta algo de novedad de vez en cuando", "Busco constantemente experiencias nuevas", "Depende mucho del momento de mi vida"]},
    {"id": "comunicacion", "pregunta": "¿Cómo describirías tu estilo de comunicación?",
     "opciones": ["Directo, digo las cosas sin rodeos", "Diplomático, cuido cómo digo las cosas", "Reservado, no suelo compartir mucho", "Expresivo, dejo ver bastante mis emociones al hablar"]},
    {"id": "fracaso", "pregunta": "¿Cómo reaccionas normalmente ante un fracaso?",
     "opciones": ["Lo asimilo rápido y sigo adelante", "Me afecta bastante y necesito tiempo para superarlo", "Tiendo a ser muy autocrítico conmigo mismo", "Tiendo a quitarle importancia"]},
    {"id": "valores", "pregunta": "Si tuvieras que elegir, ¿qué priorizarías en tu vida?",
     "opciones": ["La familia y las relaciones cercanas", "El logro personal o profesional", "La libertad y la independencia", "La seguridad y la estabilidad"]},
    {"id": "humor", "pregunta": "¿Qué papel juega el sentido del humor en tu forma de ser?",
     "opciones": ["Lo uso con mucha frecuencia", "Depende bastante de la situación", "Tiendo al humor irónico o sarcástico", "No soy muy dado al humor"]},
    {"id": "aprobacion", "pregunta": "¿Cuánto te importa lo que piensen los demás de ti?",
     "opciones": ["Muy poco, decido a partir de mí mismo", "Moderadamente, lo tengo en cuenta", "Bastante, me preocupa la opinión ajena", "Evito el conflicto para quedar bien con los demás"]},
    {"id": "espontaneidad", "pregunta": "En tu día a día, ¿cómo te organizas?",
     "opciones": ["De forma muy organizada y planificada", "Con una mezcla de planificación y espontaneidad", "Mayormente de forma espontánea", "Totalmente espontáneo, sin planificar casi nada"]},
    {"id": "colaboracion", "pregunta": "¿Prefieres trabajar solo o en equipo?",
     "opciones": ["Prefiero trabajar solo", "Prefiero trabajar en equipo", "Depende totalmente de la tarea", "Me da bastante igual una cosa u otra"]},
    {"id": "presion", "pregunta": "¿Cómo te comportas bajo presión o estrés?",
     "opciones": ["Me mantengo sereno y funciono bien", "Me tenso pero consigo rendir", "Me cuesta rendir cuando hay presión", "Evito en la medida de lo posible las situaciones de presión"]},
]


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS autopercepcion (
                usuario TEXT PRIMARY KEY,
                respuestas TEXT NOT NULL
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


class AutopercepcionIn(BaseModel):
    usuario: str = "yo"
    respuestas: dict  # { "conflicto": "texto de la opción elegida", ... }


def cargar_autopercepcion(usuario: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT respuestas FROM autopercepcion WHERE usuario = ?", (usuario,)
        ).fetchone()
    if row:
        return json.loads(row["respuestas"])
    return {}


def guardar_autopercepcion(usuario: str, respuestas: dict):
    with db() as conn:
        conn.execute(
            "INSERT INTO autopercepcion (usuario, respuestas) VALUES (?, ?) "
            "ON CONFLICT(usuario) DO UPDATE SET respuestas = excluded.respuestas",
            (usuario, json.dumps(respuestas, ensure_ascii=False)),
        )


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


def obtener_o_crear_sesion(usuario: str, sesion_id: int | None) -> tuple[int, list, str]:
    with db() as conn:
        if sesion_id is not None:
            row = conn.execute(
                "SELECT id, mensajes, fecha FROM sesiones WHERE id = ? AND usuario = ?",
                (sesion_id, usuario),
            ).fetchone()
            if row:
                return row["id"], json.loads(row["mensajes"]), row["fecha"]
        fecha = datetime.utcnow().isoformat()
        cur = conn.execute(
            "INSERT INTO sesiones (usuario, fecha, mensajes) VALUES (?, ?, ?)",
            (usuario, fecha, json.dumps([])),
        )
        return cur.lastrowid, [], fecha


def guardar_mensajes(sesion_id: int, mensajes: list):
    with db() as conn:
        conn.execute(
            "UPDATE sesiones SET mensajes = ? WHERE id = ?",
            (json.dumps(mensajes, ensure_ascii=False), sesion_id),
        )


@app.get("/api/cuestionario")
def obtener_cuestionario():
    return CUESTIONARIO_AUTOPERCEPCION


@app.get("/api/autopercepcion")
def ver_autopercepcion(usuario: str = "yo"):
    return cargar_autopercepcion(usuario)


@app.post("/api/autopercepcion")
def guardar_autopercepcion_endpoint(payload: AutopercepcionIn):
    guardar_autopercepcion(payload.usuario, payload.respuestas)
    return {"ok": True}


@app.post("/api/mensaje")
def enviar_mensaje(payload: MensajeIn):
    sesion_id, mensajes, fecha_inicio = obtener_o_crear_sesion(payload.usuario, payload.sesion_id)
    resumen = cargar_resumen(payload.usuario)

    if not mensajes:
        # primer mensaje de la sesión: inyectamos el resumen previo y la
        # autopercepción declarada (si existe) como contexto
        autopercepcion = cargar_autopercepcion(payload.usuario)
        bloque_autopercepcion = (
            f"\n\nAutopercepción que la persona ha declarado sobre sí misma "
            f"(respuestas a un cuestionario de opción múltiple, en sus propias "
            f"palabras elegidas — puedes usarlo para contrastar con anécdotas "
            f"concretas, pero no lo trates como un hecho biográfico, es su "
            f"propia forma de describirse):\n{json.dumps(autopercepcion, ensure_ascii=False)}"
            if autopercepcion else ""
        )
        contexto = (
            f"Resumen de memoria acumulado hasta ahora (JSON):\n"
            f"{json.dumps(resumen, ensure_ascii=False)}"
            f"{bloque_autopercepcion}\n\n"
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

    return {"sesion_id": sesion_id, "respuesta": texto, "fecha_inicio": fecha_inicio}


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


MARCADOR_CONTEXTO_INTERNO = "Resumen de memoria acumulado hasta ahora"


def calcular_titulo(mensajes: list) -> str:
    for m in mensajes:
        if m["role"] == "user" and not m["content"].startswith(MARCADOR_CONTEXTO_INTERNO):
            texto = m["content"].strip()
            return texto[:60] + ("…" if len(texto) > 60 else "")
    return "(sesión sin mensajes todavía)"


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
            "titulo": calcular_titulo(mensajes),
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
    mensajes = json.loads(row["mensajes"])
    # se oculta el mensaje interno de contexto (resumen previo + autopercepción)
    # que se inyecta al arrancar cada sesión: es plumbing interno, no conversación real
    mensajes_visibles = [
        m for m in mensajes
        if not (m["role"] == "user" and m["content"].startswith(MARCADOR_CONTEXTO_INTERNO))
    ]
    return mensajes_visibles


@app.get("/api/descargar-db")
def descargar_db():
    return FileResponse(
        DB_PATH,
        filename="memoria.db",
        media_type="application/octet-stream",
    )


@app.get("/api/memoria")
def ver_memoria(usuario: str = "yo"):
    return cargar_resumen(usuario)


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
