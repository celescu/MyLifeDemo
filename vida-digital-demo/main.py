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
import threading
import traceback
from difflib import SequenceMatcher
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

# En Railway, si hay un volumen montado, usar automáticamente su raíz para
# la base de datos. DB_PATH explícita tiene prioridad. Así un despliegue desde
# GitHub nunca crea accidentalmente una base nueva fuera del volumen.
DB_PATH = os.environ.get("DB_PATH") or (
    os.path.join(os.environ["RAILWAY_VOLUME_MOUNT_PATH"], "memoria.db")
    if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    else os.path.join(os.path.dirname(__file__), "memoria.db")
)
print(f"[db] usando base de datos: {DB_PATH}")
MODEL_LEGACY = os.environ.get("MODEL") or "claude-sonnet-4-6"
MODEL_ESTANDAR = os.environ.get("MODEL_ESTANDAR") or "claude-haiku-4-5-20251001"
MODEL_AVANZADO = os.environ.get("MODEL_AVANZADO") or "claude-sonnet-4-6"
MODELOS_DISPONIBLES = {
    MODEL_ESTANDAR: "Estándar — Claude Haiku 4.5",
    MODEL_AVANZADO: "Avanzado — Claude Sonnet 4.6",
}
PRECIOS_MODELO = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
}
PRECIO_INPUT_POR_MILLON = PRECIOS_MODELO.get(MODEL_LEGACY, (3.0, 15.0))[0]
PRECIO_OUTPUT_POR_MILLON = PRECIOS_MODELO.get(MODEL_LEGACY, (3.0, 15.0))[1]

# Límites de uso por usuario, para que nadie dispare el coste de la cuenta
# de Anthropic sin querer (o queriendo). Configurables por variable de
# entorno sin tocar código.
LIMITE_MENSAJES_DIARIOS = int(os.environ.get("LIMITE_MENSAJES_DIARIOS", "60"))
LIMITE_COSTE_DIARIO_USD = float(os.environ.get("LIMITE_COSTE_DIARIO_USD", "1.0"))
LIMITE_TURNOS_POR_SESION = int(os.environ.get("LIMITE_TURNOS_POR_SESION", "50"))

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


def llamar_a_claude(**kwargs):
    """Envuelve client.messages.create() para que, si la API de Anthropic falla
    (modelo no disponible, límite de la cuenta, error de red...), quede un
    traceback claro en los logs del servidor y el usuario reciba un mensaje
    entendible en vez de un error genérico sin explicación."""
    import traceback
    try:
        return client.messages.create(**kwargs)
    except anthropic.APIError as e:
        print(f"[ERROR] Llamada a Anthropic falló (modelo={kwargs.get('model')}): {e}")
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail="La entrevistadora no ha podido responder ahora mismo. Inténtalo de nuevo en un momento.",
        )

limiter = Limiter(key_func=get_remote_address)

# Estado temporal + persistente de reconstrucciones. El trabajo pesado se ejecuta
# fuera de la petición HTTP y el progreso se guarda en la BD tras cada sesión.
RECONSTRUCCIONES = {}
RECONSTRUCCIONES_EVENTOS = {}
RECONSTRUCCIONES_LOCK = threading.Lock()

def _memoria_solo_manuales(resumen: dict) -> dict:
    resumen = _normalizar_resumen(resumen)
    base={"bloques":{},"cronologia":[],"anio_nacimiento":None,"temas_pendientes":[],
          "_manual_fields":dict(resumen.get("_manual_fields",{})),
          "_manual_cronologia":json.loads(json.dumps(resumen.get("_manual_cronologia",[]),ensure_ascii=False))}
    for path,valor in base["_manual_fields"].items(): _aplicar_path_manual(base,path,valor)
    for manual in base["_manual_cronologia"]:
        evento={"_id":manual.get("id",secrets.token_hex(8)),"anio":None,"momento":manual.get("momento",""),"evento":manual.get("evento","")}
        evento.update(manual.get("campos",{})); base["cronologia"].append(evento)
    return _normalizar_resumen(base)

def _leer_reconstruccion_db(job_id):
    with db() as conn:
        row=conn.execute("SELECT * FROM reconstrucciones WHERE id=?",(job_id,)).fetchone()
    return dict(row) if row else None

def _guardar_estado_reconstruccion(job_id, **cambios):
    if not cambios: return
    with db() as conn:
        conn.execute("UPDATE reconstrucciones SET " + ", ".join(f"{k}=?" for k in cambios) + " WHERE id=?", list(cambios.values())+[job_id])
    with RECONSTRUCCIONES_LOCK:
        if job_id in RECONSTRUCCIONES: RECONSTRUCCIONES[job_id].update(cambios)

def _estado_publico_reconstruccion(row):
    errores=json.loads(row.get("errores") or "[]") if isinstance(row.get("errores"),str) else (row.get("errores") or [])
    return {"job_id":row["id"],"estado":row["estado"],"total":row["total"],"procesadas":row["procesadas"],"omitidas":row["omitidas"],"errores":errores,"sesion_actual":row.get("sesion_actual"),"mensaje":row.get("mensaje") or "","inicio":row.get("inicio"),"fin":row.get("fin")}

def _crear_reconstruccion(usuario,total,procesadas_ids=None,procesadas=0,omitidas=0,errores=None):
    job_id=secrets.token_urlsafe(18); ahora=datetime.utcnow().isoformat(); procesadas_ids=procesadas_ids or []; errores=errores or []
    with db() as conn:
        conn.execute("INSERT INTO reconstrucciones (id,usuario,estado,total,procesadas,omitidas,errores,procesadas_ids,mensaje,inicio) VALUES (?,?, 'pendiente',?,?,?,?,?,?,?)",
                     (job_id,usuario,total,procesadas,omitidas,json.dumps(errores,ensure_ascii=False),json.dumps(procesadas_ids),"Preparando la reconstrucción…",ahora))
    with RECONSTRUCCIONES_LOCK:
        RECONSTRUCCIONES[job_id]={"job_id":job_id,"usuario":usuario,"estado":"pendiente","total":total,"procesadas":procesadas,"omitidas":omitidas,"errores":errores,"sesion_actual":None,"mensaje":"Preparando la reconstrucción…","inicio":ahora,"fin":None}
        RECONSTRUCCIONES_EVENTOS[job_id]=threading.Event()
    return job_id

def _ejecutar_reconstruccion(job_id,usuario,filas):
    evento_cancelacion=RECONSTRUCCIONES_EVENTOS.setdefault(job_id,threading.Event())
    row=_leer_reconstruccion_db(job_id)
    if not row: return
    hechas=set(json.loads(row.get("procesadas_ids") or "[]")); procesadas=int(row.get("procesadas") or 0); omitidas=int(row.get("omitidas") or 0); errores=json.loads(row.get("errores") or "[]")
    resumen_actual=cargar_resumen(usuario)
    _guardar_estado_reconstruccion(job_id,estado="procesando",mensaje="Leyendo las conversaciones guardadas…")
    pendientes=[f for f in filas if f["id"] not in hechas]
    try:
        for fila in pendientes:
            if evento_cancelacion.is_set():
                _guardar_estado_reconstruccion(job_id,estado="cancelado",sesion_actual=None,fin=datetime.utcnow().isoformat(),mensaje="Reconstrucción cancelada. El progreso guardado se conserva.",procesadas=procesadas,omitidas=omitidas,errores=json.dumps(errores,ensure_ascii=False),procesadas_ids=json.dumps(sorted(hechas)))
                return
            sid=fila["id"]
            _guardar_estado_reconstruccion(job_id,sesion_actual=sid,mensaje=f"Analizando la sesión {sid}…",errores=json.dumps(errores,ensure_ascii=False))
            try:
                mensajes=json.loads(fila["mensajes"]); turnos=[m for m in mensajes if m["role"]=="assistant"]
                if not turnos:
                    omitidas+=1; hechas.add(sid); _guardar_estado_reconstruccion(job_id,omitidas=omitidas,procesadas_ids=json.dumps(sorted(hechas)),mensaje=f"La sesión {sid} no tiene respuesta de la IA; se omite."); continue
                transcripcion="\n".join(f"{m['role']}: {m['content']}" for m in mensajes if m["role"] in ("user","assistant"))
                nuevo,titulo,respuesta=construir_resumen_desde_transcripcion(usuario,resumen_actual,transcripcion)
                if nuevo is None: raise RuntimeError("El modelo no devolvió la herramienta de memoria.")
                resumen_actual=nuevo; sumar_tokens(sid,respuesta.usage.input_tokens,respuesta.usage.output_tokens); registrar_uso_diario(usuario,respuesta.usage.input_tokens,respuesta.usage.output_tokens); marcar_cerrada(sid)
                if titulo: aplicar_titulo_generado(sid,titulo)
                guardar_resumen(usuario,resumen_actual); procesadas+=1; hechas.add(sid); errores=[e for e in errores if e.get("sesion_id")!=sid]
                _guardar_estado_reconstruccion(job_id,procesadas=procesadas,errores=json.dumps(errores,ensure_ascii=False),procesadas_ids=json.dumps(sorted(hechas)),mensaje=f"Sesión {sid} reconstruida correctamente.")
            except Exception as exc:
                detalle=str(exc) or exc.__class__.__name__; print(f"[ERROR] reconstruir_memoria, sesión {sid}: {detalle}"); traceback.print_exc()
                errores=[e for e in errores if e.get("sesion_id")!=sid]+[{"sesion_id":sid,"error":detalle}]
                _guardar_estado_reconstruccion(job_id,errores=json.dumps(errores,ensure_ascii=False),mensaje=f"La sesión {sid} ha dado un error; continúo con la siguiente.")
        guardar_resumen(usuario,resumen_actual)
        if len(hechas)>=len(filas):
            _guardar_estado_reconstruccion(job_id,estado="completado",sesion_actual=None,fin=datetime.utcnow().isoformat(),mensaje="Reconstrucción terminada." if filas else "No hay conversaciones guardadas para reconstruir.",procesadas=procesadas,omitidas=omitidas,errores=json.dumps(errores,ensure_ascii=False),procesadas_ids=json.dumps(sorted(hechas)))
        else:
            _guardar_estado_reconstruccion(job_id,estado="cancelado",sesion_actual=None,fin=datetime.utcnow().isoformat(),mensaje="La reconstrucción se ha detenido. Puedes reanudarla.",procesadas=procesadas,omitidas=omitidas,errores=json.dumps(errores,ensure_ascii=False),procesadas_ids=json.dumps(sorted(hechas)))
    except Exception as exc:
        detalle=str(exc) or exc.__class__.__name__; traceback.print_exc(); _guardar_estado_reconstruccion(job_id,estado="error",sesion_actual=None,fin=datetime.utcnow().isoformat(),mensaje=f"La reconstrucción se detuvo por un error: {detalle}",errores=json.dumps(errores,ensure_ascii=False),procesadas=procesadas,omitidas=omitidas,procesadas_ids=json.dumps(sorted(hechas)))

def _iniciar_hilo_reconstruccion(job_id,usuario,filas):
    threading.Thread(target=_ejecutar_reconstruccion,args=(job_id,usuario,filas),name=f"reconstruccion-{job_id}",daemon=True).start()

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

    # Evita que el navegador se quede con una versión vieja cacheada de las
    # páginas tras cada despliegue: "no-cache" no significa "no guardar nada",
    # significa "guarda esto pero comprueba siempre con el servidor si hay
    # una versión nueva antes de usarlo".
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-cache"

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
            creada TEXT NOT NULL,
            modelo_entrevista TEXT,
            modelo_autobiografia TEXT
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
            tokens_output INTEGER DEFAULT 0,
            modelo TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autobiografia_proyectos (
            usuario TEXT PRIMARY KEY,
            titulo TEXT NOT NULL DEFAULT 'Mi autobiografía',
            tono TEXT NOT NULL DEFAULT 'natural',
            estructura_json TEXT NOT NULL DEFAULT '[]',
            fecha_actualizada TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autobiografia_capitulos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT NOT NULL,
            orden INTEGER NOT NULL,
            titulo TEXT NOT NULL,
            enfoque TEXT NOT NULL DEFAULT '',
            contenido TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'pendiente',
            modelo TEXT,
            tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0,
            fecha_generado TEXT,
            editado_manual INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS estrategia_entrevista (
            usuario TEXT PRIMARY KEY,
            estado_json TEXT NOT NULL DEFAULT '{}',
            fecha_actualizada TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconstrucciones (
            id TEXT PRIMARY KEY, usuario TEXT NOT NULL, estado TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0, procesadas INTEGER NOT NULL DEFAULT 0,
            omitidas INTEGER NOT NULL DEFAULT 0, errores TEXT NOT NULL DEFAULT '[]',
            procesadas_ids TEXT NOT NULL DEFAULT '[]', sesion_actual INTEGER,
            mensaje TEXT, inicio TEXT NOT NULL, fin TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uso_diario (
            usuario TEXT NOT NULL,
            fecha TEXT NOT NULL,
            mensajes INTEGER DEFAULT 0,
            tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0,
            PRIMARY KEY (usuario, fecha)
        )
    """)
    columnas_existentes = {fila[1] for fila in conn.execute("PRAGMA table_info(sesiones)").fetchall()}
    for columna, definicion in [
        ("fecha_cierre", "TEXT"),
        ("tokens_input", "INTEGER DEFAULT 0"),
        ("tokens_output", "INTEGER DEFAULT 0"),
        ("titulo", "TEXT"),
        ("modelo", "TEXT"),
        ("titulo_manual", "INTEGER DEFAULT 0"),
        ("tipo", "TEXT DEFAULT 'propia'"),
        ("aportante_nombre", "TEXT"),
        ("aportante_relacion", "TEXT"),
    ]:
        if columna not in columnas_existentes:
            conn.execute(f"ALTER TABLE sesiones ADD COLUMN {columna} {definicion}")
    columnas_usuarios = {fila[1] for fila in conn.execute("PRAGMA table_info(usuarios)").fetchall()}
    for columna in ("modelo_entrevista", "modelo_autobiografia"):
        if columna not in columnas_usuarios:
            conn.execute(f"ALTER TABLE usuarios ADD COLUMN {columna} TEXT")
    conn.execute("UPDATE usuarios SET modelo_entrevista = COALESCE(modelo_entrevista, ?), modelo_autobiografia = COALESCE(modelo_autobiografia, ?)", (MODEL_LEGACY, MODEL_LEGACY))
    columnas_autobiografia = {fila[1] for fila in conn.execute("PRAGMA table_info(autobiografia)").fetchall()}
    if "modelo" not in columnas_autobiografia:
        conn.execute("ALTER TABLE autobiografia ADD COLUMN modelo TEXT")


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
    modo_sorpresa: bool = False


class CerrarSesionIn(BaseModel):
    sesion_id: int


class NuevaAportacionIn(BaseModel):
    nombre_aportante: str = Field(..., min_length=1, max_length=80)
    relacion: str = Field(..., min_length=1, max_length=60)


class AutopercepcionIn(BaseModel):
    respuestas: dict

class EditarMemoriaIn(BaseModel):
    seccion: str
    campo: str
    valor: str | int | None = None
    indice: int | None = None


class LoginIn(BaseModel):
    usuario: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class CrearUsuarioBase(BaseModel):
    usuario: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=10, max_length=256)
    @field_validator("usuario")
    @classmethod
    def usuario_valido(cls,v:str)->str:
        if not all(c.isalnum() or c in "._-" for c in v): raise ValueError("El usuario solo puede contener letras, números, punto, guion y guion bajo")
        return v
    @field_validator("password")
    @classmethod
    def password_minima(cls,v:str)->str:
        if len(v)<10: raise ValueError("La contraseña debe tener al menos 10 caracteres")
        if v.lower() in {"1234567890","password","contraseña","qwertyuiop","0000000000"}: raise ValueError("Contraseña demasiado común")
        return v

class CrearUsuarioIn(CrearUsuarioBase):
    admin_password: str

class RegistroIn(CrearUsuarioBase):
    modelo_entrevista: str = MODEL_ESTANDAR
    modelo_autobiografia: str = MODEL_ESTANDAR


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


def obtener_modelos_usuario(usuario: str) -> tuple[str, str]:
    with db() as conn:
        row = conn.execute("SELECT modelo_entrevista, modelo_autobiografia FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
    if not row:
        return MODEL_ESTANDAR, MODEL_ESTANDAR
    return row["modelo_entrevista"] or MODEL_ESTANDAR, row["modelo_autobiografia"] or MODEL_ESTANDAR

def obtener_modelo_usuario(usuario: str, tipo: str) -> str:
    entrevista, autobiografia = obtener_modelos_usuario(usuario)
    modelo = entrevista if tipo == "entrevista" else autobiografia
    return modelo if modelo in MODELOS_DISPONIBLES else MODEL_ESTANDAR

def nombre_modelo(modelo: str) -> str:
    return MODELOS_DISPONIBLES.get(modelo, modelo)

def coste_tokens_modelo(modelo: str, tokens_input: int, tokens_output: int) -> float:
    pin, pout = PRECIOS_MODELO.get(modelo, (PRECIO_INPUT_POR_MILLON, PRECIO_OUTPUT_POR_MILLON))
    return (tokens_input or 0) / 1_000_000 * pin + (tokens_output or 0) / 1_000_000 * pout

def _normalizar_resumen(resumen: dict) -> dict:
    resumen = resumen or {}
    resumen.setdefault("bloques", {})
    resumen.setdefault("cronologia", [])
    resumen.setdefault("anio_nacimiento", None)
    resumen.setdefault("temas_pendientes", [])
    resumen.setdefault("_manual_fields", {})
    resumen.setdefault("_manual_cronologia", [])
    for evento in resumen["cronologia"]:
        evento.setdefault("_id", secrets.token_hex(8))
    return resumen

def _resumen_para_ia(resumen: dict) -> dict:
    limpio = json.loads(json.dumps(resumen, ensure_ascii=False))
    limpio.pop("_manual_fields", None); limpio.pop("_manual_cronologia", None)
    for evento in limpio.get("cronologia", []): evento.pop("_id", None)
    return limpio

def _aplicar_path_manual(obj: dict, path: str, valor):
    partes = path.split("."); actual = obj
    for parte in partes[:-1]:
        if not isinstance(actual, dict) or parte not in actual: return
        actual = actual[parte]
    if isinstance(actual, dict): actual[partes[-1]] = valor

def _preservar_manuales(resumen_previo: dict, nuevo: dict) -> dict:
    previo = _normalizar_resumen(resumen_previo); nuevo = _normalizar_resumen(nuevo)
    for path, valor in previo.get("_manual_fields", {}).items(): _aplicar_path_manual(nuevo, path, valor)
    for manual in previo.get("_manual_cronologia", []):
        candidato = next((e for e in nuevo.get("cronologia", []) if e.get("_id") == manual.get("id")), None)
        if candidato is None:
            base=f"{manual.get('evento','')}|{manual.get('momento','')}".lower(); mejor=(0,None)
            for e in nuevo.get("cronologia", []):
                actual=f"{e.get('evento','')}|{e.get('momento','')}".lower(); r=SequenceMatcher(None,base,actual).ratio()
                if r>mejor[0]: mejor=(r,e)
            if mejor[0]>=0.55: candidato=mejor[1]
        if candidato is not None:
            for campo,valor in manual.get("campos",{}).items(): candidato[campo]=valor
            candidato["_id"]=manual.get("id",candidato.get("_id",secrets.token_hex(8)))
    nuevo["_manual_fields"]=dict(previo.get("_manual_fields",{}))
    nuevo["_manual_cronologia"]=json.loads(json.dumps(previo.get("_manual_cronologia",[]),ensure_ascii=False))
    return nuevo

def cargar_estrategia_entrevista(usuario: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT estado_json FROM estrategia_entrevista WHERE usuario = ?", (usuario,)).fetchone()
    if not row:
        return {"hilos": [], "contradicciones": [], "hipotesis_pendientes": []}
    try:
        estado = json.loads(row["estado_json"] or "{}")
    except json.JSONDecodeError:
        estado = {}
    estado.setdefault("hilos", [])
    estado.setdefault("contradicciones", [])
    estado.setdefault("hipotesis_pendientes", [])
    return estado


def guardar_estrategia_entrevista(usuario: str, estado: dict):
    estado = estado or {}
    with db() as conn:
        conn.execute(
            "INSERT INTO estrategia_entrevista (usuario, estado_json, fecha_actualizada) VALUES (?, ?, ?) "
            "ON CONFLICT(usuario) DO UPDATE SET estado_json=excluded.estado_json, fecha_actualizada=excluded.fecha_actualizada",
            (usuario, json.dumps(estado, ensure_ascii=False), datetime.utcnow().isoformat()),
        )


def cargar_resumen(usuario: str) -> dict:
    with db() as conn:
        row=conn.execute("SELECT resumen FROM memoria WHERE usuario = ?",(usuario,)).fetchone()
    if row: return _normalizar_resumen(json.loads(row["resumen"]))
    return _normalizar_resumen({"bloques":{},"cronologia":[],"anio_nacimiento":None,"temas_pendientes":[]})

def guardar_resumen(usuario: str, resumen: dict):
    resumen=_normalizar_resumen(resumen)
    with db() as conn:
        conn.execute("INSERT INTO memoria (usuario,resumen) VALUES (?,?) ON CONFLICT(usuario) DO UPDATE SET resumen=excluded.resumen",(usuario,json.dumps(resumen,ensure_ascii=False)))


def obtener_o_crear_sesion(usuario: str, sesion_id: int | None) -> tuple[int, list, str]:
    with db() as conn:
        if sesion_id is not None:
            row = conn.execute(
                "SELECT id, mensajes, fecha, tipo FROM sesiones WHERE id = ? AND usuario = ?",
                (sesion_id, usuario),
            ).fetchone()
            if row:
                if row["tipo"] == "externa":
                    raise HTTPException(
                        status_code=400,
                        detail="Esta es una sesión de aportación externa; ciérrala desde 'Ver aportaciones' en vez de continuarla aquí.",
                    )
                return row["id"], json.loads(row["mensajes"]), row["fecha"]
        fecha = datetime.utcnow().isoformat()
        cur = conn.execute(
            "INSERT INTO sesiones (usuario, fecha, mensajes, modelo) VALUES (?, ?, ?, ?)",
            (usuario, fecha, json.dumps([]), obtener_modelo_usuario(usuario, "entrevista")),
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


def calcular_coste(tokens_input: int, tokens_output: int) -> float:
    return (
        (tokens_input or 0) / 1_000_000 * PRECIO_INPUT_POR_MILLON
        + (tokens_output or 0) / 1_000_000 * PRECIO_OUTPUT_POR_MILLON
    )


def registrar_uso_diario(usuario: str, tokens_input: int, tokens_output: int):
    fecha = datetime.utcnow().date().isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO uso_diario (usuario, fecha, mensajes, tokens_input, tokens_output)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(usuario, fecha) DO UPDATE SET
                mensajes = mensajes + 1,
                tokens_input = tokens_input + excluded.tokens_input,
                tokens_output = tokens_output + excluded.tokens_output
            """,
            (usuario, fecha, tokens_input, tokens_output),
        )


def comprobar_limite_diario(usuario: str):
    """Bloquea antes de llamar a la API si el usuario ya ha agotado su
    límite diario de mensajes o de coste estimado, para no gastar ni un
    token de más una vez alcanzado el tope."""
    fecha = datetime.utcnow().date().isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes, tokens_input, tokens_output FROM uso_diario WHERE usuario = ? AND fecha = ?",
            (usuario, fecha),
        ).fetchone()
    if not row:
        return
    if row["mensajes"] >= LIMITE_MENSAJES_DIARIOS:
        raise HTTPException(
            status_code=429,
            detail=f"Has llegado al límite de {LIMITE_MENSAJES_DIARIOS} mensajes de hoy. Puedes continuar mañana.",
        )
    if calcular_coste(row["tokens_input"], row["tokens_output"]) >= LIMITE_COSTE_DIARIO_USD:
        raise HTTPException(
            status_code=429,
            detail="Has llegado al límite de uso diario. Puedes continuar mañana.",
        )


def exportar_datos_usuario(usuario: str) -> dict:
    with db() as conn:
        sesiones = conn.execute("SELECT * FROM sesiones WHERE usuario = ?", (usuario,)).fetchall()
        cuenta = conn.execute("SELECT usuario, creada, modelo_entrevista, modelo_autobiografia FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
        memoria = conn.execute("SELECT * FROM memoria WHERE usuario = ?", (usuario,)).fetchone()
        autoperc = conn.execute("SELECT * FROM autopercepcion WHERE usuario = ?", (usuario,)).fetchone()
        autobio = conn.execute("SELECT * FROM autobiografia WHERE usuario = ?", (usuario,)).fetchone()
        autobio_proyecto = conn.execute("SELECT * FROM autobiografia_proyectos WHERE usuario = ?", (usuario,)).fetchone()
        autobio_capitulos = conn.execute("SELECT * FROM autobiografia_capitulos WHERE usuario = ? ORDER BY orden, id", (usuario,)).fetchall()
        estrategia = conn.execute("SELECT * FROM estrategia_entrevista WHERE usuario = ?", (usuario,)).fetchone()
    return {
        "formato": "backup-individual-v1",
        "usuario": usuario,
        "exportado_el": datetime.utcnow().isoformat(),
        "cuenta": dict(cuenta) if cuenta else None,
        "sesiones": [dict(row) for row in sesiones],
        "memoria": dict(memoria) if memoria else None,
        "autopercepcion": dict(autoperc) if autoperc else None,
        "autobiografia": dict(autobio) if autobio else None,
        "autobiografia_proyecto": dict(autobio_proyecto) if autobio_proyecto else None,
        "autobiografia_capitulos": [dict(row) for row in autobio_capitulos],
        "estrategia_entrevista": dict(estrategia) if estrategia else None,
    }


def borrar_datos_usuario(usuario: str):
    with db() as conn:
        conn.execute("DELETE FROM sesiones WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM memoria WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM autopercepcion WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM autobiografia WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM autobiografia_capitulos WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM autobiografia_proyectos WHERE usuario = ?", (usuario,))
        conn.execute("DELETE FROM estrategia_entrevista WHERE usuario = ?", (usuario,))


def restaurar_datos_usuario(usuario: str, datos: dict):
    borrar_datos_usuario(usuario)
    with db() as conn:
        for s in datos.get("sesiones") or []:
            conn.execute(
                "INSERT INTO sesiones (usuario, fecha, mensajes, cerrada, fecha_cierre, tokens_input, "
                "tokens_output, titulo, titulo_manual, tipo, aportante_nombre, aportante_relacion, modelo) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    usuario, s.get("fecha"), s.get("mensajes"), s.get("cerrada", 0),
                    s.get("fecha_cierre"), s.get("tokens_input", 0), s.get("tokens_output", 0),
                    s.get("titulo"), s.get("titulo_manual", 0),
                    s.get("tipo", "propia"), s.get("aportante_nombre"), s.get("aportante_relacion"), s.get("modelo"),
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
                "INSERT INTO autobiografia (usuario, contenido, fecha_generada, tokens_input, tokens_output, modelo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (usuario, ab.get("contenido"), ab.get("fecha_generada"), ab.get("tokens_input", 0), ab.get("tokens_output", 0), ab.get("modelo")),
            )
        proyecto = datos.get("autobiografia_proyecto")
        if proyecto:
            conn.execute("INSERT INTO autobiografia_proyectos (usuario,titulo,tono,estructura_json,fecha_actualizada) VALUES (?,?,?,?,?)",
                         (usuario, proyecto.get("titulo", "Mi autobiografía"), proyecto.get("tono", "natural"), proyecto.get("estructura_json", "[]"), proyecto.get("fecha_actualizada", datetime.utcnow().isoformat())))
        for cap in datos.get("autobiografia_capitulos") or []:
            conn.execute("INSERT INTO autobiografia_capitulos (id,usuario,orden,titulo,enfoque,contenido,estado,modelo,tokens_input,tokens_output,fecha_generado,editado_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (cap.get("id"),usuario,cap.get("orden",0),cap.get("titulo", "Capítulo"),cap.get("enfoque", ""),cap.get("contenido", ""),cap.get("estado", "pendiente"),cap.get("modelo"),cap.get("tokens_input",0),cap.get("tokens_output",0),cap.get("fecha_generado"),cap.get("editado_manual",0)))
        estrategia = datos.get("estrategia_entrevista")
        if estrategia:
            conn.execute("INSERT INTO estrategia_entrevista (usuario,estado_json,fecha_actualizada) VALUES (?,?,?)",
                         (usuario, estrategia.get("estado_json", "{}"), estrategia.get("fecha_actualizada", datetime.utcnow().isoformat())))


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

@app.get("/api/modelos-disponibles")
def modelos_disponibles():
    return [{"id":k,"nombre":v} for k,v in MODELOS_DISPONIBLES.items()]

@app.post("/api/registro")
@limiter.limit("5/hour")
def registro(request: Request, payload: RegistroIn):
    if payload.modelo_entrevista not in MODELOS_DISPONIBLES or payload.modelo_autobiografia not in MODELOS_DISPONIBLES:
        raise HTTPException(status_code=400, detail="Modelo no permitido")
    password_hash=bcrypt.hashpw(payload.password.encode(),bcrypt.gensalt()).decode()
    with db() as conn:
        if conn.execute("SELECT 1 FROM usuarios WHERE usuario=?",(payload.usuario,)).fetchone():
            raise HTTPException(status_code=400,detail="Ese nombre de usuario ya existe")
        conn.execute("INSERT INTO usuarios (usuario,password_hash,creada,modelo_entrevista,modelo_autobiografia) VALUES (?,?,?,?,?)",
                     (payload.usuario,password_hash,datetime.utcnow().isoformat(),payload.modelo_entrevista,payload.modelo_autobiografia))
    return {"ok":True}

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
            "INSERT INTO usuarios (usuario, password_hash, creada, modelo_entrevista, modelo_autobiografia) VALUES (?, ?, ?, ?, ?)",
            (payload.usuario, password_hash, datetime.utcnow().isoformat(), MODEL_ESTANDAR, MODEL_ESTANDAR),
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
@limiter.limit("20/minute")
def enviar_mensaje(request: Request, payload: MensajeIn, usuario: str = Depends(obtener_usuario_actual)):
    comprobar_limite_diario(usuario)
    sesion_id, mensajes, fecha_inicio = obtener_o_crear_sesion(usuario, payload.sesion_id)

    turnos_reales = len([m for m in mensajes if m["role"] == "assistant"])
    if turnos_reales >= LIMITE_TURNOS_POR_SESION:
        return {
            "sesion_id": sesion_id,
            "respuesta": (
                "Llevamos ya un buen rato en esta sesión. Para que la "
                "conversación no se alargue demasiado, ¿te parece si la "
                "cerramos aquí? Puedes hacerlo desde el menú, en \"Cerrar "
                "sesión de hoy\", y seguimos otro día."
            ),
            "fecha_inicio": fecha_inicio,
            "limite_turnos_alcanzado": True,
        }

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
            f"{json.dumps(_resumen_para_ia(resumen), ensure_ascii=False)}"
            f"\n\nEstrategia de entrevista acumulada (no es memoria biográfica factual):\n"
            f"{json.dumps(cargar_estrategia_entrevista(usuario), ensure_ascii=False)}"
            f"{bloque_autopercepcion}\n\n"
            f"Empieza la sesión de hoy. Si hay hilos abiertos, temas_pendientes o elementos poco explorados, "
            f"elige uno con criterio narrativo y formula una pregunta natural. Si el resumen está vacío, empieza por la infancia."
        )
        mensajes.append({"role": "user", "content": contexto})

    if payload.modo_sorpresa:
        mensajes.append({"role": "user", "content": "[MODO SORPRÉNDEME] Elige ahora un hilo, persona, lugar, etapa, contraste o tema poco explorado que pueda enriquecer la historia. No preguntes de forma aleatoria: explica brevemente la conexión si hace falta y haz una sola pregunta concreta."})
    mensajes.append({"role": "user", "content": payload.mensaje})

    _inicio = time.perf_counter()
    respuesta = llamar_a_claude(
        model=obtener_modelo_usuario(usuario, "entrevista"),
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
    registrar_uso_diario(usuario, respuesta.usage.input_tokens, respuesta.usage.output_tokens)

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


def construir_resumen_desde_transcripcion(usuario: str, resumen_previo: dict, transcripcion: str, timeout: float = 100.0):
    """Llama al modelo con la misma herramienta que usa el cierre de sesión
    normal, para fusionar una transcripción con el resumen de memoria previo.
    Se usa tanto al cerrar una sesión como al reconstruir la memoria completa
    a partir de sesiones antiguas."""
    respuesta = llamar_a_claude(
        model=obtener_modelo_usuario(usuario, "entrevista"),
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
                f"Resumen previo:\n{json.dumps(_resumen_para_ia(resumen_previo), ensure_ascii=False)}\n\n"
                f"Transcripción de la nueva sesión:\n{transcripcion}"
            ),
        }],
        timeout=timeout,
    )
    bloque_herramienta = next((b for b in respuesta.content if b.type == "tool_use"), None)
    if bloque_herramienta is None:
        print(f"[AVISO] El modelo no devolvió una llamada a la herramienta para {usuario}")
        print(f"[AVISO] stop_reason: {respuesta.stop_reason}, contenido: {respuesta.content}")
        return None, None, respuesta

    nuevo_resumen = bloque_herramienta.input
    titulo_sesion = nuevo_resumen.pop("titulo_sesion", None)
    estrategia = nuevo_resumen.pop("estrategia", None) or cargar_estrategia_entrevista(usuario)
    nuevo_resumen = _preservar_manuales(resumen_previo, nuevo_resumen)
    guardar_estrategia_entrevista(usuario, estrategia)
    validar_coherencia_fechas(nuevo_resumen, usuario)
    return nuevo_resumen, titulo_sesion, respuesta


@app.post("/api/cerrar_sesion")
def cerrar_sesion(payload: CerrarSesionIn, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes, tipo FROM sesiones WHERE id = ? AND usuario = ?",
            (payload.sesion_id, usuario),
        ).fetchone()
    if not row:
        return {"error": "sesión no encontrada"}
    if row["tipo"] == "externa":
        return {"error": "esta es una sesión de aportación externa; ciérrala desde 'Ver aportaciones'"}

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
        nuevo_resumen, titulo_sesion, respuesta = construir_resumen_desde_transcripcion(
            usuario, resumen_previo, transcripcion
        )
    except anthropic.APITimeoutError:
        print(f"[tiempos] cerrar_sesion ({usuario}, sesión {payload.sesion_id}): TIMEOUT tras {time.perf_counter() - _inicio:.1f}s")
        return {"error": "la generación del resumen ha tardado demasiado y se ha cancelado; la conversación sigue guardada íntegra, puedes reintentar cerrar esta sesión más tarde"}
    print(f"[tiempos] cerrar_sesion ({usuario}, sesión {payload.sesion_id}): {time.perf_counter() - _inicio:.1f}s")

    if nuevo_resumen is None:
        return {"error": "no se pudo generar el resumen esta vez, la conversación sigue guardada íntegra"}

    guardar_resumen(usuario, nuevo_resumen)
    sumar_tokens(payload.sesion_id, respuesta.usage.input_tokens, respuesta.usage.output_tokens)
    registrar_uso_diario(usuario, respuesta.usage.input_tokens, respuesta.usage.output_tokens)
    marcar_cerrada(payload.sesion_id)
    if titulo_sesion:
        aplicar_titulo_generado(payload.sesion_id, titulo_sesion)

    return {"resumen": nuevo_resumen}


@app.post("/api/reconstruir_memoria")
@limiter.limit("2/hour")
def iniciar_reconstruccion_memoria(request: Request, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        filas=conn.execute("SELECT id,mensajes FROM sesiones WHERE usuario=? AND (tipo IS NULL OR tipo='propia') ORDER BY fecha ASC,id ASC",(usuario,)).fetchall()
        activa=conn.execute("SELECT * FROM reconstrucciones WHERE usuario=? AND estado IN ('pendiente','procesando') ORDER BY inicio DESC LIMIT 1",(usuario,)).fetchone()
    if activa:
        job_id=activa["id"]
        vivo=any(t.name==f"reconstruccion-{job_id}" and t.is_alive() for t in threading.enumerate())
        if not vivo: _iniciar_hilo_reconstruccion(job_id,usuario,filas)
        return JSONResponse(status_code=202,content=_estado_publico_reconstruccion(dict(activa)))
    guardar_resumen(usuario,_memoria_solo_manuales(cargar_resumen(usuario)))
    guardar_estrategia_entrevista(usuario,{"hilos":[],"contradicciones":[],"hipotesis_pendientes":[]})
    job_id=_crear_reconstruccion(usuario,len(filas)); _iniciar_hilo_reconstruccion(job_id,usuario,filas)
    return JSONResponse(status_code=202,content={"job_id":job_id,"estado":"pendiente","total":len(filas)})

@app.post("/api/reconstruir_memoria/{job_id}/cancelar")
def cancelar_reconstruccion_memoria(job_id:str,usuario:str=Depends(obtener_usuario_actual)):
    row=_leer_reconstruccion_db(job_id)
    if not row or row["usuario"]!=usuario: raise HTTPException(status_code=404,detail="Reconstrucción no encontrada.")
    if row["estado"] not in ("pendiente","procesando"): return _estado_publico_reconstruccion(row)
    RECONSTRUCCIONES_EVENTOS.setdefault(job_id,threading.Event()).set(); _guardar_estado_reconstruccion(job_id,mensaje="Cancelando…")
    return {"ok":True,"mensaje":"Se ha solicitado la cancelación. El progreso guardado se conserva."}

@app.post("/api/reconstruir_memoria/{job_id}/reanudar")
@limiter.limit("2/hour")
def reanudar_reconstruccion_memoria(job_id:str,request:Request,usuario:str=Depends(obtener_usuario_actual)):
    row=_leer_reconstruccion_db(job_id)
    if not row or row["usuario"]!=usuario: raise HTTPException(status_code=404,detail="Reconstrucción no encontrada.")
    if row["estado"] not in ("cancelado","error"): raise HTTPException(status_code=400,detail="Esta reconstrucción todavía está activa o ya ha terminado.")
    with db() as conn: filas=conn.execute("SELECT id,mensajes FROM sesiones WHERE usuario=? AND (tipo IS NULL OR tipo='propia') ORDER BY fecha ASC,id ASC",(usuario,)).fetchall()
    job_nuevo=_crear_reconstruccion(usuario,len(filas),json.loads(row.get("procesadas_ids") or "[]"),row["procesadas"],row["omitidas"],json.loads(row.get("errores") or "[]")); _iniciar_hilo_reconstruccion(job_nuevo,usuario,filas)
    return JSONResponse(status_code=202,content={"job_id":job_nuevo,"estado":"pendiente","total":len(filas),"procesadas":row["procesadas"],"omitidas":row["omitidas"]})

@app.get("/api/reconstruir_memoria/{job_id}")
def estado_reconstruccion_memoria(job_id:str,usuario:str=Depends(obtener_usuario_actual)):
    row=_leer_reconstruccion_db(job_id)
    if not row or row["usuario"]!=usuario: raise HTTPException(status_code=404,detail="Reconstrucción no encontrada.")
    return _estado_publico_reconstruccion(row)


def aplicar_titulo_generado(sesion_id: int, titulo: str):
    """Solo sobrescribe el título si el usuario no le ha puesto ya uno a mano."""
    with db() as conn:
        conn.execute(
            "UPDATE sesiones SET titulo = ? WHERE id = ? AND titulo_manual = 0",
            (titulo, sesion_id),
        )


# ---------------------------------------------------------------------------
# Aportaciones externas: un familiar/allegado cuenta lo que recuerda de la
# persona protagonista, desde el mismo dispositivo. Se guardan como un tipo
# de sesión aparte ("externa"), nunca se mezclan con la memoria biográfica
# propia (bloques, anio_nacimiento, cronología) ni con el listado normal de
# sesiones/métricas de esa memoria.
# ---------------------------------------------------------------------------

@app.post("/api/aportacion/nueva")
def nueva_aportacion(payload: NuevaAportacionIn, usuario: str = Depends(obtener_usuario_actual)):
    fecha = datetime.utcnow().isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO sesiones (usuario, fecha, mensajes, tipo, aportante_nombre, aportante_relacion) "
            "VALUES (?, ?, ?, 'externa', ?, ?)",
            (usuario, fecha, json.dumps([]), payload.nombre_aportante.strip(), payload.relacion.strip()),
        )
        sesion_id = cur.lastrowid
    return {"sesion_id": sesion_id, "fecha_inicio": fecha}


@app.post("/api/aportacion/mensaje")
@limiter.limit("20/minute")
def enviar_mensaje_aportacion(request: Request, payload: MensajeIn, usuario: str = Depends(obtener_usuario_actual)):
    comprobar_limite_diario(usuario)

    if payload.sesion_id is None:
        raise HTTPException(status_code=400, detail="Falta sesion_id de la aportación")

    with db() as conn:
        row = conn.execute(
            "SELECT id, mensajes, fecha, aportante_nombre, aportante_relacion FROM sesiones "
            "WHERE id = ? AND usuario = ? AND tipo = 'externa'",
            (payload.sesion_id, usuario),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sesión de aportación no encontrada")

    sesion_id = row["id"]
    fecha_inicio = row["fecha"]
    mensajes = json.loads(row["mensajes"])

    turnos_reales = len([m for m in mensajes if m["role"] == "assistant"])
    if turnos_reales >= LIMITE_TURNOS_POR_SESION:
        return {
            "sesion_id": sesion_id,
            "respuesta": "Ya llevamos un buen rato charlando. ¿Lo dejamos aquí? Puedes cerrar esta aportación cuando quieras.",
            "fecha_inicio": fecha_inicio,
            "limite_turnos_alcanzado": True,
        }

    if not mensajes:
        contexto = (
            f"{MARCADOR_CONTEXTO_APORTACION}: {usuario}\n"
            f"Nombre de quien vas a entrevistar ahora: {row['aportante_nombre']}\n"
            f"Relación de esa persona con {usuario}: {row['aportante_relacion']}\n\n"
            f"Empieza la conversación saludando por su nombre y explicando brevemente para qué es esta charla."
        )
        mensajes.append({"role": "user", "content": contexto})

    mensajes.append({"role": "user", "content": payload.mensaje})

    _inicio = time.perf_counter()
    respuesta = llamar_a_claude(
        model=obtener_modelo_usuario(usuario, "entrevista"),
        max_tokens=500,
        system=SYSTEM_PROMPT_APORTACION,
        messages=mensajes,
        timeout=45.0,
    )
    print(f"[tiempos] aportacion_mensaje ({usuario}, sesión {sesion_id}): {time.perf_counter() - _inicio:.1f}s")
    texto = respuesta.content[0].text
    mensajes.append({"role": "assistant", "content": texto})
    guardar_mensajes(sesion_id, mensajes)
    sumar_tokens(sesion_id, respuesta.usage.input_tokens, respuesta.usage.output_tokens)
    registrar_uso_diario(usuario, respuesta.usage.input_tokens, respuesta.usage.output_tokens)

    return {"sesion_id": sesion_id, "respuesta": texto, "fecha_inicio": fecha_inicio}


@app.post("/api/aportacion/{sesion_id}/cerrar")
def cerrar_aportacion(sesion_id: int, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ? AND tipo = 'externa'",
            (sesion_id, usuario),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sesión de aportación no encontrada")

    mensajes = json.loads(row["mensajes"])
    marcar_cerrada(sesion_id)
    aplicar_titulo_generado(sesion_id, calcular_titulo(mensajes))
    return {"ok": True}


@app.get("/api/aportaciones")
def listar_aportaciones(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        filas = conn.execute(
            "SELECT id, fecha, cerrada, mensajes, titulo, aportante_nombre, aportante_relacion "
            "FROM sesiones WHERE usuario = ? AND tipo = 'externa' ORDER BY id DESC",
            (usuario,),
        ).fetchall()
    resultado = []
    for f in filas:
        mensajes = json.loads(f["mensajes"])
        resultado.append({
            "id": f["id"],
            "fecha": f["fecha"],
            "cerrada": bool(f["cerrada"]),
            "num_turnos": len([m for m in mensajes if m["role"] == "assistant"]),
            "titulo": f["titulo"] or calcular_titulo(mensajes),
            "aportante_nombre": f["aportante_nombre"],
            "aportante_relacion": f["aportante_relacion"],
        })
    return resultado


@app.get("/api/aportacion/{sesion_id}")
def ver_aportacion(sesion_id: int, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ? AND tipo = 'externa'",
            (sesion_id, usuario),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sesión de aportación no encontrada")
    mensajes = json.loads(row["mensajes"])
    mensajes_visibles = [
        m for m in mensajes
        if not (m["role"] == "user" and m["content"].startswith(MARCADOR_CONTEXTO_APORTACION))
    ]
    return mensajes_visibles


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
MARCADOR_CONTEXTO_APORTACION = "Persona protagonista de este proyecto"
MARCADOR_CIERRE_USO_INDEBIDO = "[CIERRE_POR_USO_INDEBIDO]"


def calcular_titulo(mensajes: list) -> str:
    for m in mensajes:
        if m["role"] == "user" and not m["content"].startswith(MARCADOR_CONTEXTO_INTERNO) \
                and not m["content"].startswith(MARCADOR_CONTEXTO_APORTACION):
            texto = m["content"].strip()
            return texto[:60] + ("…" if len(texto) > 60 else "")
    return "(sesión sin mensajes todavía)"


@app.get("/api/sesiones")
def listar_sesiones(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        filas = conn.execute(
            "SELECT id, fecha, cerrada, mensajes, titulo, titulo_manual FROM sesiones "
            "WHERE usuario = ? AND (tipo IS NULL OR tipo = 'propia') ORDER BY id DESC",
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
            "titulo": f["titulo"] or calcular_titulo(mensajes),
            "titulo_manual": bool(f["titulo_manual"]),
        })
    return resultado


class RenombrarSesionIn(BaseModel):
    titulo: str


@app.post("/api/sesion/{sesion_id}/titulo")
def renombrar_sesion(sesion_id: int, payload: RenombrarSesionIn, usuario: str = Depends(obtener_usuario_actual)):
    titulo_limpio = payload.titulo.strip()[:80]
    if not titulo_limpio:
        raise HTTPException(status_code=400, detail="El título no puede estar vacío")
    with db() as conn:
        fila = conn.execute(
            "SELECT id FROM sesiones WHERE id = ? AND usuario = ?", (sesion_id, usuario)
        ).fetchone()
        if not fila:
            raise HTTPException(status_code=404, detail="Sesión no encontrada")
        conn.execute(
            "UPDATE sesiones SET titulo = ?, titulo_manual = 1 WHERE id = ?",
            (titulo_limpio, sesion_id),
        )
    return {"ok": True, "titulo": titulo_limpio}


@app.get("/api/sesion/{sesion_id}")
def ver_sesion(sesion_id: int, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        row = conn.execute(
            "SELECT mensajes FROM sesiones WHERE id = ? AND usuario = ? AND (tipo IS NULL OR tipo = 'propia')",
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


@app.delete("/api/sesion/{sesion_id}")
def borrar_sesion(sesion_id: int, usuario: str = Depends(obtener_usuario_actual)):
    """Borra una sesión propia o de aportación externa (por ejemplo, una que
    se quedó vacía o a medias por un fallo). No toca el resumen de memoria:
    solo elimina esta conversación concreta."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM sesiones WHERE id = ? AND usuario = ?",
            (sesion_id, usuario),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return {"ok": True}


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
            "SELECT id, fecha, fecha_cierre, cerrada, mensajes, tokens_input, tokens_output, titulo, modelo "
            "FROM sesiones WHERE usuario = ? AND (tipo IS NULL OR tipo = 'propia') ORDER BY id ASC",
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

        modelo_sesion=f["modelo"] or obtener_modelo_usuario(usuario,"entrevista")
        coste=coste_tokens_modelo(modelo_sesion,f["tokens_input"],f["tokens_output"])

        sesiones.append({
            "id": f["id"],
            "titulo": f["titulo"] or calcular_titulo(mensajes),
            "fecha": f["fecha"],
            "cerrada": bool(f["cerrada"]),
            "tokens_input": f["tokens_input"] or 0,
            "tokens_output": f["tokens_output"] or 0,
            "palabras_usuario": palabras,
            "duracion_segundos": duracion_segundos,
            "coste_estimado": round(coste, 4),
            "modelo": modelo_sesion,
            "nombre_modelo": nombre_modelo(modelo_sesion),
        })

        total_tokens_input += f["tokens_input"] or 0
        total_tokens_output += f["tokens_output"] or 0
        total_palabras += palabras
        total_segundos += duracion_segundos

    total_coste = sum(x["coste_estimado"] for x in sesiones)

    with db() as conn:
        fila_autobio = conn.execute(
            "SELECT tokens_input, tokens_output, modelo FROM autobiografia WHERE usuario = ?", (usuario,)
        ).fetchone()
    tokens_input_autobio = fila_autobio["tokens_input"] if fila_autobio else 0
    tokens_output_autobio = fila_autobio["tokens_output"] if fila_autobio else 0
    modelo_autobio=fila_autobio["modelo"] if fila_autobio and fila_autobio["modelo"] else obtener_modelo_usuario(usuario,"autobiografia")
    coste_autobio=coste_tokens_modelo(modelo_autobio,tokens_input_autobio,tokens_output_autobio)

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
        filas = conn.execute("SELECT usuario, creada, modelo_entrevista, modelo_autobiografia FROM usuarios ORDER BY creada").fetchall()
    return [dict(f) | {"nombre_modelo_entrevista":nombre_modelo(f["modelo_entrevista"] or MODEL_ESTANDAR),"nombre_modelo_autobiografia":nombre_modelo(f["modelo_autobiografia"] or MODEL_ESTANDAR)} for f in filas]


@app.post("/api/admin/configurar-modelos")
@limiter.limit("30/hour")
def admin_configurar_modelos(request: Request, usuario: str = Form(...), admin_password: str = Form(...), modelo_entrevista: str = Form(...), modelo_autobiografia: str = Form(...)):
    requiere_admin(admin_password)
    if modelo_entrevista not in MODELOS_DISPONIBLES or modelo_autobiografia not in MODELOS_DISPONIBLES: raise HTTPException(status_code=400,detail="Modelo no permitido")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM usuarios WHERE usuario=?",(usuario,)).fetchone(): raise HTTPException(status_code=404,detail="Usuario no encontrado")
        conn.execute("UPDATE usuarios SET modelo_entrevista=?,modelo_autobiografia=? WHERE usuario=?",(modelo_entrevista,modelo_autobiografia,usuario))
    return {"ok":True,"usuario":usuario,"modelo_entrevista":modelo_entrevista,"modelo_autobiografia":modelo_autobiografia}

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
        conn.execute("DELETE FROM autobiografia_capitulos")
        conn.execute("DELETE FROM autobiografia_proyectos")
        conn.execute("DELETE FROM estrategia_entrevista")
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


@app.get("/api/bienvenida")
def obtener_bienvenida(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        fila = conn.execute(
            "SELECT titulo, titulo_manual, mensajes FROM sesiones "
            "WHERE usuario = ? AND cerrada = 1 ORDER BY id DESC LIMIT 1",
            (usuario,),
        ).fetchone()

    ultimo_titulo = None
    if fila:
        mensajes = json.loads(fila["mensajes"])
        ultimo_titulo = fila["titulo"] or calcular_titulo(mensajes)

    resumen = cargar_resumen(usuario)
    temas_pendientes = [t for t in resumen.get("temas_pendientes", []) if t][:3]

    return {
        "tiene_sesiones_previas": fila is not None,
        "ultimo_titulo": ultimo_titulo,
        "temas_pendientes": temas_pendientes,
    }


@app.get("/api/memoria")
def ver_memoria(usuario: str = Depends(obtener_usuario_actual)):
    return cargar_resumen(usuario)


@app.post("/api/memoria/editar")
@limiter.limit("60/hour")
def editar_memoria(request: Request, payload: EditarMemoriaIn, usuario: str = Depends(obtener_usuario_actual)):
    resumen=cargar_resumen(usuario); valor=payload.valor
    if payload.seccion=="anio_nacimiento":
        if valor in (None,"","null"): valor=None
        else:
            try: valor=int(valor)
            except: raise HTTPException(status_code=400,detail="El año debe ser un número entero")
        resumen["anio_nacimiento"]=valor; resumen["_manual_fields"]["anio_nacimiento"]=valor
    elif payload.seccion=="bloques":
        claves={"infancia","familia","lugares","estudios","trabajo","relaciones","momentos_de_cambio","valores","aficiones","otros"}
        if payload.campo not in claves: raise HTTPException(status_code=400,detail="Bloque no válido")
        valor=str(valor or ""); resumen.setdefault("bloques",{})[payload.campo]=valor; resumen["_manual_fields"][f"bloques.{payload.campo}"]=valor
    elif payload.seccion=="cronologia":
        if payload.indice is None or not 0<=payload.indice<len(resumen.get("cronologia",[])): raise HTTPException(status_code=400,detail="Evento de cronología no válido")
        if payload.campo not in {"anio","momento","evento"}: raise HTTPException(status_code=400,detail="Campo de cronología no válido")
        evento=resumen["cronologia"][payload.indice]; evento.setdefault("_id",secrets.token_hex(8))
        if payload.campo=="anio":
            if valor in (None,"","null"): valor=None
            else:
                try: valor=int(valor)
                except: raise HTTPException(status_code=400,detail="El año debe ser un número entero")
        else: valor=str(valor or "")
        evento[payload.campo]=valor
        manual=next((m for m in resumen["_manual_cronologia"] if m.get("id")==evento["_id"]),None)
        if manual is None:
            manual={"id":evento["_id"],"evento":evento.get("evento",""),"momento":evento.get("momento",""),"campos":{}}; resumen["_manual_cronologia"].append(manual)
        manual["campos"][payload.campo]=valor; manual["evento"]=evento.get("evento",""); manual["momento"]=evento.get("momento","")
    else: raise HTTPException(status_code=400,detail="Sección no válida")
    guardar_resumen(usuario,resumen); return {"ok":True,"resumen":resumen}

@app.get("/api/autobiografia")
def ver_autobiografia(usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        legacy = conn.execute("SELECT contenido, fecha_generada, modelo FROM autobiografia WHERE usuario = ?", (usuario,)).fetchone()
        proyecto = conn.execute("SELECT * FROM autobiografia_proyectos WHERE usuario = ?", (usuario,)).fetchone()
        capitulos = conn.execute(
            "SELECT id, orden, titulo, enfoque, contenido, estado, modelo, tokens_input, tokens_output, fecha_generado, editado_manual "
            "FROM autobiografia_capitulos WHERE usuario = ? ORDER BY orden, id", (usuario,)
        ).fetchall()
    return {
        "contenido": legacy["contenido"] if legacy else None,
        "fecha_generada": legacy["fecha_generada"] if legacy else None,
        "modelo": legacy["modelo"] if legacy else None,
        "proyecto": dict(proyecto) if proyecto else None,
        "capitulos": [dict(c) for c in capitulos],
    }


class AutobiografiaPreviewIn(BaseModel):
    tono: str = "natural"


class AutobiografiaProyectoIn(BaseModel):
    tono: str = "natural"
    titulo: str = "Mi autobiografía"
    capitulos: list[dict]


class EditarCapituloIn(BaseModel):
    titulo: str | None = None
    enfoque: str | None = None
    contenido: str | None = None


class AccionCapituloIn(BaseModel):
    accion: str


TONOS_AUTOBIOGRAFIA = {
    "natural": "Natural y conversacional, como si la persona estuviera contando su vida a alguien cercano.",
    "literario": "Literario pero sobrio: buena prosa y ritmo, sin embellecer ni inventar hechos.",
    "sobrio": "Sobrio y preciso, con prioridad a claridad, hechos y continuidad cronológica.",
    "intimo": "Íntimo y reflexivo, dejando espacio a pensamientos y emociones que estén explícitamente presentes en la memoria.",
    "mixto": "Equilibrado: natural, con momentos literarios solo cuando el material los sostiene.",
}


def _datos_autobiografia(resumen: dict) -> str:
    return (
        f"Año de nacimiento (null si no se conoce): {json.dumps(resumen.get('anio_nacimiento'))}\n\n"
        f"Cronología:\n{json.dumps(resumen.get('cronologia', []), ensure_ascii=False)}\n\n"
        f"Bloques temáticos:\n{json.dumps(resumen.get('bloques', {}), ensure_ascii=False)}"
    )


def _comprobar_memoria_autobiografia(resumen: dict):
    if not resumen.get("cronologia") and not any(resumen.get("bloques", {}).values()):
        raise HTTPException(status_code=400, detail="todavía no hay suficiente memoria guardada para trabajar en la autobiografía")


@app.post("/api/autobiografia/previsualizar")
def previsualizar_autobiografia(payload: AutobiografiaPreviewIn, usuario: str = Depends(obtener_usuario_actual)):
    comprobar_limite_diario(usuario)
    if payload.tono not in TONOS_AUTOBIOGRAFIA:
        raise HTTPException(status_code=400, detail="Tono no permitido")
    resumen = cargar_resumen(usuario)
    _comprobar_memoria_autobiografia(resumen)
    respuesta = llamar_a_claude(
        model=obtener_modelo_usuario(usuario, "autobiografia"),
        max_tokens=1800,
        system=SYSTEM_PROMPT_AUTOBIOGRAFIA_INDICE,
        tools=[HERRAMIENTA_INDICE_AUTOBIOGRAFIA],
        tool_choice={"type": "tool", "name": "proponer_indice_autobiografia"},
        messages=[{"role": "user", "content": f"Tono elegido: {TONOS_AUTOBIOGRAFIA[payload.tono]}\n\n{_datos_autobiografia(resumen)}"}],
        timeout=60.0,
    )
    bloque = next((b for b in respuesta.content if b.type == "tool_use"), None)
    if bloque is None:
        raise HTTPException(status_code=502, detail="No se pudo crear la estructura de la autobiografía")
    return {"tono": payload.tono, "titulo": bloque.input.get("titulo", "Mi autobiografía"), "capitulos": bloque.input.get("capitulos", []),
            "modelo": obtener_modelo_usuario(usuario, "autobiografia"), "tokens_input": respuesta.usage.input_tokens, "tokens_output": respuesta.usage.output_tokens}


@app.post("/api/autobiografia/proyecto")
def guardar_proyecto_autobiografia(payload: AutobiografiaProyectoIn, usuario: str = Depends(obtener_usuario_actual)):
    if payload.tono not in TONOS_AUTOBIOGRAFIA:
        raise HTTPException(status_code=400, detail="Tono no permitido")
    if not payload.capitulos or len(payload.capitulos) > 30:
        raise HTTPException(status_code=400, detail="La estructura debe contener entre 1 y 30 capítulos")
    ahora = datetime.utcnow().isoformat()
    with db() as conn:
        conn.execute("DELETE FROM autobiografia_capitulos WHERE usuario = ?", (usuario,))
        for i, cap in enumerate(payload.capitulos):
            titulo = str(cap.get("titulo") or f"Capítulo {i+1}").strip()[:200]
            enfoque = str(cap.get("enfoque") or "").strip()[:1000]
            conn.execute("INSERT INTO autobiografia_capitulos (usuario, orden, titulo, enfoque) VALUES (?, ?, ?, ?)", (usuario, i, titulo, enfoque))
        conn.execute("INSERT INTO autobiografia_proyectos (usuario,titulo,tono,estructura_json,fecha_actualizada) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(usuario) DO UPDATE SET titulo=excluded.titulo, tono=excluded.tono, estructura_json=excluded.estructura_json, fecha_actualizada=excluded.fecha_actualizada",
                     (usuario, payload.titulo.strip()[:200] or "Mi autobiografía", payload.tono, json.dumps(payload.capitulos, ensure_ascii=False), ahora))
    return ver_autobiografia(usuario)


@app.post("/api/autobiografia/capitulos/{capitulo_id}/generar")
def generar_capitulo_autobiografia(capitulo_id: int, usuario: str = Depends(obtener_usuario_actual)):
    comprobar_limite_diario(usuario)
    resumen = cargar_resumen(usuario)
    _comprobar_memoria_autobiografia(resumen)
    with db() as conn:
        cap = conn.execute("SELECT * FROM autobiografia_capitulos WHERE id=? AND usuario=?", (capitulo_id, usuario)).fetchone()
        proyecto = conn.execute("SELECT tono FROM autobiografia_proyectos WHERE usuario=?", (usuario,)).fetchone()
    if not cap: raise HTTPException(status_code=404, detail="Capítulo no encontrado")
    tono = proyecto["tono"] if proyecto else "natural"
    respuesta = llamar_a_claude(
        model=obtener_modelo_usuario(usuario, "autobiografia"), max_tokens=3500,
        system=SYSTEM_PROMPT_CAPITULO_AUTOBIOGRAFIA,
        messages=[{"role":"user", "content": f"Tono: {TONOS_AUTOBIOGRAFIA.get(tono, TONOS_AUTOBIOGRAFIA['natural'])}\n\nCapítulo: {cap['titulo']}\nEnfoque: {cap['enfoque']}\n\n{_datos_autobiografia(resumen)}"}],
        timeout=100.0,
    )
    contenido = next((b.text for b in respuesta.content if getattr(b, "type", None) == "text"), "").strip()
    if not contenido: raise HTTPException(status_code=502, detail="El modelo no devolvió contenido para el capítulo")
    fecha = datetime.utcnow().isoformat(); modelo = obtener_modelo_usuario(usuario, "autobiografia")
    with db() as conn:
        conn.execute("UPDATE autobiografia_capitulos SET contenido=?, estado='generado', modelo=?, tokens_input=tokens_input+?, tokens_output=tokens_output+?, fecha_generado=?, editado_manual=0 WHERE id=? AND usuario=?",
                     (contenido, modelo, respuesta.usage.input_tokens, respuesta.usage.output_tokens, fecha, capitulo_id, usuario))
    registrar_uso_diario(usuario, respuesta.usage.input_tokens, respuesta.usage.output_tokens)
    return {"ok": True, "capitulo_id": capitulo_id, "contenido": contenido, "fecha_generado": fecha, "modelo": modelo}


@app.put("/api/autobiografia/capitulos/{capitulo_id}")
def editar_capitulo_autobiografia(capitulo_id: int, payload: EditarCapituloIn, usuario: str = Depends(obtener_usuario_actual)):
    with db() as conn:
        cap = conn.execute("SELECT * FROM autobiografia_capitulos WHERE id=? AND usuario=?", (capitulo_id, usuario)).fetchone()
        if not cap: raise HTTPException(status_code=404, detail="Capítulo no encontrado")
        titulo = payload.titulo.strip()[:200] if payload.titulo is not None else cap["titulo"]
        enfoque = payload.enfoque.strip()[:1000] if payload.enfoque is not None else cap["enfoque"]
        contenido = payload.contenido if payload.contenido is not None else cap["contenido"]
        conn.execute("UPDATE autobiografia_capitulos SET titulo=?, enfoque=?, contenido=?, estado=?, editado_manual=? WHERE id=? AND usuario=?",
                     (titulo, enfoque, contenido, "editado" if payload.contenido is not None else cap["estado"], 1 if payload.contenido is not None else cap["editado_manual"], capitulo_id, usuario))
    return {"ok": True}


@app.post("/api/autobiografia/capitulos/{capitulo_id}/mejorar")
def mejorar_capitulo_autobiografia(capitulo_id: int, payload: AccionCapituloIn, usuario: str = Depends(obtener_usuario_actual)):
    comprobar_limite_diario(usuario)
    acciones = {"reescribir": "reescribe el capítulo manteniendo exactamente los hechos", "desarrollar": "desarrolla los pasajes que ya tienen material suficiente, sin añadir hechos", "acortar": "hazlo más conciso sin perder hechos importantes", "literario": "mejora el ritmo y la calidad literaria sin inventar nada", "personal": "haz la voz más personal y cercana sin inventar pensamientos ni hechos"}
    if payload.accion not in acciones: raise HTTPException(status_code=400, detail="Acción no permitida")
    with db() as conn:
        cap = conn.execute("SELECT * FROM autobiografia_capitulos WHERE id=? AND usuario=?", (capitulo_id, usuario)).fetchone()
    if not cap or not cap["contenido"]: raise HTTPException(status_code=400, detail="El capítulo todavía no tiene contenido")
    respuesta = llamar_a_claude(model=obtener_modelo_usuario(usuario, "autobiografia"), max_tokens=3500,
        system=SYSTEM_PROMPT_MEJORA_CAPITULO,
        messages=[{"role":"user", "content": f"Acción: {acciones[payload.accion]}\n\nTexto actual:\n{cap['contenido']}\n\nMemoria disponible:\n{_datos_autobiografia(cargar_resumen(usuario))}"}], timeout=100.0)
    contenido = next((b.text for b in respuesta.content if getattr(b,"type",None)=="text"), "").strip()
    fecha=datetime.utcnow().isoformat(); modelo=obtener_modelo_usuario(usuario,"autobiografia")
    with db() as conn:
        conn.execute("UPDATE autobiografia_capitulos SET contenido=?, estado='editado', modelo=?, tokens_input=tokens_input+?, tokens_output=tokens_output+?, fecha_generado=?, editado_manual=1 WHERE id=? AND usuario=?", (contenido,modelo,respuesta.usage.input_tokens,respuesta.usage.output_tokens,fecha,capitulo_id,usuario))
    registrar_uso_diario(usuario,respuesta.usage.input_tokens,respuesta.usage.output_tokens)
    return {"contenido":contenido,"fecha_generado":fecha,"modelo":modelo}


@app.post("/api/generar-autobiografia")
def generar_autobiografia(usuario: str = Depends(obtener_usuario_actual)):
    """Compatibilidad con la versión anterior: genera una autobiografía completa de una vez."""
    resumen = cargar_resumen(usuario)
    _comprobar_memoria_autobiografia(resumen)
    respuesta = llamar_a_claude(model=obtener_modelo_usuario(usuario, "autobiografia"), max_tokens=8000,
        system=SYSTEM_PROMPT_AUTOBIOGRAFIA,
        messages=[{"role":"user", "content": _datos_autobiografia(resumen)}], timeout=150.0)
    contenido = next((b.text for b in respuesta.content if getattr(b,"type",None)=="text"), "").strip(); fecha=datetime.utcnow().isoformat(); modelo=obtener_modelo_usuario(usuario,"autobiografia")
    with db() as conn:
        conn.execute("INSERT INTO autobiografia (usuario,contenido,fecha_generada,tokens_input,tokens_output,modelo) VALUES (?,?,?,?,?,?) ON CONFLICT(usuario) DO UPDATE SET contenido=excluded.contenido,fecha_generada=excluded.fecha_generada,tokens_input=autobiografia.tokens_input+excluded.tokens_input,tokens_output=autobiografia.tokens_output+excluded.tokens_output,modelo=excluded.modelo", (usuario,contenido,fecha,respuesta.usage.input_tokens,respuesta.usage.output_tokens,modelo))
    registrar_uso_diario(usuario,respuesta.usage.input_tokens,respuesta.usage.output_tokens)
    return {"contenido":contenido,"fecha_generada":fecha,"modelo":modelo}


# ---------------------------------------------------------------------------
# Prompts y datos de la entrevista (igual que antes)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_AUTOBIOGRAFIA_INDICE = """Diseña la estructura de una autobiografía a partir SOLO de la memoria proporcionada.
No escribas la autobiografía. Propón capítulos que tengan suficiente material real.
No inventes hechos ni rellenes huecos. Evita capítulos vacíos o puramente genéricos.
Los capítulos deben poder generarse de forma independiente y, juntos, cubrir la historia sin duplicarla.
"""

HERRAMIENTA_INDICE_AUTOBIOGRAFIA = {
    "name": "proponer_indice_autobiografia",
    "description": "Propone el índice editable de una autobiografía basándose exclusivamente en la memoria disponible.",
    "input_schema": {"type":"object","properties":{
        "titulo":{"type":"string"},
        "capitulos":{"type":"array","items":{"type":"object","properties":{
            "titulo":{"type":"string"}, "enfoque":{"type":"string"}
        },"required":["titulo","enfoque"]}}
    },"required":["titulo","capitulos"]}
}

SYSTEM_PROMPT_CAPITULO_AUTOBIOGRAFIA = """Escribe UN SOLO capítulo de una autobiografía en primera persona.
Usa exclusivamente los hechos y detalles presentes en la memoria proporcionada y en el enfoque del capítulo.
No inventes nombres, fechas, diálogos, pensamientos, emociones, relaciones ni detalles sensoriales.
Si el material es escaso, escribe un capítulo breve: no rellenes con prosa vacía.
No menciones que eres una IA ni el proceso de generación. No pongas un encabezado con el título: devuelve solo el texto del capítulo.
"""

SYSTEM_PROMPT_MEJORA_CAPITULO = """Edita el capítulo según la acción indicada.
Regla fundamental: no añadas ningún hecho que no esté en el texto actual o en la memoria proporcionada.
Si una mejora estilística exigiría inventar información, conserva el contenido factual y mejora solo la expresión.
Devuelve únicamente el capítulo resultante, sin comentarios sobre los cambios.
"""

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
- Cuando la persona cuente un acontecimiento concreto (algo que pasó una
  vez, no una etapa difusa) sin dar ninguna pista temporal —ni año, ni edad,
  ni una referencia relativa como "cuando estudiaba" o "de recién casados"
  que ya puedas situar por el contexto—, pregunta de forma natural, antes de
  cambiar de tema, el año o la edad aproximada en que ocurrió ("¿más o
  menos en qué año fue eso?" o "¿qué edad tenías entonces?"). Hazlo una
  sola vez por acontecimiento: si la persona no lo recuerda con precisión,
  acepta una aproximación ("por los años 80", "de adolescente") o incluso
  que no lo sepa, y sigue adelante sin insistir más en ese punto ni
  convertirlo en un interrogatorio.

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

Regla central de profundidad narrativa:
- No te limites a registrar lo que la persona acaba de decir. Cuando aparezca una persona, lugar, trabajo, afición, conflicto, pérdida, logro, objeto o episodio nuevo que pueda tener importancia biográfica, muestra interés concreto y haz al menos una pregunta de seguimiento antes de abandonar el hilo, salvo que la persona indique que no quiere profundizar.
- Si el nuevo elemento parece especialmente significativo, puedes hacer 2 o 3 preguntas de seguimiento a lo largo de varios turnos, pero UNA por turno. Después cambia de tema de forma natural.
- Busca detalles que conviertan una etiqueta en una historia: qué ocurrió, quién estaba allí, qué cambió, por qué fue importante y cómo encaja con otros momentos de su vida. No preguntes mecánicamente todas esas cosas: elige la más útil.
- Si alguien aparece por primera vez (por ejemplo, "Carlos, un amigo del instituto"), no lo dejes pasar automáticamente. Puedes preguntar quién era para la persona, cómo se conocieron o recordar una anécdota concreta, según lo que ya se haya contado.
- Si aparece un tema nuevo que parece importante, priorízalo sobre tu guion de bloques. La entrevista debe seguir la historia real, no completar una lista.
- Puedes hacer una inferencia provisional sobre el significado narrativo de algo, pero debes presentarla explícitamente como hipótesis y consultarla: "Me da la impresión de que ese trabajo fue importante para ti, ¿lo estoy interpretando bien?". Nunca presentes esa interpretación como un hecho ni la guardes como memoria factual si la persona no la confirma.
- Si detectas una posible contradicción entre lo que se acaba de contar y algo del contexto previo, no elijas silenciosamente una versión. Pregunta con tacto cuál es correcta o si ambas cosas pueden ser ciertas.
- Si la persona corrige una suposición tuya, acepta la corrección sin defender la interpretación anterior y continúa desde la nueva información.
- Si se pide "sorpréndeme", elige un hilo poco explorado, una persona mencionada de pasada, un contraste entre dos etapas, un tema pendiente o una conexión interesante entre recuerdos. No hagas una pregunta aleatoria.
- No conviertas esto en un interrogatorio. La profundidad debe alternarse con conversación natural. Si un elemento no parece importante o la persona lo responde brevemente, acéptalo y continúa.

"""

SYSTEM_PROMPT_APORTACION = """Vas a entrevistar a un familiar o allegado de la persona protagonista
de este proyecto, para recoger su testimonio y sus recuerdos sobre ella —
no sobre quien tienes delante ahora mismo.

Al principio de la conversación se te indica el nombre de la persona
protagonista, el nombre de quien vas a entrevistar y su relación con ella.
No hace falta que vuelvas a preguntar esos datos: saluda ya sabiendo quién
es, y explica en una frase para qué es esta charla.

Haz preguntas abiertas y cálidas sobre: cómo describiría a la persona
protagonista, anécdotas concretas que recuerde, momentos compartidos que
destacaría, qué virtudes o rasgos de carácter le atribuye, y cualquier
historia que crea que merece quedar recogida.

Sé breve en tus intervenciones (2-4 frases), con un tono cercano y natural,
como una charla, no un interrogatorio. No des consejos ni opines sobre lo
que se cuenta: limítate a escuchar y a preguntar con curiosidad genuina
cuando algo se cuenta muy por encima.

No pidas datos de contacto ni información sensible de terceros que no
salga de forma natural en la propia conversación.
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

Además, genera un título breve (entre 3 y 6 palabras) que resuma de qué ha
tratado principalmente ESTA sesión en concreto (no toda la biografía, solo
la transcripción nueva que se te da ahora). Debe ser concreto y reconocible
de un vistazo, por ejemplo "Servicio militar en Cartagena" o "Boda y primer
piso", no algo genérico como "Recuerdos de la infancia" si se puede ser más
específico.

Además, identifica de forma separada la estrategia futura de entrevista. Esto NO es memoria factual: son pistas para decidir qué explorar después.

Usa la herramienta que tienes disponible para guardar el resultado."""

HERRAMIENTA_RESUMEN = {
    "name": "guardar_resumen_memoria",
    "description": "Guarda el resumen actualizado de la memoria biográfica de la persona, organizado por bloques temáticos y por una cronología de eventos con años exactos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "titulo_sesion": {
                "type": "string",
                "description": "Título breve (3-6 palabras) que resume de qué ha tratado esta sesión en concreto, no toda la biografía",
            },
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
            "estrategia": {
                "type": "object",
                "description": "Estado de planificación de la entrevista; no es memoria factual",
                "properties": {
                    "hilos": {"type":"array","items":{"type":"object","properties":{
                        "nombre":{"type":"string"},"tipo":{"type":"string"},"contexto":{"type":"string"},
                        "importancia":{"type":"string"},"explorado":{"type":"integer"},"estado":{"type":"string"}
                    },"required":["nombre","tipo","contexto","importancia","explorado","estado"]}},
                    "contradicciones": {"type":"array","items":{"type":"string"}},
                    "hipotesis_pendientes": {"type":"array","items":{"type":"string"}}
                },
                "required":["hilos","contradicciones","hipotesis_pendientes"]
            },
        },
        "required": ["titulo_sesion", "anio_nacimiento", "bloques", "cronologia", "temas_pendientes", "estrategia"],
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