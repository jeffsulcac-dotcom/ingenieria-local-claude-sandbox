"""
Trabajadores V1 (T-0003): cola persistente, despacho, worktrees automáticos
y limpieza.

Qué añade sobre el Supervisor de A3.3
-------------------------------------
- Una COLA PERSISTENTE en la misma base SQLite global (tabla `cola`,
  migración 3). Sobrevive a cierres y apagones igual que el estado de las
  tareas, y su orden lo resuelve la base —`prioridad DESC, secuencia ASC`—,
  no el proceso que despacha: dos procesos que la lean ven el mismo orden.

- Un DESPACHO que reclama la tarea y marca la entrada de la cola en UNA
  sola transacción, por el gancho `tomar(..., al_conceder=...)`. Dos
  procesos despachando la misma entrada tienen exactamente un ganador,
  decidido por `rowcount` sobre `estado_cola = 'pendiente'`. La regla de un
  solo escritor por ámbito la sigue aplicando `tomar`, dentro de esa misma
  transacción.

- WORKTREES AUTOMÁTICOS dentro de una ZONA CONTROLADA: `<raíz>/.arboles/<id>`
  (ignorada por Git). El despacho sólo crea ahí, y la limpieza sólo borra
  ahí: un árbol registrado fuera de la zona no se toca nunca, y dentro de
  ella sólo se borra el que Git reconoce (`resolver_worktree`), que no
  sostiene ninguna ejecución viva y que no tiene NADA sin confirmar, sin
  `--force` jamás.

- Un PROCESO TRABAJADOR (`trabajador.py`) que recibe todo por argv
  estructurado —nunca una línea de órdenes de texto ni `shell=True`—,
  adopta la ejecución con su PID, late mientras dura el trabajo, ejecuta el
  trabajo con `subprocess` en el árbol de la tarea, comprueba que sólo
  escribió dentro de su ámbito y verifica con el corredor. Al terminar deja
  la fila liberada y la entrada de la cola cerrada con el resultado.

- RECUPERACIÓN de la cola: `reconciliar_cola` devuelve a `pendiente` la
  entrada de una tarea cuya ejecución `reanudar` liberó por huérfana, y
  cierra la de una tarea que ya terminó. Lo que `reanudar` NO libera —un
  latido vencido con el proceso vivo, un trabajador de otra máquina, una
  fila incompleta— aquí tampoco se toca: la entrada sigue `despachada` y se
  informa. Ante la duda no se libera nada; decide una persona.

Lo que NO hace (V2): lanzar trabajadores por sí solo en bucle (cada
`despachar` lanza a lo sumo uno), expirar trabajadores por tiempo, matar
un proceso que no responde, ni acciones desde el tablero.

Este módulo no realiza cálculos de ingeniería.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from ingenieria_nucleo.estados import Estado

from . import RAIZ as RAIZ_SUPERVISOR
from . import estado_global as global_
from . import pruebas as corredor
from . import supervisor as nucleo
from .supervisor import (
    ErrorSupervisor,
    ErrorWorktree,
    Git,
    entorno_git_limpio,
    resolver_worktree,
)
from .tarea import ahora_utc, validar_id


# Carpeta, dentro de la raíz, donde viven los worktrees automáticos.
ZONA_ARBOLES = ".arboles"

# Registros de salida de los trabajadores, dentro de la zona pero aparte
# de los árboles.
CARPETA_REGISTROS = ".registros"

# Tiempo máximo del trabajo de una entrada cuando no se indica otro.
TIEMPO_LIMITE_TRABAJO_S = 3600

# Reintentos de `git worktree add` cuando dos despachos preparan a la vez el
# árbol de la misma tarea (ver `preparar_arbol`), y la espera base entre
# ellos (crece con cada intento).
INTENTOS_CREAR_ARBOL = 6
ESPERA_CREAR_ARBOL_S = 0.05

# Caracteres de la salida del trabajo que se conservan en la entrada.
LIMITE_SALIDA_TRABAJO = 4000

# Motivos de rechazo de un despacho, con nombre estable.
RECHAZO_NO_PENDIENTE = "no_pendiente"
RECHAZO_ESTADO = "estado_no_tomable"
RECHAZO_AMBITO = "ambito_en_conflicto"
RECHAZO_ARBOL = "arbol_no_valido"
RECHAZO_TOMA = "toma_perdida"
RECHAZO_SIN_CANDIDATAS = "sin_candidatas"

# Resultados con que el trabajador cierra su entrada.
RESULTADO_VERIFICADA = "verificada"
RESULTADO_TRABAJO_FALLIDO = "trabajo_fallido"
RESULTADO_FUERA_DE_AMBITO = "fuera_de_ambito"
RESULTADO_TRABAJADOR_AVERIADO = "trabajador_averiado"
RESULTADO_RECONCILIADA = "reconciliada"


class ErrorCola(ErrorSupervisor):
    """La cola no admite la operación (entrada repetida, ausente o cerrada)."""


class ErrorDespacho(ErrorSupervisor):
    """
    No se despachó nada. Lleva el informe con los rechazos, uno por
    entrada considerada, para que quien llame sepa si es que la cola está
    vacía, la tarea ya la tiene otro, el ámbito choca o el árbol no vale.
    """

    def __init__(self, informe: dict):
        super().__init__(informe.get("detalle") or "No se despachó ninguna tarea.")

        self.informe = dict(informe)
        self.tarea = informe.get("tarea")
        self.motivo = informe.get("motivo")
        self.rechazos = list(informe.get("rechazos") or [])


class ErrorLimpieza(ErrorWorktree):
    """
    Un árbol no se borra: está fuera de la zona controlada, Git no lo
    reconoce, sostiene una ejecución viva o tiene trabajo sin confirmar.
    """


# ----------------------------------------------------------------------
# Zona controlada
# ----------------------------------------------------------------------

def zona_de_arboles(raiz: Path) -> Path:
    return Path(raiz).resolve() / ZONA_ARBOLES


def ruta_de_arbol(raiz: Path, identificador: str) -> Path:
    """Ruta que el despacho asigna al árbol de una tarea: siempre en la zona."""
    validar_id(identificador)

    return zona_de_arboles(raiz) / identificador


def dentro_de_zona(raiz: Path, ruta: Path) -> bool:
    """
    Si `ruta` es un HIJO DIRECTO de la zona, resueltos los dos.

    Hijo directo y no descendiente: `<zona>/T-0001/pruebas` también estaría
    «dentro», y borrarlo no sería borrar un árbol sino un trozo de uno.
    """
    zona = zona_de_arboles(raiz)

    try:
        candidata = Path(ruta).resolve()
    except OSError:
        return False

    return candidata.parent == zona and candidata != zona


def ruta_de_registro(raiz: Path, identificador: str, secuencia: int) -> Path:
    return (
        zona_de_arboles(raiz) / CARPETA_REGISTROS
        / (identificador + "." + str(int(secuencia)) + ".log")
    )


# ----------------------------------------------------------------------
# Ámbito: qué rutas caen dentro
# ----------------------------------------------------------------------

def _patron_a_regex(patron: str) -> re.Pattern:
    """
    Comodines conscientes de las barras: `*` y `?` no cruzan `/`, `**` sí.

    `fnmatch` (el de `patrones_solapan`) deja que `*` cruce directorios,
    que allí es conservador —más conflictos— y aquí sería permisivo: un
    ámbito `modulos/*.py` daría por dentro a `modulos/otro/x.py`. Para
    decidir si un trabajador escribió fuera, el error tiene que caer del
    lado de «fuera».
    """
    partes = []
    indice = 0

    while indice < len(patron):
        caracter = patron[indice]

        if patron.startswith("**", indice):
            partes.append(".*")
            indice += 2
        elif caracter == "*":
            partes.append("[^/]*")
            indice += 1
        elif caracter == "?":
            partes.append("[^/]")
            indice += 1
        else:
            partes.append(re.escape(caracter))
            indice += 1

    return re.compile("^" + "".join(partes) + "$")


def ruta_en_ambito(ruta: str, ambito: list[str]) -> bool:
    """
    Si una ruta relativa cae dentro de alguno de los patrones del ámbito.

    Un patrón sin comodines nombra un archivo o una carpeta: la carpeta
    abarca todo lo que cuelga de ella. Los patrones totales (`*`, `**`, `.`)
    abarcan todo, igual que en `patrones_solapan`.
    """
    limpia = nucleo.normalizar_patron(ruta)

    for patron in ambito:
        texto = nucleo.normalizar_patron(patron)

        if texto in nucleo.PATRONES_TOTALES:
            return True

        if limpia == texto or limpia.startswith(texto + "/"):
            return True

        if any(simbolo in texto for simbolo in "*?[") and _patron_a_regex(
            texto
        ).match(limpia):
            return True

    return False


def fuera_de_ambito(rutas: list[str], ambito: list[str]) -> list[str]:
    """Las rutas que NO caen en el ámbito, ordenadas."""
    return sorted(ruta for ruta in rutas if not ruta_en_ambito(ruta, ambito))


# ----------------------------------------------------------------------
# Entradas de la cola: lectura
# ----------------------------------------------------------------------

def _entrada_a_dict(fila) -> dict:
    datos = dict(fila)
    datos["trabajo"] = global_._de_json(datos.get("trabajo"), [])
    datos["resultado"] = global_._de_json(datos.get("resultado"))

    return datos


def _obtener_entrada(con, secuencia: int) -> dict | None:
    fila = con.execute(
        "SELECT * FROM cola WHERE secuencia = ?", (int(secuencia),)
    ).fetchone()

    return None if fila is None else _entrada_a_dict(fila)


def _entrada_viva_de(con, identificador: str) -> dict | None:
    fila = con.execute(
        "SELECT * FROM cola WHERE tarea_id = ? AND estado_cola IN (?, ?)",
        (identificador,) + tuple(global_.COLA_ESTADOS_VIVOS),
    ).fetchone()

    return None if fila is None else _entrada_a_dict(fila)


# El orden de la cola, en UN solo sitio. Lo resuelve SQLite: quien la lea
# desde cualquier proceso, antes o después de un reinicio, obtiene lo mismo.
ORDEN_DE_COLA = "ORDER BY prioridad DESC, secuencia ASC"


def _listar_entradas(con, estados=None) -> list[dict]:
    consulta = "SELECT * FROM cola "
    parametros: tuple = ()

    if estados:
        estados = tuple(str(uno) for uno in estados)
        consulta += (
            "WHERE estado_cola IN (" + ", ".join("?" for _ in estados) + ") "
        )
        parametros = estados

    return [
        _entrada_a_dict(fila)
        for fila in con.execute(consulta + ORDEN_DE_COLA, parametros)
    ]


def listar_cola(raiz: Path) -> list[dict]:
    """
    La cola en su orden de despacho, con el estado real de cada tarea y,
    para las pendientes, por qué no se despacharían ahora mismo.

    Sólo lee. Las cerradas (terminadas, fallidas, retiradas) también se
    listan, al final de su orden, porque son el registro de lo lanzado.
    """
    raiz = Path(raiz).resolve()

    with global_.conexion(raiz) as con:
        entradas = _listar_entradas(con)
        tareas = {fila["id"]: fila for fila in global_.listar_tareas(con)}

    vivas = []
    cerradas = []

    for entrada in entradas:
        fila = tareas.get(entrada["tarea_id"])

        entrada["estado_tarea"] = None if fila is None else fila["estado"]
        entrada["trabajador_tarea"] = None if fila is None else fila["trabajador_id"]
        entrada["despachable"] = None
        entrada["por_que_no"] = None

        if entrada["estado_cola"] == global_.COLA_PENDIENTE:
            motivo = _por_que_no_se_despacha(fila, tareas)
            entrada["despachable"] = motivo is None
            entrada["por_que_no"] = motivo

        if entrada["estado_cola"] in global_.COLA_ESTADOS_VIVOS:
            vivas.append(entrada)
        else:
            cerradas.append(entrada)

    return vivas + cerradas


def _por_que_no_se_despacha(fila: dict | None, tareas: dict) -> str | None:
    """Motivo por el que una entrada pendiente no se despacharía ahora; None si sí."""
    if fila is None:
        return "la tarea no existe en el estado global"

    if fila["estado"] not in {str(uno) for uno in nucleo.ESTADOS_TOMABLES}:
        return "la tarea está en estado '" + str(fila["estado"]) + "'"

    activos = {str(uno) for uno in nucleo.ESTADOS_QUE_RETIENEN_AMBITO}

    for otra in tareas.values():
        if otra["id"] == fila["id"] or otra["estado"] not in activos:
            continue

        pares = nucleo.solapamientos(
            fila["ambito_archivos"] or [], otra["ambito_archivos"] or []
        )

        if pares:
            return (
                "su ámbito choca con " + otra["id"] + " ("
                + ", ".join(uno + " <-> " + dos for uno, dos in pares) + ")"
            )

    return None


# ----------------------------------------------------------------------
# Encolar y retirar
# ----------------------------------------------------------------------

def _validar_trabajo(trabajo) -> list[str]:
    """
    El trabajo es una LISTA de cadenas, y sólo eso.

    Una cadena suelta se rechaza aunque «se pudiera partir»: partirla es
    justo interpretar una línea de órdenes, y ese es el camino que abre la
    puerta a que un argumento con espacios, comillas o `;` signifique algo
    distinto de lo que se escribió.
    """
    if trabajo is None:
        return []

    if isinstance(trabajo, (str, bytes)) or not isinstance(
        trabajo, (list, tuple)
    ):
        raise ErrorCola(
            "El trabajo de una entrada es una lista de argumentos (argv), "
            "nunca una cadena que haya que interpretar; se recibió: "
            + repr(trabajo) + "."
        )

    argumentos = []

    for argumento in trabajo:
        if not isinstance(argumento, str):
            raise ErrorCola(
                "Cada argumento del trabajo debe ser texto; se recibió: "
                + repr(argumento) + "."
            )

        if "\0" in argumento:
            raise ErrorCola("Un argumento del trabajo contiene un NUL.")

        argumentos.append(argumento)

    if argumentos and not argumentos[0].strip():
        raise ErrorCola("El ejecutable del trabajo está en blanco.")

    return argumentos


def encolar(
    raiz: Path,
    identificador: str,
    prioridad: int = 0,
    trabajo=None,
    tiempo_limite_s: int = TIEMPO_LIMITE_TRABAJO_S,
) -> dict:
    """
    Añade una tarea a la cola. Una sola entrada viva por tarea.

    La unicidad la impone el índice parcial de la base dentro de la
    transacción: dos `encolar` a la vez de la misma tarea dejan UNA
    entrada. El perdedor recibe `ErrorCola`, no una avería.
    """
    raiz = Path(raiz).resolve()
    validar_id(identificador)

    if isinstance(prioridad, bool) or not isinstance(prioridad, int):
        raise ErrorCola(
            "La prioridad debe ser un entero; se recibió: " + repr(prioridad) + "."
        )

    if isinstance(tiempo_limite_s, bool) or not isinstance(
        tiempo_limite_s, int
    ) or tiempo_limite_s <= 0:
        raise ErrorCola(
            "El tiempo límite del trabajo debe ser un entero positivo de "
            "segundos; se recibió: " + repr(tiempo_limite_s) + "."
        )

    argumentos = _validar_trabajo(trabajo)

    # La definición se carga primero: sin ficha JSON no hay tarea que
    # encolar, y `cargar` la incorpora a la base si todavía no estaba.
    ficha = nucleo.cargar(raiz, identificador)

    momento = ahora_utc()

    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            viva = _entrada_viva_de(con, identificador)

            if viva is not None:
                raise ErrorCola(
                    "La tarea '" + identificador + "' ya está en la cola "
                    "(entrada " + str(viva["secuencia"]) + ", "
                    + str(viva["estado_cola"]) + "). No se encola dos veces."
                )

            try:
                cursor = con.execute(
                    """
                    INSERT INTO cola
                        (tarea_id, prioridad, estado_cola, trabajo,
                         tiempo_limite_s, encolado_en, actualizado_en)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identificador,
                        int(prioridad),
                        global_.COLA_PENDIENTE,
                        json.dumps(argumentos, ensure_ascii=False),
                        int(tiempo_limite_s),
                        momento,
                        momento,
                    ),
                )
            except global_.sqlite3.IntegrityError as choque:
                # El índice único parcial decidió con el candado tomado: la
                # comprobación de arriba es el mensaje amable, esto es la
                # garantía.
                raise ErrorCola(
                    "La tarea '" + identificador + "' ya está en la cola: "
                    + str(choque) + "."
                ) from None

            secuencia = int(cursor.lastrowid)

            global_.insertar_evento(
                con,
                identificador,
                {
                    "fecha": momento,
                    "tipo": global_.EVENTO_COLA,
                    "estado_anterior": str(ficha.estado),
                    "estado_nuevo": str(ficha.estado),
                    "motivo": "Encolada (entrada " + str(secuencia)
                    + ", prioridad " + str(int(prioridad)) + ").",
                    "origen": nucleo.ORIGEN_HUMANO,
                    "datos": {
                        "secuencia": secuencia,
                        "prioridad": int(prioridad),
                        "trabajo": argumentos,
                    },
                },
            )

            entrada = _obtener_entrada(con, secuencia)

    return entrada


def desencolar(raiz: Path, identificador: str, motivo: str = "") -> dict:
    """Retira la entrada PENDIENTE de una tarea. Una despachada no se retira."""
    raiz = Path(raiz).resolve()
    validar_id(identificador)

    momento = ahora_utc()

    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            viva = _entrada_viva_de(con, identificador)

            if viva is None:
                raise ErrorCola(
                    "La tarea '" + identificador + "' no está en la cola."
                )

            if viva["estado_cola"] != global_.COLA_PENDIENTE:
                raise ErrorCola(
                    "La entrada " + str(viva["secuencia"]) + " de '"
                    + identificador + "' ya está despachada: la cierra el "
                    "trabajador, o la recuperación si el trabajador murió."
                )

            cursor = con.execute(
                "UPDATE cola SET estado_cola = ?, actualizado_en = ?, "
                "terminado_en = ?, resultado = ? "
                "WHERE secuencia = ? AND estado_cola = ?",
                (
                    global_.COLA_RETIRADA,
                    momento,
                    momento,
                    json.dumps(
                        {"tipo": "retirada", "motivo": motivo or "Retirada a mano."},
                        ensure_ascii=False,
                    ),
                    viva["secuencia"],
                    global_.COLA_PENDIENTE,
                ),
            )

            if cursor.rowcount != 1:
                raise ErrorCola(
                    "La entrada " + str(viva["secuencia"]) + " cambió mientras "
                    "se retiraba."
                )

            global_.insertar_evento(
                con,
                identificador,
                {
                    "fecha": momento,
                    "tipo": global_.EVENTO_COLA,
                    "motivo": "Retirada de la cola (entrada "
                    + str(viva["secuencia"]) + ").",
                    "origen": nucleo.ORIGEN_HUMANO,
                    "datos": {"secuencia": viva["secuencia"], "motivo": motivo},
                },
            )

            return _obtener_entrada(con, viva["secuencia"])


# ----------------------------------------------------------------------
# Worktrees automáticos
# ----------------------------------------------------------------------

def _git(raiz: Path, *argumentos: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *argumentos],
            cwd=str(raiz),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=entorno_git_limpio(),
        )
    except OSError as error:
        return subprocess.CompletedProcess(["git", *argumentos], 1, "", str(error))


def _rama_existe(raiz: Path, rama: str) -> bool:
    return _git(
        raiz, "rev-parse", "--verify", "--quiet", "refs/heads/" + rama
    ).returncode == 0


def preparar_arbol(raiz: Path, ficha) -> tuple:
    """
    El árbol de la tarea dentro de la zona: `<raíz>/.arboles/<id>`, en la
    rama de la tarea. Lo crea si no existe; si existe, lo VALIDA y lo reúsa.

    Devuelve (ruta resuelta, creado_ahora).

    Si la rama de la tarea no existe todavía se crea desde el HEAD de la
    raíz; si existe, se le hace checkout. Git rechaza por sí mismo una rama
    que ya esté extraída en otro árbol —incluida la raíz—, y ese rechazo se
    devuelve tal cual como `ErrorWorktree`: no se ejecuta en un árbol que no
    está en la rama que la ficha exige.

    Reusar un árbol existente exige lo mismo que cualquier otra ejecución:
    que Git lo liste, que responda por este repositorio y que esté en la
    rama de la tarea. Una carpeta cualquiera con ese nombre no vale.
    """
    raiz = Path(raiz).resolve()
    destino = ruta_de_arbol(raiz, ficha.id)
    rama = ficha.rama or nucleo.PREFIJO_RAMA + ficha.id

    creado = False
    ultimo_error = ""

    # Dos despachos pueden llegar aquí a la vez para la misma tarea. Git
    # sólo deja ganar a uno (`-b` no puede crear dos veces la rama y la
    # ruta no puede crearse dos veces), y el perdedor ve el fallo ANTES de
    # que el directorio del ganador exista. Se reintenta unas pocas veces
    # con la orden que corresponda al estado que haya en cada momento; en
    # cuanto el directorio aparece, se valida como cualquier árbol
    # existente y es la toma quien decide quién se queda con la tarea.
    for intento in range(INTENTOS_CREAR_ARBOL):
        if destino.exists():
            break

        destino.parent.mkdir(parents=True, exist_ok=True)

        if _rama_existe(raiz, rama):
            orden = ("worktree", "add", str(destino), rama)
        else:
            orden = ("worktree", "add", "-b", rama, str(destino))

        resultado = _git(raiz, *orden)

        if resultado.returncode == 0:
            creado = True
            break

        ultimo_error = (resultado.stderr or resultado.stdout).strip()
        time.sleep(ESPERA_CREAR_ARBOL_S * (intento + 1))

    if not destino.exists():
        raise ErrorWorktree(
            "Git no pudo crear el árbol '" + str(destino) + "' en la rama '"
            + rama + "': " + ultimo_error
        )

    arbol = resolver_worktree(raiz, str(destino))

    if not dentro_de_zona(raiz, arbol):
        raise ErrorWorktree(
            "El árbol '" + str(arbol) + "' quedó fuera de la zona controlada '"
            + str(zona_de_arboles(raiz)) + "'."
        )

    actual = Git(arbol).rama_actual()

    if actual != rama:
        raise ErrorWorktree(
            "El árbol '" + str(arbol) + "' está en la rama '" + str(actual)
            + "' y la tarea exige '" + rama + "'. No se ejecuta ahí."
        )

    return arbol, creado


# ----------------------------------------------------------------------
# Despacho
# ----------------------------------------------------------------------

def argumentos_del_trabajador(
    raiz: Path,
    ficha,
    entrada: dict,
    ejecutable: str | None = None,
    intervalo_latido_s: float | None = None,
) -> list[str]:
    """
    El argv COMPLETO con el que se lanza el proceso trabajador.

    Es una lista y se entrega a `subprocess` como lista. El trabajo del
    usuario va al final, detrás de `--`, argumento por argumento: lo que se
    encoló es lo que recibe el proceso, sin pasar por ningún intérprete.
    """
    argumentos = [
        ejecutable or sys.executable,
        "-m",
        "orquestacion.ingenieria_supervisor.trabajador",
        "--raiz", str(raiz),
        "--tarea", ficha.id,
        "--trabajador", str(ficha.trabajador_id),
        "--generacion", str(int(ficha.generacion)),
        "--secuencia", str(int(entrada["secuencia"])),
        "--worktree", str(ficha.worktree),
        "--tiempo-limite", str(int(entrada["tiempo_limite_s"])),
        # El PID con el que el despacho reclamó la fila: el trabajador sólo
        # adopta si sigue ahí, así que dos lanzamientos del mismo argv no
        # pueden adoptar los dos.
        "--pid-despacho", str(int(ficha.pid)),
    ]

    if intervalo_latido_s is not None:
        argumentos += ["--intervalo-latido", str(float(intervalo_latido_s))]

    argumentos.append("--")
    argumentos.extend(list(entrada["trabajo"] or []))

    return argumentos


def entorno_del_trabajador() -> dict:
    """
    El trabajador importa EL MISMO Supervisor que lo despacha —el paquete
    desde el que corre este código, `RAIZ_SUPERVISOR`—, nunca el del árbol
    de la tarea ni el del repositorio supervisado: `PYTHONPATH` se fija a
    esas raíces de paquetes, igual que hace el corredor con las suyas. Si la
    tarea modifica el propio Supervisor, el código que la vigila no es el
    que está modificando. En el uso normal el Supervisor vive en el
    repositorio que supervisa y las dos raíces coinciden; en las pruebas y
    en una instalación aparte, no.
    """
    return corredor.entorno_controlado(RAIZ_SUPERVISOR)


def lanzar_trabajador(argv: list[str], raiz: Path, registro: Path):
    """
    Lanza el proceso trabajador, desligado del que despacha. `raiz` es la
    del repositorio supervisado y viaja ya dentro de `argv`.

    `shell=False` es el valor por omisión de `Popen` y se escribe igual:
    es la garantía de que ningún argumento se interpreta. El proceso se
    pone en su propia sesión (POSIX) o grupo (Windows) para que sobreviva
    al mandato de consola que lo lanzó y a su Ctrl-C.
    """
    if not isinstance(argv, (list, tuple)) or not all(
        isinstance(uno, str) for uno in argv
    ):
        raise ErrorSupervisor(
            "El trabajador se lanza con una lista de argumentos, no con "
            "texto: " + repr(argv)
        )

    registro.parent.mkdir(parents=True, exist_ok=True)

    # `-m` pone el directorio actual delante de todo en `sys.path`: se lanza
    # desde la raíz del Supervisor para que `orquestacion.ingenieria_supervisor`
    # sea este paquete y no una copia que el árbol de la tarea pudiera tener.
    # El trabajador no depende del directorio actual para nada más: raíz y
    # árbol viajan resueltos en su argv.
    extras = {}

    if os.name == "nt":
        extras["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        extras["start_new_session"] = True

    salida = open(registro, "ab")

    try:
        proceso = subprocess.Popen(
            list(argv),
            cwd=str(RAIZ_SUPERVISOR),
            env=entorno_del_trabajador(),
            stdin=subprocess.DEVNULL,
            stdout=salida,
            stderr=subprocess.STDOUT,
            shell=False,
            **extras,
        )
    finally:
        # El hijo ya tiene su propio descriptor; el del padre se cierra
        # aquí para no retener el archivo (en Windows lo bloquearía).
        salida.close()

    return proceso


def _anotar_rechazo(raiz: Path, secuencia: int, motivo: str, detalle: str) -> None:
    """Deja en la entrada pendiente por qué no se despachó la última vez."""
    momento = ahora_utc()

    try:
        with global_.conexion(raiz) as con:
            with global_.transaccion(con):
                con.execute(
                    "UPDATE cola SET ultimo_rechazo = ?, actualizado_en = ? "
                    "WHERE secuencia = ? AND estado_cola = ?",
                    (
                        json.dumps(
                            {"fecha": momento, "motivo": motivo, "detalle": detalle},
                            ensure_ascii=False,
                        ),
                        momento,
                        int(secuencia),
                        global_.COLA_PENDIENTE,
                    ),
                )
    except global_.ErrorEstadoGlobal:
        # Es información, no decisión: si no se puede anotar, el rechazo
        # ya viaja en el informe del despacho.
        pass


def _despachar_entrada(
    raiz: Path,
    entrada: dict,
    lanzar: bool,
    ejecutable: str | None,
    trabajador_id: str | None,
    ahora,
    intervalo_latido_s: float | None,
) -> dict:
    """
    Un despacho concreto. Lanza `ErrorDespacho` con el motivo si no procede.

    Orden de las cosas, y por qué:

    1. Se mira el estado de la tarea y el ámbito en una lectura SIN
       candado. No decide nada —lo decide `tomar` con el candado tomado—,
       pero evita crear un árbol en disco para una tarea que no se va a
       poder tomar.
    2. Se prepara el árbol en la zona (disco y Git, fuera de la transacción).
    3. `tomar` con el gancho: toma y marca de la entrada en UNA transacción.
    4. Se lanza el proceso trabajador con el argv estructurado.

    Si el paso 3 se pierde (otro despacho, un `tomar` manual, un ámbito
    que chocó entre medias), el árbol se QUEDA aunque lo haya creado este
    despacho: quien ganó la toma pudo haberlo validado y grabado como suyo
    un instante antes —el árbol es de la tarea, no de quien lo creó—, y
    retirarlo le dejaría una ejecución sin árbol. Un árbol de más es
    barato: el siguiente despacho lo reutiliza y `limpiar-arboles` lo
    recoge cuando ya no sostenga nada.
    """
    raiz = Path(raiz).resolve()
    identificador = entrada["tarea_id"]
    secuencia = int(entrada["secuencia"])

    ficha = nucleo.cargar(raiz, identificador)

    with global_.conexion(raiz) as con:
        tareas = {fila["id"]: fila for fila in global_.listar_tareas(con)}

    motivo_previo = _por_que_no_se_despacha(tareas.get(identificador), tareas)

    if motivo_previo is not None:
        raise ErrorDespacho(
            {
                "tarea": identificador,
                "secuencia": secuencia,
                "motivo": (
                    RECHAZO_AMBITO if "choca" in motivo_previo else RECHAZO_ESTADO
                ),
                "detalle": "No se despacha '" + identificador + "' (entrada "
                + str(secuencia) + "): " + motivo_previo + ".",
            }
        )

    arbol, creado = preparar_arbol(raiz, ficha)

    def al_conceder(con, informe, momento):
        cursor = con.execute(
            "UPDATE cola SET estado_cola = ?, trabajador_id = ?, generacion = ?, "
            "pid = ?, worktree = ?, despachado_en = ?, actualizado_en = ?, "
            "ultimo_rechazo = NULL, registro = ? "
            "WHERE secuencia = ? AND estado_cola = ?",
            (
                global_.COLA_DESPACHADA,
                informe["propietario"],
                int(informe["generacion"]),
                informe["pid"],
                str(arbol),
                momento,
                momento,
                str(ruta_de_registro(raiz, identificador, secuencia)),
                secuencia,
                global_.COLA_PENDIENTE,
            ),
        )

        if cursor.rowcount != 1:
            # La entrada dejó de estar pendiente entre la lectura y este
            # UPDATE: otro despacho la marcó, o alguien la retiró. La toma
            # que acaba de concederse se deshace con el ROLLBACK.
            raise ErrorDespacho(
                {
                    "tarea": identificador,
                    "secuencia": secuencia,
                    "motivo": RECHAZO_NO_PENDIENTE,
                    "detalle": "La entrada " + str(secuencia) + " de '"
                    + identificador + "' ya no estaba pendiente al conceder "
                    "la toma: otro despacho se adelantó. No se modificó nada.",
                }
            )

        global_.insertar_evento(
            con,
            identificador,
            {
                "fecha": momento,
                "tipo": global_.EVENTO_COLA,
                "estado_anterior": str(Estado.EN_EJECUCION),
                "estado_nuevo": str(Estado.EN_EJECUCION),
                "motivo": "Despachada desde la cola (entrada " + str(secuencia)
                + ") a '" + str(informe["propietario"]) + "'.",
                "origen": nucleo.ORIGEN_AUTOMATICO,
                "datos": {
                    "secuencia": secuencia,
                    "generacion": int(informe["generacion"]),
                    "worktree": str(arbol),
                },
            },
        )

    try:
        ficha = nucleo.tomar(
            raiz,
            identificador,
            trabajador_id=trabajador_id or nucleo.nuevo_trabajador_id(),
            pid=os.getpid(),
            ahora=ahora,
            git=Git(raiz),
            worktree=str(arbol),
            al_conceder=al_conceder,
        )
    except nucleo.ErrorToma as perdida:
        raise ErrorDespacho(
            {
                "tarea": identificador,
                "secuencia": secuencia,
                "motivo": RECHAZO_TOMA,
                "detalle": "No se despacha '" + identificador + "' (entrada "
                + str(secuencia) + "): " + str(perdida),
            }
        ) from None
    except nucleo.ErrorSolapamiento as choque:
        raise ErrorDespacho(
            {
                "tarea": identificador,
                "secuencia": secuencia,
                "motivo": RECHAZO_AMBITO,
                "detalle": "No se despacha '" + identificador + "' (entrada "
                + str(secuencia) + "): " + str(choque),
            }
        ) from None
    argv = argumentos_del_trabajador(
        raiz, ficha, entrada, ejecutable, intervalo_latido_s
    )
    registro = ruta_de_registro(raiz, identificador, secuencia)

    proceso = lanzar_trabajador(argv, raiz, registro) if lanzar else None

    return {
        "despachada": True,
        "tarea": identificador,
        "secuencia": secuencia,
        "trabajador_id": ficha.trabajador_id,
        "generacion": int(ficha.generacion),
        "worktree": str(arbol),
        "arbol_creado": creado,
        "rama": ficha.rama,
        "commit_inicial": ficha.commit_inicial,
        "argv": argv,
        "registro": str(registro),
        "lanzado": proceso is not None,
        "pid_trabajador": None if proceso is None else proceso.pid,
        "proceso": proceso,
        "rechazos": [],
    }


def despachar(
    raiz: Path,
    identificador: str | None = None,
    lanzar: bool = True,
    ejecutable: str | None = None,
    trabajador_id: str | None = None,
    ahora=None,
    intervalo_latido_s: float | None = None,
) -> dict:
    """
    Despacha UNA entrada: la primera del orden que se pueda tomar ahora, o
    la de `identificador` si se indica.

    Una entrada que hoy no puede tomarse —su tarea la tiene otro, su ámbito
    choca con una tarea viva— no pierde el puesto: se salta y se anota el
    motivo. Así dos tareas sin solapamiento progresan aunque la cabeza de
    la cola esté frenada, y la frenada sale en cuanto el ámbito se libera.

    Devuelve el informe del despacho. Si no despachó nada lanza
    `ErrorDespacho` con un rechazo por entrada considerada.
    """
    raiz = Path(raiz).resolve()

    # Antes de elegir, se cierra lo que ya terminó y se devuelve a la cola
    # lo que la recuperación liberó: es idempotente y barato.
    reconciliar_cola(raiz)

    with global_.conexion(raiz) as con:
        pendientes = _listar_entradas(con, (global_.COLA_PENDIENTE,))

    if identificador is not None:
        validar_id(identificador)
        pendientes = [
            una for una in pendientes if una["tarea_id"] == identificador
        ]

        if not pendientes:
            raise ErrorDespacho(
                {
                    "tarea": identificador,
                    "motivo": RECHAZO_NO_PENDIENTE,
                    "detalle": "La tarea '" + identificador + "' no tiene "
                    "ninguna entrada pendiente en la cola: o no se encoló, o "
                    "ya está despachada, o su entrada se cerró.",
                    "rechazos": [],
                }
            )

    rechazos = []

    for entrada in pendientes:
        try:
            informe = _despachar_entrada(
                raiz, entrada, lanzar, ejecutable, trabajador_id, ahora,
                intervalo_latido_s,
            )
        except ErrorDespacho as rechazo:
            rechazos.append(rechazo.informe)
            _anotar_rechazo(
                raiz, entrada["secuencia"], rechazo.motivo, str(rechazo)
            )
            continue
        except ErrorWorktree as problema:
            rechazos.append(
                {
                    "tarea": entrada["tarea_id"],
                    "secuencia": entrada["secuencia"],
                    "motivo": RECHAZO_ARBOL,
                    "detalle": str(problema),
                }
            )
            _anotar_rechazo(
                raiz, entrada["secuencia"], RECHAZO_ARBOL, str(problema)
            )
            continue

        informe["rechazos"] = rechazos

        return informe

    raise ErrorDespacho(
        {
            "tarea": identificador,
            "motivo": rechazos[0]["motivo"] if len(rechazos) == 1 else RECHAZO_SIN_CANDIDATAS,
            "detalle": (
                "No hay ninguna entrada pendiente en la cola."
                if not pendientes
                else "Ninguna entrada pendiente se pudo despachar: "
                + "; ".join(str(uno["detalle"]) for uno in rechazos)
            ),
            "rechazos": rechazos,
        }
    )


# ----------------------------------------------------------------------
# Cierre de una entrada (lo hace el trabajador) y reconciliación
# ----------------------------------------------------------------------

def cerrar_entrada(
    raiz: Path,
    secuencia: int,
    trabajador_id: str,
    generacion: int,
    estado_cola: str,
    resultado: dict,
) -> bool:
    """
    El trabajador cierra SU entrada: sólo si sigue despachada a su nombre y
    con su generación. Devuelve False si ya no era suya (la reconcilió la
    recuperación, por ejemplo): el estado de la tarea es la autoridad, y
    eso no se discute desde aquí.
    """
    if estado_cola not in (global_.COLA_TERMINADA, global_.COLA_FALLIDA):
        raise ErrorCola(
            "Una entrada se cierra como terminada o fallida, no como '"
            + str(estado_cola) + "'."
        )

    momento = ahora_utc()

    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            cursor = con.execute(
                "UPDATE cola SET estado_cola = ?, terminado_en = ?, "
                "actualizado_en = ?, resultado = ? "
                "WHERE secuencia = ? AND estado_cola = ? AND trabajador_id = ? "
                "AND generacion = ?",
                (
                    estado_cola,
                    momento,
                    momento,
                    json.dumps(resultado, ensure_ascii=False),
                    int(secuencia),
                    global_.COLA_DESPACHADA,
                    trabajador_id,
                    int(generacion),
                ),
            )

            if cursor.rowcount != 1:
                return False

            entrada = _obtener_entrada(con, secuencia)

            global_.insertar_evento(
                con,
                entrada["tarea_id"],
                {
                    "fecha": momento,
                    "tipo": global_.EVENTO_COLA,
                    "motivo": "Entrada " + str(int(secuencia)) + " cerrada como "
                    + estado_cola + " por '" + trabajador_id + "': "
                    + str(resultado.get("tipo")) + ".",
                    "origen": nucleo.ORIGEN_AUTOMATICO,
                    "datos": {
                        "secuencia": int(secuencia),
                        "generacion": int(generacion),
                        "resultado": resultado,
                    },
                },
            )

    return True


def reconciliar_cola(raiz: Path) -> dict:
    """
    Pone la cola de acuerdo con el estado de las tareas. Idempotente.

    Para cada entrada DESPACHADA se mira la fila de su tarea dentro de la
    misma transacción:

    - sigue EN_EJECUCION con el mismo trabajador y generación: no se toca.
      Incluye el latido vencido con duda: la ejecución existe mientras
      `reanudar` no la libere, y aquí no se decide nada por el tiempo;
    - la tarea volvió a un estado TOMABLE (la recuperación la liberó por
      huérfana, o alguien la devolvió): la entrada vuelve a PENDIENTE con
      su misma secuencia, así que conserva su puesto;
    - la tarea espera a una persona o está cerrada (propuesta, bloqueada,
      aprobada, rechazada): la entrada se cierra como TERMINADA con nota.

    Y una entrada PENDIENTE de una tarea APROBADA se retira: aprobada no
    vuelve a ningún estado tomable, y dejarla ahí sería una entrada que
    nunca saldría.
    """
    raiz = Path(raiz).resolve()
    momento = ahora_utc()

    informe = {
        "fecha": momento,
        "revisadas": 0,
        "reencoladas": [],
        "cerradas": [],
        "retiradas": [],
        "sin_tocar": [],
    }

    tomables = {str(uno) for uno in nucleo.ESTADOS_TOMABLES}

    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            for entrada in _listar_entradas(con, global_.COLA_ESTADOS_VIVOS):
                informe["revisadas"] += 1

                fila = global_.obtener_tarea(con, entrada["tarea_id"])

                if entrada["estado_cola"] == global_.COLA_PENDIENTE:
                    if fila is not None and fila["estado"] == str(Estado.APROBADO):
                        _cambiar_entrada(
                            con, entrada, global_.COLA_RETIRADA, momento,
                            {"tipo": RESULTADO_RECONCILIADA, "motivo":
                             "La tarea está aprobada: no volverá a tomarse."},
                            cerrar=True,
                        )
                        informe["retiradas"].append(_resumen(entrada, fila))
                    continue

                viva = (
                    fila is not None
                    and fila["estado"] == str(Estado.EN_EJECUCION)
                    and fila["trabajador_id"] == entrada["trabajador_id"]
                    and int(fila["generacion"] or 0) == int(entrada["generacion"] or -1)
                )

                if viva:
                    informe["sin_tocar"].append(_resumen(entrada, fila))
                    continue

                if fila is not None and fila["estado"] in tomables:
                    _cambiar_entrada(
                        con, entrada, global_.COLA_PENDIENTE, momento,
                        None, cerrar=False,
                        nota="La ejecución " + str(entrada["generacion"])
                        + " ya no existe y la tarea está en '"
                        + str(fila["estado"]) + "': vuelve a la cola con su "
                        "misma secuencia.",
                    )
                    informe["reencoladas"].append(_resumen(entrada, fila))
                    continue

                _cambiar_entrada(
                    con, entrada, global_.COLA_TERMINADA, momento,
                    {
                        "tipo": RESULTADO_RECONCILIADA,
                        "estado": None if fila is None else fila["estado"],
                        "motivo": (
                            "La tarea ya no existe en el estado global."
                            if fila is None
                            else "La ejecución " + str(entrada["generacion"])
                            + " terminó y la tarea quedó en '"
                            + str(fila["estado"]) + "'."
                        ),
                    },
                    cerrar=True,
                )
                informe["cerradas"].append(_resumen(entrada, fila))

    return informe


def _resumen(entrada: dict, fila: dict | None) -> dict:
    return {
        "secuencia": entrada["secuencia"],
        "tarea": entrada["tarea_id"],
        "estado_cola": entrada["estado_cola"],
        "estado_tarea": None if fila is None else fila["estado"],
        "trabajador_id": entrada["trabajador_id"],
        "generacion": entrada["generacion"],
    }


def _cambiar_entrada(con, entrada, destino, momento, resultado, cerrar, nota=None):
    """UPDATE condicionado al estado que la entrada tenía al leerla."""
    if cerrar:
        cursor = con.execute(
            "UPDATE cola SET estado_cola = ?, terminado_en = ?, "
            "actualizado_en = ?, resultado = ? "
            "WHERE secuencia = ? AND estado_cola = ?",
            (
                destino, momento, momento,
                json.dumps(resultado, ensure_ascii=False),
                entrada["secuencia"], entrada["estado_cola"],
            ),
        )
    else:
        # Vuelve a pendiente: se sueltan los datos de la ejecución que ya
        # no existe, y la nota queda como último rechazo para que `cola`
        # cuente por qué volvió.
        cursor = con.execute(
            "UPDATE cola SET estado_cola = ?, actualizado_en = ?, "
            "trabajador_id = NULL, generacion = NULL, pid = NULL, "
            "worktree = NULL, despachado_en = NULL, ultimo_rechazo = ? "
            "WHERE secuencia = ? AND estado_cola = ?",
            (
                destino, momento,
                json.dumps(
                    {"fecha": momento, "motivo": "reencolada", "detalle": nota},
                    ensure_ascii=False,
                ),
                entrada["secuencia"], entrada["estado_cola"],
            ),
        )

    if cursor.rowcount != 1:
        raise ErrorCola(
            "La entrada " + str(entrada["secuencia"]) + " cambió mientras se "
            "reconciliaba la cola."
        )

    global_.insertar_evento(
        con,
        entrada["tarea_id"],
        {
            "fecha": momento,
            "tipo": global_.EVENTO_COLA,
            "motivo": "Entrada " + str(entrada["secuencia"]) + ": "
            + str(entrada["estado_cola"]) + " -> " + destino
            + " (reconciliación)." + ((" " + nota) if nota else ""),
            "origen": nucleo.ORIGEN_AUTOMATICO,
            "datos": {"secuencia": entrada["secuencia"], "resultado": resultado},
        },
    )


# ----------------------------------------------------------------------
# Limpieza de árboles
# ----------------------------------------------------------------------

def _arboles_registrados_de(raiz: Path, identificador: str) -> list[str]:
    """Rutas que la base tiene registradas para la tarea (fila y entradas vivas)."""
    rutas = []

    with global_.conexion(raiz) as con:
        fila = global_.obtener_tarea(con, identificador)

        if fila is not None and fila.get("worktree"):
            rutas.append(str(fila["worktree"]))

        for entrada in _listar_entradas(con, global_.COLA_ESTADOS_VIVOS):
            if entrada["tarea_id"] == identificador and entrada.get("worktree"):
                rutas.append(str(entrada["worktree"]))

    return rutas


def limpiar_arbol(raiz: Path, identificador: str) -> dict:
    """
    Borra el árbol automático de una tarea, sólo si es SEGURO. Nunca
    `--force`.

    Se rechaza, con `ErrorLimpieza`, cuando:

    - la base registra para esa tarea un árbol FUERA de la zona: aunque
      exista `<zona>/<id>`, tocar algo de esa tarea sería adivinar;
    - Git no reconoce `<zona>/<id>` como worktree de este repositorio
      (`resolver_worktree`): no se borra lo que no se puede verificar;
    - la tarea está EN_EJECUCION o tiene una entrada despachada;
    - el árbol tiene CUALQUIER cosa sin confirmar, versionada o no.

    Devuelve el informe cuando lo borra, o cuando no había nada que borrar.
    """
    raiz = Path(raiz).resolve()
    validar_id(identificador)

    destino = ruta_de_arbol(raiz, identificador)

    for registrada in _arboles_registrados_de(raiz, identificador):
        if not dentro_de_zona(raiz, registrada):
            raise ErrorLimpieza(
                "La tarea '" + identificador + "' tiene registrado el árbol '"
                + registrada + "', que está FUERA de la zona controlada '"
                + str(zona_de_arboles(raiz)) + "'. No se toca un árbol que "
                "el despacho no creó."
            )

    if not destino.exists():
        return {
            "tarea": identificador,
            "arbol": str(destino),
            "limpiado": False,
            "motivo": "No hay ningún árbol automático para esta tarea.",
        }

    arbol = resolver_worktree(raiz, str(destino))

    if not dentro_de_zona(raiz, arbol):
        raise ErrorLimpieza(
            "'" + str(arbol) + "' no está dentro de la zona controlada."
        )

    with global_.conexion(raiz) as con:
        fila = global_.obtener_tarea(con, identificador)
        viva = _entrada_viva_de(con, identificador)

    if fila is not None and fila["estado"] == str(Estado.EN_EJECUCION):
        raise ErrorLimpieza(
            "La tarea '" + identificador + "' está EN EJECUCIÓN (trabajador "
            + str(fila.get("trabajador_id")) + "): su árbol no se borra."
        )

    if viva is not None and viva["estado_cola"] == global_.COLA_DESPACHADA:
        raise ErrorLimpieza(
            "La tarea '" + identificador + "' tiene la entrada "
            + str(viva["secuencia"]) + " despachada: su árbol no se borra "
            "hasta que el trabajador o la recuperación la cierren."
        )

    cambios = Git(arbol).cambios_del_arbol()

    if cambios is None:
        raise ErrorLimpieza(
            "Git no pudo decir si '" + str(arbol) + "' tiene cambios sin "
            "confirmar: no se borra lo que no se puede verificar."
        )

    if cambios:
        raise ErrorLimpieza(
            "El árbol '" + str(arbol) + "' tiene " + str(len(cambios))
            + " cambio(s) sin confirmar (" + ", ".join(cambios[:5])
            + ("..." if len(cambios) > 5 else "") + "). No se borra trabajo "
            "que nadie confirmó; confírmalo o descártalo a mano."
        )

    resultado = _git(raiz, "worktree", "remove", str(arbol))

    if resultado.returncode != 0:
        raise ErrorLimpieza(
            "Git se negó a retirar '" + str(arbol) + "': "
            + (resultado.stderr or resultado.stdout).strip()
        )

    if destino.exists():
        raise ErrorLimpieza(
            "Git dio por retirado '" + str(arbol) + "' pero el directorio "
            "sigue ahí."
        )

    return {
        "tarea": identificador,
        "arbol": str(arbol),
        "limpiado": True,
        "motivo": "Árbol retirado; la rama " + str(Git(raiz).rama_actual())
        + " no se toca y la de la tarea conserva sus commits.",
    }


def limpiar_arboles(raiz: Path) -> dict:
    """Intenta limpiar el árbol de cada tarea que tiene uno en la zona."""
    raiz = Path(raiz).resolve()
    zona = zona_de_arboles(raiz)

    informe = {"limpiados": [], "rechazados": [], "sin_arbol": []}

    if not zona.is_dir():
        return informe

    for candidato in sorted(zona.iterdir()):
        if not candidato.is_dir() or candidato.name == CARPETA_REGISTROS:
            continue

        try:
            validar_id(candidato.name)
        except Exception:
            informe["rechazados"].append(
                {
                    "tarea": candidato.name,
                    "motivo": "No es el árbol de ninguna tarea (nombre "
                    "inesperado dentro de la zona): no se toca.",
                }
            )
            continue

        try:
            resultado = limpiar_arbol(raiz, candidato.name)
        except ErrorWorktree as problema:
            informe["rechazados"].append(
                {"tarea": candidato.name, "motivo": str(problema)}
            )
            continue

        if resultado["limpiado"]:
            informe["limpiados"].append(resultado)
        else:
            informe["sin_arbol"].append(resultado)

    return informe


# ----------------------------------------------------------------------
# El proceso trabajador
# ----------------------------------------------------------------------

def _recortar(texto: str) -> str:
    texto = texto or ""

    if len(texto) <= LIMITE_SALIDA_TRABAJO:
        return texto

    return "[...]" + texto[-LIMITE_SALIDA_TRABAJO:]


def correr_trabajo(argv: list[str], arbol: Path, tiempo_limite_s: int) -> dict:
    """
    Ejecuta el trabajo encolado dentro del árbol, como lista de argumentos.

    `shell=False` explícito: ni `;`, ni `$(...)`, ni `%VAR%`, ni comillas
    significan nada; cada elemento llega al proceso tal cual se encoló.
    Un ejecutable que no existe, o un trabajo que se pasa de tiempo, es un
    trabajo FALLIDO con su motivo, no una avería del trabajador.
    """
    argv = _validar_trabajo(argv)

    entorno = entorno_git_limpio()
    entorno["PYTHONDONTWRITEBYTECODE"] = "1"
    entorno["PYTHONIOENCODING"] = "utf-8"
    entorno["PYTHONUTF8"] = "1"

    inicio = time.monotonic()

    try:
        proceso = subprocess.run(
            argv,
            cwd=str(arbol),
            env=entorno,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(tiempo_limite_s),
        )
    except subprocess.TimeoutExpired as agotado:
        return {
            "codigo": None,
            "agotado": True,
            "duracion_s": round(time.monotonic() - inicio, 3),
            "salida": _recortar(
                corredor._decodificar(agotado.stdout)
                + corredor._decodificar(agotado.stderr)
            ),
            "detalle": "El trabajo superó su tiempo límite de "
            + str(int(tiempo_limite_s)) + " s y fue interrumpido.",
        }
    except OSError as error:
        return {
            "codigo": None,
            "agotado": False,
            "duracion_s": round(time.monotonic() - inicio, 3),
            "salida": "",
            "detalle": "No se pudo lanzar el trabajo " + repr(argv[:1]) + ": "
            + type(error).__name__ + ": " + str(error),
        }

    return {
        "codigo": proceso.returncode,
        "agotado": False,
        "duracion_s": round(time.monotonic() - inicio, 3),
        "salida": _recortar((proceso.stdout or "") + (proceso.stderr or "")),
        "detalle": None if proceso.returncode == 0 else
        "El trabajo terminó con código " + str(proceso.returncode) + ".",
    }


def cambios_de_la_ejecucion(arbol: Path, commit_inicial: str | None) -> list[str]:
    """
    Todo lo que la ejecución tocó en el árbol: lo confirmado desde el
    commit inicial de la tarea Y lo que sigue sin confirmar o sin
    versionar. Es la lista que se compara con el ámbito.

    Mirar sólo lo no confirmado dejaba un hueco: un trabajo que escribe
    fuera de su ámbito y hace `git commit` limpiaba el árbol y pasaba la
    comprobación. Lanza `ErrorWorktree` si Git no puede responder.
    """
    testigo = Git(arbol)

    sin_confirmar = testigo.cambios_del_arbol()

    if sin_confirmar is None:
        raise ErrorWorktree(
            "Git no pudo listar los cambios del árbol '" + str(arbol)
            + "' tras el trabajo."
        )

    confirmados = []

    if commit_inicial:
        confirmados = testigo.rutas_cambiadas_desde(commit_inicial)

        if confirmados is None:
            raise ErrorWorktree(
                "Git no pudo comparar el árbol '" + str(arbol) + "' con el "
                "commit inicial " + str(commit_inicial) + " de la tarea."
            )

    return sorted(set(sin_confirmar) | set(confirmados))


def ejecutar_trabajador(
    raiz: Path,
    identificador: str,
    trabajador_id: str,
    generacion: int,
    secuencia: int,
    worktree: str,
    trabajo: list[str],
    tiempo_limite_s: int = TIEMPO_LIMITE_TRABAJO_S,
    ejecutable: str | None = None,
    intervalo_latido_s: float | None = None,
    tiempo_limite_pruebas_s: int = corredor.TIEMPO_LIMITE_S,
    pid_despacho: int | None = None,
) -> dict:
    """
    El cuerpo del proceso trabajador. Devuelve el informe; el código de
    salida lo decide `trabajador.principal` a partir de él.

        adoptar -> [latido] trabajo -> ámbito -> verificar -> cerrar entrada

    Cada salida deja la tarea en un estado coherente y la entrada cerrada:

    - trabajo en verde y sólo dentro del ámbito: `verificar` decide
      (PROPUESTO, REQUIERE_REVISION o BLOQUEADO) y la entrada queda
      TERMINADA con el veredicto;
    - trabajo fallido (código distinto de 0, no se pudo lanzar, tiempo
      agotado): la tarea se DEVUELVE (REABIERTO) con el motivo y la
      entrada queda FALLIDA. No se verifica: un verde con el trabajo a
      medias sería un verde falso;
    - escritura fuera del ámbito: la tarea se BLOQUEA (decide una persona)
      y la entrada queda FALLIDA con la lista de rutas;
    - la ejecución ya no es de este trabajador al adoptarla: no se toca
      nada y se informa.

    Si el propio trabajador se avería (una excepción que no es de las
    previstas), intenta devolver la tarea y cerrar la entrada como fallida
    antes de salir, para no dejar una ejecución colgada con su PID vivo
    hasta que la recuperación la juzgue.
    """
    raiz = Path(raiz).resolve()

    informe = {
        "tarea": identificador,
        "trabajador_id": trabajador_id,
        "generacion": int(generacion),
        "secuencia": int(secuencia),
        "adoptada": False,
        "trabajo": None,
        "fuera_de_ambito": [],
        "verificacion": None,
        "estado_final": None,
        "resultado": None,
        "entrada_cerrada": None,
        "detalle": None,
    }

    try:
        ficha = nucleo.adoptar(
            raiz, identificador, trabajador_id, int(generacion),
            pid_anterior=pid_despacho,
        )
    except (nucleo.ErrorPropiedad, ErrorSupervisor, global_.ErrorEstadoGlobal) as rechazo:
        # Incluye la tarea que ya no está en ejecución (el primer proceso
        # terminó) y la que ya lleva el PID de otro trabajador (doble
        # lanzamiento): en los dos casos no hay nada que adoptar y no se
        # toca el árbol.
        informe["resultado"] = "no_adoptada"
        informe["detalle"] = (
            "La ejecución ya no es de este trabajador: " + str(rechazo)
        )
        return informe

    informe["adoptada"] = True

    def cerrar(estado_cola, resultado):
        informe["entrada_cerrada"] = cerrar_entrada(
            raiz, secuencia, trabajador_id, int(generacion), estado_cola, resultado
        )

    try:
        arbol = resolver_worktree(raiz, worktree)

        acompanante = nucleo.LatidoAutomatico(
            raiz, identificador, trabajador_id, int(generacion),
            intervalo_s=intervalo_latido_s,
        )

        with acompanante:
            if trabajo:
                informe["trabajo"] = correr_trabajo(trabajo, arbol, tiempo_limite_s)
            else:
                informe["trabajo"] = {
                    "codigo": 0, "agotado": False, "duracion_s": 0.0,
                    "salida": "", "detalle": "Sin trabajo encolado: sólo se verifica.",
                }

        informe["latidos"] = acompanante.emitidos

        if acompanante.propiedad_perdida:
            informe["resultado"] = "propiedad_perdida"
            informe["detalle"] = (
                "La tarea dejó de ser de este trabajador mientras corría el "
                "trabajo: " + str(acompanante.detalle)
            )
            return informe

        if informe["trabajo"]["codigo"] != 0:
            motivo = (
                "El trabajo encolado falló: "
                + str(informe["trabajo"]["detalle"])
            )
            ficha = nucleo.devolver(
                raiz, identificador, motivo,
                trabajador_id=trabajador_id, generacion=int(generacion),
            )
            informe["estado_final"] = str(ficha.estado)
            informe["resultado"] = RESULTADO_TRABAJO_FALLIDO
            informe["detalle"] = motivo
            cerrar(
                global_.COLA_FALLIDA,
                {
                    "tipo": RESULTADO_TRABAJO_FALLIDO,
                    "estado": str(ficha.estado),
                    "trabajo": informe["trabajo"],
                    "motivo": motivo,
                },
            )
            return informe

        cambios = cambios_de_la_ejecucion(arbol, ficha.commit_inicial)

        # Lo que se posee es lo que la base CONCEDIÓ, no lo que la ficha
        # declare hoy.
        ambito = list(ficha.ambito_vigente or ficha.ambito_archivos)
        informe["fuera_de_ambito"] = fuera_de_ambito(cambios, ambito)

        if informe["fuera_de_ambito"]:
            motivo = (
                "El trabajo escribió FUERA del ámbito de la tarea: "
                + ", ".join(informe["fuera_de_ambito"][:10])
                + ("..." if len(informe["fuera_de_ambito"]) > 10 else "")
                + ". Ámbito concedido: " + ", ".join(ambito) + "."
            )
            ficha = nucleo.bloquear(
                raiz, identificador, motivo, origen=nucleo.ORIGEN_AUTOMATICO,
                trabajador_id=trabajador_id, generacion=int(generacion),
            )
            informe["estado_final"] = str(ficha.estado)
            informe["resultado"] = RESULTADO_FUERA_DE_AMBITO
            informe["detalle"] = motivo
            cerrar(
                global_.COLA_FALLIDA,
                {
                    "tipo": RESULTADO_FUERA_DE_AMBITO,
                    "estado": str(ficha.estado),
                    "rutas": informe["fuera_de_ambito"],
                    "trabajo": informe["trabajo"],
                    "motivo": motivo,
                },
            )
            return informe

        verificacion = nucleo.verificar(
            raiz,
            identificador,
            tiempo_limite_s=tiempo_limite_pruebas_s,
            ejecutable=ejecutable,
            git=None,
            trabajador_id=trabajador_id,
            generacion=int(generacion),
            intervalo_latido_s=intervalo_latido_s,
        )

        informe["verificacion"] = {
            "estado": verificacion["estado"],
            "motivo": verificacion["motivo"],
            "raiz": verificacion["raiz"],
            "rama": verificacion["rama"],
            "commit": verificacion["commit"],
            "sin_confirmar": verificacion["sin_confirmar"],
            "total": verificacion["corrida"]["total"],
            "ok": verificacion["corrida"]["ok"],
            "problemas": list(verificacion["problemas"]),
            "latidos": verificacion["latidos"],
        }
        informe["estado_final"] = verificacion["estado"]
        informe["resultado"] = RESULTADO_VERIFICADA
        informe["detalle"] = verificacion["motivo"]

        cerrar(
            global_.COLA_TERMINADA,
            {
                "tipo": RESULTADO_VERIFICADA,
                "estado": verificacion["estado"],
                "motivo": verificacion["motivo"],
                "trabajo": informe["trabajo"],
                "verificacion": informe["verificacion"],
            },
        )

        return informe

    except nucleo.ErrorPropiedad as rechazo:
        # Alguien se quedó con la tarea entre medias (recuperación, orden
        # humana): no es nuestra, no se toca.
        informe["resultado"] = "propiedad_perdida"
        informe["detalle"] = str(rechazo)
        return informe

    except Exception as error:
        motivo = (
            "El trabajador se averió: " + type(error).__name__ + ": " + str(error)
        )
        informe["resultado"] = RESULTADO_TRABAJADOR_AVERIADO
        informe["detalle"] = motivo

        try:
            ficha = nucleo.devolver(
                raiz, identificador, motivo,
                trabajador_id=trabajador_id, generacion=int(generacion),
            )
            informe["estado_final"] = str(ficha.estado)
        except Exception as segundo:
            informe["detalle"] += " Y no se pudo devolver: " + str(segundo)

        try:
            cerrar(
                global_.COLA_FALLIDA,
                {
                    "tipo": RESULTADO_TRABAJADOR_AVERIADO,
                    "estado": informe["estado_final"],
                    "motivo": motivo,
                },
            )
        except Exception as tercero:
            informe["detalle"] += " Y no se pudo cerrar la entrada: " + str(tercero)

        return informe
