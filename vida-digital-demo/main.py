"""
Entrevistador biográfico con memoria progresiva entre sesiones.

Seguridad:
- Cifrado en reposo con SQLCipher (clave en variable de entorno).
- HTTPS forzado en producción + cabeceras de seguridad.
- Cookies httponly, secure solo en producción, samesite=lax.
- Rate limiting en login y endpoints de administración.
- Login en tiempo constante (no filtra si el usuario existe o no).
- Validación de contraseñas y nombres de usuario al crear cuentas.
- Migración atómica de bases de datos sin cifrar a SQLCipher.
- Backups automáticos cifrados cada 24h, con rotación.
"""

import os
import sqlite3
import json
import asyncio
import secrets
import time
import bcrypt
from datetime import datetime
from contextlib import contextmanager, asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

import sqlcipher3  # cifrado en reposo de la base de datos

from fastapi import FastAPI, Request, Response, Depends, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import anthropic

# ---------------------------------------------------------------------------
# Configuración y secretos
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "memoria.db"))
MODEL = "claude-sonnet-4-6"

PRECIO_INPUT_POR_MILLON = 2.0
PRECIO_OUTPUT_POR_MILLON = 10.0

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "yo")

ENTORNO = os.environ.get("ENTORNO", "development").lower()

DB_ENCRYPTION_KEY = os.environ.get("DB_ENCRYPTION_KEY")
if not DB_ENCRYPTION_KEY or len(DB_ENCRYPTION_KEY) < 32:
    raise RuntimeError(
        "Falta la variable de entorno DB_ENCRYPTION_KEY, o es demasiado corta "
        "(mínimo 32 caracteres). Genérala con: "
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
        "y configúrala antes de arrancar."
    )

if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
    raise RuntimeError(
        "Falta ADMIN_PASSWORD o es demasiado corta (mínimo 12 caracteres)."
    )


def _clave_hex() -> str:
    """SQLCipher acepta la clave en formato hexadecimal entre comillas."""
    return DB_ENCRYPTION_KEY.encode("utf-8").hex()


# Usa la variable de entorno ANTHROPIC_API_KEY automáticamente.
client = anthropic.Anthropic(
    timeout=60.0,   # por defecto; se amplía por llamada donde hace falta más margen
    max_retries=1,  # menos reintentos automáticos silenciosos: si falla, falla rápido y claro
)

limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Ciclo de vida (lifespan) — sustituye al obsoleto @app.on_event("startup")
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    tarea_backups = asyncio.create_task(bucle_backups_automaticos())
    try:
        yield
    finally:
        tarea_backups.cancel()


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Middleware de seguridad
# ---------------------------------------------------------------------------

@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    # Forzar HTTPS en producción (Railway pone x-forwarded-proto).
    if ENTORNO == "production":
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if proto != "https":
            url = request.url.replace(scheme="https")
            return JSONResponse(
                status_code=301,
                content={"detail": "Redirigiendo a HTTPS"},
                headers={"Location": str(url)},
            )

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(self)"

    if ENTORNO == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # CSP: permite scripts/estilos inline que usa la propia app, SVG inline
    # del gráfico de métricas (no requiere img-src porque va en el DOM).
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


if ENTORNO == "production":
    hosts = [h.strip() for h in os.environ.get("HOSTS_PERMITIDOS", "").split(",") if h.strip()]
    if hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)


# ---------------------------------------------------------------------------
# Base de datos cifrada con SQLCipher
# ---------------------------------------------------------------------------

@contextmanager
def db():
    conn = sqlcipher3.connect(DB_PATH)
    conn.row_factory = sqlcipher3.Row
    conn.execute(f"PRAGMA key = \"x'{_clave_hex()}'\";")
    conn.execute("PRAGMA cipher_page_size = 4096;")
    conn.execute("PRAGMA kdf_iter = 256000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db_sobre(conn):
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            creada TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sesiones_login (
            token TEXT PRIMARY KEY,
            usuario TEXT NOT NULL,
            creada TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autobiografia (
            usuario TEXT PRIMARY KEY,
            contenido TEXT NOT NULL,
            fecha_generada TEXT NOT NULL,
            tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0
        )
    """)
    columnas_existentes = {fila[1] for fila in conn.execute("PRAGMA table_info(sesiones)").fetchall()}
    for columna, definicion in [
        ("fecha_cierre", "TEXT"),
        ("tokens_input", "INTEGER DEFAULT 0"),
        ("tokens_output", "INTEGER DEFAULT 0"),
    ]:
        if columna not in columnas_existentes:
            conn.execute(f"ALTER TABLE sesiones ADD COLUMN {columna} {definicion}")


def init_db():
    with db() as conn:
        init_db_sobre(conn)


def migrar_a_cifrado_si_es_necesario():
    """
    Si memoria.db existe y está en SQLite normal (sin cifrar), lo convierte
    a SQLCipher conservando todos los datos. Se ejecuta una sola vez al
    arrancar; si la base ya está cifrada, no hace nada.

    El volcado se hace sobre un archivo temporal y se sustituye con
    os.replace() al final, de modo que un fallo a mitad no deja la base
    a medias: o queda la original, o queda la cifrada completa.
    """
    if not os.path.exists(DB_PATH):
        return  # no hay nada que migrar, se creará cifrada desde cero

    with open(DB_PATH, "rb") as f:
        cabecera = f.read(16)

    if not cabecera.startswith(b"SQLite format 3\x00"):
        return  # ya está cifrada

    print("[cifrado] Detectada base de datos sin cifrar. Migrando a SQLCipher...")
    ruta_backup_plano = DB_PATH + ".sin_cifrar.backup"

    # 1. Copia de seguridad del original
    with open(DB_PATH, "rb") as origen, open(ruta_backup_plano, "wb") as destino:
        destino.write(origen.read())

    # 2. Leer todos los datos con sqlite3 normal
    conn_origen = sqlite3.connect(DB_PATH)
    conn_origen.row_factory = sqlite3.Row
    tablas = conn_origen.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    datos_por_tabla = {}
    for t in tablas:
        nombre = t["name"]
        filas = conn_origen.execute(f"SELECT * FROM {nombre}").fetchall()
        datos_por_tabla[nombre] = [dict(f) for f in filas]
    conn_origen.close()

    # 3. Crear una base de datos nueva, cifrada, en un archivo temporal
    ruta_temporal = DB_PATH + ".cifrado.tmp"
    if os.path.exists(ruta_temporal):
        os.remove(ruta_temporal)

    conn_destino = sqlcipher3.connect(ruta_temporal)
    conn_destino.execute(f"PRAGMA key = \"x'{_clave_hex()}'\";")
    conn_destino.execute("PRAGMA cipher_page_size = 4096;")
    conn_destino.execute("PRAGMA kdf_iter = 256000;")
    init_db_sobre(conn_destino)

    for nombre, filas in datos_por_tabla.items():
        if not filas:
            continue
        columnas = list(filas[0].keys())
        placeholders = ", ".join("?" for _ in columnas)
        sql = f"INSERT INTO {nombre} ({', '.join(columnas)}) VALUES ({placeholders})"
        for fila in filas:
            conn_destino.execute(sql, tuple(fila[c] for c in columnas))
    conn_destino.commit()
    conn_destino.close()

    # 4. Sustitución atómica: si algo falla antes de aquí, el original sigue
    #    intacto en DB_PATH, y el .cifrado.tmp se descarta al arrancar de nuevo.
    os.replace(ruta_temporal, DB_PATH)

    print(f"[cifrado] Migración completada. Copia sin cifrar en: {ruta_backup_plano}")
    print("[cifrado] IMPORTANTE: bórrala en cuanto confirmes que todo funciona.")


migrar_a_cifrado_si_es_necesario()
init_db()


# ---------------------------------------------------------------------------
# Backups automáticos
# ---------------------------------------------------------------------------

CARPETA_BACKUPS_AUTOMATICOS = os.path.join(os.path.dirname(DB_PATH), "backups_automaticos")
NUM_BACKUPS_AUTOMATICOS_A_CONSERVAR = 7
INTERVALO_BACKUP_SEGUNDOS = 24 * 60 * 60


def hacer_backup_automatico():
    os.makedirs(CARPETA_BACKUPS_AUTOMATICOS, exist_ok=True)
    marca_tiempo = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S_%f")
    ruta_backup = os.path.join(CARPETA_BACKUPS_AUTOMATICOS, f"memoria_{marca_tiempo}.db")

    origen = sqlcipher3.connect(DB_PATH)
    origen.execute(f"PRAGMA key = \"x'{_clave_hex()}'\";")
    destino = sqlcipher3.connect(ruta_backup)
    destino.execute(f"PRAGMA key = \"x'{_clave_hex()}'\";")
    origen.backup(destino)
    destino.close()
    origen.close()

    backups_existentes = sorted(os.listdir(CARPETA_BACKUPS_AUTOMATICOS))
    while len(backups_existentes) > NUM_BACKUPS_AUTOMATICOS_A_CONSERVAR:
        os.remove(os.path.join(CARPETA_BACKUPS_AUTOMATICOS, backups_existentes.pop(0)))

    print(f"[backup] Copia automática creada: {ruta_backup}")


async def bucle_backups_automaticos():
    while True:
        try:
            await asyncio.to_thread(hacer_backup_automatico)
        except Exception as e:
            print(f"[backup] Error al generar la copia automática: {e}")
        await asyncio.sleep(INTERVALO_BACKUP_SEGUNDOS)


def nombre_backup_valido(nombre: str) -> bool:
    return (
        nombre.startswith("memoria_")
        and nombre.endswith(".db")
        and "/" not in nombre
        and "\\" not in nombre
        and ".." not in nombre
    )


# ---------------------------------------------------------------------------
# Modelos con validación
# ---------------------------------------------------------------------------

class MensajeIn(BaseModel):
    mensaje: str = Field(..., min_length=1, max_length=8000)
    sesion_id: int | None = None


class CerrarSesionIn(BaseModel):
    sesion_id: int


class AutopercepcionIn(BaseModel):
    respuestas: dict


class LoginIn(BaseModel):
    usuario: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class CrearUsuarioIn(BaseModel):
    usuario: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=10, max_length=256)
    admin_password: str

    @field_validator("usuario")
    @classmethod
    def usuario_valido(cls, v: str) -> str:
        if not all(c.isalnum() or c in "._-" for c in v):
            raise ValueError("El usuario solo puede contener letras, números, punto, guion y guion bajo")
        return v

    @field_validator("password")
    @classmethod
    def password_minima(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("La contraseña debe tener al menos 10 caracteres")
        if v.lower() in {"1234567890", "password", "contraseña", "qwertyuiop", "0000000000"}:
            raise ValueError("Contraseña demasiado común")
        return v


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

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
        resumen = json.loads(row["resumen"])
        resumen.setdefault("cronologia", [])
        resumen.setdefault("anio_nacimiento", None)
        return resumen
    return {"bloques": {}, "cronologia": [], "anio_nacimiento": None, "temas_pendientes": []}


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


def sumar_tokens(sesion_id: int, tokens_input: int, tokens_output: int):
    with db() as conn:
        conn.execute(
            "UPDATE sesiones SET tokens_input = tokens_input + ?, "
            "tokens_output = tokens_output + ? WHERE id = ?",
            (tokens_input, tokens_output, sesion_id),
        )


def marcar_cerrada(sesion_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE sesiones SET cerrada = 1, fecha_cierre = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), sesion_id),
        )


def exportar_datos_usuario(usuario: str) -> dict:
    with db() as conn:
        sesiones = conn.execute("SELECT * FROM sesiones WHERE usuario = ?", (usuario,)).fetchall()
        memoria = conn.execute("SELECT * FROM memoria WHERE usuario = ?", (usuario,)).fetchone()
        autoperc = conn.execute("SELECT * FROM autopercepcion WHERE usuario = ?", (usuario,)).fetchone()
        autobio = conn.execute("SELECT * FROM autobiografia WHERE usuario = ?", (usuario,)).fetchone()
    return {
        "formato": "backup-individual-v1",
        "usuario": usuario,
        "exportado_el": datetime.utcnow().isoformat(),
        "sesiones": [dict(row) for row in sesiones],
        "memoria": dict(memoria) if memoria else None,
        "autopercepcion": dict(autoperc) if autoperc else None,
        "autobiografia": dict(autobio) if autobio else None,
    }


def borrar_datos_usuario(usuario: str):
    with db() as conn:
        conn.execute("DELETE FROM sesiones WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM memoria WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM autopercepcion WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM autobiografia WHERE usuario = ?", (usuario,))


def restaurar_datos_usuario(usuario: str, datos: dict):
    borrar_datos_usuario(usuario)
    with db() as conn:
        for s in datos.get("sesiones") or []:
            conn.execute(
                "INSERT INTO sesiones (usuario, fecha, mensajes, cerrada, fecha_cierre, tokens_input, tokens_output) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    usuario, s.get("fecha"), s.get("mensajes"), s.get("cerrada", 0),
                    s.get("fecha_cierre"), s.get("tokens_input", 0), s.get("tokens_output", 0),
                ),
            )
        m = datos.get("memoria")
        if m:
            conn.execute("INSERT INTO memoria (usuario, resumen) VALUES (?, ?)", (usuario, m.get("resumen")))
        a = datos.get("autopercepcion")
        if a:
            conn.execute("INSERT INTO autopercepcion (usuario, respuestas) VALUES (?, ?)", (usuario, a.get("respuestas")))
        ab = datos.get("autobiografia")
        if ab:
            conn.execute(
                "INSERT INTO autobiografia (usuario, contenido, fecha_generada, tokens_input, tokens_output) "
                "VALUES (?, ?, ?, ?, ?)",
                (usuario, ab.get("contenido"), ab.get("fecha_generada"),
                 ab.get("tokens_input", 0), ab.get("tokens_output", 0)),
            )


def verificar_password_de(usuario: str, password: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM usuarios WHERE usuario = ?", (usuario,)
        ).fetchone()
    if not row:
        return False
    return bcrypt.checkpw(password.encode(), row["password_hash"].encode())


def requiere_admin(admin_password: str):
    if not ADMIN_PASSWORD or not secrets.compare_digest(admin_password, ADMIN_PASSWORD):
        # Pequeño retardo para dificultar la fuerza bruta por tiempo de respuesta
        time.sleep(0.5)
        raise HTTPException(status_code=403, detail="Contraseña de administrador incorrecta")


def obtener_usuario_actual(request: Request) -> str:
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="No has iniciado sesión")
    with db() as conn:
        row = conn.execute(
            "SELECT usuario FROM sesiones_login WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Tu sesión ha caducado, inicia sesión de nuevo")
    return row["usuario"]


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

@app.get("/api/whoami")
def whoami(usuario: str = Depends(obtener_usuario_actual)):
    return {"usuario": usuario}


@app.post("/api/login")
@limiter.limit("5/minute")
def login(request: Request, payload: LoginIn, response: Response):
    with db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM usuarios WHERE usuario = ?", (payload.usuario,)
        ).fetchone()

    # Comparación en tiempo constante: aunque el usuario no exista, gastamos
    # un bcrypt.checkpw contra un hash dummy, para no filtrar por tiempos de
    # respuesta si el usuario existe o no.
    hash_a_comparar = row["password_hash"] if row else bcrypt.hashpw(b"dummy", bcrypt.gensalt()).decode()
    password_ok = bcrypt.checkpw(payload.password.encode(), hash_a_comparar.encode())

    if not row or not password_ok:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO sesiones_login (token, usuario, creada) VALUES (?, ?, ?)",
            (token, payload.usuario, datetime.utcnow().isoformat()),
        )
    response.set_cookie(
        key="session_token", value=token,
        httponly=True,
        secure=(ENTORNO == "production"),
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return {"ok": True, "usuario": payload.usuario}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sesiones_login WHERE token = ?", (token,))
    response.delete_cookie("session_token", path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Administración
# ---------------------------------------------------------------------------

@app.post("/api/admin/crear-usuario")
@limiter.limit("10/hour")
def crear_usuario(request: Request, payload: CrearUsuarioIn):
    requiere_admin(payload.admin_password)

    password_hash = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()
    with db() as conn:
        existe = conn.execute(
            "SELECT 1 FROM usuarios WHERE usuario = ?", (payload.usuario,)
        ).fetchone()
        if existe:
            raise HTTPException(status_code=400, detail="Ese nombre de usuario ya existe")
        conn.execute(
            "INSERT INTO usuarios (usuario, password_hash, creada) VALUES (?, ?, ?)",
            (payload.usuario, password_hash, datetime.utcnow().isoformat()),
        )
    return {"ok": True}


@app.get("/api/cuestionario")
def obtener_cuestionario(usuario: str = Depends(obtener_usuario_actual)):
    return CUESTIONARIO_AUTOPERCEPCION


@app.get("/api/autopercepcion")
def ver_autopercepcion(usuario: str = Depends(obtener_usuario_actual)):
    return cargar_autopercepcion(usuario)


@app.post("/api/autopercepcion")
def guardar_autopercepcion_endpoint(payload: AutopercepcionIn, usuario: str = Depends(obtener_usuario_actual)):
    guardar_autopercepcion(usuario, payload.respuestas)
    return {"ok": True}


@app.post("/api/mensaje")
def enviar_mensaje(payload: MensajeIn, usuario: str = Depends(obtener_usuario_actual)):
    sesion_id, mensajes, fecha_inicio = obtener_o_crear_sesion(usuario, payload.sesion_id)
    resumen = cargar_resumen(usuario)

    if not mensajes:
        autopercepcion = cargar_autopercepcion(usuario)
        bloque_autopercepcion = (
            f"\n\nAutopercepción que la persona ha declarado sobre sí misma "
            f"(respuestas a un cuestionario de opción múltiple, en sus propias "
            f"palabras elegidas — puedes usarlo para contrastar con anécdotas "
            f"concretas, pero no lo trates como un hecho biográfico, es su "
            f"propia forma de describirse):\n{json.dumps(autopercepcion, ensure_ascii=False)}"
            if autopercepcion else ""
        )
        contexto = (
            f"Año actual real: {datetime.utcnow().year}\n\n"
            f"Resumen de memoria acumulado hasta ahora (JSON, incluye anio_nacimiento "
            f"si ya se conoce):\n"
            f"{json.dumps(resumen, ensure_ascii=False)}"
            f"{bloque_autopercepcion}\n\n"
            f"Empieza la sesión de hoy. Si hay temas_pendientes, prioriza uno de ellos "
            f"con una pregunta natural; si el resumen está vacío, empieza por la infancia."
        )
        mensajes.append({"role": "user", "content": contexto})

    mensajes.append({"role": "user", "content": payload.mensaje})

    _inicio = time.perf_counter()
    respuesta = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT_ENTREVISTA,
        messages=mensajes,
        timeout=45.0,  # una respuesta de entrevista debe ser rápida; si tarda más, algo va mal
    )
    print(f"[tiempos] enviar_mensaje ({usuario}, sesión {sesion_id}): {time.perf_counter() - _inicio:.1f}s")
    texto = respuesta.content[0].text
    mensajes.append({"role": "assistant", "content": texto})
    guardar_mensajes(sesion_id, mensajes)
    sumar_tokens(sesion_id, respuesta.usage.input_tokens, respuesta.usage.output_tokens)

    if texto.startswith(MARCADOR_CIERRE_USO_INDEBIDO):
        texto_visible = texto[len(MARCADOR_CIERRE_USO_INDEBIDO):].strip()
        marcar_cerrada(sesion_id)
        print(f"[AVISO] Sesión {sesion_id} cerrada automáticamente por uso indebido (usuario: {usuario})")
        return {
            "sesion_id": sesion_id,
            "respuesta": texto_visible,
            "fecha_inicio": fecha_inicio,
            "cerrada_por_uso_indebido": True,
        }

    return {"sesion_id": sesion_id, "respuesta": texto, "fecha_inicio": fecha_inicio}


@app.post("/api/cerrar_sesion")
def cerrar_sesion(payload: CerrarSesionIn, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ?",
            (payload.sesion_id, usuario),
        ).fetchone()
    if not row:
        return {"error": "sesión no encontrada"}

    mensajes = json.loads(row["mensajes"])
    resumen_previo = cargar_resumen(usuario)

    transcripcion = "\n".join(
        f"{m['role']}: {m['content']}" for m in mensajes if m["role"] in ("user", "assistant")
    )

    turnos_reales = [m for m in mensajes if m["role"] == "assistant"]
    if not turnos_reales:
        return {"error": "esta sesión no tiene ninguna respuesta todavía, no hay nada que resumir"}

    _inicio = time.perf_counter()
    try:
        respuesta = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT_RESUMEN,
            tools=[HERRAMIENTA_RESUMEN],
            tool_choice={"type": "tool", "name": "guardar_resumen_memoria"},
            messages=[{
                "role": "user",
                "content": (
                    f"Año actual real: {datetime.utcnow().year}\n"
                    f"Año de nacimiento ya conocido (null si aún no se sabe): "
                    f"{json.dumps(resumen_previo.get('anio_nacimiento'))}\n\n"
                    f"Resumen previo:\n{json.dumps(resumen_previo, ensure_ascii=False)}\n\n"
                    f"Transcripción de la nueva sesión:\n{transcripcion}"
                ),
            }],
            timeout=100.0,  # el resumen puede tardar más que un mensaje normal, pero no infinito
        )
    except anthropic.APITimeoutError:
        print(f"[tiempos] cerrar_sesion ({usuario}, sesión {payload.sesion_id}): TIMEOUT tras {time.perf_counter() - _inicio:.1f}s")
        return {"error": "la generación del resumen ha tardado demasiado y se ha cancelado; la conversación sigue guardada íntegra, puedes reintentar cerrar esta sesión más tarde"}
    print(f"[tiempos] cerrar_sesion ({usuario}, sesión {payload.sesion_id}): {time.perf_counter() - _inicio:.1f}s")

    bloque_herramienta = next(
        (b for b in respuesta.content if b.type == "tool_use"), None
    )
    if bloque_herramienta is None:
        print(f"[AVISO] El modelo no devolvió una llamada a la herramienta para {usuario}")
        print(f"[AVISO] stop_reason: {respuesta.stop_reason}, contenido: {respuesta.content}")
        return {"error": "no se pudo generar el resumen esta vez, la conversación sigue guardada íntegra"}

    nuevo_resumen = bloque_herramienta.input
    validar_coherencia_fechas(nuevo_resumen, usuario)

    guardar_resumen(usuario, nuevo_resumen)
    sumar_tokens(payload.sesion_id, respuesta.usage.input_tokens, respuesta.usage.output_tokens)
    marcar_cerrada(payload.sesion_id)

    return {"resumen": nuevo_resumen}


def validar_coherencia_fechas(resumen: dict, usuario: str):
    """
    No corrige nada automáticamente (podría estar equivocándose sobre lo que
    corrige), solo deja constancia en los logs si algún año de la cronología
    no encaja con el año de nacimiento o con el año actual — para poder
    revisarlo si los errores de fechas persisten pese al resto de medidas.
    """
    anio_nacimiento = resumen.get("anio_nacimiento")
    anio_actual = datetime.utcnow().year
    for evento in resumen.get("cronologia", []):
        anio = evento.get("anio")
        if anio is None:
            continue
        if anio_nacimiento is not None and anio < anio_nacimiento:
            print(f"[AVISO] Posible error de fechas para {usuario}: evento '{evento.get('evento')}' "
                  f"con año {anio}, anterior al año de nacimiento {anio_nacimiento}")
        if anio > anio_actual:
            print(f"[AVISO] Posible error de fechas para {usuario}: evento '{evento.get('evento')}' "
                  f"con año {anio}, posterior al año actual {anio_actual}")


MARCADOR_CONTEXTO_INTERNO = "Resumen de memoria acumulado hasta ahora"
MARCADOR_CIERRE_USO_INDEBIDO = "[CIERRE_POR_USO_INDEBIDO]"


def calcular_titulo(mensajes: list) -> str:
    for m in mensajes:
        if m["role"] == "user" and not m["content"].startswith(MARCADOR_CONTEXTO_INTERNO):
            texto = m["content"].strip()
            return texto[:60] + ("…" if len(texto) > 60 else "")
    return "(sesión sin mensajes todavía)"


@app.get("/api/sesiones")
def listar_sesiones(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        filas = conn.execute(
            "SELECT id, fecha, cerrada, mensajes FROM sesiones WHERE usuario = ? ORDER BY id DESC",
            (usuario,),
        ).fetchall()
    resultado = []
    for f in filas:
        mensajes = json.loads(f["mensajes"])
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
def ver_sesion(sesion_id: int, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ?",
            (sesion_id, usuario),
        ).fetchone()
    if not row:
        return {"error": "sesión no encontrada"}
    mensajes = json.loads(row["mensajes"])
    mensajes_visibles = [
        m for m in mensajes
        if not (m["role"] == "user" and m["content"].startswith(MARCADOR_CONTEXTO_INTERNO))
    ]
    return mensajes_visibles


def contar_palabras_usuario(mensajes: list) -> int:
    total = 0
    for m in mensajes:
        if m["role"] == "user" and not m["content"].startswith(MARCADOR_CONTEXTO_INTERNO):
            total += len(m["content"].split())
    return total


@app.get("/api/metricas")
def obtener_metricas(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        filas = conn.execute(
            "SELECT id, fecha, fecha_cierre, cerrada, mensajes, tokens_input, tokens_output "
            "FROM sesiones WHERE usuario = ? ORDER BY id ASC",
            (usuario,),
        ).fetchall()

    sesiones = []
    total_tokens_input = 0
    total_tokens_output = 0
    total_palabras = 0
    total_segundos = 0

    for f in filas:
        mensajes = json.loads(f["mensajes"])
        palabras = contar_palabras_usuario(mensajes)

        inicio = datetime.fromisoformat(f["fecha"])
        if f["fecha_cierre"]:
            fin = datetime.fromisoformat(f["fecha_cierre"])
        else:
            fin = datetime.utcnow()
        duracion_segundos = max(0, int((fin - inicio).total_seconds()))

        coste = (
            (f["tokens_input"] or 0) / 1_000_000 * PRECIO_INPUT_POR_MILLON
            + (f["tokens_output"] or 0) / 1_000_000 * PRECIO_OUTPUT_POR_MILLON
        )

        sesiones.append({
            "id": f["id"],
            "titulo": calcular_titulo(mensajes),
            "fecha": f["fecha"],
            "cerrada": bool(f["cerrada"]),
            "tokens_input": f["tokens_input"] or 0,
            "tokens_output": f["tokens_output"] or 0,
            "palabras_usuario": palabras,
            "duracion_segundos": duracion_segundos,
            "coste_estimado": round(coste, 4),
        })

        total_tokens_input += f["tokens_input"] or 0
        total_tokens_output += f["tokens_output"] or 0
        total_palabras += palabras
        total_segundos += duracion_segundos

    total_coste = (
        total_tokens_input / 1_000_000 * PRECIO_INPUT_POR_MILLON
        + total_tokens_output / 1_000_000 * PRECIO_OUTPUT_POR_MILLON
    )

    with db() as conn:
        fila_autobio = conn.execute(
            "SELECT tokens_input, tokens_output FROM autobiografia WHERE usuario = ?", (usuario,)
        ).fetchone()
    tokens_input_autobio = fila_autobio["tokens_input"] if fila_autobio else 0
    tokens_output_autobio = fila_autobio["tokens_output"] if fila_autobio else 0
    coste_autobio = (
        tokens_input_autobio / 1_000_000 * PRECIO_INPUT_POR_MILLON
        + tokens_output_autobio / 1_000_000 * PRECIO_OUTPUT_POR_MILLON
    )

    return {
        "sesiones": sesiones,
        "totales": {
            "num_sesiones": len(sesiones),
            "tokens_input": total_tokens_input + tokens_input_autobio,
            "tokens_output": total_tokens_output + tokens_output_autobio,
            "palabras_usuario": total_palabras,
            "duracion_segundos": total_segundos,
            "coste_estimado": round(total_coste + coste_autobio, 4),
            "coste_autobiografia": round(coste_autobio, 4),
        },
    }


@app.get("/api/descargar-db")
def descargar_db(usuario: str = Depends(obtener_usuario_actual)):
    if usuario != ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="Solo el administrador puede descargar la base de datos completa")
    return FileResponse(
        DB_PATH,
        filename="memoria.db",
        media_type="application/octet-stream",
    )


@app.post("/api/admin/restaurar-db")
@limiter.limit("3/hour")
async def restaurar_db(
    request: Request,
    admin_password: str = Form(...),
    archivo: UploadFile = File(...),
):
    requiere_admin(admin_password)

    contenido = await archivo.read()
    ruta_temporal = DB_PATH + ".restaurando.tmp"
    with open(ruta_temporal, "wb") as f:
        f.write(contenido)

    es_sqlite_plano = contenido.startswith(b"SQLite format 3\x00")

    if not es_sqlite_plano:
        try:
            conn_prueba = sqlcipher3.connect(ruta_temporal)
            conn_prueba.execute(f"PRAGMA key = \"x'{_clave_hex()}'\";")
            conn_prueba.execute("SELECT count(*) FROM sqlite_master;")
            conn_prueba.close()
        except Exception:
            os.remove(ruta_temporal)
            raise HTTPException(
                status_code=400,
                detail="El archivo no es una base de datos válida, o está cifrado con una clave distinta a la actual",
            )

    os.replace(ruta_temporal, DB_PATH)
    migrar_a_cifrado_si_es_necesario()
    init_db()

    return {"ok": True, "mensaje": "Base de datos restaurada correctamente"}


@app.get("/api/mis-datos/exportar")
@limiter.limit("10/hour")
def exportar_mis_datos(request: Request, usuario: str = Depends(obtener_usuario_actual)):
    return exportar_datos_usuario(usuario)


@app.post("/api/mis-datos/restaurar")
@limiter.limit("5/hour")
async def restaurar_mis_datos(
    request: Request,
    password: str = Form(...),
    archivo: UploadFile = File(...),
    usuario: str = Depends(obtener_usuario_actual),
):
    if not verificar_password_de(usuario, password):
        time.sleep(0.5)
        raise HTTPException(status_code=403, detail="Contraseña incorrecta")
    try:
        datos = json.loads(await archivo.read())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="El archivo no es un backup válido")
    restaurar_datos_usuario(usuario, datos)
    return {"ok": True, "mensaje": "Tus datos han sido restaurados"}


@app.post("/api/mis-datos/borrar")
@limiter.limit("5/hour")
def borrar_mis_datos(request: Request, password: str = Form(...), usuario: str = Depends(obtener_usuario_actual)):
    if not verificar_password_de(usuario, password):
        time.sleep(0.5)
        raise HTTPException(status_code=403, detail="Contraseña incorrecta")
    borrar_datos_usuario(usuario)
    return {"ok": True, "mensaje": "Tus datos han sido borrados"}


@app.post("/api/admin/listar-usuarios")
@limiter.limit("30/hour")
def admin_listar_usuarios(request: Request, admin_password: str = Form(...)):
    requiere_admin(admin_password)
    with db() as conn:
        filas = conn.execute("SELECT usuario, creada FROM usuarios ORDER BY creada").fetchall()
    return [dict(f) for f in filas]


@app.post("/api/admin/exportar-usuario")
@limiter.limit("20/hour")
def admin_exportar_usuario(request: Request, usuario: str = Form(...), admin_password: str = Form(...)):
    requiere_admin(admin_password)
    return exportar_datos_usuario(usuario)


@app.post("/api/admin/restaurar-usuario")
@limiter.limit("10/hour")
async def admin_restaurar_usuario(
    request: Request,
    usuario: str = Form(...),
    admin_password: str = Form(...),
    archivo: UploadFile = File(...),
):
    requiere_admin(admin_password)
    try:
        datos = json.loads(await archivo.read())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="El archivo no es un backup válido")
    restaurar_datos_usuario(usuario, datos)
    return {"ok": True, "mensaje": f"Datos de '{usuario}' restaurados"}


@app.post("/api/admin/borrar-usuario")
@limiter.limit("10/hour")
def admin_borrar_usuario(request: Request, usuario: str = Form(...), admin_password: str = Form(...)):
    requiere_admin(admin_password)
    borrar_datos_usuario(usuario)
    return {"ok": True, "mensaje": f"Datos de '{usuario}' borrados"}


@app.post("/api/admin/borrar-todo")
@limiter.limit("5/hour")
def admin_borrar_todo(request: Request, admin_password: str = Form(...), confirmacion: str = Form(...)):
    requiere_admin(admin_password)
    if confirmacion != "BORRAR TODO":
        raise HTTPException(status_code=400, detail="Frase de confirmación incorrecta")
    with db() as conn:
        conn.execute("DELETE FROM sesiones")
        conn.execute("DELETE FROM memoria")
        conn.execute("DELETE FROM autopercepcion")
        conn.execute("DELETE FROM autobiografia")
    return {"ok": True, "mensaje": "Todos los datos de todos los usuarios han sido borrados"}


@app.post("/api/admin/comprobar-backup-sin-cifrar")
@limiter.limit("30/hour")
def comprobar_backup_sin_cifrar(request: Request, admin_password: str = Form(...)):
    requiere_admin(admin_password)
    ruta = DB_PATH + ".sin_cifrar.backup"
    if os.path.exists(ruta):
        return {"existe": True, "tamano_bytes": os.path.getsize(ruta)}
    return {"existe": False}


@app.post("/api/admin/borrar-backup-sin-cifrar")
@limiter.limit("10/hour")
def borrar_backup_sin_cifrar(request: Request, admin_password: str = Form(...)):
    requiere_admin(admin_password)
    ruta = DB_PATH + ".sin_cifrar.backup"
    if os.path.exists(ruta):
        os.remove(ruta)
        return {"ok": True, "mensaje": "Copia sin cifrar eliminada"}
    return {"ok": True, "mensaje": "No había ninguna copia sin cifrar que borrar"}


@app.post("/api/admin/listar-backups-automaticos")
@limiter.limit("30/hour")
def listar_backups_automaticos(request: Request, admin_password: str = Form(...)):
    requiere_admin(admin_password)
    if not os.path.exists(CARPETA_BACKUPS_AUTOMATICOS):
        return []
    resultado = []
    for nombre in sorted(os.listdir(CARPETA_BACKUPS_AUTOMATICOS), reverse=True):
        ruta = os.path.join(CARPETA_BACKUPS_AUTOMATICOS, nombre)
        resultado.append({"nombre": nombre, "tamano_bytes": os.path.getsize(ruta)})
    return resultado


@app.post("/api/admin/descargar-backup-automatico")
@limiter.limit("20/hour")
def descargar_backup_automatico(request: Request, admin_password: str = Form(...), nombre: str = Form(...)):
    requiere_admin(admin_password)
    if not nombre_backup_valido(nombre):
        raise HTTPException(status_code=400, detail="Nombre de archivo no válido")
    ruta = os.path.join(CARPETA_BACKUPS_AUTOMATICOS, nombre)
    if not os.path.exists(ruta):
        raise HTTPException(status_code=404, detail="Ese backup ya no existe")
    return FileResponse(ruta, filename=nombre, media_type="application/octet-stream")


@app.get("/api/memoria")
def ver_memoria(usuario: str = Depends(obtener_usuario_actual)):
    return cargar_resumen(usuario)


@app.get("/api/autobiografia")
def ver_autobiografia(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT contenido, fecha_generada FROM autobiografia WHERE usuario = ?",
            (usuario,),
        ).fetchone()
    if not row:
        return {"contenido": None, "fecha_generada": None}
    return {"contenido": row["contenido"], "fecha_generada": row["fecha_generada"]}


@app.post("/api/generar-autobiografia")
def generar_autobiografia(usuario: str = Depends(obtener_usuario_actual)):
    resumen = cargar_resumen(usuario)

    if not resumen.get("cronologia") and not any(resumen.get("bloques", {}).values()):
        return {"error": "todavía no hay suficiente memoria guardada para generar una autobiografía"}

    _inicio = time.perf_counter()
    try:
        respuesta = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM_PROMPT_AUTOBIOGRAFIA,
            messages=[{
                "role": "user",
                "content": (
                    f"Año de nacimiento (null si no se conoce): {json.dumps(resumen.get('anio_nacimiento'))}\n\n"
                    f"Cronología de eventos (cada uno ya trae su año calculado):\n"
                    f"{json.dumps(resumen.get('cronologia', []), ensure_ascii=False)}\n\n"
                    f"Bloques temáticos con detalle:\n{json.dumps(resumen.get('bloques', {}), ensure_ascii=False)}"
                ),
            }],
            timeout=150.0,  # texto largo (hasta 8000 tokens), necesita más margen que el resumen
        )
    except anthropic.APITimeoutError:
        print(f"[tiempos] generar_autobiografia ({usuario}): TIMEOUT tras {time.perf_counter() - _inicio:.1f}s")
        return {"error": "la generación de la autobiografía ha tardado demasiado y se ha cancelado; puedes intentarlo de nuevo"}
    print(f"[tiempos] generar_autobiografia ({usuario}): {time.perf_counter() - _inicio:.1f}s")
    contenido = respuesta.content[0].text
    fecha = datetime.utcnow().isoformat()

    with db() as conn:
        conn.execute(
            "INSERT INTO autobiografia (usuario, contenido, fecha_generada, tokens_input, tokens_output) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(usuario) DO UPDATE SET contenido = excluded.contenido, "
            "fecha_generada = excluded.fecha_generada, "
            "tokens_input = autobiografia.tokens_input + excluded.tokens_input, "
            "tokens_output = autobiografia.tokens_output + excluded.tokens_output",
            (usuario, contenido, fecha, respuesta.usage.input_tokens, respuesta.usage.output_tokens),
        )

    return {"contenido": contenido, "fecha_generada": fecha}


# ---------------------------------------------------------------------------
# Prompts y datos de la entrevista (igual que antes)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_ENTREVISTA = """Eres una entrevistadora biográfica profesional, curiosa y paciente.
Tu objetivo es ayudar a la persona a contar su vida con el máximo detalle posible,
a lo largo de muchas sesiones (no tienes que cubrir todo hoy).

Bloques temáticos a cubrir con el tiempo: infancia, familia, lugares donde vivió,
estudios, primeros trabajos, amistades, relaciones de pareja, momentos de cambio
importantes, valores y creencias, aficiones, pérdidas, logros, cómo se ve a sí misma hoy.

Tono:
- Profesional y cercano, pero no efusivo. Nunca uses emojis.
- Evita exclamaciones y adjetivos superlativos vacíos ("qué interesante",
  "qué bonito", "qué fuerte", "increíble") como reacción automática a lo que
  cuenta la persona. Esas coletillas suenan complacientes y no aportan nada.
- Para mostrar que has entendido y que la escuchas, usa en su lugar una
  paráfrasis breve o una conexión concreta con algo que ya contó antes
  ("eso encaja con lo que decías sobre..."), no una valoración positiva
  genérica. La validación real viene de demostrar comprensión, no de opinar
  que lo que cuenta es bueno, admirable o interesante.
- No emitas juicios de valor sobre las decisiones o el comportamiento de la
  persona ni de terceros que mencione, ni en positivo ni en negativo.
  Mantente en el papel de quien escucha y pregunta, no de quien evalúa.

Fechas y edades:
- En el contexto tienes el año actual real y, si ya se conoce, el año de
  nacimiento de la persona (campo anio_nacimiento). Si la persona dice
  "cuando tenía X años", el año de ese momento es anio_nacimiento + X —
  si necesitas decir ese año en voz alta, haz la suma con cuidado, cifra a
  cifra si hace falta, no la des por hecha de memoria. Es un error fácil de
  cometer y ya ha pasado antes.
- Si todavía no conoces el año de nacimiento y en algún momento la persona
  lo menciona (o da su fecha de nacimiento completa), tenlo en cuenta para
  el resto de la conversación.
- Si tienes dudas sobre en qué año ocurrió algo, es mejor preguntarlo
  directamente ("¿en qué año fue eso, más o menos?") que arriesgarte a
  calcularlo mal y decirlo como si fuera un hecho.

Reglas:
- Haz UNA pregunta a la vez, nunca varias juntas.
- Si la respuesta anterior tiene carga emocional o abre un hilo interesante, profundiza
  ahí antes de saltar a otro bloque.
- Si llevas varios turnos en un bloque, puedes pasar a otro con una transición natural.
- No inventes ni completes huecos: si no lo sabes, pregúntalo.
- Ten en cuenta el resumen de memoria previo (si existe) para no repetir preguntas
  ya respondidas en sesiones anteriores, y para retomar los "temas pendientes".

Si la persona se va por las ramas pero sigue hablando de su vida o de algo relacionado
(aunque no responda exactamente a tu pregunta), síguele el hilo con naturalidad, como
haría cualquier buen conversador — no lo corrijas ni insistas en volver a tu pregunta
original si lo que cuenta es interesante.

Si en cambio la persona te pide algo que no tiene nada que ver con contar su vida
(resolver un problema de matemáticas, escribir código, traducir un texto, hacerle
de asistente general, redactar textos ajenos a su biografía, etc.), sigue esta
política de tres pasos — cuenta tú misma, revisando tus propios turnos anteriores
en esta conversación, cuántas veces ya ha ocurrido esto:

1ª vez: no lo hagas. Dile con amabilidad que tu papel aquí es acompañarla a contar
su historia, no resolver ese tipo de tareas, y retoma la conversación biográfica
con una pregunta relacionada con lo último que sí contó. Sin dramatizar.

2ª vez en la misma conversación: repite la negativa, pero esta vez dile explícita
y claramente que es el segundo aviso, y que si vuelve a pedir algo ajeno a la
entrevista la sesión se cerrará. Sé directa y seria en este punto, sin perder
la educación.

3ª vez: no expliques ni negocies más. Tu respuesta debe empezar EXACTAMENTE con
el texto "[CIERRE_POR_USO_INDEBIDO]" seguido de una frase breve y seria explicando
que la sesión se cierra por uso indebido repetido, sin nada más después.

Si la persona pide algo ajeno una sola vez y luego vuelve a hablar con normalidad
de su vida, no sigas contando aviso tras aviso indefinidamente — pero si el patrón
de pedir tareas ajenas se repite de forma clara, aplica los tres pasos sin
excepciones ni margen adicional.

Si tienes disponible su autopercepción declarada (cómo se describe a sí misma en
un cuestionario), puedes usarla ocasionalmente para pedir un ejemplo concreto que
la ilustre o la contraste ("dijiste que te mantienes calmado ante un conflicto,
¿me cuentas alguna vez que lo vivieras así?"), pero no la menciones constantemente
ni la trates como un hecho — es solo cómo ella misma se ve, no una verdad
verificada.

De vez en cuando, cuando encaje de forma natural con lo que se está contando,
puedes preguntar si conserva alguna foto o vídeo de ese momento o esa época del
que le apetezca hablar. Si responde que no tiene o no quiere hablar de ello, no
insistas ni lo vuelvas a sacar en el resto de la sesión. Si en cambio la persona
menciona fotos o vídeos por su cuenta varias veces a lo largo de las sesiones,
tómalo como una señal de que es un tema que le importa, e invítala activamente
a profundizar en ellos cuando surja la ocasión. Todo esto se queda en la
conversación (descripción de la foto o el vídeo, lo que significa), nunca le
pidas que suba ni envíe ningún archivo.
"""

SYSTEM_PROMPT_RESUMEN = """Vas a recibir un resumen de memoria previo (puede estar vacío),
el año de nacimiento de la persona si ya se conoce, el año actual real, y la
transcripción de una nueva sesión de entrevista biográfica.

Actualiza y amplía el resumen previo con lo nuevo de esta sesión, no lo sustituyas
por completo: conserva lo anterior y añade/enriquece. Además de los bloques
temáticos, mantén también una cronología: una lista de eventos concretos,
ordenada de más antiguo a más reciente. Añade a la cronología los eventos
nuevos que aparezcan en esta sesión, sin duplicar los que ya estuvieran.

Sobre las fechas, sigue esto con mucho cuidado, porque es una fuente frecuente
de errores:

- Si en algún momento la persona menciona directamente su año de nacimiento
  (o una fecha de nacimiento completa), guárdalo en el campo "anio_nacimiento".
  Una vez conocido, nunca lo cambies ni lo recalcules.
- Para cada evento de la cronología, calcula su año exacto ("anio") así:
  si la persona dio un año concreto, usa ese año directamente. Si en cambio
  dio una edad ("cuando tenía 20 años"), y el año de nacimiento ya se conoce,
  calcula: anio_nacimiento + edad = anio del evento. Haz esta suma con
  cuidado, cifra a cifra si hace falta — es un error común equivocarse aquí.
  Si no hay año de nacimiento conocido todavía ni año explícito, deja "anio"
  en null en vez de adivinar.
- Antes de terminar, revisa que cada "anio" de la cronología sea coherente:
  ningún evento puede tener un año anterior a anio_nacimiento, y ninguno
  puede ser posterior al año actual que se te ha indicado. Si algo no
  cuadra, revisa el cálculo en vez de dejarlo así.

Usa la herramienta que tienes disponible para guardar el resultado."""

HERRAMIENTA_RESUMEN = {
    "name": "guardar_resumen_memoria",
    "description": "Guarda el resumen actualizado de la memoria biográfica de la persona, organizado por bloques temáticos y por una cronología de eventos con años exactos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "anio_nacimiento": {
                "type": ["integer", "null"],
                "description": "Año de nacimiento de la persona si ya se conoce (verbatim, tal como lo dijo o se dedujo de una fecha completa), o null si todavía no se sabe.",
            },
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
            "cronologia": {
                "type": "array",
                "description": "Eventos concretos ordenados de más antiguo a más reciente",
                "items": {
                    "type": "object",
                    "properties": {
                        "anio": {
                            "type": ["integer", "null"],
                            "description": "Año exacto en el que ocurrió, calculado con cuidado (ver instrucciones), o null si no se puede determinar todavía",
                        },
                        "momento": {"type": "string", "description": "Descripción del momento en palabras de la persona, ej. 'a los 10 años' o 'recién casado'"},
                        "evento": {"type": "string", "description": "Descripción breve del evento"},
                    },
                    "required": ["anio", "momento", "evento"],
                },
            },
            "temas_pendientes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista breve de temas apenas tocados o sin tocar todavía",
            },
        },
        "required": ["anio_nacimiento", "bloques", "cronologia", "temas_pendientes"],
    },
}


SYSTEM_PROMPT_AUTOBIOGRAFIA = """Vas a recibir la memoria biográfica acumulada de una persona:
una cronología de eventos y un conjunto de bloques temáticos con detalle narrativo.

Tu tarea es redactar una autobiografía en primera persona, en formato Markdown,
ordenada cronológicamente y dividida en capítulos naturales (por ejemplo:
infancia, juventud, vida adulta — adapta los capítulos a lo que la cronología
realmente contenga, no fuerces una estructura fija).

Reglas:
- Usa encabezados Markdown para los capítulos (## Nombre del capítulo).
- Escribe en primera persona, con un tono narrativo natural, no como una lista
  de datos ni como un informe.
- Basa el texto SOLO en la información proporcionada. No inventes fechas,
  nombres, ni detalles que no estén en el material. Si hay huecos temporales,
  no los rellenes con suposiciones — simplemente pasa al siguiente evento
  conocido.
- Integra el detalle narrativo de los bloques temáticos en el lugar cronológico
  que corresponda, no los repitas como secciones aparte.
- Cada evento de la cronología ya trae su año calculado (campo "anio"). Usa
  ese año directamente si necesitas mencionarlo; no vuelvas a calcularlo tú
  a partir de edades mencionadas en los bloques temáticos, para no introducir
  un nuevo error de suma sobre un cálculo que ya estaba hecho. Si el año de
  un evento es null, no inventes uno ni lo menciones como si fuera exacto.
- No incluyas ningún comentario tuyo sobre el proceso, ni introducciones tipo
  "aquí tienes tu autobiografía" — empieza directamente con el primer capítulo.
"""


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


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")