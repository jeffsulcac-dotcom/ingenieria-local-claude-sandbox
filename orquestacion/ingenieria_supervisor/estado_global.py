"""
Estado operativo GLOBAL del Supervisor: una única base SQLite por repositorio.

A partir de A2:

    SQLite  = autoridad del ESTADO OPERATIVO de cada tarea
              (estado, rama, worktree, intentos, trabajador, latido, fallas,
              resolución de decisiones humanas, última verificación, eventos)
              y, desde T-0003, de la COLA de trabajadores (tabla `cola`,
              migración 3).

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

Desde A3.2 ese primitivo tiene compañía: `actualizar_si_propietario` hace
lo mismo para el RESTO del ciclo. Lleva la precondición —generación de
propiedad, y según el caso identidad y estado— en el WHERE del UPDATE y
decide por `rowcount`, de modo que una orden compuesta contra una lectura
vieja ya no puede sobrescribir al propietario vigente. La generación la
aporta la columna `tareas.generacion`, que sólo incrementa `reclamar` y
que nunca sale de esta base: no se serializa al JSON, para que no se pueda
fijar ni hacer retroceder editando un archivo del árbol de trabajo.

`actualizar_tarea` sigue existiendo y sigue siendo incondicional, pero ya
no la usa ninguna orden del ciclo: `supervisor.persistir` pasa por la
versión condicionada. Queda para la sincronización de definiciones, y
tiene vetadas las columnas `id` y `generacion`.

Los latidos automáticos, la vitalidad con dos señales y la recuperación
manual son de A3.3; la cola, el despacho y los trabajadores, de T-0003
(`trabajadores.py`). Sigue sin implementar la expiración automática de
trabajadores: ante la duda, decide una persona.
"""

from __future__ import annotations

import hashlib
import json
import os
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

VERSION_ESQUEMA = 3

# Milisegundos que una conexión espera si otra tiene la base ocupada.
BUSY_TIMEOUT_MS = 5000

# Reintentos de la conversión inicial a WAL. Ver `_activar_journal`: el
# fallo que absorben es INMEDIATO (varios procesos convirtiendo a la vez una
# base nueva), no una espera larga, así que bastan pocos intentos con
# siestas cortas. Suman poco más de un segundo de espera en total.
INTENTOS_JOURNAL = 8
ESPERA_JOURNAL_S = 0.02
ESPERA_JOURNAL_MAXIMA_S = 0.25

# Temporizador de ocupado SÓLO durante la conversión, para que el peor caso
# del bucle quede acotado en TIEMPO y no sólo en número de intentos.
ESPERA_OCUPADO_JOURNAL_MS = 250

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
# T-0003 — movimientos de la cola de trabajadores (encolar, despachar,
# terminar, reconciliar). Se anotan como eventos de la tarea para que el
# historial de una tarea cuente también por qué y cuándo se lanzó.
EVENTO_COLA = "cola"

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
# La fila sigue siendo del mismo dueño, generación y estado, pero una
# columna sobre la que la orden DECIDIÓ cambió entre su lectura y su
# escritura (A3.3, `exigir_iguales`), o la marca que traía es más vieja que
# la grabada (`exigir_no_retroceso`).
MOTIVO_PRECONDICION_CAMBIADA = "precondicion_cambiada"
MOTIVO_MARCA_MAS_NUEVA = "marca_mas_nueva"

# T-0003 — estados de una entrada de la cola de trabajadores. La cola vive
# en esta misma base (tabla `cola`, migración 3): sobrevive a un cierre o
# a un apagón igual que el estado de las tareas, y se despacha dentro de
# la misma transacción que concede la toma. Las primitivas que la leen y
# escriben están en `trabajadores.py`; aquí sólo el esquema y los nombres.
COLA_PENDIENTE = "pendiente"
COLA_DESPACHADA = "despachada"
COLA_TERMINADA = "terminada"
COLA_FALLIDA = "fallida"
COLA_RETIRADA = "retirada"

# Entradas que siguen VIVAS: una tarea sólo puede tener una a la vez.
COLA_ESTADOS_VIVOS = (COLA_PENDIENTE, COLA_DESPACHADA)

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
    3: [
        # T-0003 — cola persistente de trabajadores.
        #
        # `secuencia` es AUTOINCREMENT a propósito: SQLite no reutiliza un
        # número aunque se borre la fila, así que el orden de llegada es
        # estable después de cualquier reinicio. El orden de despacho es
        # `prioridad DESC, secuencia ASC` y lo resuelve la base, no el
        # proceso que despacha.
        #
        # `trabajo` es una LISTA JSON de argumentos (argv). Nunca una
        # cadena: el trabajador la entrega a `subprocess` tal cual, sin
        # intérprete de órdenes por medio.
        #
        # Una tarea puede tener varias entradas a lo largo del tiempo
        # (cada una es el registro de un lanzamiento), pero sólo UNA viva
        # —pendiente o despachada— a la vez: lo garantiza el índice único
        # parcial, en el motor, no una comprobación previa en Python.
        """
        CREATE TABLE cola (
            secuencia       INTEGER PRIMARY KEY AUTOINCREMENT,
            tarea_id        TEXT NOT NULL
                            REFERENCES tareas(id) ON DELETE RESTRICT,
            prioridad       INTEGER NOT NULL DEFAULT 0,
            estado_cola     TEXT NOT NULL,
            trabajo         TEXT NOT NULL DEFAULT '[]',
            tiempo_limite_s INTEGER NOT NULL DEFAULT 3600,
            base            TEXT,
            encolado_en     TEXT NOT NULL,
            actualizado_en  TEXT NOT NULL,
            despachado_en   TEXT,
            terminado_en    TEXT,
            trabajador_id   TEXT,
            generacion      INTEGER,
            pid             INTEGER,
            worktree        TEXT,
            registro        TEXT,
            adoptado_en     TEXT,
            ultimo_rechazo  TEXT,
            resultado       TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX cola_una_viva_por_tarea
            ON cola (tarea_id)
            WHERE estado_cola IN ('pendiente', 'despachada')
        """,
        """
        CREATE INDEX cola_por_orden
            ON cola (estado_cola, prioridad DESC, secuencia ASC)
        """,
    ],
}


class ErrorEstadoGlobal(Exception):
    """La base global no se pudo ubicar, abrir, migrar o escribir."""


# ----------------------------------------------------------------------
# Ubicación
# ----------------------------------------------------------------------

# Directorio común de Git ya resuelto, por raíz. Ver `git_common_dir`.
#
# Cada entrada guarda sólo la ruta resuelta: lo que la valida después es
# `_comun_declarado`, que vuelve a mirar qué directorio común declara el
# `.git` de esa raíz.
_COMUNES_RESUELTOS: dict = {}


def _comun_declarado(raiz: Path):
    """
    Directorio común que la propia carpeta `.git` de la raíz declara.

    No lanza ningún proceso: mira el sistema de archivos y, si hace falta,
    lee un archivo de pocos bytes. Sirve para COMPROBAR lo memorizado, no
    para sustituir a Git.

    Dos formas, que son las dos que Git usa:

    - `.git` es un directorio  ->  el común es ese mismo directorio.
    - `.git` es un archivo (worktree enlazado)  ->  contiene
      `gitdir: <ruta>/.git/worktrees/<nombre>`, y el común es el `.git`
      del que cuelga ese `worktrees`.

    Devuelve None cuando no puede decidir —no hay `.git`, el archivo no
    tiene el formato esperado, la ruta no existe—, y entonces no se
    memoriza nada: mejor pagar la llamada a Git que arriesgarse a devolver
    la base equivocada.

    Por qué no se comparan inodos, que es lo primero que se intentó: el
    sistema de archivos los REUTILIZA. Al borrar el `.git` de un worktree y
    hacer `git init` en su lugar, el directorio nuevo puede recibir el
    mismo número de inodo que el archivo borrado, y entonces la memoria
    daba por bueno el directorio común del repositorio anterior. Está
    reproducido bajo carga en la comprobación 17.
    """
    enlace = raiz / ".git"

    try:
        if enlace.is_dir():
            return enlace.resolve()

        if not enlace.is_file():
            return None

        texto = enlace.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None

    if not texto.startswith("gitdir:"):
        return None

    apuntado = Path(texto[len("gitdir:"):].strip())

    if not apuntado.is_absolute():
        apuntado = (raiz / apuntado)

    # <comun>/worktrees/<nombre>  ->  <comun>
    if apuntado.parent.name != "worktrees":
        return None

    try:
        return apuntado.parent.parent.resolve()
    except OSError:
        return None


_VARIABLES_GIT_HEREDADAS = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


def _entorno_git_limpio() -> dict:
    """Copia del entorno sin las variables que redirigen a `git`."""
    entorno = dict(os.environ)

    for nombre in _VARIABLES_GIT_HEREDADAS:
        entorno.pop(nombre, None)

    return entorno


def git_common_dir(raiz: Path) -> Path:
    """
    Directorio común de Git del repositorio que contiene `raiz`.

    Desde la rama principal Git responde `.git` (relativo a la raíz); desde un
    worktree enlazado responde la ruta absoluta del `.git` principal. En ambos
    casos el resultado es el MISMO directorio.

    Por qué se memoriza (A3.2)
    --------------------------
    Cada operación del Supervisor necesita esta ruta para saber dónde está la
    base global, así que una tanda de pruebas la pedía cientos de veces por
    proceso. En Linux cuesta unos 2 ms y no se nota; en Windows, con Defender
    vigilando la carpeta, cuesta entre 50 y 250 ms, y el límite de 120 s por
    archivo del corredor único entraba en juego. A3.1 ya midió la mitigación
    y la dejó anotada como lo primero que hacer si la corrida se acercaba al
    límite; la batería de A3.2 es la que la acerca.

    Es una memoria por PROCESO y por raíz, no una caché global persistente:
    nada sobrevive a la salida del proceso, así que no hay estado compartido
    que pueda quedar obsoleto entre ejecuciones.

    Y dentro del proceso tampoco puede quedarse obsoleta en silencio: antes
    de devolver lo memorizado se comprueba que el directorio siga existiendo.
    Si el repositorio se movió o se borró —lo que pasa constantemente en las
    pruebas, que crean y destruyen repositorios temporales— se vuelve a
    preguntar a Git. Esa comprobación es una llamada al sistema de archivos,
    no un proceso nuevo: es justo lo que se quería ahorrar.
    """
    raiz = Path(raiz)
    clave = str(raiz.resolve())
    declarado = _comun_declarado(raiz)

    memorizado = _COMUNES_RESUELTOS.get(clave)

    if memorizado is not None:
        # Lo memorizado vale si el propio `.git` de la raíz SIGUE
        # declarando ese mismo directorio común. Es una comprobación
        # estructural, no de metadatos: detecta que esa ruta pertenece
        # ahora a otro repositorio aunque el sistema de archivos haya
        # reutilizado el inodo, que es justo lo que pasaba antes.
        if declarado is not None and declarado == memorizado:
            return memorizado

        _COMUNES_RESUELTOS.pop(clave, None)

    try:
        resultado = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(raiz),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Un `GIT_DIR` heredado —siempre presente dentro de un hook, y
            # también en `git rebase --exec` o `git bisect run`— hace que
            # `git` ignore `cwd` y responda por otro repositorio. La base
            # global se ubicaría entonces en el sitio equivocado.
            env=_entorno_git_limpio(),
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

    comun = comun.resolve()

    # Sólo se memoriza lo que después se podrá COMPROBAR sin lanzar Git, y
    # sólo si Git y el sistema de archivos dicen lo mismo. Si no coinciden,
    # manda Git: se devuelve su respuesta y no se guarda nada.
    if declarado is not None and declarado == comun:
        _COMUNES_RESUELTOS[clave] = comun

    return comun


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

    Dos medidas, y las dos hicieron falta de verdad.

    1. No se pide el cambio si la base YA está en el modo deseado. La
       conversión `delete` -> `wal` es el único momento en que abrir la base
       necesita un bloqueo exclusivo; a partir de la segunda apertura no hay
       nada que convertir. Si cada proceso emitiera igualmente la sentencia,
       todos competirían por un bloqueo que ninguno necesita.

    2. Si hay que convertir, se reintenta de forma acotada.

    Sobre (2) conviene dejar escrito lo que costó, porque el camino tuvo una
    vuelta. Primero se midió que la conversión SÍ respeta el `busy_timeout`
    —con un lector abierto esperó los 5,007 s completos antes de rendirse—,
    y de ahí se concluyó que el reintento sobraba y se retiró. La corrida
    completa del corredor lo desmintió en el acto: con 6 procesos saliendo a
    la vez contra una base que no existe, 1 de 6 muere con "database is
    locked" SIN esperar nada. Las dos observaciones son ciertas y no se
    contradicen: el temporizador cubre el conflicto con un lector, pero no
    el choque entre varios que intentan convertir a la vez.

    Por eso la espera es corta y el número de intentos pequeño: el fallo que
    hay que absorber es inmediato, no una espera larga. Entre intento e
    intento se vuelve a LEER el modo, porque lo más probable es que otro ya
    terminara y entonces no haya nada que hacer.

    La lectura del modo va DENTRO del bucle, no antes: leer de una base que
    otro tiene tomada en exclusiva falla igual, y dejarla fuera hacía morir
    sin reintentar nada al primero que llegara mientras otro convierte.

    El modo se devuelve para que quien llama compruebe el resultado real, y
    sólo se devuelve cuando es el bueno: cualquier otro desenlace lanza con
    el diagnóstico que corresponda.
    """
    # Durante la conversión se baja el temporizador de ocupado.
    #
    # El pragma de conversión SÍ lo respeta, así que con los 5 s normales
    # cada intento podía quedarse esperando ese tiempo y el peor caso del
    # bucle subía a unos 41 s: acotado en número de intentos, pero no en
    # tiempo, que es lo que de verdad importa. Con 250 ms el peor caso baja
    # a unos 3 s, y no se pierde nada: el choque que hay que absorber aquí
    # es inmediato, y quien de verdad necesite esperar mucho es el resto de
    # operaciones, que conservan BUSY_TIMEOUT_MS.
    con.execute("PRAGMA busy_timeout = " + str(ESPERA_OCUPADO_JOURNAL_MS))

    try:
        return _convertir_journal(con, ruta)
    finally:
        con.execute("PRAGMA busy_timeout = " + str(BUSY_TIMEOUT_MS))


def _convertir_journal(con: sqlite3.Connection, ruta: Path) -> str:
    """Bucle de conversión propiamente dicho. Ver `_activar_journal`."""
    espera = ESPERA_JOURNAL_S
    ultimo = None

    # Lo que decide el diagnóstico es el ÚLTIMO desenlace, no si alguna vez
    # hubo contención. Con un acumulado, unos primeros intentos bloqueados
    # seguidos de un sistema de archivos que no admite WAL daban el mensaje
    # equivocado y mandaban a buscar un proceso que no existía.
    ultimo_fue_bloqueo = False

    for intento in range(INTENTOS_JOURNAL):
        try:
            modo = con.execute("PRAGMA journal_mode").fetchone()[0]

            if str(modo).lower() == JOURNAL_MODE:
                return str(modo)

            modo = con.execute(
                "PRAGMA journal_mode = " + JOURNAL_MODE
            ).fetchone()[0]

            if str(modo).lower() == JOURNAL_MODE:
                return str(modo)

            # El motor no se quejó y aun así no cambió de modo. Eso ya no
            # es contención: es que este sistema de archivos no admite WAL.
            ultimo = "quedó en '" + str(modo) + "'"
            ultimo_fue_bloqueo = False
        except sqlite3.OperationalError as error:
            # Sólo se reintenta el choque con otro que tiene la base.
            # Cualquier otro error operativo es real y sale sin disfrazarse.
            if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                raise

            ultimo_fue_bloqueo = True
            ultimo = str(error)

        if intento + 1 < INTENTOS_JOURNAL:
            time.sleep(espera)
            espera = min(espera * 2, ESPERA_JOURNAL_MAXIMA_S)

    # Los dos desenlaces piden diagnósticos distintos, y antes se daba
    # siempre el mismo. Si el último intento no chocó con nadie, no hay
    # ninguna contención que esperar: el sistema de archivos no admite WAL.
    if not ultimo_fue_bloqueo:
        raise ErrorEstadoGlobal(
            "SQLite no pudo activar journal_mode=" + JOURNAL_MODE + " en '"
            + str(ruta) + "' (" + str(ultimo) + "), y no por estar ocupada."
            " ¿La base está en una unidad de red?"
        )

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
            # `_activar_journal` devuelve el modo sólo cuando es el bueno;
            # en cualquier otro caso lanza con el diagnóstico que
            # corresponda (contención o sistema de archivos). Repetir aquí
            # la comprobación sería una rama inalcanzable que aparenta
            # cubrir un caso sin cubrirlo.
            _activar_journal(con, ruta)

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
    # Este DDL se ejecutaba en autocommit en CADA apertura de conexión, y
    # la regla del proyecto es que toda escritura va dentro de una
    # transacción. Envolverlo sin más tenía un precio: `BEGIN IMMEDIATE`
    # pide el bloqueo de escritura de toda la base, así que cada `ver` o
    # cada refresco del tablero lo habría pedido para no escribir nada.
    #
    # Se pregunta primero —una lectura, sin candado— y sólo se crea cuando
    # de verdad falta. La regla queda sin excepciones y el camino normal no
    # paga nada.
    existe_esquema = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'esquema'"
    ).fetchone()

    if existe_esquema is None:
        with transaccion(con):
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
                actual = version_esquema(con)

                if actual > VERSION_ESQUEMA:
                    # La misma guarda que arriba, reevaluada con el bloqueo
                    # tomado. La de fuera se evalúa en autocommit, así que
                    # otro proceso puede migrar a una versión más nueva
                    # entremedias y esta build seguiría adelante sobre un
                    # esquema que no entiende.
                    raise ErrorEstadoGlobal(
                        "La base global está en la versión de esquema "
                        + str(actual)
                        + ", más nueva que la que entiende este Supervisor ("
                        + str(VERSION_ESQUEMA)
                        + ")."
                    )

                if actual >= version:
                    continue

                for sentencia in sentencias:
                    con.execute(sentencia)

                con.execute(
                    "INSERT INTO esquema (version, aplicado_en) VALUES (?, ?)",
                    (version, ahora_utc()),
                )
        except ErrorEstadoGlobal as error:
            # `transaccion` ya convirtió cualquier sqlite3.Error en
            # ErrorEstadoGlobal, así que capturar sqlite3.Error aquí era
            # código muerto y el número de migración se perdía.
            if "versión de esquema" in str(error):
                raise

            raise ErrorEstadoGlobal(
                "Falló la migración de esquema número "
                + str(version)
                + ": "
                + str(error)
            ) from None

        aplicadas.append(version)

    final = version_esquema(con)

    # Última comprobación, y hace falta aunque parezca redundante: cuando la
    # base ya está al día el bucle de arriba no se ejecuta ni una vez, así
    # que la guarda que vive dentro no llega a evaluarse. Ése es justo el
    # camino que recorre `conexion()` en CADA orden. Sin esto, un proceso
    # con una build antigua podía seguir adelante sobre un esquema que otro
    # acababa de migrar por delante de él.
    if final > VERSION_ESQUEMA:
        raise ErrorEstadoGlobal(
            "La base global está en la versión de esquema "
            + str(final)
            + ", más nueva que la que entiende este Supervisor ("
            + str(VERSION_ESQUEMA)
            + ")."
        )

    return {
        "version_anterior": anterior,
        "version_actual": final,
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
        # El presupuesto de intentos es declarativo: decide cuándo una
        # tarea acaba BLOQUEADA. Estaba fuera de la huella y ninguna orden
        # lo escribía, así que una persona podía editarlo en la ficha y no
        # pasaba nada; encima el espejo le deshacía la edición sin avisar.
        "max_intentos": int(ficha.max_intentos),
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


# Descripción que se da a una decisión registrada en la base cuya clave la
# definición de ESTE árbol de trabajo no declara.
DESCRIPCION_NO_DECLARADA = (
    "(no declarada en la definición de este árbol de trabajo)"
)


def fusionar_decisiones(declaradas: list[dict], operativas: list[dict]) -> list[dict]:
    """
    Une la definición (JSON: clave, descripción) con el estado (SQLite).

    El orden lo manda la definición. Una clave declarada sin estado
    registrado es una decisión pendiente.

    Una clave REGISTRADA que esta definición no declara se conserva al
    final (A3.3). Antes se descartaba, y el efecto era grave: el conjunto
    de claves lo mandaba el JSON de ESTE árbol de trabajo, así que abrir la
    tarea desde otra rama que la declarase con menos decisiones borraba de
    la base —que es la autoridad— la resolución de las que faltaban.
    Restituir la clave en el JSON no la devolvía: volvía como pendiente. Y
    para borrarla bastaba una orden de SÓLO LECTURA, porque `cargar` pasa
    por la sincronización.

    Una resolución humana no se tira porque un archivo de otra rama no la
    mencione. Se conserva y se dice que no está declarada aquí.
    """
    por_clave = {}

    for operativa in operativas:
        clave = str(operativa.get("clave"))
        if clave not in por_clave:
            por_clave[clave] = operativa

    fusionadas = []
    vistas = set()

    for declarada in declaradas:
        clave = str(declarada.get("clave"))

        if clave in vistas:
            continue

        vistas.add(clave)
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

    for clave, estado in por_clave.items():
        if clave in vistas:
            continue

        fusionadas.append(
            {
                "clave": clave,
                "descripcion": DESCRIPCION_NO_DECLARADA,
                "resuelta": bool(estado.get("resuelta", False)),
                "resolucion": estado.get("resolucion"),
                "resuelta_en": estado.get("resuelta_en"),
                "origen": estado.get("origen"),
            }
        )

    return fusionadas


def resolver_decision(
    con: sqlite3.Connection,
    identificador: str,
    clave: str,
    resolucion: str,
    momento: str,
    origen: str,
    declaradas: list[dict] | None = None,
) -> dict:
    """
    Marca UNA decisión como resuelta, fusionando con lo que la fila tiene.

    DEBE ejecutarse dentro de `transaccion(con)`.

    Por qué esto vive en el motor y no en Python
    --------------------------------------------
    `decisiones` es UNA columna con el JSON de todas las decisiones dentro.
    Resolver una leyendo la lista, cambiando un elemento y reescribiendo la
    columna entera es el lost update de manual, y aquí no lo frenaba nada:
    dos `decidir` sobre claves DISTINTAS son dos órdenes perfectamente
    válidas —misma generación, mismo estado, sin propietario que exigir—,
    así que las dos pasan el WHERE y la segunda devuelve a «pendiente» lo
    que la primera acababa de resolver. Sin error y sin rastro: al humano
    que decidió se le devolvía su decisión como resuelta.

    Medido antes de arreglarlo: 24 de 25 carreras entre dos procesos
    perdían una resolución.

    Leyendo la fila DENTRO de la misma transacción, la segunda orden ve lo
    que la primera confirmó y las dos resoluciones sobreviven.
    """
    if not con.in_transaction:
        raise ErrorEstadoGlobal(
            "resolver_decision debe ejecutarse dentro de una transacción."
        )

    fila = obtener_tarea(con, identificador)

    if fila is None:
        raise ErrorEstadoGlobal(
            "La tarea '" + str(identificador) + "' no existe en el estado "
            "global."
        )

    registradas = list(fila.get("decisiones") or [])
    fusionadas = fusionar_decisiones(declaradas or [], registradas)

    objetivo = None

    for decision in fusionadas:
        if decision["clave"] == str(clave):
            objetivo = decision
            break

    if objetivo is None:
        return {
            "resuelta": False,
            "motivo": "inexistente",
            "decisiones": fusionadas,
        }

    if objetivo["resuelta"]:
        return {
            "resuelta": False,
            "motivo": "ya_resuelta",
            "decisiones": fusionadas,
        }

    objetivo["resuelta"] = True
    objetivo["resolucion"] = resolucion
    objetivo["resuelta_en"] = momento
    objetivo["origen"] = origen

    pendientes = [una for una in fusionadas if not una["resuelta"]]

    campos = {
        "decisiones": _a_json(decisiones_operativas(fusionadas)),
        "requiere_decision_humana": 1 if pendientes else 0,
        "actualizado_en": momento,
    }

    # Si lo único que frenaba la propuesta eran decisiones pendientes y se
    # acaba de resolver la última, esa falla ya no describe nada: dejarla
    # hacía que la fila dijera a la vez «resueltas» y «hay decisiones sin
    # resolver» hasta la siguiente corrida.
    falla = fila.get("ultima_falla") or {}

    if not pendientes and isinstance(falla, dict) and falla.get("tipo") == (
        "decisiones_pendientes"
    ):
        campos["ultima_falla"] = None

    actualizar_tarea(con, identificador, campos)

    return {
        "resuelta": True,
        "motivo": None,
        "decisiones": fusionadas,
        "pendientes": pendientes,
    }


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
        # A3.3 — DÓNDE se ejecutó. Sin esto, dos corridas idénticas de
        # árboles distintos son indistinguibles en el historial y nadie
        # puede auditar después si se verificó lo correcto.
        "raiz": corrida.get("raiz"),
        "es_worktree": corrida.get("es_worktree"),
        "rama": corrida.get("rama"),
        "commit": corrida.get("commit"),
        "commit_final": corrida.get("commit_final"),
        "arbol_estable": corrida.get("arbol_estable"),
        # Si había cambios sin confirmar, el commit no contiene lo que se
        # ejecutó. Se guarda para que la evidencia no diga «commit X en
        # verde» a secas.
        "sin_confirmar": corrida.get("sin_confirmar"),
        # A QUIÉN pertenece esta verificación. `ultima_verificacion`
        # sobrevive a `reabrir`, a `reanudar` y a una retoma, así que sin
        # esto el tablero seguía enseñando el verde de una ejecución muerta
        # como si fuera el de la que está en curso.
        "generacion": corrida.get("generacion"),
        "trabajador_id": corrida.get("trabajador_id"),
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
        # El árbol de trabajo NO se importa del JSON, igual que la
        # generación. Es estado de ejecución, no definición: lo concede
        # `tomar` tras validarlo contra `git worktree list`. Si viniera del
        # archivo, escribir "worktree": "cualquier/cosa" en una ficha
        # bastaba para que `verificar` corriera ahí, porque la validación
        # de la toma sólo miraba el argumento explícito.
        "worktree": None,
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
        # Cero SIEMPRE, y no `ficha.generacion`. Esta función alimenta a
        # `insertar_tarea`, o sea al alta de una tarea que la base todavía
        # no conocía: su contador de propiedad empieza de cero. Copiar aquí
        # lo que trajera la ficha permitiría que una definición del árbol de
        # trabajo fijara la generación de una fila nueva.
        #
        # `persistir` no se ve afectado: filtra por COLUMNAS_OPERATIVAS, y
        # `generacion` no está ahí. La única que la mueve es `reclamar`.
        "generacion": 0,
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
    ficha.estado_leido = ficha.estado
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

    # El ámbito VIGENTE, que es el grabado, viaja aparte del DECLARADO (A3.2).
    #
    # No se pisa `ambito_archivos` con el valor de la base, y el motivo se
    # descubrió rompiéndolo: `persistir` regenera el espejo JSON a partir de
    # la ficha, así que pisarlo hacía que la primera orden posterior borrara
    # del archivo la declaración que una persona acababa de escribir. El
    # cambio no quedaba en espera: desaparecía.
    #
    # Con los dos campos separados, cada uno dice la verdad de lo suyo:
    # `ambito_archivos` es lo que la ficha DECLARA y lo que se sincronizará
    # cuando la tarea deje de estar viva; `ambito_vigente` es lo que la base
    # CONCEDIÓ y lo único que la regla de un solo escritor reconoce.
    ficha.ambito_vigente = list(fila.get("ambito_archivos") or [])
    ficha.ejecuciones = list(fila.get("ejecuciones") or [])

    ficha.requiere_decision_humana = fusionar_decisiones(
        ficha.requiere_decision_humana,
        fila.get("decisiones") or [],
    )

    return ficha


# ----------------------------------------------------------------------
# Lectura y escritura de tareas y eventos
# ----------------------------------------------------------------------

# Columnas que una orden puede INCREMENTAR en vez de fijar, para que el
# valor lo resuelva el motor y no una lectura anterior (ver A3.3).
COLUMNAS_NUMERICAS = ("intentos",)

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
        # `generacion` queda fuera igual que `id`: era el último escritor
        # del paquete capaz de fijar el testigo de propiedad a un valor
        # arbitrario, y encima con un UPDATE sin más predicado que el id.
        # La única que la mueve es `reclamar`, y sólo sumando uno.
        if columna not in COLUMNAS_TAREA or columna in ("id", "generacion"):
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
    incrementos=None,
    exigir_iguales=None,
    exigir_no_retroceso=None,
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

    Dos precondiciones más, ambas opcionales (A3.3)
    -----------------------------------------------
    `exigir_iguales` añade `columna IS ?` por cada entrada. Sirve para las
    órdenes que deciden mirando una columna que otra orden legítima puede
    cambiar sin mover ni el estado ni la generación. El caso real: la
    recuperación clasifica una ejecución leyendo `ultimo_latido` y escribe
    después; si entre medias el dueño late, ni el estado ni la generación
    cambian, así que el UPDATE casaba y la tarea se le arrebataba a alguien
    que acababa de demostrar que estaba vivo. Exigiendo el latido sobre el
    que se clasificó, esa escritura cae y se informa como rechazada.

    `exigir_no_retroceso` añade `(columna IS NULL OR columna <= ?)`. Sirve
    para que una marca de tiempo no pueda RETROCEDER: el reloj de pared no es
    monótono —NTP, cambio de zona, una máquina virtual restaurada— y un
    latido con la hora atrasada reducía la antigüedad registrada de la
    señal hasta hacer que la propia tarea pareciese huérfana.

    Un rechazo NO es una excepción aquí: se devuelve descrito, igual que en
    `reclamar`. Y no escribe nada: ni estado, ni intentos, ni marcas de
    tiempo. Quien llama decide si lo convierte en error.
    """
    incrementos = tuple(incrementos or ())

    if not campos and not incrementos:
        raise ErrorEstadoGlobal(
            "Una escritura condicionada necesita al menos una columna."
        )

    for columna in incrementos:
        # Un incremento se resuelve DENTRO del motor, sobre el valor real de
        # la fila en el instante de escribir. Calcularlo en Python a partir
        # de una lectura anterior es un lost update de manual: dos órdenes
        # que hubieran leído el mismo valor escribirían el mismo resultado y
        # una de las dos se perdería sin que nadie se enterase.
        if columna not in COLUMNAS_NUMERICAS:
            raise ErrorEstadoGlobal(
                "No se puede incrementar la columna '" + str(columna) + "'."
            )

        if columna in campos:
            raise ErrorEstadoGlobal(
                "La columna '" + str(columna) + "' no puede fijarse y "
                "además incrementarse en la misma escritura."
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

    for nombre, valores in (
        ("exigir_iguales", exigir_iguales),
        ("exigir_no_retroceso", exigir_no_retroceso),
    ):
        for columna in (valores or {}):
            if columna not in COLUMNAS_TAREA:
                raise ErrorEstadoGlobal(
                    "Columna desconocida en " + nombre + ": '"
                    + str(columna) + "'."
                )

    for columna, valor in (exigir_iguales or {}).items():
        # `IS` y no `=`: con `=`, NULL nunca casa consigo mismo y una fila
        # cuya columna esté vacía rechazaría una orden correcta.
        condiciones.append(columna + " IS ?")
        parametros.append(valor)

    for columna, valor in (exigir_no_retroceso or {}).items():
        # `<=` y no `<`: escribir el MISMO valor no es un retroceso. Las
        # marcas están truncadas a segundos, así que dos latidos del mismo
        # segundo llevan el mismo texto y rechazarlos sería rechazar un
        # latido correcto.
        condiciones.append("(" + columna + " IS NULL OR " + columna + " <= ?)")
        parametros.append(valor)

    partes = [columna + " = ?" for columna in campos]
    partes += [columna + " = " + columna + " + 1" for columna in incrementos]

    cursor = con.execute(
        "UPDATE tareas SET " + ", ".join(partes)
        + " WHERE " + " AND ".join(condiciones),
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
        exigir_iguales=exigir_iguales,
        exigir_no_retroceso=exigir_no_retroceso,
    )


def rechazo_propiedad(
    fila: dict | None,
    identificador: str,
    trabajador_id: str | None,
    generacion: int,
    momento: str,
    estados_admitidos=None,
    exigir_iguales=None,
    exigir_no_retroceso=None,
) -> dict:
    """
    Describe por qué se rechaza una orden del ciclo, con un formato único.

    Distinguir el motivo importa: "otro propietario" y "generación vencida"
    son fallos distintos. El segundo es el caso del MISMO trabajador que
    vuelve a tomar la tarea, y es justo el que un control por identidad
    dejaría pasar.

    Las precondiciones de A3.3 también se describen. Sin esto, un rechazo
    por `exigir_iguales` —el dueño latió mientras la recuperación decidía—
    caía en «estado incompatible» con un detalle que se contradecía a sí
    mismo («está en en_ejecucion, que no admite esta orden; la admiten:
    en_ejecucion»), y eso era lo que la consola le enseñaba al operador
    bajo el rótulo de «reclamada».
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

    if estados_admitidos and str(fila["estado"]) not in {
        str(uno) for uno in estados_admitidos
    }:
        base.update(
            {
                "motivo": MOTIVO_ESTADO_INCOMPATIBLE,
                "detalle": "La tarea '" + str(identificador) + "' está en "
                "estado '" + str(fila["estado"]) + "', que no admite esta "
                "orden. La admiten: " + ", ".join(estados_admitidos) + ".",
            }
        )

        return base

    for columna, valor in (exigir_iguales or {}).items():
        actual = fila.get(columna)

        if actual != valor:
            base.update(
                {
                    "motivo": MOTIVO_PRECONDICION_CAMBIADA,
                    "detalle": "La columna '" + str(columna) + "' de '"
                    + str(identificador) + "' cambió entre la lectura y la "
                    "escritura de esta orden (era " + repr(valor)
                    + ", ahora " + repr(actual) + ")"
                    + (
                        ": el propietario dio señal de vida entre medias"
                        if columna == "ultimo_latido"
                        else ": otra orden legítima escribió entre medias"
                    )
                    + ". No se modificó nada.",
                }
            )

            return base

    for columna, valor in (exigir_no_retroceso or {}).items():
        actual = fila.get(columna)

        if actual is not None and valor is not None and actual > valor:
            base.update(
                {
                    "motivo": MOTIVO_MARCA_MAS_NUEVA,
                    "detalle": "La columna '" + str(columna) + "' de '"
                    + str(identificador) + "' ya tiene una marca más nueva ("
                    + repr(actual) + ") que la que traía esta orden ("
                    + repr(valor) + "). No se modificó nada.",
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


def _pendientes_que_desaparecen(existente: dict, ficha: Ficha) -> list:
    """
    Decisiones humanas PENDIENTES que la declaración nueva ya no trae.

    Una decisión pendiente frena la tarea: `verificar` no puede llevarla a
    PROPUESTO mientras quede alguna. Si al refrescar la definición esa
    decisión desaparece, el freno desaparece con ella, y eso es alterar en
    silencio una garantía que la tarea tenía cuando se tomó.
    """
    declaradas = {
        str(una.get("clave"))
        for una in ficha.requiere_decision_humana
        if una.get("clave") is not None
    }

    return [
        str(una.get("clave"))
        for una in (existente["decisiones"] or [])
        if not una.get("resuelta", False)
        and str(una.get("clave")) not in declaradas
    ]


def ambito_congelado(existente: dict, ficha: Ficha) -> bool:
    """
    ¿Hay que dejar la definición como está porque la tarea está viva?

    Se congela por dos motivos, y los dos alteran en silencio una garantía
    que la tarea tenía cuando se tomó:

    1. Cambia el ÁMBITO. Es el caso que da nombre a la función y el que
       sostiene la regla de un solo escritor.

       Se compara por CONTENIDO y no por orden: `supervisor.solapamientos`
       recorre el producto cartesiano, así que ['a','b'] y ['b','a']
       garantizan lo mismo. Comparar las listas tal cual haría que
       reordenar un patrón, sin mover un solo archivo, congelara toda la
       definición y bloqueara de paso un arreglo de título.

    2. DESAPARECE una decisión humana pendiente. Una decisión pendiente
       frena la tarea, y borrarla del JSON quitaba el freno: comprobado,
       bastaba un `ver` después de editar el archivo. Añadir decisiones
       nuevas sí se permite, porque añade frenos, no los quita.

    Lo demás —título, descripción de una decisión, criterios— se sincroniza
    con normalidad: sólo se frena lo que rompería una garantía.
    """
    if str(existente["estado"]) not in ESTADOS_QUE_RETIENEN_AMBITO:
        return False

    if set(ficha.ambito_archivos) != set(existente["ambito_archivos"] or []):
        return True

    return bool(_pendientes_que_desaparecen(existente, ficha))


def informe_ambito_congelado(existente: dict, ficha: Ficha) -> dict:
    """
    El informe de un ámbito que NO se aplica, construido en un solo sitio.

    Lo usan `sincronizar_ficha`, que decide dentro de su transacción, y
    `asegurar_ficha`, que necesita devolverlo SIN abrir ninguna. Que salga
    de aquí es lo que garantiza que las dos cuenten lo mismo.
    """
    perdidas = _pendientes_que_desaparecen(existente, ficha)

    if set(ficha.ambito_archivos) != set(existente["ambito_archivos"] or []):
        motivo = "no se aplica el cambio de ámbito declarado en el JSON"
    else:
        motivo = (
            "el JSON ya no declara decisiones humanas que siguen pendientes"
            " (" + ", ".join(sorted(perdidas)) + "), y quitarlas le quitaría"
            " el freno"
        )

    return {
        "id": ficha.id,
        "accion": ACCION_AMBITO_CONGELADO,
        "eventos": 0,
        "estado": existente["estado"],
        "ambito_grabado": list(existente["ambito_archivos"] or []),
        "ambito_declarado": list(ficha.ambito_archivos),
        "decisiones_pendientes_perdidas": sorted(perdidas),
        "detalle": "La tarea '" + ficha.id + "' está en estado '"
        + str(existente["estado"]) + "' y su definición queda congelada: "
        + motivo + ". Se sincronizará sola cuando la tarea deje de estar "
        "viva.",
    }


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

    if ambito_congelado(existente, ficha):
        return informe_ambito_congelado(existente, ficha)

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
            "max_intentos": int(ficha.max_intentos),
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

    existente = obtener_tarea(con, ficha.id)

    # Atajo imprescindible, no una optimización. Mientras el ámbito está
    # congelado la huella NO avanza a propósito, así que
    # `necesita_sincronizacion` dice que sí para siempre. Sin esta salida,
    # cada orden —incluidas las de SÓLO LECTURA, porque `cargar` pasa por
    # aquí— abriría un BEGIN IMMEDIATE, es decir pediría el bloqueo de
    # escritura de toda la base, para no escribir nada. Bajo concurrencia
    # eso convierte un `ver` en una espera de 5 s que acaba en "database is
    # locked".
    if existente is not None and ambito_congelado(existente, ficha):
        # Se DEVUELVE el informe, no se delega en `sincronizar_ficha`.
        #
        # Delegar era una regresión de esta misma etapa, encontrada por la
        # ronda focalizada: esa función vuelve a leer la fila y a decidir
        # por su cuenta, y como aquí no hay candado, si la tarea dejaba de
        # estar viva entre las dos lecturas acababa ejecutando su UPDATE y
        # su evento EN AUTOCOMMIT, fuera de toda transacción. Es decir, la
        # salida que existe para NO escribir podía escribir, y encima sin
        # protección.
        #
        # `sincronizar_ficha` exige transacción en su contrato y aquí no la
        # hay: lo correcto es no llamarla.
        return informe_ambito_congelado(existente, ficha)

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
        # A3.2: tareas cuyo cambio de ámbito NO se aplicó por estar vivas.
        # Va en su propia cubeta y no en `sin_cambios` porque no es lo
        # mismo: en `sin_cambios` no había nada que hacer, aquí sí lo hay y
        # está esperando. Informarlo como "sin cambios" le diría al usuario
        # que su edición se aplicó cuando no se aplicó.
        "ambito_congelado": [],
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

    # Las congeladas se apartan ANTES de decidir si hace falta el candado.
    #
    # Mientras el ámbito está congelado la huella no avanza a propósito, así
    # que esas fichas entran siempre en `pendientes` y, sin esto, una tanda
    # en la que TODO está congelado abría un BEGIN IMMEDIATE sobre la base
    # entera para no escribir nada. Es el mismo atajo que ya tiene
    # `asegurar_ficha`, aplicado donde también hacía falta.
    congeladas = []
    restantes = []

    for ficha in pendientes:
        existente = obtener_tarea(con, ficha.id)

        if existente is not None and ambito_congelado(existente, ficha):
            congeladas.append(informe_ambito_congelado(existente, ficha))
        else:
            restantes.append(ficha)

    for resultado in congeladas:
        informe["ambito_congelado"].append(
            {
                "id": resultado["id"],
                "estado": resultado["estado"],
                "detalle": resultado["detalle"],
            }
        )

    pendientes = restantes

    if not pendientes:
        informe["sin_cambios"].sort()
        informe["ambito_congelado"].sort(key=lambda uno: uno["id"])

        return informe

    with transaccion(con):
        for ficha in pendientes:
            resultado = sincronizar_ficha(con, ficha, ahora)

            if resultado["accion"] == ACCION_IMPORTADA:
                informe["importadas"].append(ficha.id)
            elif resultado["accion"] == ACCION_ACTUALIZADA:
                informe["actualizadas"].append(ficha.id)
            elif resultado["accion"] == ACCION_AMBITO_CONGELADO:
                informe["ambito_congelado"].append(
                    {
                        "id": ficha.id,
                        "estado": resultado["estado"],
                        "detalle": resultado["detalle"],
                    }
                )
            else:
                informe["sin_cambios"].append(ficha.id)

    informe["sin_cambios"].sort()
    informe["ambito_congelado"].sort(key=lambda uno: uno["id"])

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
        # Definiciones que NO se aplicarán mientras la tarea siga viva.
        "congeladas": [],
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

        if informe["version_esquema"] > VERSION_ESQUEMA:
            # Otra build más nueva (otro worktree, otra rama) ya migró la
            # base común. `inicializar-estado` no puede bajarla: lo que
            # toca es usar un Supervisor que la entienda.
            informe["estado"] = "ESQUEMA_MAS_NUEVO"
            informe["detalle"] = (
                "Versión de esquema "
                + str(informe["version_esquema"])
                + ", más nueva que la que entiende este Supervisor ("
                + str(VERSION_ESQUEMA)
                + "). Use la build que la migró; 'inicializar-estado' no "
                "puede retrocederla."
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
        # Una definición que no se ha aplicado y una que NO SE VA A aplicar
        # hasta que la tarea deje de estar viva no son lo mismo, y meterlas
        # en la misma lista dejaba al operador esperando un refresco que no
        # iba a llegar. Se separan.
        pendientes = [
            ficha
            for ficha in fichas
            if ficha.id in registradas
            and registradas[ficha.id]["definicion_hash"] != hash_definicion(ficha)
        ]

        informe["congeladas"] = [
            ficha.id
            for ficha in pendientes
            if ambito_congelado(registradas[ficha.id], ficha)
        ]
        informe["desactualizadas"] = [
            ficha.id
            for ficha in pendientes
            if ficha.id not in informe["congeladas"]
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
