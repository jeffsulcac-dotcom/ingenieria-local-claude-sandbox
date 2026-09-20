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
  (anotada en `<común>/info/exclude`, que es local, para que Git de la
  raíz no la vea como archivos sin versionar sin tocar el `.gitignore`
  del proyecto). El despacho sólo crea ahí, y la limpieza sólo borra
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

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
from .tarea import FORMATO_ID, ErrorFicha, ahora_utc, listar_con_errores, validar_id


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
# La ficha de la entrada no se puede cargar o no sirve para tomar (R1).
RECHAZO_FICHA = "ficha_invalida"
# La toma se concedió pero el proceso no se pudo lanzar: se devolvió (R1).
RECHAZO_LANZAMIENTO = "lanzamiento_fallido"
# Una avería que no es de las previstas al considerar la entrada (R1).
RECHAZO_AVERIA = "averia"

# Resultados con que el trabajador cierra su entrada.
RESULTADO_VERIFICADA = "verificada"
RESULTADO_TRABAJO_FALLIDO = "trabajo_fallido"
RESULTADO_FUERA_DE_AMBITO = "fuera_de_ambito"
RESULTADO_TRABAJADOR_AVERIADO = "trabajador_averiado"
RESULTADO_RECONCILIADA = "reconciliada"
RESULTADO_NO_LANZADO = "no_lanzado"
RESULTADO_PROPIEDAD_PERDIDA = "propiedad_perdida"
RESULTADO_SUSTITUIDA = "sustituida"

# Reintentos de la adopción ante un error transitorio de SQLite (R1): un
# `database is locked` no significa que la ejecución no sea nuestra.
INTENTOS_ADOPCION = 5
ESPERA_ADOPCION_S = 1.0

# Lo que se espera a que termine el checkout de un árbol que otro despacho
# está creando en este mismo instante (R1).
ESPERA_CHECKOUT_S = 10.0

# Cota de la prioridad: lo que cabe en un entero de SQLite.
PRIORIDAD_MAXIMA = 2 ** 63 - 1

# Extensiones que en Windows NO se ejecutan directamente sino a través de
# `cmd.exe`, que reinterpreta la línea de órdenes (`%VAR%`, `&`, `|`):
# justo lo que el trabajo promete no hacer nunca. Se rechazan al encolar.
EXTENSIONES_INTERPRETADAS = (".bat", ".cmd")

# Bytes de la cola de la salida del trabajo que se leen del archivo.
COLA_DE_SALIDA_BYTES = 64 * 1024

# El proceso del trabajo en curso en ESTE proceso trabajador, para que la
# señal de interrupción pueda matar su grupo entero.
_TRABAJO_EN_CURSO = None

# Lo que se ignora al comparar la raíz antes y después del trabajo: el
# espejo JSON lo reescribe el propio Supervisor.
RASTRO_DEL_SUPERVISOR = "orquestacion/tareas/"


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


def excluir_zona_de_git(raiz: Path) -> bool:
    """
    Anota `/.arboles/` en `<directorio común>/info/exclude`, si no está.

    Es local y no versionado: la zona deja de aparecer como «sin
    versionar» en `git status` de la raíz sin modificar el `.gitignore`
    del proyecto, que es un archivo fuente. Devuelve si quedó anotada.
    Es comodidad, no seguridad: si no se puede escribir, nada deja de
    funcionar (los archivos sin versionar no cuentan para `verificar`).
    """
    try:
        comun = global_.git_common_dir(raiz)
    except global_.ErrorEstadoGlobal:
        return False

    exclusion = Path(comun) / "info" / "exclude"
    linea = "/" + ZONA_ARBOLES + "/"

    try:
        existente = (
            exclusion.read_text(encoding="utf-8") if exclusion.is_file() else ""
        )

        if linea in existente.splitlines():
            return True

        exclusion.parent.mkdir(parents=True, exist_ok=True)

        with open(exclusion, "a", encoding="utf-8", newline="\n") as manejador:
            if existente and not existente.endswith("\n"):
                manejador.write("\n")

            manejador.write(
                "# Zona de worktrees automáticos del Supervisor (T-0003)\n"
                + linea + "\n"
            )
    except OSError:
        return False

    return True


def ruta_de_arbol(raiz: Path, identificador: str) -> Path:
    """Ruta que el despacho asigna al árbol de una tarea: siempre en la zona."""
    validar_id(identificador)

    return zona_de_arboles(raiz) / identificador


def zona_resuelta(raiz: Path) -> Path:
    """La zona con los enlaces resueltos, que es contra lo que se compara."""
    zona = zona_de_arboles(raiz)

    try:
        return zona.resolve()
    except OSError:
        return zona


def ranura_de(raiz: Path, identificador: str) -> Path:
    """La única ruta resuelta que puede ser el árbol de esa tarea."""
    validar_id(identificador)

    return zona_resuelta(raiz) / identificador


def dentro_de_zona(raiz: Path, ruta: Path) -> bool:
    """
    Si `ruta` es un HIJO DIRECTO de la zona, resueltos los dos.

    Hijo directo y no descendiente: `<zona>/T-0001/pruebas` también estaría
    «dentro», y borrarlo no sería borrar un árbol sino un trozo de uno.
    """
    zona = zona_resuelta(raiz)

    try:
        candidata = Path(ruta).resolve()
    except OSError:
        return False

    return candidata.parent == zona and candidata != zona


def _exigir_zona_sin_enlaces(raiz: Path, identificador: str) -> Path:
    """
    La zona y la ranura de la tarea no pueden ser enlaces simbólicos.

    Con `<zona>/T-0201 -> <zona>/T-0202`, todo lo que se comprobara sobre
    la fila de T-0201 se estaría aplicando al árbol de T-0202 (auditoría
    R1: la limpieza borraba el árbol de la ejecución viva de OTRA tarea).
    Un trabajo de cualquier árbol puede crear ese enlace. Devuelve la
    ranura resuelta.
    """
    zona = zona_de_arboles(raiz)

    if zona.is_symlink():
        raise ErrorWorktree(
            "La zona '" + str(zona) + "' es un enlace simbólico: no se crea "
            "ni se borra nada a través de un enlace."
        )

    ranura = zona / identificador

    if ranura.is_symlink():
        raise ErrorWorktree(
            "'" + str(ranura) + "' es un enlace simbólico y no un árbol: no "
            "se toca. Retíralo a mano."
        )

    return ranura_de(raiz, identificador)


def ruta_de_registro(
    raiz: Path, identificador: str, secuencia: int, generacion: int
) -> Path:
    """Un registro por LANZAMIENTO: la entrada puede volver a la cola y
    despacharse otra vez con otra generación, y cada corrida tiene que
    poder leerse entera y sola."""
    return (
        zona_de_arboles(raiz) / CARPETA_REGISTROS
        / (identificador + "." + str(int(secuencia)) + "."
           + str(int(generacion)) + ".log")
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

        if patron.startswith("**/", indice):
            # Cero o más directorios, como en .gitignore: `a/**/c.py`
            # cubre `a/c.py` y `**/x.py` cubre `x.py` en la raíz.
            partes.append("(?:.*/)?")
            indice += 3
        elif patron.startswith("/**", indice) and indice + 3 == len(patron):
            partes.append("(?:/.*)?")
            indice += 3
        elif patron.startswith("**", indice):
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
    # La RUTA viene de `git status`, siempre con `/`. En POSIX una barra
    # invertida es un carácter legal del nombre y NO se normaliza: si se
    # hiciera, `modulos\\x.py` en la raíz pasaría por `modulos/x.py`.
    limpia = (
        nucleo.normalizar_patron(ruta) if os.name == "nt"
        else nucleo.normalizar_patron(ruta.replace("\\", "\0")).replace("\0", "\\")
    )

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
    datos["ultimo_rechazo"] = global_._de_json(datos.get("ultimo_rechazo"))

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

    # Lo DECLARADO en el árbol también cuenta: `tomar` reclama la unión
    # del ámbito declarado y el retenido, y una ficha ilegible que la
    # base no conoce interrumpe la toma. Sin mirarlo, `cola` decía
    # «despachable» de lo que `tomar` iba a rechazar.
    definiciones, ilegibles = listar_con_errores(raiz)
    declarados = {ficha.id: list(ficha.ambito_archivos) for ficha in definiciones}

    vivas = []
    cerradas = []

    for entrada in entradas:
        fila = tareas.get(entrada["tarea_id"])

        entrada["estado_tarea"] = None if fila is None else fila["estado"]
        entrada["trabajador_tarea"] = None if fila is None else fila["trabajador_id"]
        # Una entrada despachada que ningún proceso trabajador adoptó
        # todavía sigue con el PID del despacho, que muere en el acto: es
        # el dato que una persona necesita para juzgarla.
        entrada["adoptada"] = (
            bool(entrada.get("adoptado_en"))
            if entrada["estado_cola"] == global_.COLA_DESPACHADA else None
        )
        entrada["despachable"] = None
        entrada["por_que_no"] = None

        if entrada["estado_cola"] == global_.COLA_PENDIENTE:
            motivo = _por_que_no_se_despacha(
                fila, tareas, declarados.get(entrada["tarea_id"]), ilegibles,
            )
            entrada["despachable"] = motivo is None
            entrada["por_que_no"] = motivo

        if entrada["estado_cola"] in global_.COLA_ESTADOS_VIVOS:
            vivas.append(entrada)
        else:
            cerradas.append(entrada)

    return vivas + cerradas


def ambito_que_reclamaria(fila: dict, declarado: list[str] | None) -> list[str]:
    """
    El ámbito que `tomar` grabaría para esta fila: lo declarado en el
    árbol (o lo grabado, si no se conoce lo declarado) más lo que la fila
    RETIENE por su estado. Misma regla que la toma, en un solo sitio.
    """
    base = list(declarado if declarado is not None else (fila.get("ambito_archivos") or []))

    if fila.get("estado") in {str(uno) for uno in nucleo.ESTADOS_QUE_RETIENEN_AMBITO}:
        for patron in fila.get("ambito_archivos") or []:
            if patron not in base:
                base.append(patron)

    return base


def _por_que_no_se_despacha(
    fila: dict | None, tareas: dict, declarado=None, ilegibles=None,
) -> str | None:
    """Motivo por el que una entrada pendiente no se despacharía ahora; None si sí."""
    if fila is None:
        return "la tarea no existe en el estado global"

    if fila["estado"] not in {str(uno) for uno in nucleo.ESTADOS_TOMABLES}:
        return "la tarea está en estado '" + str(fila["estado"]) + "'"

    for error in (ilegibles or []):
        if Path(error["archivo"]).stem == fila["id"]:
            return "su ficha JSON no se puede leer (" + str(error["motivo"]) + ")"

    ambito = ambito_que_reclamaria(fila, declarado)

    if not ambito:
        return "la ficha no declara ningún ámbito de archivos"

    invalidos = [p for p in ambito if not nucleo.patron_es_relativo(p)]

    if invalidos:
        return (
            "la ficha declara patrones de ámbito que no son relativos a la "
            "raíz (" + ", ".join(invalidos) + ")"
        )

    desconocidas = [
        error["archivo"] for error in (ilegibles or [])
        if FORMATO_ID.match(Path(error["archivo"]).stem)
        and Path(error["archivo"]).stem not in tareas
    ]

    if desconocidas:
        return (
            "hay fichas ilegibles que el estado global no conoce ("
            + ", ".join(desconocidas) + ") y sin su ámbito la toma no se concede"
        )

    activos = {str(uno) for uno in nucleo.ESTADOS_QUE_RETIENEN_AMBITO}

    for otra in tareas.values():
        if otra["id"] == fila["id"] or otra["estado"] not in activos:
            continue

        pares = nucleo.solapamientos(ambito, otra["ambito_archivos"] or [])

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

    if argumentos and argumentos[0].lower().endswith(EXTENSIONES_INTERPRETADAS):
        raise ErrorCola(
            "El ejecutable del trabajo '" + argumentos[0] + "' es un guion de "
            "cmd.exe, que reinterpreta la línea de órdenes: no se acepta. "
            "Envuélvelo en un guion de Python o de PowerShell (`pwsh -File`)."
        )

    return argumentos


def encolar(
    raiz: Path,
    identificador: str,
    prioridad: int = 0,
    trabajo=None,
    tiempo_limite_s: int = TIEMPO_LIMITE_TRABAJO_S,
    base: str | None = None,
) -> dict:
    """
    Añade una tarea a la cola. Una sola entrada viva por tarea.

    `base` es la referencia de Git desde la que nacerá la rama de la
    tarea si todavía no existe (por omisión, la rama principal si existe
    y, si no, el HEAD de la raíz en el momento del despacho). Se resuelve
    y se anota al despachar.

    La unicidad la impone el índice parcial de la base dentro de la
    transacción: dos `encolar` a la vez de la misma tarea dejan UNA
    entrada. El perdedor recibe `ErrorCola`, no una avería.
    """
    raiz = Path(raiz).resolve()
    validar_id(identificador)

    if (
        isinstance(prioridad, bool)
        or not isinstance(prioridad, int)
        or not -PRIORIDAD_MAXIMA - 1 <= prioridad <= PRIORIDAD_MAXIMA
    ):
        raise ErrorCola(
            "La prioridad debe ser un entero de 64 bits; se recibió: "
            + repr(prioridad) + "."
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

    # Sin ámbito, `tomar` rechazará siempre la entrada y la cabeza de la
    # cola quedaría envenenada: se dice ahora, cuando aún se puede
    # arreglar la ficha.
    if not ficha.ambito_archivos:
        raise ErrorCola(
            "La ficha '" + identificador + "' no declara ningún ámbito de "
            "archivos, y sin ámbito la toma no se concede: no se encola."
        )

    if ficha.estado == Estado.APROBADO:
        raise ErrorCola(
            "La tarea '" + identificador + "' está aprobada y no vuelve a "
            "ningún estado tomable: no se encola."
        )

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
                         tiempo_limite_s, base, encolado_en, actualizado_en)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identificador,
                        int(prioridad),
                        global_.COLA_PENDIENTE,
                        json.dumps(argumentos, ensure_ascii=False),
                        int(tiempo_limite_s),
                        (str(base).strip() or None) if base is not None else None,
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

            fila = global_.obtener_tarea(con, identificador)
            estado_tarea = None if fila is None else str(fila["estado"])

            global_.insertar_evento(
                con,
                identificador,
                {
                    "fecha": momento,
                    "tipo": global_.EVENTO_COLA,
                    "estado_anterior": estado_tarea,
                    "estado_nuevo": estado_tarea,
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


def _esperar_checkout(arbol: Path) -> None:
    """Espera, con tope, a que no haya un `index.lock` en el árbol."""
    respuesta = _git(arbol, "rev-parse", "--git-path", "index.lock")

    if respuesta.returncode != 0:
        return

    candado = Path(respuesta.stdout.strip())

    if not candado.is_absolute():
        candado = Path(arbol) / candado

    limite = time.monotonic() + ESPERA_CHECKOUT_S

    while candado.exists() and time.monotonic() < limite:
        time.sleep(ESPERA_CREAR_ARBOL_S)

    if candado.exists():
        raise ErrorWorktree(
            "El árbol '" + str(arbol) + "' sigue con un checkout en curso ("
            + str(candado) + ") tras " + str(ESPERA_CHECKOUT_S) + " s."
        )


def _reparar_restos(raiz: Path, destino: Path) -> bool:
    """
    Deja utilizable la ruta de un árbol tras un `worktree add`/`remove`
    interrumpido (apagón, Ctrl-C). Sólo dentro de la zona, y sólo lo que
    no contiene trabajo de nadie:

    - un directorio VACÍO que Git no lista (murió nada más crearlo): se
      retira con `rmdir`, que sólo borra si de verdad está vacío;
    - metadatos de un árbol cuyo directorio ya no existe (murió a mitad
      del `remove`, o alguien hizo `rm -rf`): `git worktree prune`, que
      sólo borra metadatos de árboles sin directorio y no toca ramas ni
      archivos.

    Un directorio con contenido pero sin `.git` (prunable por dentro) NO
    se toca: puede tener trabajo de alguien; lo dice `resolver_worktree`.
    Devuelve si retiró algo.
    """
    if not dentro_de_zona(raiz, destino) and destino.parent != zona_de_arboles(raiz):
        return False

    reparado = False

    if destino.is_dir():
        try:
            vacio = not any(destino.iterdir())
        except OSError:
            vacio = False

        if vacio:
            registrados, descartados = nucleo._inventario_de_arboles(raiz)
            resuelto = destino.resolve()

            if resuelto not in registrados and resuelto not in descartados:
                try:
                    destino.rmdir()
                    reparado = True
                except OSError:
                    pass

    if not destino.exists():
        # Metadatos huérfanos de ESTA ruta: `prune` los retira. Si no los
        # hay, `prune` no hace nada.
        _, descartados = nucleo._inventario_de_arboles(raiz)

        for ruta, motivo in descartados.items():
            if ruta == destino.resolve() and "prunable" in motivo:
                if _git(raiz, "worktree", "prune").returncode == 0:
                    reparado = True
                break

    return reparado


def _base_de_la_rama(raiz: Path, base: str | None) -> str:
    """La referencia desde la que nace la rama de una tarea nueva."""
    if base:
        if _git(raiz, "rev-parse", "--verify", "--quiet", base + "^{commit}").returncode != 0:
            raise ErrorWorktree(
                "La base '" + str(base) + "' pedida para la rama no existe en "
                "este repositorio."
            )

        return str(base)

    if _rama_existe(raiz, nucleo.RAMA_PRINCIPAL):
        return nucleo.RAMA_PRINCIPAL

    return "HEAD"


def preparar_arbol(raiz: Path, ficha, base: str | None = None) -> tuple:
    """
    El árbol de la tarea dentro de la zona: `<raíz>/.arboles/<id>`, en la
    rama de la tarea. Lo crea si no existe; si existe, lo VALIDA y lo reúsa.

    Devuelve (ruta resuelta, creado_ahora).

    Si la rama de la tarea no existe todavía se crea desde `base` (la
    referencia que pida la entrada; si no, la rama principal si existe;
    si no, el HEAD de la raíz), y el commit de partida queda en el
    informe y en el evento del despacho: nacer del HEAD que la raíz
    tuviera extraído ese instante no era determinista (auditoría R1). Si
    existe, se le hace checkout. Git rechaza por sí mismo una rama
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
    partida = None

    zona_de_arboles(raiz).mkdir(parents=True, exist_ok=True)
    ranura = _exigir_zona_sin_enlaces(raiz, ficha.id)

    # Dos despachos pueden llegar aquí a la vez para la misma tarea. Git
    # sólo deja ganar a uno (`-b` no puede crear dos veces la rama y la
    # ruta no puede crearse dos veces), y el perdedor ve el fallo ANTES de
    # que el directorio del ganador exista. Se reintenta unas pocas veces
    # con la orden que corresponda al estado que haya en cada momento; en
    # cuanto el directorio aparece, se valida como cualquier árbol
    # existente y es la toma quien decide quién se queda con la tarea.
    _reparar_restos(raiz, destino)

    for intento in range(INTENTOS_CREAR_ARBOL):
        if destino.exists():
            break

        destino.parent.mkdir(parents=True, exist_ok=True)
        excluir_zona_de_git(raiz)

        if _rama_existe(raiz, rama):
            orden = ("worktree", "add", str(destino), rama)
        else:
            partida = _base_de_la_rama(raiz, base)
            orden = ("worktree", "add", "-b", rama, str(destino), partida)

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

    # Otro despacho puede estar creándolo en este instante: Git lo deja
    # `locked initializing` hasta terminar el checkout y el inventario lo
    # descarta mientras tanto. Se espera, con tope, a que deje de estarlo.
    limite = time.monotonic() + ESPERA_CHECKOUT_S

    while True:
        try:
            arbol = resolver_worktree(raiz, str(destino))
            break
        except ErrorWorktree as todavia:
            if "creando todavía" not in str(todavia) or time.monotonic() >= limite:
                raise

            time.sleep(ESPERA_CREAR_ARBOL_S)

    if arbol != ranura:
        raise ErrorWorktree(
            "El árbol resuelto '" + str(arbol) + "' no es la ranura de la "
            "tarea '" + str(ranura) + "': no se ejecuta ahí."
        )

    if not creado:
        # Otro despacho pudo estar creándolo en este mismo instante: Git
        # registra el árbol y escribe HEAD antes de poblar los archivos.
        # Mientras su `index.lock` exista, el checkout no ha terminado y
        # no se lanza nada encima.
        _esperar_checkout(arbol)

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

    return arbol, creado, partida


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
    # Cada opción viaja como `--clave=valor` en UN solo elemento: un valor
    # que empiece por `-` (una identidad de trabajador `-x`) no puede
    # confundirse con otra opción (auditoría R1).
    argumentos = [
        ejecutable or sys.executable,
        "-m",
        "orquestacion.ingenieria_supervisor.trabajador",
        "--raiz=" + str(raiz),
        "--tarea=" + ficha.id,
        "--trabajador=" + str(ficha.trabajador_id),
        "--generacion=" + str(int(ficha.generacion)),
        "--secuencia=" + str(int(entrada["secuencia"])),
        "--worktree=" + str(ficha.worktree),
        "--tiempo-limite=" + str(int(entrada["tiempo_limite_s"])),
        # El PID con el que el despacho reclamó la fila: el trabajador sólo
        # adopta si sigue ahí, así que dos lanzamientos del mismo argv no
        # pueden adoptar los dos.
        "--pid-despacho=" + str(int(ficha.pid)),
    ]

    if intervalo_latido_s is not None:
        argumentos.append("--intervalo-latido=" + str(float(intervalo_latido_s)))

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


# Banderas de Windows. `DETACHED_PROCESS` suelta la consola (cerrar la
# ventana ya no mata al proceso); `CREATE_BREAKAWAY_FROM_JOB` lo saca del
# Job del padre cuando el sistema lo permite (si no, se reintenta sin
# ella). No se ha ejecutado en Windows: lo comprueba el gate.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _lanzar_desligado(argv: list[str], cwd: str, entorno: dict, salida):
    """`Popen` de un proceso en su propia sesión (POSIX) o grupo (Windows),
    con la salida a un archivo. Sin intérprete de órdenes."""
    comunes = dict(
        cwd=cwd, env=entorno, stdin=subprocess.DEVNULL, stdout=salida,
        stderr=subprocess.STDOUT, shell=False,
    )

    if os.name != "nt":
        return subprocess.Popen(list(argv), start_new_session=True, **comunes)

    banderas = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )

    try:
        return subprocess.Popen(
            list(argv), creationflags=banderas | CREATE_BREAKAWAY_FROM_JOB, **comunes,
        )
    except OSError:
        return subprocess.Popen(list(argv), creationflags=banderas, **comunes)


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
    salida = open(registro, "ab")

    try:
        proceso = _lanzar_desligado(
            list(argv), str(RAIZ_SUPERVISOR), entorno_del_trabajador(), salida,
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

    _, ilegibles = listar_con_errores(raiz)

    motivo_previo = _por_que_no_se_despacha(
        tareas.get(identificador), tareas, list(ficha.ambito_archivos), ilegibles,
    )

    if motivo_previo is not None:
        raise ErrorDespacho(
            {
                "tarea": identificador,
                "secuencia": secuencia,
                "motivo": (
                    RECHAZO_AMBITO if "choca" in motivo_previo
                    else RECHAZO_FICHA if "ficha" in motivo_previo
                    else RECHAZO_ESTADO
                ),
                "detalle": "No se despacha '" + identificador + "' (entrada "
                + str(secuencia) + "): " + motivo_previo + ".",
            }
        )

    arbol, creado, partida = preparar_arbol(raiz, ficha, entrada.get("base"))

    def al_conceder(con, informe, momento):
        # El árbol se validó antes de pedir el candado. Si entre medias
        # una limpieza lo retiró, la toma quedaría registrada sobre una
        # ruta vacía: se comprueba (un `stat`, sin Git) con el candado
        # tomado, que es cuando la limpieza ya no puede colarse.
        if not (arbol / ".git").exists():
            raise ErrorDespacho(
                {
                    "tarea": identificador,
                    "secuencia": secuencia,
                    "motivo": RECHAZO_ARBOL,
                    "detalle": "El árbol '" + str(arbol) + "' desapareció "
                    "entre su validación y la toma: no se despacha sobre él.",
                }
            )

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
                str(ruta_de_registro(
                    raiz, identificador, secuencia, int(informe["generacion"])
                )),
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
                    "arbol_creado": creado,
                    "rama_creada_desde": partida,
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
    registro = ruta_de_registro(raiz, identificador, secuencia, int(ficha.generacion))

    # A partir de aquí la toma y la marca ya están confirmadas. Si el
    # proceso no se puede lanzar —intérprete inexistente, carpeta de
    # registros sin permisos, límite de procesos—, no puede quedar una
    # ejecución sin trabajador: se devuelve la tarea con la credencial
    # recién concedida y se cierra la entrada como fallida, en una sola
    # transacción, y el despacho sigue con las demás entradas.
    try:
        argv = argumentos_del_trabajador(
            raiz, ficha, entrada, ejecutable, intervalo_latido_s
        )
        proceso = lanzar_trabajador(argv, raiz, registro) if lanzar else None
    except Exception as fallo:
        motivo = (
            "El proceso trabajador no se pudo lanzar: "
            + type(fallo).__name__ + ": " + str(fallo)
        )
        detalle = motivo

        try:
            nucleo.devolver(
                raiz, identificador, motivo,
                trabajador_id=ficha.trabajador_id,
                generacion=int(ficha.generacion),
                al_confirmar=gancho_de_cierre(
                    secuencia, ficha.trabajador_id, int(ficha.generacion),
                    global_.COLA_FALLIDA,
                    {"tipo": RESULTADO_NO_LANZADO, "motivo": motivo, "argv": None},
                ),
            )
        except Exception as segundo:
            detalle += (
                " Y no se pudo devolver la tarea: " + type(segundo).__name__
                + ": " + str(segundo) + " (queda EN_EJECUCION con el PID del "
                "despacho; la recuperación la juzgará)."
            )

        raise ErrorDespacho(
            {
                "tarea": identificador,
                "secuencia": secuencia,
                "motivo": RECHAZO_LANZAMIENTO,
                "detalle": detalle,
            }
        ) from fallo

    return {
        "despachada": True,
        "tarea": identificador,
        "secuencia": secuencia,
        "trabajador_id": ficha.trabajador_id,
        "generacion": int(ficha.generacion),
        "worktree": str(arbol),
        "arbol_creado": creado,
        "rama_creada_desde": partida,
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

    if trabajador_id is not None:
        texto_id = str(trabajador_id)

        if not texto_id.strip() or texto_id.startswith("-") or any(
            caracter.isspace() for caracter in texto_id
        ):
            raise ErrorCola(
                "La identidad del trabajador no puede estar vacía, empezar por "
                "'-' ni contener espacios: " + repr(trabajador_id) + "."
            )

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
        except (ErrorFicha, ErrorSupervisor, global_.ErrorEstadoGlobal) as problema:
            # Una ficha borrada o corrupta, una toma que no procede por la
            # definición, un error de la base: la entrada se anota y se
            # salta. Antes escapaba de aquí y una cabeza envenenada paraba
            # la cola entera sin dejar rastro en ella.
            motivo = (
                RECHAZO_FICHA if isinstance(problema, (ErrorFicha, ErrorSupervisor))
                else RECHAZO_AVERIA
            )
            rechazos.append(
                {
                    "tarea": entrada["tarea_id"],
                    "secuencia": entrada["secuencia"],
                    "motivo": motivo,
                    "detalle": type(problema).__name__ + ": " + str(problema),
                }
            )
            _anotar_rechazo(
                raiz, entrada["secuencia"], motivo,
                type(problema).__name__ + ": " + str(problema),
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

def _cerrar_en_transaccion(
    con, momento: str, secuencia: int, trabajador_id: str, generacion: int,
    estado_cola: str, resultado: dict,
) -> bool:
    """
    El UPDATE que cierra una entrada, sobre una conexión que YA está en
    transacción: sólo si sigue despachada a ese trabajador y con esa
    generación. Devuelve si casó. No lanza cuando no casa: el estado de
    la tarea es la autoridad y una entrada que ya no era nuestra no
    invalida la transición que la acompaña.
    """
    if estado_cola not in (global_.COLA_TERMINADA, global_.COLA_FALLIDA):
        raise ErrorCola(
            "Una entrada se cierra como terminada o fallida, no como '"
            + str(estado_cola) + "'."
        )

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
    fila = global_.obtener_tarea(con, entrada["tarea_id"])
    estado_tarea = None if fila is None else str(fila["estado"])

    global_.insertar_evento(
        con,
        entrada["tarea_id"],
        {
            "fecha": momento,
            "tipo": global_.EVENTO_COLA,
            "estado_anterior": estado_tarea,
            "estado_nuevo": estado_tarea,
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


def gancho_de_cierre(
    secuencia: int, trabajador_id: str, generacion: int, estado_cola: str,
    resultado: dict, informe: dict | None = None,
):
    """
    Un `al_confirmar` para `devolver`, `bloquear` y `verificar`: cierra la
    entrada DENTRO de la transacción que confirma la transición (R1).

    Con dos transacciones —transición, y luego cierre— la base mostraba
    entre medias «tarea liberada + entrada despachada», que es la firma
    con la que `reconciliar_cola` (que corre en cada `despachar`) devuelve
    una entrada a la cola. Reproducido: un trabajo fallido se relanzaba
    sin tope y su resultado nunca se grababa. Ahora nadie ve ese estado.

    Si se pasa `informe`, deja en `informe["entrada_cerrada"]` si casó.
    """
    def al_confirmar(con, momento):
        casó = _cerrar_en_transaccion(
            con, momento, secuencia, trabajador_id, generacion, estado_cola,
            resultado,
        )

        if informe is not None:
            informe["entrada_cerrada"] = casó

    return al_confirmar


def cerrar_entrada(
    raiz: Path,
    secuencia: int,
    trabajador_id: str,
    generacion: int,
    estado_cola: str,
    resultado: dict,
) -> bool:
    """
    Cierre de una entrada en su propia transacción. Sólo para los caminos
    en que NO hay transición que acompañar (la avería del trabajador
    cuando la tarea ya no está en ejecución). En el camino normal el
    cierre viaja en `gancho_de_cierre`, dentro de la transición.
    """
    momento = ahora_utc()

    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            return _cerrar_en_transaccion(
                con, momento, secuencia, trabajador_id, generacion,
                estado_cola, resultado,
            )


def reconciliar_cola(raiz: Path, comprobar_proceso=nucleo.proceso_vivo) -> dict:
    """
    Pone la cola de acuerdo con el estado de las tareas. Idempotente.

    Para cada entrada DESPACHADA se mira la fila de su tarea dentro de la
    misma transacción:

    - sigue EN_EJECUCION con el mismo trabajador y generación: no se toca.
      Incluye el latido vencido con duda: la ejecución existe mientras
      `reanudar` no la libere, y aquí no se decide nada por el tiempo;
    - la tarea volvió a un estado TOMABLE (la recuperación la liberó por
      huérfana, o alguien la devolvió), o está EN_EJECUCION en manos de
      OTRA ejecución (alguien la tomó a mano entre la recuperación y esta
      pasada): la entrada vuelve a PENDIENTE con su misma secuencia, así
      que conserva su puesto y saldrá cuando la tarea vuelva a ser
      tomable. Cerrarla aquí perdía un trabajo que nadie había hecho.
      SALVO que el proceso trabajador de la entrada (`cola.pid`, el que
      adoptó) siga vivo en esta máquina: entonces no se reencola —un
      segundo lanzamiento escribiría en el mismo árbol a la vez que el
      primero— y se informa como duda (`vivas_sin_tarea`). Una persona
      decide (matar el proceso, o `desencolar`). Es la misma regla que
      la recuperación: ante la duda, no se libera nada;
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
        # Entradas cuya tarea ya no es de su ejecución pero cuyo proceso
        # trabajador sigue vivo aquí: no se reencolan, decide una persona.
        "vivas_sin_tarea": [],
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
                            fila=fila,
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

                if fila is None:
                    # Sólo con las claves foráneas apagadas y una fila
                    # borrada a mano. No hay tarea a la que colgar un
                    # evento: se cierra sin él, y el motivo va en la entrada.
                    _cambiar_entrada(
                        con, entrada, global_.COLA_FALLIDA, momento,
                        {
                            "tipo": RESULTADO_RECONCILIADA,
                            "estado": None,
                            "motivo": "La tarea ya no existe en el estado global.",
                        },
                        cerrar=True, fila=None, con_evento=False,
                    )
                    informe["cerradas"].append(_resumen(entrada, None))
                    continue

                if fila["estado"] in tomables or fila["estado"] == str(
                    Estado.EN_EJECUCION
                ):
                    # ¿Sigue vivo AQUÍ el proceso que adoptó la entrada?
                    # `cola.pid` es el del trabajador desde la adopción (y
                    # el del despacho antes de ella, que muere en el acto).
                    # Si vive, reencolar lanzaría un segundo trabajo sobre
                    # el mismo árbol: se deja despachada y se informa.
                    if entrada.get("pid") and comprobar_proceso(entrada["pid"]) and (
                        nucleo.equipo_de(entrada.get("trabajador_id")) in (
                            None, socket.gethostname()
                        )
                    ):
                        informe["vivas_sin_tarea"].append(_resumen(entrada, fila))
                        continue

                    _cambiar_entrada(
                        con, entrada, global_.COLA_PENDIENTE, momento,
                        None, cerrar=False,
                        nota="La ejecución " + str(entrada["generacion"])
                        + " ya no existe y la tarea está en '"
                        + str(fila["estado"]) + "': vuelve a la cola con su "
                        "misma secuencia.",
                        fila=fila,
                    )
                    informe["reencoladas"].append(_resumen(entrada, fila))
                    continue

                _cambiar_entrada(
                    con, entrada, global_.COLA_TERMINADA, momento,
                    {
                        "tipo": RESULTADO_RECONCILIADA,
                        "estado": fila["estado"],
                        "motivo": "La ejecución " + str(entrada["generacion"])
                        + " terminó y la tarea quedó en '"
                        + str(fila["estado"]) + "'.",
                    },
                    cerrar=True,
                    fila=fila,
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


def _cambiar_entrada(
    con, entrada, destino, momento, resultado, cerrar, nota=None, fila=None,
    con_evento=True,
):
    """UPDATE condicionado al estado que la entrada tenía al leerla."""
    estado_tarea = None if fila is None else str(fila["estado"])

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
            "worktree = NULL, despachado_en = NULL, adoptado_en = NULL, "
            "ultimo_rechazo = ? "
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

    if not con_evento:
        return

    global_.insertar_evento(
        con,
        entrada["tarea_id"],
        {
            "fecha": momento,
            "tipo": global_.EVENTO_COLA,
            "estado_anterior": estado_tarea,
            "estado_nuevo": estado_tarea,
            "motivo": "Entrada " + str(entrada["secuencia"]) + ": "
            + str(entrada["estado_cola"]) + " -> " + destino
            + " (reconciliación)." + ((" " + nota) if nota else ""),
            "origen": nucleo.ORIGEN_AUTOMATICO,
            # Lo que el reencolado anula queda aquí: quién la tenía, con
            # qué generación y PID y en qué árbol. Es lo único que lo
            # conserva.
            "datos": {
                "secuencia": entrada["secuencia"],
                "resultado": resultado,
                "trabajador_id": entrada.get("trabajador_id"),
                "generacion": entrada.get("generacion"),
                "pid": entrada.get("pid"),
                "worktree": entrada.get("worktree"),
            },
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

    try:
        ranura = _exigir_zona_sin_enlaces(raiz, identificador)
    except ErrorWorktree as enlace:
        raise ErrorLimpieza(str(enlace)) from None

    for registrada in _arboles_registrados_de(raiz, identificador):
        if not dentro_de_zona(raiz, registrada):
            raise ErrorLimpieza(
                "La tarea '" + identificador + "' tiene registrado el árbol '"
                + registrada + "', que está FUERA de la zona controlada '"
                + str(zona_de_arboles(raiz)) + "'. No se toca un árbol que "
                "el despacho no creó."
            )

    reparado = _reparar_restos(raiz, destino)

    if not destino.exists():
        return {
            "tarea": identificador,
            "arbol": str(destino),
            "limpiado": reparado,
            "motivo": (
                "Sólo quedaban restos de un árbol interrumpido (carpeta vacía "
                "o metadatos sin directorio); retirados."
                if reparado
                else "No hay ningún árbol automático para esta tarea."
            ),
        }

    arbol = resolver_worktree(raiz, str(destino))

    if arbol != ranura or not dentro_de_zona(raiz, arbol):
        raise ErrorLimpieza(
            "'" + str(arbol) + "' no es la ranura '" + str(ranura) + "' de la "
            "tarea dentro de la zona controlada: no se toca."
        )

    rama = nucleo.PREFIJO_RAMA + identificador
    actual = Git(arbol).rama_actual()

    if actual != rama:
        # HEAD separada o en otra rama: `worktree remove` borraría el
        # reflog del árbol y un commit que sólo viva ahí quedaría suelto.
        raise ErrorLimpieza(
            "El árbol '" + str(arbol) + "' está en '" + str(actual) + "' y no "
            "en la rama de la tarea (" + rama + "): un commit que sólo viva "
            "ahí se perdería. Vuelve a la rama o confírmalo a mano."
        )

    # Decidir y borrar CON EL CANDADO DE ESCRITURA TOMADO. Es la única
    # operación del paquete que hace Git dentro de una transacción, y es
    # a propósito (R1): con las lecturas en autocommit, un `despachar`
    # simultáneo reutilizaba el árbol, confirmaba la toma y lanzaba el
    # trabajador mientras la limpieza, que ya había decidido, ejecutaba
    # el `remove`: ejecución viva con el árbol borrado. Con el candado,
    # la toma espera a que la limpieza confirme y entonces `al_conceder`
    # ve que el árbol ya no está. Es una orden manual, rara y local, y
    # retirar un árbol limpio tarda milisegundos.
    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            fila = global_.obtener_tarea(con, identificador)
            viva = _entrada_viva_de(con, identificador)

            if fila is not None and fila["estado"] == str(Estado.EN_EJECUCION):
                raise ErrorLimpieza(
                    "La tarea '" + identificador + "' está EN EJECUCIÓN "
                    "(trabajador " + str(fila.get("trabajador_id"))
                    + "): su árbol no se borra."
                )

            if viva is not None and viva["estado_cola"] == global_.COLA_DESPACHADA:
                raise ErrorLimpieza(
                    "La tarea '" + identificador + "' tiene la entrada "
                    + str(viva["secuencia"]) + " despachada: su árbol no se "
                    "borra hasta que el trabajador o la recuperación la cierren."
                )

            cambios = Git(arbol).cambios_del_arbol()

            if cambios is None:
                raise ErrorLimpieza(
                    "Git no pudo decir si '" + str(arbol) + "' tiene cambios "
                    "sin confirmar: no se borra lo que no se puede verificar."
                )

            if cambios:
                raise ErrorLimpieza(
                    "El árbol '" + str(arbol) + "' tiene " + str(len(cambios))
                    + " cambio(s) sin confirmar (" + ", ".join(cambios[:5])
                    + ("..." if len(cambios) > 5 else "") + "). No se borra "
                    "trabajo que nadie confirmó; confírmalo o descártalo a mano."
                )

            # Lo IGNORADO por Git también es de alguien: salidas, modelos,
            # un `.env`. `worktree remove` sin `--force` lo borraría sin
            # decir nada (auditoría R1). El bytecode de Python se
            # exceptúa: se regenera solo y bloquearía toda limpieza.
            ignorados = _ignorados_del_arbol(arbol)

            if ignorados is None:
                raise ErrorLimpieza(
                    "Git no pudo listar los archivos ignorados de '" + str(arbol)
                    + "': no se borra lo que no se puede verificar."
                )

            if ignorados:
                raise ErrorLimpieza(
                    "El árbol '" + str(arbol) + "' tiene " + str(len(ignorados))
                    + " archivo(s) ignorados por Git (" + ", ".join(ignorados[:5])
                    + ("..." if len(ignorados) > 5 else "") + "). No se borran "
                    "sin que alguien los mire; retíralos a mano si sobran."
                )

            resultado = _git(raiz, "worktree", "remove", str(arbol))

            if resultado.returncode != 0:
                raise ErrorLimpieza(
                    "Git se negó a retirar '" + str(arbol) + "': "
                    + (resultado.stderr or resultado.stdout).strip()
                )

            global_.insertar_evento(
                con,
                identificador,
                {
                    "fecha": ahora_utc(),
                    "tipo": global_.EVENTO_COLA,
                    "estado_anterior": None if fila is None else str(fila["estado"]),
                    "estado_nuevo": None if fila is None else str(fila["estado"]),
                    "motivo": "Árbol automático retirado: " + str(arbol) + ".",
                    "origen": nucleo.ORIGEN_HUMANO,
                    "datos": {"worktree": str(arbol)},
                },
            ) if fila is not None else None

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


def _ignorados_del_arbol(arbol: Path) -> list[str] | None:
    """Rutas ignoradas por Git presentes en el árbol, sin el bytecode de
    Python. None si Git no puede responder."""
    resultado = _git(
        arbol, "status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all",
    )

    if resultado.returncode != 0:
        return None

    rutas = []

    for entrada in resultado.stdout.split("\0"):
        if not entrada.startswith("!! "):
            continue

        ruta = entrada[3:]
        partes = ruta.rstrip("/").split("/")

        if "__pycache__" in partes or ruta.endswith(".pyc"):
            continue

        rutas.append(ruta)

    return sorted(rutas)


def limpiar_arboles(raiz: Path) -> dict:
    """Intenta limpiar el árbol de cada tarea que tiene uno en la zona."""
    raiz = Path(raiz).resolve()
    zona = zona_de_arboles(raiz)

    informe = {"limpiados": [], "rechazados": [], "sin_arbol": []}

    if not zona.is_dir():
        return informe

    for candidato in sorted(zona.iterdir()):
        if candidato.name == CARPETA_REGISTROS:
            continue

        if candidato.is_symlink():
            informe["rechazados"].append(
                {
                    "tarea": candidato.name,
                    "motivo": "Es un enlace simbólico dentro de la zona: no se "
                    "sigue ni se toca.",
                }
            )
            continue

        if not candidato.is_dir():
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


def matar_grupo(proceso) -> None:
    """
    Mata el trabajo Y todo lo que haya lanzado: en POSIX el trabajo es
    líder de su propia sesión y `killpg` alcanza a los nietos; en Windows
    sólo se mata al hijo directo (un Job Object queda para V2, y el gate
    de Windows lo anota).
    """
    if proceso is None or proceso.poll() is not None:
        return

    try:
        if os.name != "nt":
            os.killpg(proceso.pid, signal.SIGKILL)
        else:
            proceso.kill()
    except OSError:
        try:
            proceso.kill()
        except OSError:
            pass

    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _cola_del_archivo(ruta: Path) -> str:
    """Los últimos bytes de un archivo, decodificados; vacío si no está."""
    try:
        with open(ruta, "rb") as manejador:
            manejador.seek(0, os.SEEK_END)
            tamano = manejador.tell()
            manejador.seek(max(0, tamano - COLA_DE_SALIDA_BYTES))
            datos = manejador.read()
    except OSError:
        return ""

    return _recortar(datos.decode("utf-8", errors="replace"))


def _resolver_ejecutable(argv: list[str]) -> str | None:
    """
    `argv[0]` sin separador de rutas se resuelve por PATH, aquí, y se
    entrega ya resuelto. En Windows `CreateProcess` buscaría antes en el
    directorio del ejecutable padre y en el cwd del padre, que es la
    raíz del Supervisor: un `git.exe` dejado ahí ganaría al del PATH.
    """
    ejecutable = argv[0]

    if "/" in ejecutable or os.sep in ejecutable or (
        os.altsep and os.altsep in ejecutable
    ):
        return ejecutable

    return shutil.which(ejecutable)


def correr_trabajo(
    argv: list[str], arbol: Path, tiempo_limite_s: int, salida: Path | None = None,
) -> dict:
    """
    Ejecuta el trabajo encolado dentro del árbol, como lista de argumentos.

    `shell=False` explícito: ni `;`, ni `$(...)`, ni `%VAR%`, ni comillas
    significan nada; cada elemento llega al proceso tal cual se encoló.
    Un ejecutable que no existe, o un trabajo que se pasa de tiempo, es un
    trabajo FALLIDO con su motivo, no una avería del trabajador.

    El trabajo corre en su PROPIO grupo de procesos y su salida va a un
    ARCHIVO (`salida`, o uno temporal), no a tuberías (auditoría R1):

    - al agotar el tiempo, o si este proceso recibe una señal, se mata el
      grupo entero; con `subprocess.run` sólo moría el hijo directo y un
      nieto seguía escribiendo en el árbol después de devolver la tarea;
    - un nieto que heredara las tuberías bloqueaba la recogida de la
      salida (en Windows, para siempre) y una salida de cientos de MB
      pasaba entera por la memoria: del archivo sólo se lee la cola.

    El entorno es el del ÁRBOL (`entorno_controlado(arbol)`), el mismo que
    usará `verificar`: `PYTHONPATH` apunta a las raíces de paquetes del
    árbol, no a las del Supervisor, para que el trabajo y las pruebas
    vean el mismo código. Lo demás del entorno se hereda: el trabajo
    corre como el usuario, sin aislamiento (documentado).
    """
    global _TRABAJO_EN_CURSO

    argv = _validar_trabajo(argv)

    entorno = corredor.entorno_controlado(Path(arbol))

    inicio = time.monotonic()

    ejecutable = _resolver_ejecutable(argv)

    if ejecutable is None:
        return {
            "codigo": None,
            "agotado": False,
            "duracion_s": 0.0,
            "salida": "",
            "detalle": "No se pudo lanzar el trabajo " + repr(argv[:1])
            + ": no se encontró el ejecutable en PATH.",
        }

    temporal = salida is None

    if temporal:
        descriptor, nombre = tempfile.mkstemp(prefix="trabajo_", suffix=".log")
        os.close(descriptor)
        salida = Path(nombre)

    salida = Path(salida)
    salida.parent.mkdir(parents=True, exist_ok=True)

    def informe(codigo, agotado, detalle):
        texto = _cola_del_archivo(salida)

        if temporal:
            try:
                salida.unlink()
            except OSError:
                pass

        return {
            "codigo": codigo,
            "agotado": agotado,
            "duracion_s": round(time.monotonic() - inicio, 3),
            "salida": texto,
            "detalle": detalle,
        }

    try:
        with open(salida, "ab") as archivo:
            proceso = _lanzar_desligado(
                [ejecutable] + argv[1:], str(arbol), entorno, archivo,
            )
    except OSError as error:
        return informe(
            None, False,
            "No se pudo lanzar el trabajo " + repr(argv[:1]) + ": "
            + type(error).__name__ + ": " + str(error),
        )

    _TRABAJO_EN_CURSO = proceso

    try:
        try:
            codigo = proceso.wait(timeout=int(tiempo_limite_s))
        except subprocess.TimeoutExpired:
            matar_grupo(proceso)

            return informe(
                None, True,
                "El trabajo superó su tiempo límite de " + str(int(tiempo_limite_s))
                + " s y fue interrumpido, con todo lo que hubiera lanzado.",
            )
        except BaseException:
            # Una señal (SIGTERM, Ctrl-C) o cualquier otra interrupción de
            # este proceso: el trabajo no sobrevive a su trabajador.
            matar_grupo(proceso)
            raise
    finally:
        _TRABAJO_EN_CURSO = None

    return informe(
        codigo, False,
        None if codigo == 0 else "El trabajo terminó con código " + str(codigo) + ".",
    )


def interrumpir_trabajo_en_curso() -> None:
    """Para el manejador de señales del trabajador: mata el grupo del
    trabajo si lo hay."""
    matar_grupo(_TRABAJO_EN_CURSO)


def cambios_de_la_ejecucion(
    arbol: Path, commit_inicial: str | None, rama: str | None = None,
    identificador: str | None = None,
) -> list[str]:
    """
    Todo lo que la ejecución tocó en el árbol: lo confirmado desde el
    commit inicial de la tarea —en HEAD y en la PUNTA DE LA RAMA de la
    tarea, que no tienen por qué coincidir— y lo que sigue sin confirmar
    o sin versionar. Es la lista que se compara con el ámbito.

    Mirar sólo lo no confirmado dejaba un hueco: un trabajo que escribe
    fuera de su ámbito y hace `git commit` limpiaba el árbol y pasaba la
    comprobación. Y mirar sólo HEAD dejaba otro: confirmar fuera del
    ámbito y volver con `checkout --detach` al commit inicial dejaba la
    rama contaminada y el árbol «limpio» (auditoría R1). El espejo JSON
    de la propia tarea se excluye: lo escribe el Supervisor, no el
    trabajo. Lanza `ErrorWorktree` si Git no puede responder.
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

        if rama:
            en_rama = testigo.rutas_cambiadas_desde(
                commit_inicial, "refs/heads/" + str(rama)
            )

            if en_rama is None:
                raise ErrorWorktree(
                    "Git no pudo comparar la rama '" + str(rama) + "' con el "
                    "commit inicial " + str(commit_inicial) + " de la tarea."
                )

            confirmados = list(confirmados) + list(en_rama)

    propio = (
        RASTRO_DEL_SUPERVISOR + str(identificador) + ".json" if identificador else None
    )

    return sorted(
        ruta for ruta in set(sin_confirmar) | set(confirmados) if ruta != propio
    )


def huella_de_la_raiz(raiz: Path) -> dict | None:
    """
    Lo que un trabajo NO debe cambiar y no está en su árbol: el checkout
    de la raíz (salvo el espejo JSON del Supervisor), la configuración
    del repositorio común y sus hooks. Se toma antes y después del
    trabajo; si difiere, el trabajo escribió fuera del árbol (un `../..`
    mal calculado, un `git config`), y se bloquea con el motivo. Es una
    red contra el descuido, no contra un trabajo malicioso: el trabajo
    corre como el usuario. None si no se puede calcular.
    """
    cambios = Git(raiz).cambios_del_arbol()

    if cambios is None:
        return None

    try:
        comun = Path(global_.git_common_dir(raiz))
    except global_.ErrorEstadoGlobal:
        return None

    def huella(ruta: Path) -> str:
        try:
            return hashlib.sha1(ruta.read_bytes()).hexdigest()
        except OSError:
            return "ausente"

    hooks = comun / "hooks"
    listado = []

    if hooks.is_dir():
        for archivo in sorted(hooks.iterdir()):
            if archivo.is_file() and not archivo.name.endswith(".sample"):
                listado.append(archivo.name + ":" + huella(archivo))

    return {
        "raiz": sorted(
            ruta for ruta in cambios if not ruta.startswith(RASTRO_DEL_SUPERVISOR)
        ),
        "config": huella(comun / "config"),
        "hooks": listado,
    }


def fuera_del_arbol(antes: dict | None, despues: dict | None) -> list[str]:
    """Qué cambió en la raíz o en el repositorio común entre dos huellas."""
    if antes is None or despues is None:
        return []

    fuera = [
        "raíz: " + ruta for ruta in despues["raiz"] if ruta not in antes["raiz"]
    ]

    if antes["config"] != despues["config"]:
        fuera.append("configuración del repositorio (.git/config)")

    if antes["hooks"] != despues["hooks"]:
        fuera.append("hooks del repositorio (.git/hooks)")

    return fuera


def _cerrar_si_sigue_siendo_mia(
    raiz, secuencia, trabajador_id, generacion, informe, resultado,
) -> None:
    """Cierra la entrada como FALLIDA si aún está despachada a este
    trabajador; si no, no toca nada. Nunca lanza: se anota en el informe."""
    try:
        informe["entrada_cerrada"] = cerrar_entrada(
            raiz, secuencia, trabajador_id, generacion,
            global_.COLA_FALLIDA, resultado,
        )
    except Exception as error:
        informe["detalle"] += " Y no se pudo cerrar la entrada: " + str(error)


def _cierre_de_verificacion(
    raiz, identificador, secuencia, trabajador_id, generacion, resumen, informe,
):
    """
    `al_confirmar` de `verificar`: cierra la entrada como TERMINADA con el
    veredicto que la fila lleva en esa misma transacción (estado destino,
    motivo de la última corrida), sin esperar a que `verificar` devuelva.
    """
    def al_confirmar(con, momento):
        fila = global_.obtener_tarea(con, identificador)
        corrida = (fila or {}).get("ultima_verificacion") or {}
        ejecuciones = (fila or {}).get("ejecuciones") or []
        ultima = ejecuciones[-1] if ejecuciones else {}

        resumen.update(
            {
                "estado": None if fila is None else str(fila["estado"]),
                "motivo": None,
                "verificacion": {
                    "resultado": corrida.get("resultado") or ultima.get("resultado"),
                    "raiz": ultima.get("raiz"),
                    "rama": ultima.get("rama"),
                    "commit": ultima.get("commit"),
                    "sin_confirmar": ultima.get("sin_confirmar"),
                    "total": ultima.get("total"),
                    "ok": ultima.get("ok"),
                    "problemas": list(ultima.get("problemas") or []),
                },
                "ultima_falla": (fila or {}).get("ultima_falla"),
            }
        )

        informe["entrada_cerrada"] = _cerrar_en_transaccion(
            con, momento, secuencia, trabajador_id, generacion,
            global_.COLA_TERMINADA, resumen,
        )

    return al_confirmar


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

    if pid_despacho is None:
        informe["resultado"] = RESULTADO_TRABAJADOR_AVERIADO
        informe["detalle"] = (
            "Falta el PID del despacho (--pid-despacho): sin él la adopción "
            "no sería exclusiva y no se intenta. No se tocó nada."
        )
        return informe

    def anotar_pid_en_cola(con, momento):
        # El PID del proceso que trabaja de verdad, en la entrada, en la
        # misma transacción que lo adopta en la fila. Antes la cola
        # conservaba el del despacho, ya muerto.
        con.execute(
            "UPDATE cola SET pid = ?, adoptado_en = ?, actualizado_en = ? "
            "WHERE secuencia = ? AND estado_cola = ? AND trabajador_id = ? "
            "AND generacion = ?",
            (
                os.getpid(), momento, momento, int(secuencia),
                global_.COLA_DESPACHADA, trabajador_id, int(generacion),
            ),
        )

    ficha = None
    ultimo_transitorio = None

    for intento in range(INTENTOS_ADOPCION):
        try:
            ficha = nucleo.adoptar(
                raiz, identificador, trabajador_id, int(generacion),
                pid_anterior=pid_despacho, al_confirmar=anotar_pid_en_cola,
            )
            break
        except (nucleo.ErrorPropiedad, ErrorSupervisor) as rechazo:
            # La tarea ya no está en ejecución (el primer proceso terminó),
            # ya lleva el PID de otro trabajador (doble lanzamiento) o
            # cambió de manos: no hay nada que adoptar y no se toca el
            # árbol.
            informe["resultado"] = "no_adoptada"
            informe["detalle"] = (
                "La ejecución ya no es de este trabajador: " + str(rechazo)
            )
            return informe
        except global_.ErrorEstadoGlobal as transitorio:
            # `database is locked` bajo contención no dice nada sobre la
            # propiedad. Tratarlo como «no es mía» dejaba la tarea colgada
            # con el PID del despacho hasta la recuperación (R1).
            ultimo_transitorio = transitorio
            time.sleep(ESPERA_ADOPCION_S * (intento + 1))

    if ficha is None:
        informe["resultado"] = RESULTADO_TRABAJADOR_AVERIADO
        informe["detalle"] = (
            "No se pudo adoptar la ejecución tras " + str(INTENTOS_ADOPCION)
            + " intentos por un error de la base (no por propiedad): "
            + str(ultimo_transitorio) + ". No se tocó nada; la fila sigue "
            "con el PID del despacho y la recuperación la juzgará."
        )
        return informe

    informe["adoptada"] = True

    def cierre(estado_cola, resultado):
        # Gancho para la transición: cierra la entrada en la MISMA
        # transacción que la orden que lo recibe.
        return gancho_de_cierre(
            secuencia, trabajador_id, int(generacion), estado_cola, resultado,
            informe,
        )

    try:
        arbol = resolver_worktree(raiz, worktree)

        acompanante = nucleo.LatidoAutomatico(
            raiz, identificador, trabajador_id, int(generacion),
            intervalo_s=intervalo_latido_s,
        )

        huella_antes = huella_de_la_raiz(raiz)

        with acompanante:
            if trabajo:
                informe["trabajo"] = correr_trabajo(
                    trabajo, arbol, tiempo_limite_s,
                    salida=ruta_de_registro(
                        raiz, identificador, secuencia, int(generacion)
                    ).with_suffix(".trabajo.log"),
                )
            else:
                informe["trabajo"] = {
                    "codigo": 0, "agotado": False, "duracion_s": 0.0,
                    "salida": "", "detalle": "Sin trabajo encolado: sólo se verifica.",
                }

        informe["latidos"] = acompanante.emitidos

        if acompanante.propiedad_perdida:
            informe["resultado"] = RESULTADO_PROPIEDAD_PERDIDA
            informe["detalle"] = (
                "La tarea dejó de ser de este trabajador mientras corría el "
                "trabajo: " + str(acompanante.detalle)
            )
            # La entrada se cierra como FALLIDA si sigue siendo nuestra:
            # dejarla despachada obligaba a la reconciliación a adivinar,
            # y adivinaba «reencolar», es decir, un segundo trabajo sobre
            # el mismo árbol. Quien nos quitó la tarea decide si la vuelve
            # a encolar.
            _cerrar_si_sigue_siendo_mia(
                raiz, secuencia, trabajador_id, int(generacion), informe,
                {
                    "tipo": RESULTADO_PROPIEDAD_PERDIDA,
                    "estado": None,
                    "motivo": informe["detalle"],
                    "trabajo": informe["trabajo"],
                },
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
                al_confirmar=cierre(
                    global_.COLA_FALLIDA,
                    {
                        "tipo": RESULTADO_TRABAJO_FALLIDO,
                        "estado": str(Estado.REABIERTO),
                        "trabajo": informe["trabajo"],
                        "motivo": motivo,
                    },
                ),
            )
            informe["estado_final"] = str(ficha.estado)
            informe["resultado"] = RESULTADO_TRABAJO_FALLIDO
            informe["detalle"] = motivo
            return informe

        # El árbol tiene que seguir siendo el que era: un trabajo que
        # borrase su `.git` haría que Git respondiera por el repositorio
        # padre y el ámbito se juzgara contra la raíz (auditoría R1).
        arbol = resolver_worktree(raiz, worktree)

        rama = ficha.rama or nucleo.PREFIJO_RAMA + identificador
        actual = Git(arbol).rama_actual()

        cambios = cambios_de_la_ejecucion(
            arbol, ficha.commit_inicial, rama, identificador
        )

        # Lo que se posee es lo que la base CONCEDIÓ, no lo que la ficha
        # declare hoy.
        ambito = list(ficha.ambito_vigente or ficha.ambito_archivos)
        informe["fuera_de_ambito"] = fuera_de_ambito(cambios, ambito)
        informe["fuera_de_ambito"] += fuera_del_arbol(
            huella_antes, huella_de_la_raiz(raiz)
        )

        if actual != rama:
            # Un trabajo que mueve HEAD fuera de la rama de la tarea (un
            # `checkout --detach` para esconder un commit, por ejemplo) no
            # se verifica: se bloquea con lo que la rama contiene, y decide
            # una persona. Devolverla haría que el siguiente despacho
            # rechazara el árbol para siempre.
            informe["fuera_de_ambito"].append(
                "(el árbol quedó en '" + str(actual) + "' y no en la rama de "
                "la tarea " + rama + ")"
            )

        if informe["fuera_de_ambito"]:
            motivo = (
                "El trabajo escribió FUERA del ámbito de la tarea (o fuera de "
                "su árbol): "
                + ", ".join(informe["fuera_de_ambito"][:10])
                + ("..." if len(informe["fuera_de_ambito"]) > 10 else "")
                + ". Ámbito concedido: " + ", ".join(ambito) + "."
            )
            ficha = nucleo.bloquear(
                raiz, identificador, motivo, origen=nucleo.ORIGEN_AUTOMATICO,
                trabajador_id=trabajador_id, generacion=int(generacion),
                al_confirmar=cierre(
                    global_.COLA_FALLIDA,
                    {
                        "tipo": RESULTADO_FUERA_DE_AMBITO,
                        "estado": str(Estado.BLOQUEADO),
                        "rutas": informe["fuera_de_ambito"],
                        "trabajo": informe["trabajo"],
                        "motivo": motivo,
                    },
                ),
            )
            informe["estado_final"] = str(ficha.estado)
            informe["resultado"] = RESULTADO_FUERA_DE_AMBITO
            informe["detalle"] = motivo
            return informe

        # El resumen de la verificación se rellena DENTRO del gancho, con
        # lo que `verificar` ya decidió pero antes de que confirme: así la
        # entrada se cierra con el veredicto en la misma transacción.
        resumen_verificacion = {"tipo": RESULTADO_VERIFICADA}

        verificacion = nucleo.verificar(
            raiz,
            identificador,
            tiempo_limite_s=tiempo_limite_pruebas_s,
            ejecutable=ejecutable,
            git=None,
            trabajador_id=trabajador_id,
            generacion=int(generacion),
            intervalo_latido_s=intervalo_latido_s,
            al_confirmar=_cierre_de_verificacion(
                raiz, identificador, secuencia, trabajador_id, int(generacion),
                resumen_verificacion, informe,
            ),
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

        return informe

    except nucleo.ErrorPropiedad as rechazo:
        # Alguien se quedó con la tarea entre medias (recuperación, orden
        # humana): no es nuestra, no se toca; la entrada, si aún es
        # nuestra, se cierra para que nadie la reencole a ciegas.
        informe["resultado"] = RESULTADO_PROPIEDAD_PERDIDA
        informe["detalle"] = str(rechazo)
        _cerrar_si_sigue_siendo_mia(
            raiz, secuencia, trabajador_id, int(generacion), informe,
            {
                "tipo": RESULTADO_PROPIEDAD_PERDIDA,
                "estado": None,
                "motivo": str(rechazo),
                "trabajo": informe["trabajo"],
            },
        )
        return informe

    except ErrorWorktree as problema:
        # El árbol registrado no sirve (desapareció, otro repositorio):
        # se devuelve la tarea con el motivo y el proceso sale con el
        # código propio del árbol (6), que la ayuda ya prometía.
        motivo = "El árbol de la tarea no sirve: " + str(problema)
        informe["resultado"] = "arbol_no_valido"
        informe["detalle"] = motivo

        try:
            ficha = nucleo.devolver(
                raiz, identificador, motivo,
                trabajador_id=trabajador_id, generacion=int(generacion),
                al_confirmar=cierre(
                    global_.COLA_FALLIDA,
                    {"tipo": "arbol_no_valido", "estado": str(Estado.REABIERTO),
                     "motivo": motivo},
                ),
            )
            informe["estado_final"] = str(ficha.estado)
        except Exception as segundo:
            informe["detalle"] += " Y no se pudo devolver: " + str(segundo)

        return informe

    except Exception as error:
        motivo = (
            "El trabajador se averió: " + type(error).__name__ + ": " + str(error)
        )
        informe["resultado"] = RESULTADO_TRABAJADOR_AVERIADO
        informe["detalle"] = motivo

        resultado = {
            "tipo": RESULTADO_TRABAJADOR_AVERIADO,
            "estado": None,
            "motivo": motivo,
        }

        if informe["estado_final"] is not None:
            # La transición ya se confirmó (con su cierre dentro): lo que
            # falló vino después. No se devuelve una tarea que ya no está
            # en ejecución ni se pisa un cierre que ya se hizo.
            if informe["entrada_cerrada"] is None:
                try:
                    resultado["estado"] = informe["estado_final"]
                    informe["entrada_cerrada"] = cerrar_entrada(
                        raiz, secuencia, trabajador_id, int(generacion),
                        global_.COLA_FALLIDA, resultado,
                    )
                except Exception as tercero:
                    informe["detalle"] += (
                        " Y no se pudo cerrar la entrada: " + str(tercero)
                    )

            return informe

        try:
            resultado["estado"] = str(Estado.REABIERTO)
            ficha = nucleo.devolver(
                raiz, identificador, motivo,
                trabajador_id=trabajador_id, generacion=int(generacion),
                al_confirmar=cierre(global_.COLA_FALLIDA, resultado),
            )
            informe["estado_final"] = str(ficha.estado)
        except Exception as segundo:
            informe["detalle"] += " Y no se pudo devolver: " + str(segundo)

            try:
                resultado["estado"] = None
                informe["entrada_cerrada"] = cerrar_entrada(
                    raiz, secuencia, trabajador_id, int(generacion),
                    global_.COLA_FALLIDA, resultado,
                )
            except Exception as tercero:
                informe["detalle"] += " Y no se pudo cerrar la entrada: " + str(tercero)

        return informe
