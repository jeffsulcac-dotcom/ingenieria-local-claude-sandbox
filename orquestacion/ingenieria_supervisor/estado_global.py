"""
Estado operativo GLOBAL del Supervisor: una única base SQLite por repositorio.

A partir de A2:

    SQLite  = autoridad del ESTADO OPERATIVO de cada tarea
              (estado, rama, worktree, intentos, trabajador, latido, fallas,
              resolución de decisiones humanas, última verificación, eventos).

    JSON    = DEFINICIÓN versionada de cada tarea
              (id, título, objetivo, criterios, ámbito, pruebas requeridas y
              decisiones humanas declaradas: clave y descripción).

Ubicación de la base
--------------------
La base vive en el directorio común de Git del repositorio:

    <git rev-parse --git-common-dir>/ingenieria-supervisor.sqlite3

Ese directorio es el mismo desde la rama principal y desde cualquier
worktree enlazado, de modo que todos comparten UNA sola base. Al estar dentro
de `.git/`, nunca se versiona ni aparece en `git status`. Si el repositorio se
mueve de carpeta, la ruta se vuelve a resolver sola.

Sin Git no hay ubicación global posible: se falla de forma explícita en lugar
de crear una base local silenciosa que pudiera divergir de la global.

Sólo se usa `sqlite3` de la biblioteca estándar. Sin ORM. Sin Internet.

Desde A3.1 este módulo sí implementa la TOMA ATÓMICA de una tarea
(`reclamar`): un UPDATE condicional dentro de una transacción
BEGIN IMMEDIATE, resuelto por `rowcount`, que garantiza un único ganador
entre trabajadores concurrentes.

Eso cubre la CONCESIÓN de la toma, no su propiedad posterior:
`actualizar_tarea` sigue siendo un UPDATE incondicional y las demás
operaciones del ciclo lo usan a través de `supervisor.persistir`. Una de
ellas que llegue con una lectura vieja puede sobrescribir al ganador.

Sigue sin implementar: propiedad efectiva del claim, latidos automáticos,
expiración de trabajadores, detección de trabajadores muertos y
recuperación automática de tareas abandonadas. Eso queda para A3.2/B.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from ingenieria_nucleo.estados import Estado

from .tarea import (
    Ficha,
    ahora_utc,
    listar_con_errores,
    ruta_relativa_ficha,
)


NOMBRE_BASE = "ingenieria-supervisor.sqlite3"

VERSION_ESQUEMA = 2

# Milisegundos que una conexión espera si otra tiene la base ocupada.
BUSY_TIMEOUT_MS = 5000

# Reintentos para la conversión inicial a WAL, que el busy_timeout no cubre.
INTENTOS_JOURNAL = 12
ESPERA_JOURNAL_S = 0.02
ESPERA_JOURNAL_MAXIMA_S = 0.25

# WAL: lecturas que no bloquean escrituras, adecuado para uso local.
# synchronous=FULL: un corte de energía no pierde transacciones confirmadas.
JOURNAL_MODE = "wal"
SYNCHRONOUS = "FULL"

# Tipos de evento del historial operativo.
EVENTO_CREACION = "creacion"
EVENTO_IMPORTACION = "importacion"
EVENTO_SINCRONIZACION = "sincronizacion"
EVENTO_TRANSICION = "transicion"
EVENTO_VERIFICACION = "verificacion"
EVENTO_DECISION = "decision"
EVENTO_RECUPERACION = "recuperacion"

ORIGEN_AUTOMATICO = "automático"

# Estados en los que una tarea RETIENE su ámbito de archivos.
#
# No basta con EN_EJECUCION: una tarea que quedó en requiere_revision o en
# propuesto conserva cambios sin confirmar en el árbol de trabajo, así que
# sigue siendo la dueña de esos archivos hasta que un humano la cierre.
#
# Vive aquí, y no en `supervisor`, porque la guarda que impide cambiarle el
# ámbito a una tarea viva está en `sincronizar_ficha`, que es de este
# módulo. `supervisor` la reexporta para no romper a quien ya la importaba.
ESTADOS_QUE_RETIENEN_AMBITO = frozenset(
    {
        str(Estado.EN_EJECUCION),
        str(Estado.REQUIERE_REVISION),
        str(Estado.PROPUESTO),
    }
)

# Acciones que puede devolver la sincronización de una definición.
ACCION_SIN_CAMBIOS = "sin_cambios"
ACCION_ACTUALIZADA = "actualizada"
ACCION_IMPORTADA = "importada"
ACCION_AMBITO_CONGELADO = "ambito_congelado"

# Resultados posibles de un intento de toma atómica (A3.1).
CLAIM_OTORGADO = "otorgado"
CLAIM_RECHAZADO = "rechazado"

# Motivos por los que una toma se rechaza.
MOTIVO_INEXISTENTE = "inexistente"
MOTIVO_YA_RECLAMADA = "ya_reclamada"
MOTIVO_ESTADO_NO_RECLAMABLE = "estado_no_reclamable"

# Resultado de una escritura condicionada por propiedad (A3.2).
ESCRITURA_ACEPTADA = "aceptada"
ESCRITURA_RECHAZADA = "rechazada"

# Motivos por los que se rechaza una orden del ciclo.
MOTIVO_SIN_PROPIETARIO = "sin_propietario"
MOTIVO_OTRO_PROPIETARIO = "otro_propietario"
MOTIVO_GENERACION_VENCIDA = "generacion_vencida"
MOTIVO_ESTADO_INCOMPATIBLE = "estado_incompatible"

# Migraciones versionadas. Cada versión es una lista de sentencias que se
# aplican dentro de una única transacción. Nunca se edita una versión ya
# publicada: se añade la siguiente.
MIGRACIONES = {
    1: [
        """
        CREATE TABLE tareas (
            id                          TEXT PRIMARY KEY,
            titulo                      TEXT NOT NULL,
            estado                      TEXT NOT NULL,
            rama                        TEXT,
            worktree                    TEXT,
            intentos                    INTEGER NOT NULL DEFAULT 0,
            max_intentos                INTEGER NOT NULL DEFAULT 3,
            trabajador_id               TEXT,
            pid                         INTEGER,
            iniciado_en                 TEXT,
            ultimo_latido               TEXT,
            creado_en                   TEXT NOT NULL,
            actualizado_en              TEXT NOT NULL,
            ultima_falla                TEXT,
            requiere_decision_humana    INTEGER NOT NULL DEFAULT 0,
            decisiones                  TEXT NOT NULL DEFAULT '[]',
            ejecuciones                 TEXT NOT NULL DEFAULT '[]',
            ultima_verificacion         TEXT,
            commit_inicial              TEXT,
            ambito_archivos             TEXT NOT NULL DEFAULT '[]',
            definicion_ruta             TEXT NOT NULL,
            definicion_hash             TEXT NOT NULL,
            definicion_sincronizada_en  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE eventos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tarea_id        TEXT NOT NULL
                            REFERENCES tareas(id) ON DELETE RESTRICT,
            fecha           TEXT NOT NULL,
            tipo            TEXT NOT NULL,
            estado_anterior TEXT,
            estado_nuevo    TEXT,
            motivo          TEXT,
            origen          TEXT,
            datos           TEXT
        )
        """,
        """
        CREATE INDEX eventos_por_tarea
            ON eventos (tarea_id, fecha, id)
        """,
        """
        CREATE INDEX eventos_por_fecha
            ON eventos (fecha, id)
        """,
    ],
    2: [
        # A3.2 — identificador de propiedad vigente.
        #
        # `generacion` distingue una ejecución de otra sobre la MISMA tarea.
        # Sólo `reclamar` la incrementa, y lo hace dentro del mismo UPDATE
        # condicional que concede la toma. Las órdenes posteriores viajan con
        # la generación que leyeron: si entretanto hubo una toma nueva, su
        # predicado ya no casa y la escritura se rechaza sin tocar nada.
        #
        # Un entero es suficiente y es lo más simple que funciona: no
        # depende del reloj (dos tomas en el mismo segundo se distinguen),
        # no depende del PID (el sistema los reutiliza) y no necesita
        # criptografía para una sola PC.
        #
        # 0 = fila heredada de A2/A3.1 que nunca fue reclamada bajo A3.2.
        "ALTER TABLE tareas ADD COLUMN generacion INTEGER NOT NULL DEFAULT 0",
    ],
}


class ErrorEstadoGlobal(Exception):
    """La base global no se pudo ubicar, abrir, migrar o escribir."""


# ----------------------------------------------------------------------
# Ubicación
# ----------------------------------------------------------------------

def git_common_dir(raiz: Path) -> Path:
    """
    Directorio común de Git del repositorio que contiene `raiz`.

    Desde la rama principal Git responde `.git` (relativo a la raíz); desde un
    worktree enlazado responde la ruta absoluta del `.git` principal. En ambos
    casos el resultado es el MISMO directorio.
    """
    raiz = Path(raiz)

    try:
        resultado = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(raiz),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise ErrorEstadoGlobal(
            "Git no está disponible; sin Git no se puede ubicar la base "
            "global del Supervisor: " + str(error)
        ) from None

    if resultado.returncode != 0:
        raise ErrorEstadoGlobal(
            "La carpeta '" + str(raiz) + "' no pertenece a un repositorio "
            "Git; sin repositorio no existe base global del Supervisor. "
            + resultado.stderr.strip()
        )

    salida = resultado.stdout.strip()

    if not salida:
        raise ErrorEstadoGlobal(
            "Git no devolvió ningún directorio común para '" + str(raiz) + "'."
        )

    comun = Path(salida)

    if not comun.is_absolute():
        comun = raiz / comun

    return comun.resolve()


def ruta_base(raiz: Path) -> Path:
    """Ruta absoluta de la base global para el repositorio de `raiz`."""
    return git_common_dir(raiz) / NOMBRE_BASE


def ubicacion_resumida(ruta: Path) -> str:
    """Forma corta y estable de la ruta, para mostrar en pantalla."""
    ruta = Path(ruta)

    partes = ruta.parts

    if len(partes) >= 3:
        return "…/" + "/".join(partes[-3:])

    return ruta.as_posix()


# ----------------------------------------------------------------------
# Conexión, pragmas, transacciones
# ----------------------------------------------------------------------

def _activar_journal(con: sqlite3.Connection, ruta: Path) -> str:
    """
    Deja la base en `JOURNAL_MODE`, tolerando el arranque concurrente.

    La conversión inicial delete -> wal necesita un bloqueo exclusivo
    momentáneo, y para ESE cambio SQLite no llama al manejador de ocupado:
    el `busy_timeout` no lo cubre y la orden falla en el acto con
    "database is locked". Con varios procesos arrancando a la vez sobre una
    base nueva, los perdedores morían ahí, antes incluso de llegar al
    esquema.

    Dos medidas, las dos mínimas:

    1. No se pide el cambio si la base YA está en el modo deseado. Ése es el
       caso normal a partir del segundo arranque, y así deja de haber
       conversión que pueda chocar.

    2. Si hay que convertir y otro proceso está haciéndolo, se reintenta un
       número acotado de veces. Entre intento e intento se vuelve a LEER el
       modo: lo más probable es que el otro ya terminara, y entonces no hay
       nada que hacer.

    El modo se devuelve para que quien llama compruebe el resultado real.
    """
    modo = con.execute("PRAGMA journal_mode").fetchone()[0]

    if str(modo).lower() == JOURNAL_MODE:
        return str(modo)

    espera = ESPERA_JOURNAL_S
    ultimo = None

    for intento in range(INTENTOS_JOURNAL):
        try:
            modo = con.execute(
                "PRAGMA journal_mode = " + JOURNAL_MODE
            ).fetchone()[0]

            if str(modo).lower() == JOURNAL_MODE:
                return str(modo)

            ultimo = "quedó en '" + str(modo) + "'"
        except sqlite3.OperationalError as error:
            # Sólo se reintenta el choque de la conversión. Cualquier otro
            # error operativo es real y debe salir sin disfrazarse.
            if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                raise

            ultimo = str(error)

        if intento + 1 < INTENTOS_JOURNAL:
            time.sleep(espera)
            espera = min(espera * 2, ESPERA_JOURNAL_MAXIMA_S)

            # Puede que el otro proceso ya lo dejara listo.
            modo = con.execute("PRAGMA journal_mode").fetchone()[0]

            if str(modo).lower() == JOURNAL_MODE:
                return str(modo)

    raise ErrorEstadoGlobal(
        "No se pudo poner la base '" + str(ruta) + "' en journal_mode="
        + JOURNAL_MODE + " tras " + str(INTENTOS_JOURNAL)
        + " intentos (" + str(ultimo) + "). ¿Hay otro proceso bloqueándola?"
    )


def abrir(ruta: Path, solo_lectura: bool = False) -> sqlite3.Connection:
    """
    Abre una conexión con los pragmas del Supervisor ya aplicados.

    `isolation_level=None`: las transacciones se manejan de forma explícita
    con BEGIN IMMEDIATE / COMMIT / ROLLBACK (ver `transaccion`).
    """
    ruta = Path(ruta)

    try:
        if solo_lectura:
            con = sqlite3.connect(
                ruta.as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
        else:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(
                str(ruta),
                isolation_level=None,
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
    except sqlite3.Error as error:
        raise ErrorEstadoGlobal(
            "No se pudo abrir la base global '" + str(ruta) + "': "
            + str(error)
        ) from None

    con.row_factory = sqlite3.Row

    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = " + str(BUSY_TIMEOUT_MS))

        if not solo_lectura:
            modo = _activar_journal(con, ruta)

            if str(modo).lower() != JOURNAL_MODE:
                raise ErrorEstadoGlobal(
                    "SQLite no pudo activar journal_mode=" + JOURNAL_MODE
                    + " en '" + str(ruta) + "' (quedó en '" + str(modo)
                    + "'). ¿La base está en una unidad de red?"
                )

            con.execute("PRAGMA synchronous = " + SYNCHRONOUS)
    except sqlite3.Error as error:
        con.close()
        raise ErrorEstadoGlobal(
            "No se pudieron aplicar los pragmas a '" + str(ruta) + "': "
            + str(error)
        ) from None
    except ErrorEstadoGlobal:
        con.close()
        raise

    return con


@contextmanager
def conexion(raiz: Path, inicializar_esquema: bool = True):
    """
    Conexión a la base global del repositorio, cerrada al salir.

    Por omisión garantiza el esquema al día (operación idempotente).
    """
    con = abrir(ruta_base(raiz))

    try:
        if inicializar_esquema:
            inicializar(con)
        yield con
    finally:
        con.close()


def _deshacer(con: sqlite3.Connection) -> None:
    """
    ROLLBACK que nunca tapa la causa real del fallo.

    Ante un disco lleno o un error de E/S, SQLite deshace la transacción
    por su cuenta. El ROLLBACK explícito llega entonces a una transacción
    que ya no existe y lanza 'cannot rollback - no transaction is active'.
    Si se dejara escapar, esa excepción sustituiría al error que de verdad
    importa ("database or disk is full") y, al no ser `ErrorEstadoGlobal`,
    la línea de órdenes la mostraría como un fallo desnudo en inglés en
    lugar del mensaje en español con su código de salida.

    Aquí no hay nada que rescatar: la transacción está deshecha de un modo
    u otro, que es lo único que se pedía.
    """
    try:
        con.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextmanager
def transaccion(con: sqlite3.Connection):
    """
    BEGIN IMMEDIATE ... COMMIT, con ROLLBACK ante cualquier excepción.

    BEGIN IMMEDIATE toma el bloqueo de escritura al empezar, de modo que
    una transacción no descubre a mitad de camino que otra conexión escribió.

    Salga por donde salga, la transacción queda cerrada y el error que
    llega a quien llamó es siempre `ErrorEstadoGlobal` (o la excepción
    original, si no vino de SQLite), nunca un fallo de la propia limpieza.
    """
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        raise ErrorEstadoGlobal(
            "No se pudo iniciar la transacción: " + str(error)
        ) from None

    try:
        yield con
    except sqlite3.Error as error:
        _deshacer(con)
        raise ErrorEstadoGlobal(
            "Transacción anulada por error de SQLite: " + str(error)
        ) from error
    except BaseException:
        _deshacer(con)
        raise

    try:
        con.execute("COMMIT")
    except sqlite3.Error as error:
        # Un COMMIT que falla puede dejar la transacción todavía abierta:
        # se deshace antes de informar, para no bloquear a los demás.
        _deshacer(con)
        raise ErrorEstadoGlobal(
            "No se pudo confirmar la transacción: " + str(error)
        ) from error


# ----------------------------------------------------------------------
# Esquema y migraciones
# ----------------------------------------------------------------------

def version_esquema(con: sqlite3.Connection) -> int:
    existe = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'esquema'"
    ).fetchone()

    if existe is None:
        return 0

    fila = con.execute("SELECT MAX(version) AS version FROM esquema").fetchone()

    return int(fila["version"] or 0)


def inicializar(con: sqlite3.Connection) -> dict:
    """
    Crea o migra el esquema hasta VERSION_ESQUEMA. Idempotente.

    Cada versión se aplica en su propia transacción: un corte a mitad de una
    migración deja la base en la versión anterior, íntegra.
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS esquema (
            version     INTEGER PRIMARY KEY,
            aplicado_en TEXT NOT NULL
        )
        """
    )

    anterior = version_esquema(con)

    if anterior > VERSION_ESQUEMA:
        raise ErrorEstadoGlobal(
            "La base global está en la versión de esquema "
            + str(anterior)
            + ", más nueva que la que entiende este Supervisor ("
            + str(VERSION_ESQUEMA)
            + ")."
        )

    aplicadas = []

    for version in range(anterior + 1, VERSION_ESQUEMA + 1):
        sentencias = MIGRACIONES.get(version)

        if not sentencias:
            raise ErrorEstadoGlobal(
                "Falta la migración de esquema número " + str(version) + "."
            )

        try:
            with transaccion(con):
                # La versión se RELEE aquí dentro, con el bloqueo de
                # escritura ya tomado por BEGIN IMMEDIATE.
                #
                # La lectura de arriba se hizo en autocommit: entre aquella
                # lectura y este punto, otro proceso pudo aplicar esta misma
                # migración entera. Sin esta comprobación, el perdedor de esa
                # carrera ejecutaba "CREATE TABLE tareas" sobre una base que
                # ya la tenía y moría con "table tareas already exists".
                #
                # Añadir IF NOT EXISTS a los CREATE no bastaba: el error se
                # desplazaba al INSERT de la versión, que choca contra la
                # clave primaria de `esquema`.
                if version_esquema(con) >= version:
                    continue

                for sentencia in sentencias:
                    con.execute(sentencia)

                con.execute(
                    "INSERT INTO esquema (version, aplicado_en) VALUES (?, ?)",
                    (version, ahora_utc()),
                )
        except sqlite3.Error as error:
            raise ErrorEstadoGlobal(
                "Falló la migración de esquema número "
                + str(version)
                + ": "
                + str(error)
            ) from None

        aplicadas.append(version)

    return {
        "version_anterior": anterior,
        "version_actual": version_esquema(con),
        "aplicadas": aplicadas,
    }


# ----------------------------------------------------------------------
# Conversión fila <-> ficha
# ----------------------------------------------------------------------

def _a_json(valor) -> str | None:
    if valor is None:
        return None

    return json.dumps(valor, ensure_ascii=False, sort_keys=True)


def _de_json(texto, por_omision=None):
    if texto is None or texto == "":
        return por_omision

    try:
        return json.loads(texto)
    except (TypeError, ValueError):
        return por_omision


def hash_definicion(ficha: Ficha) -> str:
    """
    Huella de la parte DECLARATIVA de la ficha.

    Sólo entran los campos que definen la tarea. Los campos operativos y las
    resoluciones de decisiones quedan fuera a propósito: cambian sin que la
    definición cambie.
    """
    definicion = {
        "id": ficha.id,
        "titulo": ficha.titulo,
        "objetivo": ficha.objetivo,
        "criterios_aceptacion": list(ficha.criterios_aceptacion),
        "ambito_archivos": list(ficha.ambito_archivos),
        "pruebas_requeridas": list(ficha.pruebas_requeridas),
        "decisiones": [
            {
                "clave": str(decision.get("clave")),
                "descripcion": str(decision.get("descripcion") or ""),
            }
            for decision in ficha.requiere_decision_humana
        ],
    }

    crudo = json.dumps(definicion, ensure_ascii=False, sort_keys=True)

    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


def decisiones_operativas(decisiones: list[dict]) -> list[dict]:
    """Parte operativa de cada decisión: lo que gobierna SQLite."""
    resultado = []

    for decision in decisiones:
        resultado.append(
            {
                "clave": str(decision.get("clave")),
                "resuelta": bool(decision.get("resuelta", False)),
                "resolucion": decision.get("resolucion"),
                "resuelta_en": decision.get("resuelta_en"),
                "origen": decision.get("origen"),
            }
        )

    return resultado


def fusionar_decisiones(declaradas: list[dict], operativas: list[dict]) -> list[dict]:
    """
    Une la definición (JSON: clave, descripción) con el estado (SQLite).

    El orden y el conjunto de claves los manda la definición. Una clave
    declarada sin estado registrado es una decisión pendiente. El estado
    registrado para una clave que la definición ya no declara se ignora.
    """
    por_clave = {}

    for operativa in operativas:
        clave = str(operativa.get("clave"))
        if clave not in por_clave:
            por_clave[clave] = operativa

    fusionadas = []

    for declarada in declaradas:
        clave = str(declarada.get("clave"))
        estado = por_clave.get(clave, {})

        fusionadas.append(
            {
                "clave": clave,
                "descripcion": str(declarada.get("descripcion") or ""),
                "resuelta": bool(estado.get("resuelta", False)),
                "resolucion": estado.get("resolucion"),
                "resuelta_en": estado.get("resuelta_en"),
                "origen": estado.get("origen"),
            }
        )

    return fusionadas


def resumen_de_corrida(corrida: dict | None) -> dict | None:
    """
    Lo que se conserva de una verificación en el estado global.

    Se guarda el veredicto por prueba, pero no la salida completa de cada
    una: ésa permanece en la ficha espejo y en la consola del corredor.
    """
    if not corrida or corrida.get("tipo") != "corrida":
        return None

    return {
        "fecha": corrida.get("fecha"),
        "resultado": corrida.get("resultado"),
        "motivo": corrida.get("motivo"),
        "total": corrida.get("total", 0),
        "ok": corrida.get("ok", 0),
        "fallidas": corrida.get("fallidas", 0),
        "indeterminadas": corrida.get("indeterminadas", 0),
        "agotadas": corrida.get("agotadas", 0),
        "problemas": list(corrida.get("problemas", [])),
        "pruebas": [
            {
                "prueba": una.get("prueba"),
                "veredicto": una.get("veredicto"),
                "marca": una.get("marca"),
                "codigo": una.get("codigo"),
                "duracion_s": una.get("duracion_s"),
            }
            for una in corrida.get("detalle", [])
        ],
    }


def ultima_corrida_de(ficha: Ficha) -> dict | None:
    for ejecucion in reversed(ficha.ejecuciones):
        if ejecucion.get("tipo") == "corrida":
            return ejecucion

    return None


def fila_desde_ficha(ficha: Ficha, ahora: str | None = None) -> dict:
    """Columnas de `tareas` a partir de una ficha en memoria."""
    ahora = ahora or ahora_utc()

    pendientes = [
        una for una in ficha.requiere_decision_humana
        if not una.get("resuelta", False)
    ]

    return {
        "id": ficha.id,
        "titulo": ficha.titulo,
        "estado": str(ficha.estado),
        "rama": ficha.rama,
        "worktree": ficha.worktree,
        "intentos": int(ficha.intentos),
        "max_intentos": int(ficha.max_intentos),
        "trabajador_id": ficha.trabajador_id,
        "pid": ficha.pid,
        "iniciado_en": ficha.iniciado_en,
        "ultimo_latido": ficha.ultimo_latido,
        "creado_en": ficha.creado_en or ahora,
        "actualizado_en": ficha.actualizado_en or ahora,
        "ultima_falla": _a_json(ficha.ultima_falla),
        "requiere_decision_humana": 1 if pendientes else 0,
        "decisiones": _a_json(
            decisiones_operativas(ficha.requiere_decision_humana)
        ),
        "ejecuciones": _a_json(list(ficha.ejecuciones)),
        "ultima_verificacion": _a_json(
            resumen_de_corrida(ultima_corrida_de(ficha))
        ),
        "commit_inicial": ficha.commit_inicial,
        "ambito_archivos": _a_json(list(ficha.ambito_archivos)),
        "definicion_ruta": ruta_relativa_ficha(ficha.id),
        "definicion_hash": hash_definicion(ficha),
        "definicion_sincronizada_en": ahora,
        "generacion": int(ficha.generacion or 0),
    }


def fila_a_dict(fila) -> dict:
    """Fila de `tareas` como diccionario plano con los JSON ya decodificados."""
    datos = dict(fila)

    datos["ultima_falla"] = _de_json(datos.get("ultima_falla"))
    datos["decisiones"] = _de_json(datos.get("decisiones"), [])
    datos["ejecuciones"] = _de_json(datos.get("ejecuciones"), [])
    datos["ultima_verificacion"] = _de_json(datos.get("ultima_verificacion"))
    datos["ambito_archivos"] = _de_json(datos.get("ambito_archivos"), [])
    datos["requiere_decision_humana"] = bool(
        datos.get("requiere_decision_humana")
    )

    return datos


def aplicar_fila(ficha: Ficha, fila: dict) -> Ficha:
    """
    Superpone el estado operativo de SQLite sobre la ficha leída del JSON.

    Después de esta llamada, la ficha refleja la AUTORIDAD: lo que el JSON
    dijera sobre estos campos deja de contar.
    """
    ficha.estado = Estado(fila["estado"])
    ficha.rama = fila.get("rama")
    ficha.worktree = fila.get("worktree")
    ficha.intentos = int(fila.get("intentos") or 0)
    ficha.max_intentos = int(fila.get("max_intentos") or 1)
    ficha.trabajador_id = fila.get("trabajador_id")
    ficha.pid = fila.get("pid")
    ficha.iniciado_en = fila.get("iniciado_en")
    ficha.ultimo_latido = fila.get("ultimo_latido")
    ficha.actualizado_en = fila.get("actualizado_en") or ficha.actualizado_en
    ficha.creado_en = fila.get("creado_en") or ficha.creado_en
    ficha.ultima_falla = fila.get("ultima_falla")
    ficha.commit_inicial = fila.get("commit_inicial")
    ficha.generacion = int(fila.get("generacion") or 0)
    ficha.ejecuciones = list(fila.get("ejecuciones") or [])

    ficha.requiere_decision_humana = fusionar_decisiones(
        ficha.requiere_decision_humana,
        fila.get("decisiones") or [],
    )

    return ficha


# ----------------------------------------------------------------------
# Lectura y escritura de tareas y eventos
# ----------------------------------------------------------------------

COLUMNAS_TAREA = (
    "id", "titulo", "estado", "rama", "worktree", "intentos", "max_intentos",
    "trabajador_id", "pid", "iniciado_en", "ultimo_latido", "creado_en",
    "actualizado_en", "ultima_falla", "requiere_decision_humana",
    "decisiones", "ejecuciones", "ultima_verificacion", "commit_inicial",
    "ambito_archivos", "definicion_ruta", "definicion_hash",
    "definicion_sincronizada_en", "generacion",
)


def obtener_tarea(con: sqlite3.Connection, identificador: str) -> dict | None:
    fila = con.execute(
        "SELECT * FROM tareas WHERE id = ?", (identificador,)
    ).fetchone()

    if fila is None:
        return None

    return fila_a_dict(fila)


def listar_tareas(con: sqlite3.Connection) -> list[dict]:
    return [
        fila_a_dict(fila)
        for fila in con.execute("SELECT * FROM tareas ORDER BY id")
    ]


def contar_tareas(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM tareas").fetchone()[0])


def insertar_tarea(con: sqlite3.Connection, fila: dict) -> None:
    columnas = ", ".join(COLUMNAS_TAREA)
    marcas = ", ".join("?" for _ in COLUMNAS_TAREA)

    con.execute(
        "INSERT INTO tareas (" + columnas + ") VALUES (" + marcas + ")",
        tuple(fila[columna] for columna in COLUMNAS_TAREA),
    )


def actualizar_tarea(con: sqlite3.Connection, identificador: str, campos: dict) -> None:
    """Actualiza sólo las columnas indicadas. Falla si la tarea no existe."""
    if not campos:
        return

    for columna in campos:
        if columna not in COLUMNAS_TAREA or columna == "id":
            raise ErrorEstadoGlobal(
                "Columna desconocida o no actualizable: '" + str(columna) + "'."
            )

    asignaciones = ", ".join(columna + " = ?" for columna in campos)

    cursor = con.execute(
        "UPDATE tareas SET " + asignaciones + " WHERE id = ?",
        tuple(campos.values()) + (identificador,),
    )

    if cursor.rowcount != 1:
        raise ErrorEstadoGlobal(
            "La tarea '" + identificador + "' no existe en el estado global."
        )


# ----------------------------------------------------------------------
# Toma atómica de tareas (A3.1)
# ----------------------------------------------------------------------

def reclamar(
    con: sqlite3.Connection,
    identificador: str,
    trabajador_id: str,
    pid: int | None,
    momento: str,
    estados_reclamables,
    estado_destino: str,
    campos_extra: dict | None = None,
) -> dict:
    """
    Toma ATÓMICA de una tarea. Un solo ganador, siempre.

    DEBE ejecutarse dentro de `transaccion(con)` (BEGIN IMMEDIATE).

    Por qué no puede haber dos tomas concedidas
    -------------------------------------------
    1. `BEGIN IMMEDIATE` pide el bloqueo de escritura en el primer instante
       de la transacción, no a mitad de camino. SQLite admite UN solo
       escritor a la vez sobre la base, así que dos tomas no se solapan:
       la segunda espera (busy_timeout) a que la primera confirme o anule,
       y sólo entonces ve la base.

    2. El estado esperado viaja en la propia cláusula WHERE del UPDATE. El
       motor comprueba la condición contra la fila REAL en el momento de
       escribir, no contra una lectura anterior: no queda ninguna ventana
       entre comprobar y escribir (TOCTOU).

    3. La decisión se toma con `rowcount`, que es el número de filas que el
       motor modificó de verdad. 1 = la fila seguía reclamable y ahora es
       nuestra; 0 = alguien se adelantó. No se deduce de ninguna lectura
       previa hecha por Python.

    Conceder la toma cambia el estado a uno que YA NO es reclamable, de modo
    que el propio cambio cierra la puerta al siguiente aspirante.

    Un conflicto NO es una excepción: se devuelve descrito. Las excepciones
    quedan para los datos mal formados y para los fallos de SQLite.
    """
    if not isinstance(trabajador_id, str) or not trabajador_id.strip():
        raise ErrorEstadoGlobal(
            "Para reclamar una tarea hace falta la identidad del trabajador."
        )

    trabajador_id = trabajador_id.strip()

    if pid is not None and (
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
    ):
        raise ErrorEstadoGlobal(
            "El PID del trabajador debe ser un entero positivo; se recibió: "
            + repr(pid)
            + "."
        )

    # Ordenados: el SQL y el mensaje de rechazo deben ser idénticos en cada
    # ejecución, aunque quien llama pase un conjunto sin orden definido.
    estados = tuple(sorted({str(estado) for estado in estados_reclamables}))

    if not estados:
        raise ErrorEstadoGlobal(
            "No se indicó ningún estado desde el cual se pueda reclamar."
        )

    destino = str(estado_destino)

    if destino in estados:
        raise ErrorEstadoGlobal(
            "El estado de destino '" + destino + "' también es reclamable: "
            "la toma no cerraría la puerta al siguiente aspirante."
        )

    campos = {
        "estado": destino,
        "trabajador_id": trabajador_id,
        "pid": pid,
        "iniciado_en": momento,
        "ultimo_latido": momento,
    }

    for columna, valor in (campos_extra or {}).items():
        if columna not in COLUMNAS_TAREA or columna in (
            "id", "estado", "trabajador_id", "generacion",
        ):
            raise ErrorEstadoGlobal(
                "Columna que la toma no puede fijar: '" + str(columna) + "'."
            )
        campos[columna] = valor

    # `generacion = generacion + 1` se calcula dentro del motor, sobre el
    # valor REAL de la fila en el instante de escribir. Si se leyera antes y
    # se escribiera el número ya resuelto, dos tomas separadas por un mismo
    # valor leído podrían repetir generación: volvería el problema ABA que
    # esta columna existe para cerrar.
    asignaciones = ", ".join(columna + " = ?" for columna in campos)
    asignaciones += ", generacion = generacion + 1"
    marcas = ", ".join("?" for _ in estados)

    cursor = con.execute(
        "UPDATE tareas SET "
        + asignaciones
        + " WHERE id = ? AND estado IN ("
        + marcas
        + ")",
        tuple(campos.values()) + (identificador,) + estados,
    )

    # `rowcount` sólo puede valer 0 o 1: el WHERE filtra por `id`, que es
    # PRIMARY KEY. La unicidad la garantiza el esquema, no una comprobación
    # en tiempo de ejecución que nadie podría llegar a ejercitar.
    if cursor.rowcount == 1:
        # La generación se relee DENTRO de la misma transacción: es el valor
        # que el COMMIT confirmará, no una predicción hecha en Python.
        concedida = obtener_tarea(con, identificador)

        return {
            "resultado": CLAIM_OTORGADO,
            "tarea": identificador,
            "motivo": None,
            "estado": destino,
            "propietario": trabajador_id,
            "propia": True,
            "pid": pid,
            "generacion": int(concedida["generacion"]),
            "momento": momento,
            "detalle": "Toma concedida a '" + trabajador_id + "' (generación "
            + str(concedida["generacion"]) + ").",
        }

    # rowcount = 0: la fila no existe o ya no estaba en un estado reclamable.
    # El motivo se lee DENTRO de la misma transacción, así que describe
    # exactamente el estado que rechazó esta toma, no uno posterior.
    return rechazo(
        obtener_tarea(con, identificador),
        identificador,
        trabajador_id,
        momento,
        estados,
    )


def rechazo(
    fila: dict | None,
    identificador: str,
    trabajador_id: str,
    momento: str,
    estados_reclamables,
) -> dict:
    """
    Describe por qué NO se concede una toma, con el mismo formato siempre.

    La usan `reclamar`, cuando su UPDATE condicional no modifica ninguna
    fila, y quien necesite rechazar antes de llegar al UPDATE por un
    motivo que ya conoce. Que el informe salga de un solo sitio es lo que
    garantiza que quien reciba el rechazo lo lea igual venga de donde venga.
    """
    estados = tuple(sorted({str(estado) for estado in estados_reclamables}))

    # La misma guarda que `reclamar`: sin estados reclamables, el detalle
    # terminaría en "La admiten: ." y el usuario leería ese sinsentido.
    if not estados:
        raise ErrorEstadoGlobal(
            "No se indicó ningún estado desde el cual se pueda reclamar."
        )

    if fila is None:
        return {
            "resultado": CLAIM_RECHAZADO,
            "tarea": identificador,
            "motivo": MOTIVO_INEXISTENTE,
            "estado": None,
            "propietario": None,
            "propia": False,
            "pid": None,
            "generacion": None,
            "momento": momento,
            "detalle": "La tarea '" + str(identificador) + "' no existe en el "
            "estado global.",
        }

    propietario = fila.get("trabajador_id")
    propia = bool(propietario) and propietario == trabajador_id

    if propietario:
        motivo = MOTIVO_YA_RECLAMADA

        if propia:
            detalle = (
                "La tarea '" + str(identificador) + "' ya la tiene tomada este "
                "mismo trabajador ('" + str(propietario) + "'), en estado '"
                + str(fila["estado"]) + "'. No se vuelve a tomar."
            )
        else:
            detalle = (
                "La tarea '" + str(identificador) + "' ya no está disponible: "
                "la tomó '" + str(propietario) + "' y está en estado '"
                + str(fila["estado"]) + "'."
            )
    else:
        motivo = MOTIVO_ESTADO_NO_RECLAMABLE
        detalle = (
            "La tarea '" + str(identificador) + "' está en estado '"
            + str(fila["estado"]) + "', que no admite toma. La admiten: "
            + ", ".join(estados) + "."
        )

    return {
        "resultado": CLAIM_RECHAZADO,
        "tarea": identificador,
        "motivo": motivo,
        "estado": fila["estado"],
        "propietario": propietario,
        "propia": propia,
        "pid": fila.get("pid"),
        "generacion": fila.get("generacion"),
        "momento": momento,
        "detalle": detalle,
    }


# ----------------------------------------------------------------------
# Propiedad efectiva durante el ciclo (A3.2)
# ----------------------------------------------------------------------

def actualizar_si_propietario(
    con: sqlite3.Connection,
    identificador: str,
    campos: dict,
    generacion: int,
    momento: str,
    trabajador_id: str | None = None,
    estados_admitidos=None,
) -> dict:
    """
    Escritura CONDICIONADA a que quien ordena siga siendo el dueño vigente.

    DEBE ejecutarse dentro de `transaccion(con)` (BEGIN IMMEDIATE).

    Por qué una orden rezagada no puede colarse
    -------------------------------------------
    1. La precondición viaja en el WHERE del UPDATE, no en una lectura
       previa. El motor la comprueba contra la fila REAL en el instante de
       escribir: no queda ninguna ventana entre comprobar y escribir.

    2. `generacion` cambia en cada toma concedida. Una orden emitida por la
       ejecución anterior lleva la generación que leyó y ya no casa con la
       de la fila, aunque el `trabajador_id` sea idéntico. Eso es lo que
       distingue "trabajador A, ejecución vieja" de "trabajador A, ejecución
       nueva": el nombre del trabajador solo nunca bastaría.

    3. La decisión se toma con `rowcount`, no deduciéndola de una lectura
       anterior hecha en Python.

    Un rechazo NO es una excepción aquí: se devuelve descrito, igual que en
    `reclamar`. Y no escribe nada: ni estado, ni intentos, ni marcas de
    tiempo. Quien llama decide si lo convierte en error.
    """
    if not campos:
        raise ErrorEstadoGlobal(
            "Una escritura condicionada necesita al menos una columna."
        )

    for columna in campos:
        if columna not in COLUMNAS_TAREA or columna in ("id", "generacion"):
            raise ErrorEstadoGlobal(
                "Columna desconocida o no actualizable por una orden del "
                "ciclo: '" + str(columna) + "'."
            )

    if isinstance(generacion, bool) or not isinstance(generacion, int):
        raise ErrorEstadoGlobal(
            "La generación de propiedad debe ser un entero; se recibió: "
            + repr(generacion)
            + "."
        )

    condiciones = ["id = ?", "generacion = ?"]
    parametros = list(campos.values()) + [identificador, generacion]

    if trabajador_id is not None:
        if not isinstance(trabajador_id, str) or not trabajador_id.strip():
            raise ErrorEstadoGlobal(
                "La identidad del propietario, si se exige, no puede estar "
                "vacía."
            )

        trabajador_id = trabajador_id.strip()
        condiciones.append("trabajador_id = ?")
        parametros.append(trabajador_id)

    estados = None

    if estados_admitidos is not None:
        estados = tuple(sorted({str(estado) for estado in estados_admitidos}))

        if not estados:
            raise ErrorEstadoGlobal(
                "Si se exige un estado compatible, hay que indicar al menos "
                "uno."
            )

        condiciones.append(
            "estado IN (" + ", ".join("?" for _ in estados) + ")"
        )
        parametros.extend(estados)

    asignaciones = ", ".join(columna + " = ?" for columna in campos)

    cursor = con.execute(
        "UPDATE tareas SET " + asignaciones + " WHERE " + " AND ".join(condiciones),
        tuple(parametros),
    )

    if cursor.rowcount == 1:
        return {
            "resultado": ESCRITURA_ACEPTADA,
            "tarea": identificador,
            "motivo": None,
            "propietario": trabajador_id,
            "generacion": generacion,
            "generacion_vigente": generacion,
            "estado": None,
            "momento": momento,
            "detalle": "Escritura aceptada: la propiedad sigue vigente.",
        }

    # rowcount = 0: la fila no existe, cambió de dueño, cambió de generación
    # o su estado ya no admite esta orden. El motivo se lee DENTRO de la
    # misma transacción, así que describe la fila que rechazó esta orden.
    return rechazo_propiedad(
        obtener_tarea(con, identificador),
        identificador,
        trabajador_id,
        generacion,
        momento,
        estados,
    )


def rechazo_propiedad(
    fila: dict | None,
    identificador: str,
    trabajador_id: str | None,
    generacion: int,
    momento: str,
    estados_admitidos=None,
) -> dict:
    """
    Describe por qué se rechaza una orden del ciclo, con un formato único.

    Distinguir el motivo importa: "otro propietario" y "generación vencida"
    son fallos distintos. El segundo es el caso del MISMO trabajador que
    vuelve a tomar la tarea, y es justo el que un control por identidad
    dejaría pasar.
    """
    base = {
        "resultado": ESCRITURA_RECHAZADA,
        "tarea": identificador,
        "propietario": trabajador_id,
        "generacion": generacion,
        "momento": momento,
    }

    if fila is None:
        base.update(
            {
                "motivo": MOTIVO_INEXISTENTE,
                "generacion_vigente": None,
                "estado": None,
                "propietario_vigente": None,
                "detalle": "La tarea '" + str(identificador) + "' no existe "
                "en el estado global.",
            }
        )

        return base

    vigente = int(fila.get("generacion") or 0)
    dueno = fila.get("trabajador_id")

    base.update(
        {
            "generacion_vigente": vigente,
            "estado": fila["estado"],
            "propietario_vigente": dueno,
        }
    )

    if vigente != generacion:
        base.update(
            {
                "motivo": MOTIVO_GENERACION_VENCIDA,
                "detalle": "Orden rezagada sobre '" + str(identificador)
                + "': se emitió para la generación " + str(generacion)
                + " y la vigente es la " + str(vigente)
                + " (propietario actual: " + str(dueno or "ninguno")
                + "). No se modificó nada.",
            }
        )

        return base

    if trabajador_id is not None and dueno != trabajador_id:
        base.update(
            {
                "motivo": MOTIVO_SIN_PROPIETARIO if not dueno
                else MOTIVO_OTRO_PROPIETARIO,
                "detalle": "La tarea '" + str(identificador) + "' ya no "
                "pertenece a '" + str(trabajador_id) + "': su propietario "
                "vigente es " + (("'" + str(dueno) + "'") if dueno else "ninguno")
                + ". No se modificó nada.",
            }
        )

        return base

    base.update(
        {
            "motivo": MOTIVO_ESTADO_INCOMPATIBLE,
            "detalle": "La tarea '" + str(identificador) + "' está en estado '"
            + str(fila["estado"]) + "', que no admite esta orden"
            + (
                ". La admiten: " + ", ".join(estados_admitidos) + "."
                if estados_admitidos
                else "."
            ),
        }
    )

    return base


def insertar_evento(con: sqlite3.Connection, tarea_id: str, evento: dict) -> int:
    cursor = con.execute(
        """
        INSERT INTO eventos
            (tarea_id, fecha, tipo, estado_anterior, estado_nuevo,
             motivo, origen, datos)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tarea_id,
            evento.get("fecha") or ahora_utc(),
            evento.get("tipo") or EVENTO_TRANSICION,
            evento.get("estado_anterior"),
            evento.get("estado_nuevo"),
            evento.get("motivo"),
            evento.get("origen"),
            _a_json(evento.get("datos")),
        ),
    )

    return int(cursor.lastrowid)


def _evento_a_dict(fila) -> dict:
    datos = dict(fila)
    datos["datos"] = _de_json(datos.get("datos"))
    return datos


def listar_eventos(
    con: sqlite3.Connection,
    tarea_id: str | None = None,
    maximo: int | None = None,
) -> list[dict]:
    """Eventos más recientes primero. Mismo segundo: desempata el id."""
    consulta = (
        "SELECT eventos.*, tareas.titulo AS titulo "
        "FROM eventos JOIN tareas ON tareas.id = eventos.tarea_id "
    )
    parametros: list = []

    if tarea_id is not None:
        consulta = consulta + "WHERE eventos.tarea_id = ? "
        parametros.append(tarea_id)

    consulta = consulta + "ORDER BY eventos.fecha DESC, eventos.id DESC "

    if maximo is not None:
        consulta = consulta + "LIMIT ? "
        parametros.append(int(maximo))

    return [_evento_a_dict(fila) for fila in con.execute(consulta, parametros)]


def contar_eventos(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM eventos").fetchone()[0])


def ultimo_evento(con: sqlite3.Connection) -> dict | None:
    eventos = listar_eventos(con, maximo=1)
    return eventos[0] if eventos else None


def ultima_actualizacion(con: sqlite3.Connection) -> str | None:
    fila = con.execute(
        "SELECT MAX(actualizado_en) AS ultima FROM tareas"
    ).fetchone()

    return fila["ultima"] if fila and fila["ultima"] else None


# ----------------------------------------------------------------------
# Sincronización de definiciones (bootstrap)
# ----------------------------------------------------------------------

def _tipo_de_evento_importado(evento: dict) -> str:
    if evento.get("tipo"):
        return str(evento["tipo"])

    if evento.get("estado_anterior") is None and evento.get("estado_nuevo"):
        return EVENTO_CREACION

    return EVENTO_TRANSICION


def importar_ficha(
    con: sqlite3.Connection,
    ficha: Ficha,
    ahora: str | None = None,
    evento_importacion: bool = True,
) -> dict:
    """
    Primera incorporación de una ficha al estado global.

    Es la migración V1 -> A2 de esa tarea: se copia tal cual el estado
    operativo que el JSON traía (única vez en que el JSON operativo cuenta
    como entrada), se conserva su historial como eventos y su última corrida
    como última verificación. No cambia el estado, no resuelve decisiones,
    no ejecuta nada.

    `evento_importacion=False` se usa cuando la ficha acaba de crearse: el
    evento de creación ya documenta su incorporación.

    Debe llamarse dentro de una transacción.
    """
    ahora = ahora or ahora_utc()

    fila = fila_desde_ficha(ficha, ahora)

    # El JSON conserva su propia fecha de actualización: importar no es
    # una actualización operativa.
    fila["actualizado_en"] = ficha.actualizado_en or ahora

    insertar_tarea(con, fila)

    importados = 0

    for evento in ficha.historial:
        insertar_evento(
            con,
            ficha.id,
            {
                "fecha": evento.get("fecha") or ahora,
                "tipo": _tipo_de_evento_importado(evento),
                "estado_anterior": evento.get("estado_anterior"),
                "estado_nuevo": evento.get("estado_nuevo"),
                "motivo": evento.get("motivo"),
                "origen": evento.get("origen"),
                "datos": evento.get("datos"),
            },
        )
        importados = importados + 1

    if not evento_importacion:
        return {"id": ficha.id, "accion": "importada", "eventos": importados}

    insertar_evento(
        con,
        ficha.id,
        {
            "fecha": ahora,
            "tipo": EVENTO_IMPORTACION,
            "estado_anterior": str(ficha.estado),
            "estado_nuevo": str(ficha.estado),
            "motivo": "Tarea incorporada al estado global desde "
            + fila["definicion_ruta"] + ".",
            "origen": ORIGEN_AUTOMATICO,
            "datos": {
                "definicion_hash": fila["definicion_hash"],
                "eventos_importados": importados,
            },
        },
    )

    return {"id": ficha.id, "accion": ACCION_IMPORTADA, "eventos": importados + 1}


def sincronizar_ficha(con: sqlite3.Connection, ficha: Ficha, ahora: str | None = None) -> dict:
    """
    Incorpora una ficha nueva o refresca su definición si cambió.

    Nunca toca estado, intentos ni propietario. Sí reescribe el título, las
    decisiones declaradas, la huella y —con la salvedad de abajo— el ámbito
    de archivos. Debe llamarse dentro de una transacción.

    Ámbito congelado mientras la tarea está viva (A3.2)
    ---------------------------------------------------
    La regla de un solo escritor se comprueba en `tomar` contra el ámbito
    GRABADO. Si esta función lo reescribiera desde el JSON del árbol, una
    tarea ya tomada podría quedar registrada con un ámbito más estrecho, la
    siguiente toma no vería el solapamiento y dos trabajadores acabarían
    escribiendo los mismos archivos.

    No hacía falta ninguna orden peligrosa para provocarlo: `cargar` llama
    aquí, y `cargar` encabeza casi todas las órdenes, incluidas las de sólo
    lectura y las tomas que terminan rechazadas.

    La política es la mínima que cierra el agujero: mientras el estado de la
    tarea retenga su ámbito, un cambio de ámbito NO se aplica y la
    sincronización entera se deja para después. La huella NO se avanza, así
    que el refresco no se pierde: vuelve a intentarse solo en cuanto la
    tarea deje de estar viva.

    Un cambio declarativo inocuo —el título, una descripción de decisión—
    sigue sincronizándose con normalidad: sólo se frena cuando lo que
    cambia es el ámbito, que es lo único de lo que depende la garantía.
    """
    ahora = ahora or ahora_utc()

    existente = obtener_tarea(con, ficha.id)

    if existente is None:
        return importar_ficha(con, ficha, ahora)

    huella = hash_definicion(ficha)

    if existente["definicion_hash"] == huella:
        return {"id": ficha.id, "accion": ACCION_SIN_CAMBIOS, "eventos": 0}

    ambito_nuevo = list(ficha.ambito_archivos)
    ambito_grabado = list(existente["ambito_archivos"] or [])

    if (
        ambito_nuevo != ambito_grabado
        and str(existente["estado"]) in ESTADOS_QUE_RETIENEN_AMBITO
    ):
        return {
            "id": ficha.id,
            "accion": ACCION_AMBITO_CONGELADO,
            "eventos": 0,
            "estado": existente["estado"],
            "ambito_grabado": ambito_grabado,
            "ambito_declarado": ambito_nuevo,
            "detalle": "La tarea '" + ficha.id + "' está en estado '"
            + str(existente["estado"]) + "' y retiene su ámbito: no se "
            "aplica el cambio de ámbito declarado en el JSON. Se "
            "sincronizará sola cuando la tarea deje de estar viva.",
        }

    fusionadas = fusionar_decisiones(
        ficha.requiere_decision_humana, existente["decisiones"]
    )

    pendientes = [una for una in fusionadas if not una["resuelta"]]

    actualizar_tarea(
        con,
        ficha.id,
        {
            "titulo": ficha.titulo,
            "ambito_archivos": _a_json(list(ficha.ambito_archivos)),
            "decisiones": _a_json(decisiones_operativas(fusionadas)),
            "requiere_decision_humana": 1 if pendientes else 0,
            "definicion_hash": huella,
            "definicion_sincronizada_en": ahora,
        },
    )

    insertar_evento(
        con,
        ficha.id,
        {
            "fecha": ahora,
            "tipo": EVENTO_SINCRONIZACION,
            "estado_anterior": existente["estado"],
            "estado_nuevo": existente["estado"],
            "motivo": "Definición de la tarea sincronizada desde "
            + existente["definicion_ruta"] + ".",
            "origen": ORIGEN_AUTOMATICO,
            "datos": {
                "hash_anterior": existente["definicion_hash"],
                "hash_nuevo": huella,
            },
        },
    )

    return {"id": ficha.id, "accion": ACCION_ACTUALIZADA, "eventos": 1}


def necesita_sincronizacion(con: sqlite3.Connection, ficha: Ficha) -> bool:
    """Lectura pura: ¿falta la tarea o cambió su definición?"""
    existente = obtener_tarea(con, ficha.id)

    if existente is None:
        return True

    return existente["definicion_hash"] != hash_definicion(ficha)


def asegurar_ficha(con: sqlite3.Connection, ficha: Ficha, ahora: str | None = None) -> dict:
    """
    Garantiza que la tarea está en el estado global con su definición al
    día. Sólo abre una transacción de escritura si hace falta.
    """
    if not necesita_sincronizacion(con, ficha):
        return {"id": ficha.id, "accion": ACCION_SIN_CAMBIOS, "eventos": 0}

    with transaccion(con):
        return sincronizar_ficha(con, ficha, ahora)


def sincronizar_lista(
    con: sqlite3.Connection,
    fichas: list[Ficha],
    ahora: str | None = None,
    solo_importar: bool = False,
) -> dict:
    """
    Sincroniza varias fichas en una sola transacción, si alguna lo necesita.

    `solo_importar=True` incorpora las tareas ausentes pero NO refresca
    definiciones ya registradas. Es lo que usan los caminos de sólo lectura
    (tablero, API): dos worktrees en ramas distintas podrían tener
    definiciones distintas de la misma tarea, y un refresco automático en
    cada consulta produciría un vaivén de eventos. El refresco por huella
    queda para `sincronizar-definiciones` y para las operaciones explícitas.
    """
    ahora = ahora or ahora_utc()

    informe = {
        "fecha": ahora,
        "importadas": [],
        "actualizadas": [],
        "sin_cambios": [],
    }

    if solo_importar:
        pendientes = [
            ficha for ficha in fichas
            if obtener_tarea(con, ficha.id) is None
        ]
    else:
        pendientes = [
            ficha for ficha in fichas if necesita_sincronizacion(con, ficha)
        ]

    informe["sin_cambios"] = [
        ficha.id for ficha in fichas if ficha not in pendientes
    ]

    if not pendientes:
        return informe

    with transaccion(con):
        for ficha in pendientes:
            resultado = sincronizar_ficha(con, ficha, ahora)

            if resultado["accion"] == "importada":
                informe["importadas"].append(ficha.id)
            elif resultado["accion"] == "actualizada":
                informe["actualizadas"].append(ficha.id)
            else:
                informe["sin_cambios"].append(ficha.id)

    informe["sin_cambios"].sort()

    return informe


def sincronizar_definiciones(raiz: Path, con: sqlite3.Connection | None = None) -> dict:
    """
    Importa al estado global todas las fichas JSON legibles del repositorio.

    Seguro, repetible e idempotente: ejecutarlo dos veces seguidas produce
    exactamente el mismo estado. No ejecuta tareas ni cambia estados.
    Las fichas ilegibles se informan, no interrumpen.
    """
    raiz = Path(raiz)

    fichas, errores = listar_con_errores(raiz)

    if con is not None:
        informe = sincronizar_lista(con, fichas)
    else:
        with conexion(raiz) as propia:
            informe = sincronizar_lista(propia, fichas)

    informe["fichas_ilegibles"] = errores

    return informe


def inicializar_base(raiz: Path) -> dict:
    """Crea la base y el esquema si faltan, e importa las definiciones."""
    raiz = Path(raiz)
    ruta = ruta_base(raiz)

    con = abrir(ruta)

    try:
        esquema = inicializar(con)
        sincronizacion = sincronizar_definiciones(raiz, con)
    finally:
        con.close()

    return {
        "ruta": str(ruta),
        "esquema": esquema,
        "sincronizacion": sincronizacion,
    }


# ----------------------------------------------------------------------
# Diagnóstico
# ----------------------------------------------------------------------

def diagnostico(raiz: Path) -> dict:
    """
    Estado real de la base global, sin crearla ni modificarla.

    Nunca lanza excepción: cada problema se informa en el propio resultado.
    """
    raiz = Path(raiz)

    informe = {
        "fecha": ahora_utc(),
        "raiz": str(raiz),
        "git_common_dir": None,
        "ruta": None,
        "ubicacion_resumida": None,
        "existe": False,
        "tamano_bytes": None,
        "estado": "ERROR",
        "detalle": None,
        "version_esquema": None,
        "version_esperada": VERSION_ESQUEMA,
        "journal_mode": None,
        "synchronous": None,
        "busy_timeout_ms": None,
        "foreign_keys": None,
        "integridad": None,
        "tareas": None,
        "eventos": None,
        "ultima_actualizacion": None,
        "ultimo_evento": None,
        "definiciones_json": None,
        "fichas_ilegibles": [],
        "sin_importar": [],
        "desactualizadas": [],
    }

    try:
        comun = git_common_dir(raiz)
    except ErrorEstadoGlobal as error:
        informe["detalle"] = str(error)
        return informe

    ruta = comun / NOMBRE_BASE

    informe["git_common_dir"] = str(comun)
    informe["ruta"] = str(ruta)
    informe["ubicacion_resumida"] = ubicacion_resumida(ruta)
    informe["existe"] = ruta.is_file()

    fichas, errores = listar_con_errores(raiz)
    informe["definiciones_json"] = len(fichas)
    informe["fichas_ilegibles"] = errores

    if not informe["existe"]:
        informe["estado"] = "NO_INICIALIZADA"
        informe["detalle"] = (
            "La base global todavía no existe. Ejecute 'inicializar-estado'."
        )
        informe["sin_importar"] = [ficha.id for ficha in fichas]
        return informe

    try:
        informe["tamano_bytes"] = ruta.stat().st_size
    except OSError as error:
        informe["detalle"] = (
            "La base existía al comprobarla pero no se pudo consultar: "
            + str(error)
        )
        return informe

    try:
        con = abrir(ruta, solo_lectura=True)
    except ErrorEstadoGlobal as error:
        informe["detalle"] = str(error)
        return informe

    try:
        informe["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        informe["synchronous"] = con.execute("PRAGMA synchronous").fetchone()[0]
        informe["busy_timeout_ms"] = con.execute("PRAGMA busy_timeout").fetchone()[0]
        informe["foreign_keys"] = bool(con.execute("PRAGMA foreign_keys").fetchone()[0])
        informe["integridad"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        informe["version_esquema"] = version_esquema(con)

        if informe["version_esquema"] == 0:
            informe["estado"] = "SIN_ESQUEMA"
            informe["detalle"] = (
                "El archivo existe pero no tiene esquema. "
                "Ejecute 'inicializar-estado'."
            )
            return informe

        if informe["version_esquema"] != VERSION_ESQUEMA:
            informe["estado"] = "ESQUEMA_DESACTUALIZADO"
            informe["detalle"] = (
                "Versión de esquema "
                + str(informe["version_esquema"])
                + "; se esperaba "
                + str(VERSION_ESQUEMA)
                + ". Ejecute 'inicializar-estado'."
            )
            return informe

        informe["tareas"] = contar_tareas(con)
        informe["eventos"] = contar_eventos(con)
        informe["ultima_actualizacion"] = ultima_actualizacion(con)

        evento = ultimo_evento(con)

        if evento is not None:
            informe["ultimo_evento"] = {
                "fecha": evento["fecha"],
                "tarea": evento["tarea_id"],
                "tipo": evento["tipo"],
                "motivo": evento["motivo"],
            }

        registradas = {fila["id"]: fila for fila in listar_tareas(con)}
        informe["sin_importar"] = [
            ficha.id for ficha in fichas if ficha.id not in registradas
        ]
        informe["desactualizadas"] = [
            ficha.id
            for ficha in fichas
            if ficha.id in registradas
            and registradas[ficha.id]["definicion_hash"] != hash_definicion(ficha)
        ]

        if informe["integridad"] != "ok":
            informe["estado"] = "ERROR"
            informe["detalle"] = (
                "La comprobación de integridad devolvió: "
                + str(informe["integridad"])
            )
        else:
            informe["estado"] = "ACTIVA"
            informe["detalle"] = "Base global operativa."

    except sqlite3.Error as error:
        informe["estado"] = "ERROR"
        informe["detalle"] = "Error al consultar la base: " + str(error)
    finally:
        con.close()

    return informe
