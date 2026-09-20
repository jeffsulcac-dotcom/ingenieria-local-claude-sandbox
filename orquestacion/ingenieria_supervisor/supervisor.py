"""
Máquina de estados y operaciones del Supervisor de Desarrollo V1.

Reglas duras que este módulo hace cumplir:

1. El Supervisor puede llegar automáticamente, como máximo, a
   PROPUESTO, REQUIERE_REVISION o BLOQUEADO.
   APROBADO y RECHAZADO exigen siempre una acción humana explícita.

2. Una tarea con decisiones humanas pendientes nunca llega a PROPUESTO.

3. Dos tareas activas no pueden declarar ámbitos de archivos que se solapen:
   un solo escritor por archivo en conflicto.

4. Los commits automáticos se limitan a la ficha de la propia tarea,
   dentro de la rama de la tarea, y jamás en la rama principal.

5. Desde A2, el estado operativo se lee y se escribe en la base SQLite
   global (`estado_global.py`). Toda operación que cambia estado persiste
   primero en SQLite y sólo después regenera la ficha JSON como espejo.

6. Desde A3.1, `tomar` es ATÓMICA: comprobación de estado, comprobación
   de ámbitos y toma ocurren en una sola transacción BEGIN IMMEDIATE, y la
   concede un UPDATE condicional resuelto por rowcount. Compitan los trabajadores que
   compitan por la misma tarea, la gana exactamente uno.

7. Desde A3.2 esa garantía alcanza al CICLO ENTERO. `persistir` escribe
   con un UPDATE condicionado y una orden sólo entra si sigue siendo suya:

   - la generación de propiedad con la que se leyó la ficha, siempre;
   - la identidad, en `latido`, `devolver` y `verificar`;
   - el estado que la fila tenía cuando se leyó, por omisión.

   Las tres hacen falta. La generación sola no basta, porque las
   transiciones no la mueven y una orden humana lenta revertía un cambio
   ya confirmado. La identidad sola tampoco, porque no distingue dos
   ejecuciones del mismo trabajador.

   Un rechazo lanza `ErrorPropiedad` dentro de la transacción: nada se
   escribe, ni el estado, ni los eventos, ni el espejo JSON.

Lo que NO hace este módulo (reservado para A3.3/C): latidos automáticos,
expiración temporal de trabajadores, detección automática de trabajadores
muertos, recuperación automática de tareas abandonadas, cola o
planificador de tareas, verificación dentro del worktree de la tarea,
lanzamiento de trabajadores.

Este módulo no realiza cálculos de ingeniería.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ingenieria_nucleo.estados import Estado

from . import estado_global as global_
from . import pruebas as corredor
from .tarea import (
    FORMATO_ID,
    ErrorFicha,
    Ficha,
    ahora_utc,
    existe,
    guardar,
    leer,
    listar_con_errores,
    ruta_relativa_ficha,
    temporales_huerfanos,
    validar_id,
)


RAMA_PRINCIPAL = "main"
PREFIJO_RAMA = "tarea/"

# Un latido más viejo que esto se considera vencido.
LATIDO_MAXIMO_S = 900

# Margen de cortesía antes de fiarse de la ausencia del proceso.
#
# Un trabajador puede ser un mandato breve de línea de comandos: el proceso
# que ejecuta 'tomar' termina de inmediato y su PID muere enseguida, aunque
# el trabajo siga vivo en manos de una persona o de un agente. Por eso la
# desaparición del proceso sólo se considera señal de abandono cuando el
# latido tampoco es reciente.
LATIDO_GRACIA_S = 120

# Antigüedad de latido a partir de la cual se da la ejecución por perdida
# aunque no se pueda comprobar el proceso (por ejemplo, otra máquina).
#
# Es el único umbral que decide SOLO, y por eso es holgado: una hora sin
# una señal que se emite automáticamente mientras dura el trabajo ya no
# admite otra lectura.
LATIDO_ABANDONO_S = 3600

# Cada cuánto late el acompañante automático de una operación larga.
#
# Un minuto es holgado frente a los 900 s que tarda un latido en caducar:
# harían falta quince fallos seguidos para que una operación viva pareciera
# caducada, y cada latido cuesta una escritura de una columna.
INTERVALO_LATIDO_S = 60

# Lo que se espera a que el hilo del latido termine al cerrar.
ESPERA_CIERRE_LATIDO_S = 10

# Fallos transitorios de SQLite seguidos que se toleran antes de darse por
# vencido. Uno solo no puede apagar el latido: `database is locked` ocurre
# de verdad bajo concurrencia y dura milisegundos.
FALLOS_LATIDO_SEGUIDOS = 5

ORIGEN_AUTOMATICO = "automático"
ORIGEN_HUMANO = "humano"

# Estados a los que el Supervisor NUNCA puede llegar por su cuenta.
ESTADOS_SOLO_HUMANOS = frozenset({Estado.APROBADO, Estado.RECHAZADO})

# Estados desde los cuales una tarea puede tomarse para trabajar.
ESTADOS_TOMABLES = frozenset(
    {Estado.NUEVO, Estado.REABIERTO, Estado.REQUIERE_REVISION}
)

# Estados en los que una tarea RETIENE su ámbito de archivos.
#
# La lista canónica vive en `estado_global`, porque allí está la guarda que
# impide cambiarle el ámbito a una tarea viva (A3.2). Aquí se reexporta con
# los miembros de `Estado`, que es como la usa el resto de este módulo.
ESTADOS_QUE_RETIENEN_AMBITO = frozenset(
    Estado(texto) for texto in global_.ESTADOS_QUE_RETIENEN_AMBITO
)

# Subconjunto de la máquina de estados común utilizado por el Supervisor V1.
TRANSICIONES = {
    Estado.NUEVO: frozenset({Estado.EN_EJECUCION, Estado.BLOQUEADO}),
    Estado.EN_EJECUCION: frozenset(
        {
            Estado.PROPUESTO,
            Estado.REQUIERE_REVISION,
            Estado.BLOQUEADO,
            Estado.REABIERTO,
        }
    ),
    Estado.REQUIERE_REVISION: frozenset(
        {
            Estado.EN_EJECUCION,
            Estado.BLOQUEADO,
            Estado.REABIERTO,
            Estado.RECHAZADO,
        }
    ),
    Estado.PROPUESTO: frozenset(
        {
            Estado.APROBADO,
            Estado.RECHAZADO,
            Estado.REABIERTO,
            Estado.BLOQUEADO,
        }
    ),
    Estado.REABIERTO: frozenset({Estado.EN_EJECUCION, Estado.BLOQUEADO}),
    Estado.BLOQUEADO: frozenset({Estado.REABIERTO, Estado.RECHAZADO}),
    Estado.RECHAZADO: frozenset({Estado.REABIERTO}),
    Estado.APROBADO: frozenset(),
}

CLASE_ACTIVA = "ACTIVA"
CLASE_HUERFANA = "HUERFANA"
CLASE_INCONSISTENTE = "INCONSISTENTE"

# A3.3 — una ejecución cuyo latido caducó pero que NO está demostrada muerta.
#
# Es el estado que faltaba, y su ausencia hacía que un latido viejo bastara
# por sí solo para declarar abandono. Un latido es una señal débil: puede
# faltar porque el trabajador murió, pero también porque estuvo una hora
# compilando, porque el reloj de la otra máquina va adelantado o porque
# nadie emitió latidos manualmente. Declarar huérfana una tarea por eso es
# arrebatársela a alguien que sigue trabajando.
CLASE_LATIDO_VENCIDO = "LATIDO_VENCIDO"

# Estados de vitalidad que se informan (no son estados de la tarea).
VITALIDAD_ACTIVA = "ACTIVA"
VITALIDAD_LATIDO_VENCIDO = "LATIDO_VENCIDO"
VITALIDAD_HUERFANA = "HUERFANA"
VITALIDAD_FINALIZADA = "FINALIZADA"
VITALIDAD_REANUDABLE = "REANUDABLE"

# Estados sin ejecución en curso que NO están cerrados: esperan a alguien.
ESTADOS_QUE_ESPERAN_A_UNA_PERSONA = frozenset(
    {str(Estado.PROPUESTO), str(Estado.BLOQUEADO)}
)


class ErrorSupervisor(Exception):
    """Operación no permitida por el Supervisor."""


class ErrorTransicion(ErrorSupervisor):
    """La transición de estado solicitada es ilegal."""


class ErrorSolapamiento(ErrorSupervisor):
    """Dos tareas activas quieren escribir sobre el mismo ámbito."""


class ErrorPropiedad(ErrorSupervisor):
    """
    Una orden del ciclo llegó sin ser ya la dueña de la ejecución.

    Lleva el informe de `estado_global.rechazo_propiedad`, que dice si el
    motivo fue otro propietario, una generación vencida (el mismo trabajador
    en una ejecución posterior) o un estado incompatible. Nada se escribió.
    """

    def __init__(self, informe: dict):
        super().__init__(
            informe.get("detalle") or "La orden no pertenece al propietario "
            "vigente."
        )

        self.informe = dict(informe)
        self.tarea = informe.get("tarea")
        self.motivo = informe.get("motivo")
        self.estado = informe.get("estado")
        self.propietario = informe.get("propietario")
        self.propietario_vigente = informe.get("propietario_vigente")
        self.generacion = informe.get("generacion")
        self.generacion_vigente = informe.get("generacion_vigente")


class ErrorToma(ErrorSupervisor):
    """
    La tarea no se pudo reclamar: otro trabajador se adelantó, la tarea no
    existe o su estado no admite toma.

    No es un fallo del sistema sino el resultado NORMAL del perdedor de una
    carrera: se comunica con todos los datos para que quien lo reciba pueda
    decidir qué hacer (elegir otra tarea, reintentar, avisar).

    Hereda de ErrorSupervisor, así que quien ya capturaba ErrorSupervisor
    sigue funcionando sin cambios.
    """

    def __init__(self, informe: dict):
        super().__init__(informe.get("detalle") or "No se pudo tomar la tarea.")

        self.tarea = informe.get("tarea")
        self.motivo = informe.get("motivo")
        self.estado = informe.get("estado")
        self.propietario = informe.get("propietario")
        self.propia = bool(informe.get("propia"))


# ----------------------------------------------------------------------
# Utilidades de tiempo y proceso
# ----------------------------------------------------------------------

def ahora_datetime() -> datetime:
    return datetime.now(timezone.utc)


def a_datetime(texto: str | None) -> datetime | None:
    """Convierte una marca ISO de la ficha en datetime con zona horaria."""
    if not texto:
        return None

    try:
        valor = datetime.fromisoformat(texto)
    except ValueError:
        return None

    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)

    return valor


def nuevo_trabajador_id() -> str:
    """Identidad del trabajador: equipo, proceso y sufijo irrepetible."""
    return (
        socket.gethostname()
        + "/"
        + str(os.getpid())
        + "/"
        + uuid4().hex[:8]
    )


def equipo_de(trabajador_id: str | None) -> str | None:
    if not trabajador_id or "/" not in trabajador_id:
        return None

    return trabajador_id.split("/", 1)[0]


def proceso_vivo(pid: int | None) -> bool:
    """
    Comprueba si un proceso sigue existiendo.

    Nunca se usa como única señal: el Supervisor exige además un latido
    reciente, porque el sistema operativo puede reutilizar un PID.
    """
    if pid is None or pid <= 0:
        return False

    if os.name == "nt":
        import ctypes

        PROCESO_CONSULTA_LIMITADA = 0x1000
        SIGUE_ACTIVO = 259

        kernel32 = ctypes.windll.kernel32

        manejador = kernel32.OpenProcess(
            PROCESO_CONSULTA_LIMITADA, False, int(pid)
        )

        if not manejador:
            return False

        try:
            codigo = ctypes.c_ulong()
            correcto = kernel32.GetExitCodeProcess(
                manejador, ctypes.byref(codigo)
            )
            return bool(correcto) and codigo.value == SIGUE_ACTIVO
        finally:
            kernel32.CloseHandle(manejador)

    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    return True


# ----------------------------------------------------------------------
# Ámbitos de archivos: un solo escritor
# ----------------------------------------------------------------------

# Patrones que, en la práctica, abarcan todo el repositorio.
PATRONES_TOTALES = frozenset({"", ".", "*", "**", "/"})


def normalizar_patron(patron: str) -> str:
    texto = (patron or "").strip().replace("\\", "/")

    while texto.startswith("./"):
        texto = texto[2:]

    texto = texto.rstrip("/")

    if texto == ".":
        return ""

    return texto


def patron_es_relativo(patron: str) -> bool:
    """
    Un ámbito debe expresarse siempre en rutas relativas a la raíz.

    Una ruta absoluta o con '..' no puede compararse de forma fiable contra
    los demás ámbitos, y eso rompería la regla de un solo escritor.
    """
    texto = normalizar_patron(patron)

    if not texto:
        return False

    if texto.startswith("/"):
        return False

    # Unidad de Windows: C:/...
    if len(texto) >= 2 and texto[1] == ":":
        return False

    return ".." not in PurePosixPath(texto).parts


def _raiz_sin_comodin(patron: str) -> str:
    partes = []

    for parte in PurePosixPath(patron).parts:
        if any(simbolo in parte for simbolo in "*?["):
            break
        partes.append(parte)

    return "/".join(partes)


def patrones_solapan(primero: str, segundo: str) -> bool:
    """
    Decide, de forma deliberadamente conservadora, si dos patrones pueden
    tocar el mismo archivo. Ante la duda, responde que sí: es preferible
    frenar una tarea que permitir dos escritores sobre el mismo archivo.
    """
    uno = normalizar_patron(primero)
    dos = normalizar_patron(segundo)

    # Un patrón vacío o que abarca todo el repositorio choca con cualquiera:
    # fallar en cerrado, nunca en abierto.
    if uno in PATRONES_TOTALES or dos in PATRONES_TOTALES:
        return True

    if uno == dos:
        return True

    if fnmatch(uno, dos) or fnmatch(dos, uno):
        return True

    if uno.startswith(dos + "/") or dos.startswith(uno + "/"):
        return True

    raiz_uno = _raiz_sin_comodin(uno)
    raiz_dos = _raiz_sin_comodin(dos)

    if raiz_uno and raiz_dos:
        if raiz_uno == raiz_dos:
            return True
        if raiz_uno.startswith(raiz_dos + "/") or raiz_dos.startswith(
            raiz_uno + "/"
        ):
            return True

    return False


def solapamientos(ambito_uno: list[str], ambito_dos: list[str]) -> list[tuple]:
    """Pares concretos de patrones en conflicto."""
    encontrados = []

    for uno in ambito_uno:
        for dos in ambito_dos:
            if patrones_solapan(uno, dos):
                encontrados.append((uno, dos))

    return encontrados


def conflictos_de_ambito(
    ficha: Ficha,
    filas: list[dict],
    errores: list[dict] | None = None,
    estados_activos: frozenset = ESTADOS_QUE_RETIENEN_AMBITO,
    ambito: list[str] | None = None,
) -> list[dict]:
    """
    Conflictos de esta ficha contra todas las tareas que retienen ámbito.

    Función PURA: recibe las `filas` ya leídas de SQLite y los `errores` de
    lectura del árbol. No abre conexiones ni toca el disco.

    Es así desde A3.1 a propósito: `tomar` la ejecuta DENTRO de la misma
    transacción que concede la toma, con las filas leídas en esa
    transacción, de modo que entre comprobar el ámbito y escribir la toma
    no queda ninguna ventana. Si abriera su propia conexión, no podría.

    Se juzga por SQLite (estado y ámbito registrados), no por los JSON del
    árbol actual: así una tarea cuya ficha sólo existe en la rama de otro
    worktree sigue contando para la regla de un solo escritor.

    Una ficha ilegible que la base todavía no conoce interrumpe, igual que
    en V1: sin conocer su ámbito no se puede garantizar nada.

    `ambito` permite juzgar un ámbito distinto del declarado en la ficha.
    Lo usa `tomar` para comprobar la UNIÓN del declarado con el que la
    tarea ya retenía: hay que validar exactamente lo que se va a grabar.
    """
    conflictos = []
    propio = list(ficha.ambito_archivos if ambito is None else ambito)

    errores = errores or []

    registradas = {fila["id"] for fila in filas}

    desconocidas = [
        error for error in errores
        if FORMATO_ID.match(Path(error["archivo"]).stem)
        and Path(error["archivo"]).stem not in registradas
    ]

    if desconocidas:
        raise ErrorSupervisor(
            "Hay fichas ilegibles que el estado global no conoce; sin su "
            "ámbito no se puede garantizar un solo escritor: "
            + ", ".join(error["archivo"] for error in desconocidas)
            + "."
        )

    for fila in filas:
        if fila["id"] == ficha.id:
            continue

        if fila["estado"] not in {str(estado) for estado in estados_activos}:
            continue

        pares = solapamientos(propio, fila["ambito_archivos"])

        if pares:
            conflictos.append(
                {
                    "tarea": fila["id"],
                    "titulo": fila["titulo"],
                    "estado": fila["estado"],
                    "pares": [
                        {"propio": uno, "ajeno": dos} for uno, dos in pares
                    ],
                }
            )

    return conflictos


# ----------------------------------------------------------------------
# Transiciones
# ----------------------------------------------------------------------

def transicion_permitida(origen_estado: Estado, destino: Estado) -> bool:
    return destino in TRANSICIONES.get(origen_estado, frozenset())


def transicionar(
    ficha: Ficha,
    destino: Estado,
    motivo: str,
    origen: str = ORIGEN_AUTOMATICO,
) -> Ficha:
    """
    Aplica una transición de estado sobre la ficha en memoria.

    Rechaza:
    - transiciones que la máquina de estados no contempla;
    - cualquier intento automático de llegar a APROBADO o RECHAZADO.
    """
    anterior = ficha.estado

    if destino in ESTADOS_SOLO_HUMANOS and origen != ORIGEN_HUMANO:
        raise ErrorTransicion(
            "El Supervisor no puede llegar automáticamente a '"
            + str(destino)
            + "'. Ese estado exige una acción humana explícita."
        )

    if not transicion_permitida(anterior, destino):
        raise ErrorTransicion(
            "Transición inválida: '"
            + str(anterior)
            + "' -> '"
            + str(destino)
            + "'."
        )

    ficha.estado = destino

    ficha.registrar_evento(
        {
            "fecha": ahora_utc(),
            "tipo": global_.EVENTO_TRANSICION,
            "estado_anterior": str(anterior),
            "estado_nuevo": str(destino),
            "motivo": motivo,
            "origen": origen,
        }
    )

    return ficha


# ----------------------------------------------------------------------
# Carga y persistencia: SQLite manda, el JSON refleja
# ----------------------------------------------------------------------

# Columnas de `tareas` que una operación del ciclo puede modificar.
# Las de definición (titulo, definicion_*) sólo las toca la sincronización.
COLUMNAS_OPERATIVAS = (
    "estado",
    "rama",
    "worktree",
    "intentos",
    "max_intentos",
    "trabajador_id",
    "pid",
    "iniciado_en",
    "ultimo_latido",
    "actualizado_en",
    "ultima_falla",
    "requiere_decision_humana",
    "decisiones",
    "ejecuciones",
    "ultima_verificacion",
    "commit_inicial",
)


# Campos que cada orden POSEE, es decir los únicos que puede escribir.
#
# A3.2 protegió QUIÉN escribe y DESDE QUÉ momento; no QUÉ. `persistir`
# reescribía las dieciséis columnas operativas en bloque a partir de la foto
# que `cargar` había leído, así que dos órdenes perfectamente válidas de la
# misma generación, propietario y estado —un latido y una decisión humana,
# por ejemplo— se pisaban campo a campo: la segunda devolvía a la columna
# de la primera el valor que tenía cuando ella leyó. Un lost update de
# manual, y silencioso.
#
# Desde A3.3 cada orden declara lo suyo y no toca nada más.
# `actualizado_en` se añade siempre: es la marca de "algo cambió aquí", la
# escribe con derecho cualquier orden que confirme, y que dos la pisen no
# pierde información de estado.
CAMPOS_LATIDO = ("ultimo_latido",)

# Soltar al trabajador: lo hacen todas las órdenes que cierran un turno.
CAMPOS_LIBERACION = ("trabajador_id", "pid", "iniciado_en", "ultimo_latido")

CAMPOS_TRANSICION = ("estado",)

CAMPOS_DEVOLVER = CAMPOS_TRANSICION + CAMPOS_LIBERACION

CAMPOS_VERIFICAR = (
    CAMPOS_TRANSICION
    + CAMPOS_LIBERACION
    + ("ultima_falla", "ultima_verificacion", "ejecuciones")
)

CAMPOS_DECIDIR = ("decisiones", "requiere_decision_humana")

# `reabrir` devuelve además el presupuesto de intentos, que es un valor
# fijo (cero) y no un incremento.
CAMPOS_REABRIR = CAMPOS_TRANSICION + CAMPOS_LIBERACION + ("intentos",)

# `reanudar` cierra la ejecución interrumpida: la anota en `ejecuciones`,
# deja constancia en `ultima_falla` y devuelve la tarea al circuito.
CAMPOS_RECUPERAR = (
    CAMPOS_TRANSICION
    + CAMPOS_LIBERACION
    + ("ejecuciones", "ultima_falla")
)


def cargar(raiz: Path, identificador: str) -> Ficha:
    """
    Tarea completa: definición desde el JSON, estado operativo desde SQLite.

    Si la tarea todavía no está en la base global se incorpora en ese
    momento (misma sincronización idempotente del bootstrap). Después,
    el estado de SQLite se superpone a cualquier valor operativo del JSON.
    """
    ficha = leer(raiz, identificador)

    with global_.conexion(raiz) as con:
        global_.asegurar_ficha(con, ficha)
        fila = global_.obtener_tarea(con, ficha.id)

    global_.aplicar_fila(ficha, fila)
    ficha.eventos_pendientes.clear()

    return ficha


def persistir(
    raiz: Path,
    ficha: Ficha,
    exigir_propietario: str | None = None,
    estados_admitidos=None,
    exigir_generacion: int | None = None,
    campos_propios=None,
    incrementos=None,
    exigir_iguales=None,
) -> Ficha:
    """
    Confirma el estado operativo de la ficha, si la propiedad sigue vigente.

    1. Se valida el contrato de la ficha antes de tocar nada.
    2. Transacción SQLite: UPDATE CONDICIONADO + eventos pendientes. COMMIT.
    3. Sólo entonces se regenera el JSON como espejo, con la misma marca de
       actualización, mediante la escritura atómica ya existente.

    Qué condiciona la escritura (A3.2)
    ----------------------------------
    SIEMPRE la generación con la que se leyó la ficha (`ficha.generacion`).
    Basta una toma nueva entre la lectura y esta escritura para que el
    predicado deje de casar: la orden se rechaza sin tocar nada. Eso vale
    también para las órdenes humanas, que no tienen propietario pero
    tampoco deben pisar una ejecución que empezó mientras decidían.

    ADEMÁS la identidad, cuando `exigir_propietario` la indica. Las órdenes
    que pertenecen a una ejecución viva —`latido`, `devolver`, `verificar`—
    la exigen: sin ella, una orden emitida después de que su emisor soltara
    la tarea seguiría entrando mientras la generación no hubiera cambiado.

    El ESTADO. Por omisión se exige que la fila siga en el estado que tenía
    cuando se leyó (`ficha.estado_leido`). Ésta es la precondición que
    faltaba, y sin ella la generación no bastaba: `devolver`, `bloquear`,
    `aprobar` y las demás transiciones NO mueven la generación, así que dos
    órdenes separadas por varias transiciones seguían teniendo el mismo
    testigo. Comprobado: una orden humana lenta revertía a PROPUESTO una
    tarea que entretanto había quedado BLOQUEADA, saltándose además la
    máquina de estados, porque `transicionar` validó contra su foto vieja.

    La comprobación en Python que hacen las órdenes tras `cargar` explica el
    error con precisión; ésta cierra la ventana entre aquella lectura y esta
    escritura. `estados_admitidos` permite ampliarlo o afinarlo; una ficha
    que no venga de la base (`estado_leido` a None) no exige ninguno, que es
    lo que hacía V1.

    Si la orden se rechaza
    ----------------------
    Se lanza `ErrorPropiedad` DENTRO de la transacción, así que el ROLLBACK
    deshace todo: no se escribe el estado, no se escriben los eventos
    pendientes (que siguen en la ficha, sin consumir), no se regenera el
    espejo JSON y `actualizado_en` se restaura al valor que tenía. Un
    rechazo no deja rastro de haber pasado por aquí.

    No sirve para la TOMA de una tarea: la toma es quien CONCEDE la
    propiedad y la genera, y usa `estado_global.reclamar` (ver `tomar`).
    """
    Ficha.desde_dict(ficha.a_dict())

    actualizado_previo = ficha.actualizado_en
    creado_previo = ficha.creado_en

    ficha.actualizado_en = ahora_utc()

    if not ficha.creado_en:
        ficha.creado_en = ficha.actualizado_en

    fila = global_.fila_desde_ficha(ficha, ficha.actualizado_en)

    # Sólo lo que esta orden posee (A3.3). Sin `campos_propios` se escriben
    # las dieciséis, que es lo que hacía A3.2 y lo que abre la puerta al
    # lost update: queda disponible para quien deba escribir de verdad todo
    # el estado operativo, pero ninguna orden del ciclo lo usa ya.
    propios = tuple(
        COLUMNAS_OPERATIVAS if campos_propios is None else campos_propios
    )

    incrementos = tuple(incrementos or ())

    for columna in propios + incrementos:
        if columna not in COLUMNAS_OPERATIVAS:
            raise ErrorSupervisor(
                "'" + str(columna) + "' no es una columna operativa: una "
                "orden no puede declararla como suya."
            )

    campos = {columna: fila[columna] for columna in propios}

    # La marca de actualización la escribe cualquier orden que confirme.
    campos["actualizado_en"] = fila["actualizado_en"]

    for columna in incrementos:
        campos.pop(columna, None)

    eventos = list(ficha.eventos_pendientes)

    if estados_admitidos is None and ficha.estado_leido is not None:
        estados_admitidos = {ficha.estado_leido}

    try:
        with global_.conexion(raiz) as con:
            with global_.transaccion(con):
                informe = global_.actualizar_si_propietario(
                    con,
                    ficha.id,
                    campos,
                    generacion=(
                        int(ficha.generacion or 0)
                        if exigir_generacion is None
                        else int(exigir_generacion)
                    ),
                    momento=ficha.actualizado_en,
                    trabajador_id=exigir_propietario,
                    estados_admitidos=estados_admitidos,
                    incrementos=incrementos,
                    exigir_iguales=exigir_iguales,
                )

                if informe["resultado"] != global_.ESCRITURA_ACEPTADA:
                    raise ErrorPropiedad(informe)

                for evento in eventos:
                    global_.insertar_evento(con, ficha.id, evento)

                # La fila se relee DENTRO de la transacción para que la
                # ficha refleje lo que el COMMIT confirma, incluidos los
                # incrementos que resolvió el motor y los campos que esta
                # orden NO escribió y que otra pudo haber cambiado.
                confirmada = global_.obtener_tarea(con, ficha.id)
    except ErrorPropiedad:
        # La ficha en memoria vuelve a ser el reflejo de lo que hay grabado:
        # nada cambió, y sus marcas de tiempo no deben sugerir lo contrario.
        ficha.actualizado_en = actualizado_previo
        ficha.creado_en = creado_previo
        raise

    ficha.eventos_pendientes.clear()

    # La ficha vuelve a ser el reflejo de la fila, no de lo que esta orden
    # creía. Sin esto, escribir sólo lo propio dejaría en memoria los
    # valores viejos de las columnas ajenas, y el espejo JSON los volcaría
    # a disco: se habría cambiado un lost update en SQLite por otro en el
    # archivo.
    historial = list(ficha.historial)
    global_.aplicar_fila(ficha, confirmada)
    ficha.historial = historial

    # Lo que se acaba de confirmar es, a partir de ahora, lo leído: si la
    # misma ficha se persiste otra vez, la precondición tiene que ser el
    # estado nuevo y no el de antes de esta escritura.
    ficha.estado_leido = ficha.estado

    _regenerar_espejo(raiz, ficha)

    return ficha


def credencial_de(
    ficha: Ficha,
    orden: str,
    trabajador_id: str | None = None,
    generacion: int | None = None,
) -> tuple:
    """
    Credencial (propietario, generación) que una orden debe acreditar.

    Se resuelve ANTES de que la orden modifique la ficha: `devolver` y
    `verificar` liberan al trabajador como parte de su trabajo, y si se
    leyera después iría vacía y la condición no exigiría nada.

    Dos formas de acreditarse, y la diferencia importa
    --------------------------------------------------
    DECLARADA (`trabajador_id`, y opcionalmente `generacion`): quien llama
    dice quién es. Es la única forma que detiene de verdad a una orden
    rezagada, porque la orden vieja lleva SU identidad y SU generación, no
    las que haya ahora en la base. Un emisor que releyera la fila para
    saber quién es no estaría acreditándose: estaría suplantando al dueño
    actual, y ninguna condición podría distinguirlo.

    IMPLÍCITA (nada): se toma la de la ficha recién leída. Es lo que hacía
    V1 y se conserva para no romper a quien ya llamaba así. Protege contra
    el caso en que la propiedad cambie ENTRE esta lectura y la escritura,
    que no es poco, pero no contra un emisor que ya había perdido la tarea
    antes de leer.

    Lo declarado NO se comprueba aquí contra la base: eso sería volver a
    comprobar antes de escribir (TOCTOU). Viaja tal cual al WHERE, y decide
    el motor.
    """
    if trabajador_id is not None:
        if not str(trabajador_id).strip():
            raise ErrorSupervisor(
                "La identidad declarada para '" + orden + "' está vacía."
            )

        declarada = str(trabajador_id).strip()

        if generacion is None:
            # Sin generación declarada se usa la de la lectura actual. Basta
            # para distinguir a otro trabajador, no para distinguir dos
            # ejecuciones del mismo: para eso hay que declararla.
            return (declarada, int(ficha.generacion or 0))

        if isinstance(generacion, bool) or not isinstance(generacion, int):
            raise ErrorSupervisor(
                "La generación declarada para '" + orden + "' debe ser un "
                "entero; se recibió: " + repr(generacion) + "."
            )

        return (declarada, generacion)

    if generacion is not None:
        raise ErrorSupervisor(
            "No se puede declarar una generación para '" + orden + "' sin "
            "declarar también el trabajador: la generación por sí sola no "
            "identifica a nadie."
        )

    if not ficha.trabajador_id:
        raise ErrorSupervisor(
            "La tarea '" + ficha.id + "' no tiene propietario, así que nadie "
            "puede emitir '" + orden + "' sobre ella."
        )

    return (ficha.trabajador_id, int(ficha.generacion or 0))


CAMPOS_DECLARATIVOS = (
    "objetivo",
    "criterios_aceptacion",
    "pruebas_requeridas",
    "ambito_archivos",
    "max_intentos",
)


def _refrescar_declarativo(raiz: Path, ficha: Ficha) -> None:
    """
    Relee del disco lo que la ficha declara, justo antes de escribirla.

    El espejo sólo debe reescribir los campos OPERATIVOS, que salen de
    SQLite. Lo declarativo —objetivo, criterios, pruebas requeridas,
    ámbito, presupuesto de intentos, decisiones declaradas— lo escribe una
    persona en el archivo, y se quedaba en memoria tal como se leyó AL
    EMPEZAR la orden. Con `verificar` esa ventana no son milisegundos: es
    toda la batería de pruebas, minutos enteros. Lo que el ingeniero
    escribiera mientras tanto desaparecía al terminar, sin aviso; incluida
    una decisión humana recién declarada, con lo que la tarea se iba a
    PROPUESTO saltándose justo la decisión que esa persona quería forzar.

    Si el archivo no se puede leer, se escribe lo que hay en memoria: es lo
    que se hacía siempre y no empeora nada.
    """
    try:
        en_disco = leer(raiz, ficha.id)
    except (ErrorFicha, OSError):
        return

    for campo in CAMPOS_DECLARATIVOS:
        valor = getattr(en_disco, campo)
        setattr(
            ficha, campo, list(valor) if isinstance(valor, list) else valor
        )

    # Las decisiones son mixtas: la clave y la descripción son del archivo,
    # la resolución es de la base. Se fusionan en vez de elegir una.
    ficha.requiere_decision_humana = global_.fusionar_decisiones(
        en_disco.requiere_decision_humana,
        global_.decisiones_operativas(ficha.requiere_decision_humana),
    )


def _regenerar_espejo(raiz: Path, ficha: Ficha) -> None:
    """
    Reescribe el JSON como espejo de lo que SQLite ya confirmó.

    Si el espejo no se pudiera escribir, el estado global ya quedó
    confirmado y la siguiente persistencia lo regenera: nunca hay dos
    escrituras contradictorias, porque el JSON siempre sale de SQLite.
    """
    _refrescar_declarativo(raiz, ficha)

    try:
        guardar(raiz, ficha, marcar_actualizacion=False)
    except (ErrorFicha, OSError) as error:
        raise ErrorSupervisor(
            "El estado global de '" + ficha.id + "' quedó confirmado en "
            "SQLite (" + str(ficha.estado) + "), pero no se pudo regenerar "
            "el espejo JSON: " + str(error) + ". La siguiente operación lo "
            "regenerará; no se registró ningún commit."
        ) from error


# ----------------------------------------------------------------------
# Git: commits automáticos estrictamente limitados
# ----------------------------------------------------------------------

class ErrorCreacion(ErrorSupervisor):
    """
    No se pudo crear la tarea porque ya existía.

    Es un error propio y no genérico porque perder una carrera de creación
    es un resultado NORMAL —igual que perder una toma—, no una avería: el
    que llega segundo tiene que poder distinguirlo de un fallo del sistema.
    """


class ErrorWorktree(ErrorSupervisor):
    """
    La ruta registrada como worktree de una tarea no se puede usar.

    No existe, no es un directorio, o pertenece a otro repositorio. Es un
    error propio y no genérico porque la respuesta del operador es distinta
    en cada caso y porque una ruta ajena NUNCA debe ejecutarse por el
    hecho de existir.
    """


# Variables que hacen que `git` deje de mirar el directorio en el que se le
# invoca. Están puestas SIEMPRE dentro de un hook, y también en
# `git rebase --exec`, `git bisect run` y en muchos envoltorios de CI. Con
# `GIT_DIR` heredado, dos `rev-parse` desde directorios distintos devuelven
# lo mismo y la comprobación de pertenencia deja de comprobar nada: se llegó
# a aceptar `/tmp` como worktree de la tarea. Se quitan antes de preguntar.
VARIABLES_GIT_HEREDADAS = (
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


def entorno_git_limpio() -> dict:
    """Copia del entorno sin las variables que redirigen a `git`."""
    entorno = dict(os.environ)

    for nombre in VARIABLES_GIT_HEREDADAS:
        entorno.pop(nombre, None)

    return entorno


def arboles_registrados(raiz: Path) -> set:
    """
    Los worktrees que Git reconoce para este repositorio, ya resueltos.

    Es la lista canónica: la que `git worktree list` imprime, la misma que
    ve una persona. Incluye el árbol principal.

    Se pregunta a Git cada vez, sin memorizar. Es una invocación por
    validación —no cientos—, y una decisión de seguridad no debe depender
    de si este proceso ya había mirado antes ese directorio: eso haría que
    el mismo estado del disco diera veredictos distintos.
    """
    try:
        resultado = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(raiz),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=entorno_git_limpio(),
        )
    except OSError as error:
        raise ErrorWorktree(
            "No se pudo consultar la lista de worktrees de Git: "
            + str(error)
        ) from None

    if resultado.returncode != 0:
        raise ErrorWorktree(
            "Git no pudo listar los worktrees desde '" + str(raiz) + "': "
            + (resultado.stderr or "").strip()
        )

    arboles = set()

    for linea in resultado.stdout.splitlines():
        if not linea.startswith("worktree "):
            continue

        declarada = Path(linea[len("worktree "):])

        try:
            arboles.add(declarada.resolve())
        except OSError:
            # Un worktree listado pero irresoluble no sirve como destino;
            # tampoco es motivo para tumbar la validación de los demás.
            continue

    return arboles


def resolver_worktree(raiz: Path, declarado: str | None) -> Path:
    """
    Convierte la ruta registrada de un worktree en una raíz utilizable.

    Devuelve la raíz resuelta cuando la tarea no declara worktree: es el
    comportamiento de siempre y el caso normal hoy.

    Qué se comprueba, y por qué cada cosa
    -------------------------------------
    - Que exista y sea un DIRECTORIO. Un archivo con ese nombre, o una ruta
      borrada, no es un árbol de trabajo.

    - Que Git lo reconozca como worktree de ESTE repositorio, preguntándole
      a `git worktree list`. Es la comprobación que impide ejecutar una ruta
      ajena, y es más estricta que comparar el directorio común: ese valor
      no lo decide el repositorio, lo decide un archivo `.git` de una línea
      que vive en el directorio candidato. Copiar un worktree con `cp -a`,
      moverlo sin `git worktree repair`, o escribir a mano
      `gitdir: <principal>/.git/worktrees/A` en cualquier carpeta, producía
      un directorio que se aceptaba y que `git worktree list` no ha listado
      nunca. Peor: la rama y el commit se leían de ese `.git` prestado, así
      que la evidencia grabada era la del worktree legítimo y el historial
      afirmaba haber verificado un commit que nadie ejecutó.

      Un subdirectorio cualquiera tampoco vale, por lo mismo: el corredor
      descubre `<arbol>/pruebas/**/prueba_*.py`, así que un subdirectorio
      con una sola prueba verde dentro bastaba para llegar a PROPUESTO
      saltándose la batería entera.

    Sobre las rutas, que es donde se esconden los disgustos:

    - Una ruta RELATIVA se interpreta contra `raiz`, nunca contra el
      directorio desde el que se invocó el Supervisor, que puede ser
      cualquiera. En Windows hay dos formas que NO son absolutas y que sin
      embargo ignorarían la raíz al unirlas —`C:pruebas`, relativa a la
      unidad, y `\\pruebas`, con raíz pero sin unidad—: se rechazan con un
      mensaje propio en vez de resolverse contra el directorio actual del
      proceso.
    - `expanduser` resuelve `~`; `resolve` normaliza `..`, los enlaces
      simbólicos y las junctions de Windows, y en Windows además unifica la
      letra de unidad y el caso del sistema de archivos. Por eso la
      comparación se hace SIEMPRE entre rutas resueltas.
    - La ruta NO se recorta: un directorio cuyo nombre termina en espacio es
      legal en POSIX y `git worktree add` lo crea sin protestar. Sólo se
      recorta para decidir si la cadena está en blanco, que es otra cosa.
    - Los espacios no necesitan nada especial porque nunca se construye una
      línea de órdenes de texto: `subprocess` recibe una lista.
    """
    if declarado is None or not str(declarado).strip():
        return Path(raiz).resolve()

    candidato = Path(str(declarado)).expanduser()

    if not candidato.is_absolute():
        if candidato.drive or candidato.root:
            # `C:pruebas` o `\\pruebas` en Windows. Unirlas a la raíz
            # descartaría la raíz y acabarían resolviéndose contra el
            # directorio actual del proceso, que es justo lo que esta
            # función promete no hacer.
            raise ErrorWorktree(
                "La ruta del worktree '" + str(declarado) + "' no es "
                "absoluta pero lleva unidad o raíz, así que no se puede "
                "interpretar contra la raíz del repositorio. Decláralo con "
                "una ruta absoluta o relativa sin unidad."
            )

        candidato = Path(raiz) / candidato

    try:
        candidato = candidato.resolve()
    except OSError as error:
        raise ErrorWorktree(
            "No se pudo resolver la ruta del worktree '" + str(declarado)
            + "': " + str(error)
        ) from None

    try:
        existe = candidato.exists()
        es_directorio = candidato.is_dir()
    except OSError as error:
        # Ruta demasiado larga, volumen desmontado, recurso de red que no
        # responde. Decirlo así evita mandar al operador a buscar un
        # directorio borrado que en realidad está ahí.
        raise ErrorWorktree(
            "No se pudo consultar la ruta del worktree '" + str(candidato)
            + "': " + str(error)
        ) from None

    if not existe:
        raise ErrorWorktree(
            "El worktree registrado no existe: '" + str(candidato) + "'."
        )

    if not es_directorio:
        raise ErrorWorktree(
            "El worktree registrado no es un directorio: '"
            + str(candidato) + "'."
        )

    try:
        propia = Path(raiz).resolve()
    except OSError:
        propia = Path(raiz)

    if candidato == propia:
        return candidato

    registrados = arboles_registrados(raiz)

    if candidato not in registrados:
        raise ErrorWorktree(
            "La ruta '" + str(candidato) + "' no es un worktree registrado "
            "de este repositorio. Git conoce estos: "
            + (", ".join(sorted(str(uno) for uno in registrados)) or "ninguno")
            + ". No se ejecuta una ruta ajena por el hecho de que exista."
        )

    return candidato


class Git:
    """Acceso mínimo a Git, con los límites del Supervisor incorporados."""

    def __init__(self, raiz: Path):
        self.raiz = Path(raiz)

    def _ejecutar(self, *argumentos: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *argumentos],
                cwd=str(self.raiz),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Sin sanear, un `GIT_DIR` heredado (siempre presente dentro
                # de un hook) haría que `git` ignorase `cwd` y respondiera
                # por OTRO árbol: la evidencia de la corrida sería falsa.
                env=entorno_git_limpio(),
            )
        except OSError as error:
            # El árbol puede desaparecer mientras corren las pruebas. Eso no
            # debe salir como un traceback de la biblioteca estándar: se
            # devuelve un fallo normal y quien llama decide.
            return subprocess.CompletedProcess(
                ["git", *argumentos], 1, "", str(error)
            )

    def disponible(self) -> bool:
        try:
            resultado = self._ejecutar("rev-parse", "--is-inside-work-tree")
        except OSError:
            return False

        return resultado.returncode == 0

    def rama_actual(self) -> str | None:
        resultado = self._ejecutar("rev-parse", "--abbrev-ref", "HEAD")

        if resultado.returncode != 0:
            return None

        return resultado.stdout.strip() or None

    def hash_actual(self) -> str | None:
        resultado = self._ejecutar("rev-parse", "--short", "HEAD")

        if resultado.returncode != 0:
            return None

        return resultado.stdout.strip() or None

    def hay_cambios_sin_confirmar(self) -> bool:
        """
        Si el árbol tiene algo sin confirmar.

        Hace falta junto al commit: un cambio sin confirmar no mueve el
        hash, así que sin esto un archivo editado a mitad de la corrida
        pasaría por «el árbol no se movió».
        """
        resultado = self._ejecutar("status", "--porcelain")

        if resultado.returncode != 0:
            return False

        return bool(resultado.stdout.strip())

    def commit_ficha(self, identificador: str, mensaje: str) -> dict:
        """
        Commit automático permitido: exclusivamente la ficha de la tarea,
        exclusivamente dentro de la rama de esa tarea, jamás en la principal.
        """
        validar_id(identificador)

        relativa = ruta_relativa_ficha(identificador)
        esperada = PREFIJO_RAMA + identificador

        if not self.disponible():
            return {
                "realizado": False,
                "motivo": "Git no está disponible en esta ruta.",
                "commit": None,
                "rama": None,
            }

        rama = self.rama_actual()

        if rama is None:
            return {
                "realizado": False,
                "motivo": "No se pudo determinar la rama actual.",
                "commit": None,
                "rama": None,
            }

        if rama == RAMA_PRINCIPAL:
            return {
                "realizado": False,
                "motivo": (
                    "Prohibido el commit automático en la rama '"
                    + RAMA_PRINCIPAL
                    + "'."
                ),
                "commit": None,
                "rama": rama,
            }

        if rama != esperada:
            return {
                "realizado": False,
                "motivo": (
                    "La rama actual es '"
                    + rama
                    + "' y la ficha exige '"
                    + esperada
                    + "'."
                ),
                "commit": None,
                "rama": rama,
            }

        agregado = self._ejecutar("add", "--", relativa)

        if agregado.returncode != 0:
            return {
                "realizado": False,
                "motivo": "No se pudo preparar la ficha: "
                + agregado.stderr.strip(),
                "commit": None,
                "rama": rama,
            }

        confirmado = self._ejecutar("commit", "-m", mensaje, "--", relativa)

        if confirmado.returncode != 0:
            salida = (confirmado.stdout + confirmado.stderr).strip()

            return {
                "realizado": False,
                "motivo": "Sin cambios que registrar o commit rechazado: "
                + salida,
                "commit": None,
                "rama": rama,
            }

        return {
            "realizado": True,
            "motivo": "Ficha registrada en la rama de la tarea.",
            "commit": self.hash_actual(),
            "rama": rama,
        }


def _registrar_en_git(git, ficha: Ficha, motivo: str) -> dict | None:
    if git is None:
        return None

    mensaje = "Tarea " + ficha.id + ": " + str(ficha.estado)

    if motivo:
        mensaje = mensaje + "\n\n" + motivo

    return git.commit_ficha(ficha.id, mensaje)


# ----------------------------------------------------------------------
# Operaciones del ciclo
# ----------------------------------------------------------------------

def crear(
    raiz: Path,
    identificador: str,
    titulo: str,
    objetivo: str = "",
    criterios_aceptacion: list[str] | None = None,
    ambito_archivos: list[str] | None = None,
    pruebas_requeridas: list[str] | None = None,
    max_intentos: int = 3,
    decisiones: list[dict] | None = None,
    commit_inicial: str | None = None,
) -> Ficha:
    """Crea una ficha nueva en estado NUEVO."""
    validar_id(identificador)

    # Atajo amable, no la garantía: evita construir la ficha entera para
    # nada en el caso normal. Quien decide de verdad es la comprobación de
    # abajo, hecha con el bloqueo de escritura tomado.
    if existe(raiz, identificador):
        raise ErrorCreacion(
            "Ya existe la ficha '" + identificador + "'."
        )

    ficha = Ficha(
        id=identificador,
        titulo=titulo,
        objetivo=objetivo,
        criterios_aceptacion=list(criterios_aceptacion or []),
        ambito_archivos=list(ambito_archivos or []),
        pruebas_requeridas=list(pruebas_requeridas or []),
        estado=Estado.NUEVO,
        rama=PREFIJO_RAMA + identificador,
        worktree=None,
        max_intentos=max_intentos,
        commit_inicial=commit_inicial,
        requiere_decision_humana=normalizar_decisiones(decisiones),
    )

    # Las marcas se ponen aquí y no al escribir el JSON: el espejo se
    # escribe DESPUÉS del COMMIT y con `marcar_actualizacion=False`, así
    # que si no se fijaran ahora la fila nacería sin fechas.
    nacimiento = ahora_utc()

    ficha.creado_en = nacimiento
    ficha.actualizado_en = nacimiento

    ficha.registrar_evento(
        {
            "fecha": nacimiento,
            "tipo": global_.EVENTO_CREACION,
            "estado_anterior": None,
            "estado_nuevo": str(Estado.NUEVO),
            "motivo": "Ficha creada.",
            "origen": ORIGEN_HUMANO,
        }
    )

    # Crear es un acto único: o lo hace uno o no lo hace nadie (A3.3).
    #
    # La comprobación de existencia y el INSERT ocurren DENTRO de la misma
    # transacción. Antes la comprobación se hacía en autocommit, con lo que
    # dos procesos que crearan la misma tarea a la vez pasaban los dos, los
    # dos escribían su JSON —el segundo pisando al primero— y sólo entonces
    # la clave primaria rechazaba a uno. El perdedor se iba con un error,
    # pero dejaba su definición escrita encima de la del ganador.
    #
    # Con BEGIN IMMEDIATE el segundo espera a que el primero confirme, y
    # entonces ve la fila y se rechaza SIN escribir nada.
    #
    # EL JSON SE ESCRIBE DESPUÉS DEL COMMIT, y esto importa (auditoría R1).
    # El sistema de archivos no es transaccional: `os.replace` es visible
    # para todo el mundo en el acto y ningún ROLLBACK lo deshace. Con la
    # escritura dentro, cualquier corte posterior —Ctrl-C, un `kill`, un
    # apagón— dejaba exactamente lo que esta función declara imposible:
    # ficha JSON sin fila. Y el huérfano no era inerte: `crear` fallaba
    # para siempre con ese identificador, y la primera orden de LECTURA lo
    # incorporaba en silencio, así que la tarea que no creó nadie acababa
    # existiendo con la definición del proceso muerto.
    #
    # Al revés el daño es reparable y menor: si falla el espejo, hay fila
    # sin ficha, y la base es la autoridad. Se dice y se puede regenerar.
    #
    # De paso, el candado de escritura de TODA la base deja de retenerse
    # durante el `fsync` del archivo, que es lo que `tomar` ya evitaba a
    # propósito.
    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            if global_.obtener_tarea(con, identificador) is not None:
                raise ErrorCreacion(
                    "La tarea '" + identificador + "' ya existe en el "
                    "estado global: no se puede volver a crear con el "
                    "mismo identificador."
                )

            # Se relee el árbol con el bloqueo tomado: entre la
            # comprobación de arriba y este punto, otro proceso pudo haber
            # creado la ficha y confirmado.
            if existe(raiz, identificador):
                raise ErrorCreacion(
                    "Ya existe la ficha '" + identificador + "'."
                )

            global_.importar_ficha(con, ficha, evento_importacion=False)

    _regenerar_espejo(raiz, ficha)

    ficha.eventos_pendientes.clear()

    return ficha


def normalizar_decisiones(decisiones: list[dict] | None) -> list[dict]:
    """Da forma completa a las decisiones humanas pendientes."""
    resultado = []

    for numero, cruda in enumerate(decisiones or [], start=1):
        clave = str(cruda.get("clave") or ("D-" + str(numero)))

        resultado.append(
            {
                "clave": clave,
                "descripcion": str(cruda.get("descripcion") or ""),
                "resuelta": bool(cruda.get("resuelta", False)),
                "resolucion": cruda.get("resolucion"),
                "resuelta_en": cruda.get("resuelta_en"),
            }
        )

    return resultado


def tomar(
    raiz: Path,
    identificador: str,
    trabajador_id: str | None = None,
    pid: int | None = None,
    ahora: datetime | None = None,
    git=None,
    worktree: str | None = None,
) -> Ficha:
    """
    Reclama una tarea para trabajarla. Toma ATÓMICA desde A3.1.

    Cuando varios trabajadores compiten por la MISMA tarea, exactamente uno
    obtiene la toma; los demás reciben `ErrorToma`, que describe quién la
    tiene y en qué estado quedó. Dos tomas nunca se conceden a la vez.

    Desde A3.2 la toma además CONCEDE una generación de propiedad, que es
    lo que permite que el propietario resultante sobreviva a lo que hagan
    después las demás órdenes: todas escriben con `persistir`, cuyo UPDATE
    lleva ahora la precondición en el WHERE.

    Aquí se aplica además la regla de un solo escritor: si otra tarea activa
    declara un ámbito que se solapa, la toma se rechaza.

    Cómo se garantiza
    -----------------
    Todo lo que decide la toma ocurre dentro de UNA sola transacción
    `BEGIN IMMEDIATE` sobre la base global:

        comprobación de estado  ->  comprobación de ámbitos
        ->  UPDATE condicional  ->  evento  ->  COMMIT

    El UPDATE lleva el estado esperado en su WHERE y la decisión se toma con
    `rowcount` (ver `estado_global.reclamar`). Si algo falla en medio, el
    ROLLBACK deshace la toma entera: no quedan tomas a medias.

    La comprobación de estado que abre la secuencia SÍ deniega: al leerse
    la fila dentro de esta misma transacción, el perdedor de una carrera ya
    ve el estado que dejó el ganador y se rechaza aquí, con el motivo
    verdadero. Sin ella, una tarea aprobada o bloqueada cuyo ámbito además
    se solapara se rechazaría por "ámbito en conflicto" y quien la pidiera
    esperaría a que se liberase un ámbito que no la desbloquearía nunca.

    Lo que NO hace es conceder: eso lo decide el UPDATE condicional, que
    queda como red de seguridad del primitivo. Y no abre ninguna ventana,
    porque decide sobre una fila leída con el bloqueo de escritura ya
    tomado.

    Lo que NO está dentro de la transacción es deliberado: leer el árbol de
    trabajo y consultar Git son esperas de disco, y sostener el bloqueo de
    escritura mientras tanto castigaría a todos los demás trabajadores. Nada
    de eso decide la toma; sólo aporta datos que el UPDATE vuelve a validar.

    El espejo JSON se regenera DESPUÉS del COMMIT, a partir de la fila que
    la propia transacción confirmó.

    Un rechazo no cambia el estado operativo de ninguna tarea: ni el
    estado, ni el propietario, ni los intentos. Sí puede haber quedado
    antes el trabajo de incorporación que hacen `cargar` y
    `sincronizar_lista`, que registran en la base las fichas JSON que
    todavía no conocía. Eso es bootstrap de definiciones, no la toma, y
    ocurre igual aunque nadie reclame nada.
    """
    ficha = cargar(raiz, identificador)

    # Sin ámbito declarado no hay forma de garantizar un solo escritor.
    if not ficha.ambito_archivos:
        raise ErrorSupervisor(
            "La ficha no declara ningún ámbito de archivos. Sin ámbito no "
            "se puede garantizar la regla de un solo escritor."
        )

    invalidos = [
        patron
        for patron in ficha.ambito_archivos
        if not patron_es_relativo(patron)
    ]

    if invalidos:
        raise ErrorSupervisor(
            "El ámbito debe expresarse en rutas relativas a la raíz del "
            "repositorio. Patrones inválidos: " + ", ".join(invalidos) + "."
        )

    # El worktree se valida ANTES de abrir la transacción, porque mirar el
    # sistema de archivos y preguntarle a Git son esperas de disco y no
    # deben hacerse con el bloqueo de escritura tomado. Si la ruta no vale,
    # la toma ni se intenta.
    if worktree:
        arbol_declarado = str(resolver_worktree(raiz, worktree))
    elif ficha.worktree:
        # Heredado de una ejecución anterior. Se valida IGUAL que el
        # declarado: si no, una retoma después de recuperar concedería la
        # tarea sobre un árbol que ya no existe, y el fallo aparecería
        # mucho más tarde, al verificar, con el trabajo ya hecho. Quien
        # quiera trabajarla en otro sitio lo declara con `--worktree`.
        arbol_declarado = str(resolver_worktree(raiz, ficha.worktree))
    else:
        arbol_declarado = None

    momento = (ahora or ahora_datetime()).isoformat(timespec="seconds")

    aspirante = trabajador_id or nuevo_trabajador_id()
    proceso = pid if pid is not None else os.getpid()

    rama = ficha.rama or PREFIJO_RAMA + ficha.id

    commit_inicial = ficha.commit_inicial

    if git is not None and commit_inicial is None:
        commit_inicial = git.hash_actual()

    # El árbol de trabajo y Git se leen ANTES de abrir la transacción.
    definiciones, ilegibles = listar_con_errores(raiz)

    with global_.conexion(raiz) as con:
        # `solo_importar`: se incorporan las tareas que la base todavía no
        # conoce (sin ellas, la comprobación de ámbitos se interrumpiría),
        # pero NO se refresca la definición de las que ya están.
        #
        # Refrescarlas reescribiría `ambito_archivos`, que es justo el dato
        # del que depende la regla de un solo escritor, y lo haría con la
        # definición de ESTA rama aunque la tarea esté en ejecución en otro
        # worktree con otro ámbito. La toma siguiente ya no vería el
        # solapamiento y dos trabajadores acabarían escribiendo los mismos
        # archivos. El refresco por huella es de `sincronizar-definiciones`.
        #
        # La definición de la tarea que se toma sí está al día: `cargar`
        # la sincronizó al principio.
        global_.sincronizar_lista(con, definiciones, solo_importar=True)

        with global_.transaccion(con):
            filas = global_.listar_tareas(con)

            # Estado previo tal como lo ve ESTA transacción. Sólo sirve
            # para explicar el rechazo y documentar el evento: quien
            # concede la toma es el WHERE del UPDATE, no esta lectura.
            previa = next(
                (fila for fila in filas if fila["id"] == ficha.id), None
            )

            # Si el estado ya no admite toma, se dice ESO y no otra cosa.
            # Sin esta comprobación, una tarea aprobada o rechazada cuyo
            # ámbito además se solape se rechazaría por "ámbito en
            # conflicto", mandando a quien la pidió a esperar a que se
            # libere un ámbito que no la desbloquearía nunca.
            #
            # No reintroduce ninguna ventana: se decide sobre la fila leída
            # DENTRO de la transacción, y la toma la sigue concediendo el
            # UPDATE condicional, no esta lectura.
            if previa is not None and previa["estado"] not in {
                str(estado) for estado in ESTADOS_TOMABLES
            }:
                raise ErrorToma(
                    global_.rechazo(
                        previa, ficha.id, aspirante, momento, ESTADOS_TOMABLES
                    )
                )

            # Ámbito que esta toma va a reclamar de verdad.
            #
            # Si el estado del que se viene RETENÍA ámbito, la tarea
            # conserva cambios sin confirmar en el árbol de trabajo sobre
            # los archivos que tenía grabados. Tomarla otra vez con un
            # ámbito ENCOGIDO liberaría ese terreno sin que los cambios se
            # hayan ido a ninguna parte, y otra tarea podría entrar en él:
            # dos escritores sobre los mismos archivos.
            #
            # Por eso se reclama la UNIÓN de lo declarado y lo retenido.
            # Ampliar sí se permite —el ámbito nuevo se valida aquí mismo
            # contra las demás—; encoger no libera nada mientras la
            # retención siga en pie. El ámbito encogido se aplicará solo
            # cuando la tarea deje de retener, por la sincronización normal.
            ambito_reclamado = list(ficha.ambito_archivos)

            if previa is not None and previa["estado"] in {
                str(estado) for estado in ESTADOS_QUE_RETIENEN_AMBITO
            }:
                retenido = list(previa["ambito_archivos"] or [])
                ambito_reclamado = ambito_reclamado + [
                    patron for patron in retenido
                    if patron not in ambito_reclamado
                ]

            conflictos = conflictos_de_ambito(
                ficha, filas, ilegibles, ambito=ambito_reclamado
            )

            if conflictos:
                detalle = "; ".join(
                    uno["tarea"]
                    + " ("
                    + ", ".join(
                        par["propio"] + " <-> " + par["ajeno"]
                        for par in uno["pares"]
                    )
                    + ")"
                    for uno in conflictos
                )

                raise ErrorSolapamiento(
                    "Ámbito en conflicto con tareas activas: "
                    + detalle
                    + ". Un solo escritor por archivo."
                )

            informe = global_.reclamar(
                con,
                ficha.id,
                trabajador_id=aspirante,
                pid=proceso,
                momento=momento,
                estados_reclamables=ESTADOS_TOMABLES,
                estado_destino=str(Estado.EN_EJECUCION),
                campos_extra={
                    "rama": rama,
                    "commit_inicial": commit_inicial,
                    "actualizado_en": ahora_utc(),
                    # El ámbito que esta misma transacción acaba de validar
                    # contra todas las demás tareas activas.
                    #
                    # Hace falta porque `requiere_revision` es el único
                    # estado que está a la vez en ESTADOS_TOMABLES y en
                    # ESTADOS_QUE_RETIENEN_AMBITO: una tarea ahí puede tener
                    # el ámbito CONGELADO —la definición del árbol dice una
                    # cosa y la fila otra— y ser tomable al mismo tiempo. Sin
                    # esta línea, la toma concedía la propiedad sobre el
                    # ámbito declarado mientras la base seguía guardando el
                    # viejo, y la siguiente toma comprobaba el solapamiento
                    # contra un ámbito que ya no era el que nadie usaba:
                    # dos escritores sobre los mismos archivos.
                    #
                    # Grabarlo aquí es coherente con la guarda: congelar
                    # protege a una ejecución VIVA de que le cambien el
                    # terreno debajo; una toma nueva es justamente el momento
                    # en que empieza otra ejecución, y su ámbito acaba de
                    # comprobarse dentro de esta transacción.
                    # Exactamente lo que se acaba de validar arriba, ni
                    # más ni menos: grabar otra cosa dejaría la fila
                    # diciendo algo que nadie comprobó.
                    "ambito_archivos": global_._a_json(ambito_reclamado),
                    # El árbol donde esta ejecución va a trabajar, ya
                    # validado. Queda grabado con la toma porque pertenece a
                    # la ejecución, no a la definición de la tarea.
                    "worktree": arbol_declarado,
                },
            )

            if informe["resultado"] != global_.CLAIM_OTORGADO:
                raise ErrorToma(informe)

            evento = {
                "fecha": momento,
                "tipo": global_.EVENTO_TRANSICION,
                "estado_anterior": previa["estado"] if previa else None,
                "estado_nuevo": str(Estado.EN_EJECUCION),
                "motivo": "Tarea tomada por " + aspirante + ".",
                "origen": ORIGEN_AUTOMATICO,
                "datos": {"trabajador_id": aspirante, "pid": proceso},
            }

            # Una tarea reclamable no debería conservar propietario. Si lo
            # conserva (ficha V1 importada a medias), la toma lo desplaza y
            # lo deja anotado: todo cambio tiene que ser trazable.
            if previa and previa.get("trabajador_id"):
                evento["datos"]["propietario_desplazado"] = previa[
                    "trabajador_id"
                ]

            global_.insertar_evento(con, ficha.id, evento)

            # La fila se lee DENTRO de la transacción, no después: así lo
            # que se devuelve es exactamente lo que el COMMIT confirmó. Si
            # se leyera fuera, otra operación podría colarse en medio y
            # `tomar` devolvería una ficha que ya no es de quien la pidió.
            fila = global_.obtener_tarea(con, ficha.id)

    global_.aplicar_fila(ficha, fila)

    # El historial del JSON no se reconstruye desde SQLite: se le añade el
    # evento que la transacción acaba de confirmar.
    ficha.registrar_evento(evento)
    ficha.eventos_pendientes.clear()

    _regenerar_espejo(raiz, ficha)

    _registrar_en_git(git, ficha, "Tarea tomada.")

    return ficha


def latido(
    raiz: Path,
    identificador: str,
    ahora: datetime | None = None,
    trabajador_id: str | None = None,
    generacion: int | None = None,
) -> Ficha:
    """
    Señal de vida del trabajador que sostiene la tarea.

    Es una orden manual. Los latidos automáticos pertenecen a A3.3/B.

    A3.2: sólo la escribe el propietario VIGENTE. Un latido rezagado del
    dueño anterior es el caso más peligroso de todos, porque `persistir`
    reescribe también `trabajador_id`, `pid` e `iniciado_en`: sin condición
    resucitaría a un propietario ya desplazado.
    """
    ficha = cargar(raiz, identificador)

    if ficha.estado != Estado.EN_EJECUCION:
        raise ErrorSupervisor(
            "Sólo una tarea en ejecución puede emitir latido."
        )

    propietario, esperada = credencial_de(
        ficha, "latido", trabajador_id, generacion
    )

    ficha.ultimo_latido = (
        ahora or ahora_datetime()
    ).isoformat(timespec="seconds")

    persistir(
        raiz,
        ficha,
        exigir_propietario=propietario,
        estados_admitidos={Estado.EN_EJECUCION},
        exigir_generacion=esperada,
        campos_propios=CAMPOS_LATIDO,
    )

    return ficha


def devolver(
    raiz: Path,
    identificador: str,
    motivo: str = "Tarea devuelta por el trabajador.",
    git=None,
    trabajador_id: str | None = None,
    generacion: int | None = None,
) -> Ficha:
    """
    El trabajador suelta la tarea sin haberla terminado.

    A3.2: sólo la devuelve el propietario VIGENTE. Una devolución rezagada
    dejaría la tarea en REABIERTO —es decir, TOMABLE— quitándosela al dueño
    actual sin que él se entere.
    """
    ficha = cargar(raiz, identificador)

    if ficha.estado != Estado.EN_EJECUCION:
        raise ErrorSupervisor(
            "Sólo se puede devolver una tarea en ejecución."
        )

    # Antes de liberar: después, la ficha ya no sabe de quién era.
    propietario, esperada = credencial_de(
        ficha, "devolver", trabajador_id, generacion
    )

    _liberar_trabajador(ficha)

    transicionar(ficha, Estado.REABIERTO, motivo, ORIGEN_AUTOMATICO)

    persistir(
        raiz,
        ficha,
        exigir_propietario=propietario,
        estados_admitidos={Estado.EN_EJECUCION},
        exigir_generacion=esperada,
        campos_propios=CAMPOS_DEVOLVER,
    )

    _registrar_en_git(git, ficha, motivo)

    return ficha


def _liberar_trabajador(ficha: Ficha) -> None:
    ficha.trabajador_id = None
    ficha.pid = None
    ficha.iniciado_en = None
    ficha.ultimo_latido = None


def verificar(
    raiz: Path,
    identificador: str,
    tiempo_limite_s: int = corredor.TIEMPO_LIMITE_S,
    ejecutable: str | None = None,
    git=None,
    trabajador_id: str | None = None,
    generacion: int | None = None,
    intervalo_latido_s: float | None = None,
) -> dict:
    """
    Corre el filtro completo y decide el estado resultante.

    Verde:  todas las pruebas del repositorio en OK, todas las pruebas
            requeridas presentes y en OK, y ninguna decisión humana
            pendiente               ->  PROPUESTO
    Rojo:   cualquier problema de pruebas
            ->  REQUIERE_REVISION, o BLOQUEADO al agotar los intentos
    Ámbar:  pruebas verdes pero con decisión humana pendiente
            ->  REQUIERE_REVISION, sin consumir un intento

    A3.3: las pruebas se ejecutan en el worktree REGISTRADO de la tarea, y
    el resultado guarda dónde se ejecutó —árbol, rama y commit— además de
    comprobar que el árbol no se movió mientras corrían.
    """
    ficha = cargar(raiz, identificador)

    if ficha.estado != Estado.EN_EJECUCION:
        raise ErrorSupervisor(
            "Sólo se puede verificar una tarea en ejecución. Estado actual: '"
            + str(ficha.estado)
            + "'."
        )

    # Se acredita ANTES de correr las pruebas, que es la espera más larga
    # del sistema y por tanto la ventana más ancha para perder la tarea.
    propietario, esperada = credencial_de(
        ficha, "verificar", trabajador_id, generacion
    )

    # A3.3 — LA RAÍZ DE EJECUCIÓN ES LA DE LA TAREA, NO LA DEL MANDATO.
    #
    # Si la tarea declara un worktree, las pruebas se corren AHÍ, venga el
    # Supervisor invocado desde donde venga. Antes se corrían siempre sobre
    # `raiz`, de modo que una tarea que vivía en el worktree X y se
    # verificaba desde main ejecutaba las pruebas de main y grababa ese
    # resultado como si fuera el suyo: un verde que no dice nada del
    # trabajo que se estaba juzgando.
    arbol = resolver_worktree(raiz, ficha.worktree)

    testigo = Git(arbol)

    # Correr la batería entera es la espera más larga del sistema. Sin
    # latidos, una verificación de diez minutos deja la tarea sin señal todo
    # ese rato y la recuperación la ve caducada: el trabajo honesto parece
    # abandono. El acompañante late mientras dura y se para solo al salir,
    # también si la corrida lanza.
    acompanante = LatidoAutomatico(
        raiz,
        ficha.id,
        propietario,
        esperada,
        intervalo_s=intervalo_latido_s,
    )

    # La evidencia se toma ANTES de correr y se vuelve a tomar después.
    #
    # Leerla sólo al final era una afirmación falsa esperando a ocurrir:
    # entre el arranque de la batería (hasta dos minutos por archivo) y la
    # lectura del commit cabe cualquier `commit`, `rebase` o `checkout` del
    # propio trabajador que sigue trabajando en ese árbol. Quedaba grabado
    # «commit X, todo en verde» cuando X nunca se ejecutó y encima estaba
    # rojo.
    rama_inicio = testigo.rama_actual()
    commit_inicio = testigo.hash_actual()
    sucio_inicio = testigo.hay_cambios_sin_confirmar()

    with acompanante:
        corrida = corredor.ejecutar_todas(arbol, tiempo_limite_s, ejecutable)

    # Si durante la corrida la tarea cambió de manos, el acompañante lo
    # detectó antes que nadie. Se dice aquí y no se disimula: el resultado
    # de esa corrida ya no pertenece a nadie.
    if acompanante.propiedad_perdida:
        raise ErrorPropiedad(
            {
                "tarea": ficha.id,
                "motivo": (
                    acompanante.motivo or global_.MOTIVO_GENERACION_VENCIDA
                ),
                "detalle": "La tarea dejó de ser de '" + str(propietario)
                + "' MIENTRAS se verificaba, así que el resultado de esa "
                "corrida no es de nadie. " + str(acompanante.detalle),
                "propietario": propietario,
                "generacion": esperada,
            }
        )

    # La evidencia de DÓNDE se ejecutó viaja con el resultado. Sin esto,
    # dos corridas idénticas de árboles distintos son indistinguibles en el
    # historial, y no se puede auditar después si se verificó lo correcto.
    corrida["raiz"] = str(arbol)

    try:
        corrida["es_worktree"] = arbol != Path(raiz).resolve()
    except OSError:
        corrida["es_worktree"] = arbol != Path(raiz)

    rama_fin = testigo.rama_actual()
    commit_fin = testigo.hash_actual()
    sucio_fin = testigo.hay_cambios_sin_confirmar()

    corrida["rama"] = rama_inicio
    corrida["commit"] = commit_inicio
    corrida["commit_final"] = commit_fin
    corrida["generacion"] = esperada
    corrida["trabajador_id"] = propietario

    # Un árbol que cambió a mitad invalida la corrida entera: no se sabe qué
    # se ejecutó. Se dice, y no se graba un verde que nadie puede reproducir.
    corrida["arbol_estable"] = (
        commit_inicio == commit_fin
        and rama_inicio == rama_fin
        and sucio_inicio == sucio_fin
    )

    if not corrida["arbol_estable"]:
        raise ErrorWorktree(
            "El árbol '" + str(arbol) + "' cambió MIENTRAS corrían las "
            "pruebas (antes: " + str(rama_inicio) + " @ "
            + str(commit_inicio) + (", con cambios sin confirmar"
                                    if sucio_inicio else "")
            + "; después: " + str(rama_fin) + " @ " + str(commit_fin)
            + (", con cambios sin confirmar" if sucio_fin else "")
            + "). El resultado no corresponde a ningún estado concreto del "
            "árbol, así que no se graba. Repite la verificación con el "
            "árbol quieto."
        )

    consume_intento = False

    problemas = []

    if corrida["resultado"] != corredor.RESULTADO_APROBADO:
        problemas.append(
            corrida.get("motivo", "La corrida de pruebas no quedó aprobada.")
        )

    problemas.extend(
        corredor.problemas_de_pruebas_requeridas(
            corrida, ficha.pruebas_requeridas
        )
    )

    if not ficha.pruebas_requeridas:
        problemas.append(
            "La ficha no declara ninguna prueba requerida: una tarea no "
            "puede proponerse sin prueba propia."
        )

    # Las decisiones se releen DESPUÉS de la corrida, no antes.
    #
    # `decidir` no mueve ni el estado ni la generación, así que una
    # resolución llegada mientras corrían las pruebas no invalida la
    # escritura: el veredicto se dictaba con la foto de hace minutos. El
    # resultado era una fila que se contradecía a sí misma —todas las
    # decisiones resueltas y un `ultima_falla` diciendo que faltaban— y una
    # batería completa tirada a la basura.
    # También se relee lo DECLARADO en el archivo: una decisión humana
    # recién escrita por una persona mientras corrían las pruebas es un
    # freno, y con la foto vieja la tarea se iba a PROPUESTO saltándose
    # justo la decisión que esa persona quería forzar.
    declaradas = list(ficha.requiere_decision_humana)

    try:
        declaradas = leer(raiz, ficha.id).requiere_decision_humana
    except (ErrorFicha, OSError):
        pass

    with global_.conexion(raiz) as con:
        actual = global_.obtener_tarea(con, ficha.id)

    ficha.requiere_decision_humana = global_.fusionar_decisiones(
        declaradas,
        global_.decisiones_operativas(ficha.requiere_decision_humana)
        + list((actual or {}).get("decisiones") or []),
    )

    pendientes = ficha.decisiones_pendientes()

    corrida["problemas"] = list(problemas)
    ficha.registrar_ejecucion(corrida)

    ficha.registrar_evento(
        {
            "fecha": corrida["fecha"],
            "tipo": global_.EVENTO_VERIFICACION,
            "estado_anterior": str(ficha.estado),
            "estado_nuevo": str(ficha.estado),
            "motivo": "Verificación: "
            + str(corrida["resultado"])
            + " ("
            + str(corrida["ok"])
            + " de "
            + str(corrida["total"])
            + " pruebas en OK).",
            "origen": ORIGEN_AUTOMATICO,
            "datos": global_.resumen_de_corrida(corrida),
        }
    )

    if problemas:
        # El intento lo incrementa el MOTOR en el propio UPDATE
        # (`incrementos`), no Python: sumar uno sobre una lectura anterior
        # es un lost update en cuanto haya dos verificaciones. Aquí sólo se
        # anota que esta orden lo consume, y el valor real se relee de la
        # fila confirmada.
        consume_intento = True
        ficha.intentos = ficha.intentos + 1

        ficha.ultima_falla = {
            "fecha": corrida["fecha"],
            "intento": ficha.intentos,
            "problemas": list(problemas),
        }

        if ficha.intentos >= ficha.max_intentos:
            destino = Estado.BLOQUEADO
            motivo = (
                "Se agotaron los "
                + str(ficha.max_intentos)
                + " intentos permitidos. Requiere intervención humana."
            )
        else:
            destino = Estado.REQUIERE_REVISION
            motivo = (
                "Intento "
                + str(ficha.intentos)
                + " de "
                + str(ficha.max_intentos)
                + ": "
                + problemas[0]
            )

    elif pendientes:
        destino = Estado.REQUIERE_REVISION

        claves = ", ".join(str(una.get("clave")) for una in pendientes)

        motivo = (
            "Pruebas en verde, pero hay decisiones humanas sin resolver: "
            + claves
            + "."
        )

        ficha.ultima_falla = {
            "fecha": corrida["fecha"],
            "intento": ficha.intentos,
            "problemas": [motivo],
        }

    else:
        destino = Estado.PROPUESTO
        motivo = (
            "Todas las pruebas en OK ("
            + str(corrida["ok"])
            + " de "
            + str(corrida["total"])
            + "). Queda a la espera de aprobación humana."
        )
        ficha.ultima_falla = None

    # El turno del trabajador terminó. El ámbito sigue retenido por el
    # ESTADO de la tarea, no por la identidad de quien la trabajó, así que
    # no queda ningún PID fantasma en el tablero.
    _liberar_trabajador(ficha)

    transicionar(ficha, destino, motivo, ORIGEN_AUTOMATICO)

    persistir(
        raiz,
        ficha,
        exigir_propietario=propietario,
        estados_admitidos={Estado.EN_EJECUCION},
        exigir_generacion=esperada,
        campos_propios=CAMPOS_VERIFICAR,
        incrementos=("intentos",) if consume_intento else (),
    )

    registro = _registrar_en_git(git, ficha, motivo)

    return {
        "ficha": ficha,
        "corrida": corrida,
        "problemas": problemas,
        "decisiones_pendientes": pendientes,
        "estado": str(ficha.estado),
        "motivo": motivo,
        "git": registro,
        "raiz": str(arbol),
        "latidos": acompanante.emitidos,
        # Que el latido muriera no invalida la corrida, pero tiene que
        # verse: hasta ahora se calculaba y se tiraba, y una verificación
        # larga sin ninguna señal acababa pareciendo una tarea abandonada.
        "latido_error": acompanante.error,
        "latido_cierre_incompleto": acompanante.cierre_incompleto,
        "es_worktree": corrida["es_worktree"],
        "rama": corrida["rama"],
        "commit": corrida["commit"],
        "commit_final": corrida["commit_final"],
        "arbol_estable": corrida["arbol_estable"],
    }


def decidir(
    raiz: Path,
    identificador: str,
    clave: str,
    resolucion: str,
) -> Ficha:
    """
    Resuelve explícitamente una decisión humana pendiente.

    La definición de la decisión (clave, descripción) vive en el JSON; su
    resolución (resuelta, resolución, fecha, origen) queda en SQLite.

    La resolución la hace el MOTOR, dentro de la transacción (A3.3). Antes
    se leía la lista de decisiones, se cambiaba un elemento en Python y se
    reescribía la columna entera: dos `decidir` sobre claves distintas se
    pisaban y una resolución humana volvía a «pendiente» sin error y sin
    rastro. Medido: 24 de 25 carreras entre dos procesos perdían una.

    No genera commit automático: no es una transición de estado.
    """
    ficha = cargar(raiz, identificador)

    momento = ahora_utc()

    with global_.conexion(raiz) as con:
        with global_.transaccion(con):
            informe = global_.resolver_decision(
                con,
                ficha.id,
                clave,
                resolucion,
                momento,
                ORIGEN_HUMANO,
                declaradas=ficha.requiere_decision_humana,
            )

            if not informe["resuelta"]:
                if informe["motivo"] == "inexistente":
                    raise ErrorSupervisor(
                        "La tarea no tiene ninguna decisión con clave '"
                        + str(clave)
                        + "'."
                    )

                raise ErrorSupervisor(
                    "La decisión '" + str(clave) + "' ya estaba resuelta."
                )

            global_.insertar_evento(
                con,
                ficha.id,
                {
                    "fecha": momento,
                    "tipo": global_.EVENTO_DECISION,
                    "estado_anterior": str(ficha.estado),
                    "estado_nuevo": str(ficha.estado),
                    "motivo": "Decisión humana '" + str(clave)
                    + "' resuelta.",
                    "origen": ORIGEN_HUMANO,
                    "datos": {
                        "clave": str(clave),
                        "resolucion": resolucion,
                    },
                },
            )

            confirmada = global_.obtener_tarea(con, ficha.id)

    historial = list(ficha.historial)
    global_.aplicar_fila(ficha, confirmada)
    ficha.historial = historial

    ficha.requiere_decision_humana = informe["decisiones"]

    ficha.registrar_evento(
        {
            "fecha": momento,
            "tipo": global_.EVENTO_DECISION,
            "estado_anterior": str(ficha.estado),
            "estado_nuevo": str(ficha.estado),
            "motivo": "Decisión humana '" + str(clave) + "' resuelta.",
            "origen": ORIGEN_HUMANO,
            "datos": {"clave": str(clave), "resolucion": resolucion},
        }
    )
    ficha.eventos_pendientes.clear()

    # Resolver una decisión no es una transición de estado, y el commit
    # automático está reservado a las transiciones. El espejo sí se
    # regenera: es lo que hace visible la resolución en el archivo.
    _regenerar_espejo(raiz, ficha)

    return ficha


def aprobar(
    raiz: Path,
    identificador: str,
    comentario: str = "",
    git=None,
) -> Ficha:
    """Aprobación humana. El Supervisor nunca puede ejecutar esto solo."""
    ficha = cargar(raiz, identificador)

    if ficha.estado != Estado.PROPUESTO:
        raise ErrorSupervisor(
            "Sólo se puede aprobar una tarea en estado 'propuesto'. "
            "Estado actual: '" + str(ficha.estado) + "'."
        )

    if ficha.tiene_decisiones_pendientes():
        claves = ", ".join(
            str(una.get("clave")) for una in ficha.decisiones_pendientes()
        )

        raise ErrorSupervisor(
            "No se puede aprobar con decisiones humanas sin resolver: "
            + claves
            + "."
        )

    transicionar(
        ficha,
        Estado.APROBADO,
        comentario or "Aprobación humana.",
        ORIGEN_HUMANO,
    )

    persistir(raiz, ficha, campos_propios=CAMPOS_TRANSICION)

    _registrar_en_git(git, ficha, "Aprobación humana.")

    return ficha


def rechazar(
    raiz: Path,
    identificador: str,
    motivo: str,
    git=None,
) -> Ficha:
    """Rechazo humano explícito."""
    if not (motivo or "").strip():
        raise ErrorSupervisor("El rechazo exige un motivo.")

    ficha = cargar(raiz, identificador)

    _liberar_trabajador(ficha)

    transicionar(ficha, Estado.RECHAZADO, motivo, ORIGEN_HUMANO)

    persistir(raiz, ficha, campos_propios=CAMPOS_DEVOLVER)

    _registrar_en_git(git, ficha, motivo)

    return ficha


def reabrir(
    raiz: Path,
    identificador: str,
    motivo: str = "Reapertura humana.",
    git=None,
) -> Ficha:
    """
    Devuelve una tarea al circuito tras una intervención humana.

    La reapertura devuelve además el presupuesto completo de intentos: si
    no se reiniciara, una tarea desbloqueada a mano volvería a bloquearse
    en la siguiente verificación.
    """
    ficha = cargar(raiz, identificador)

    _liberar_trabajador(ficha)

    ficha.intentos = 0

    transicionar(ficha, Estado.REABIERTO, motivo, ORIGEN_HUMANO)

    persistir(raiz, ficha, campos_propios=CAMPOS_REABRIR)

    _registrar_en_git(git, ficha, motivo)

    return ficha


def bloquear(
    raiz: Path,
    identificador: str,
    motivo: str,
    origen: str = ORIGEN_HUMANO,
    git=None,
) -> Ficha:
    """Marca un bloqueo real que exige intervención humana."""
    if not (motivo or "").strip():
        raise ErrorSupervisor("El bloqueo exige un motivo.")

    ficha = cargar(raiz, identificador)

    _liberar_trabajador(ficha)

    transicionar(ficha, Estado.BLOQUEADO, motivo, origen)

    persistir(raiz, ficha, campos_propios=CAMPOS_DEVOLVER)

    _registrar_en_git(git, ficha, motivo)

    return ficha


# ----------------------------------------------------------------------
# Recuperación tras cierre, cambio de sesión o apagón
# ----------------------------------------------------------------------

class LatidoAutomatico:
    """
    Emite latidos mientras dura una operación larga del Supervisor (A3.3).

    A3.2 hizo segura la orden `latido`; alguien tenía que emitirla. Sin
    esto, una verificación que tarda diez minutos deja la tarea sin señal
    todo ese rato y la recuperación la ve caducada: el trabajo honesto
    parece abandono.

    Esto NO es un demonio de trabajadores. Es un acompañante de UNA
    operación concreta, que empieza y termina con ella. El lanzamiento de
    trabajadores es C.

    Lo que garantiza, y por qué cada cosa
    -------------------------------------
    - Escribe SÓLO `ultimo_latido`, y con las precondiciones de A3.2:
      identidad, generación y estado EN_EJECUCION. Un latido no puede
      resucitar nada: si la ejecución terminó o la tarea cambió de manos,
      la escritura se rechaza sola.

    - Se PARA si pierde la propiedad, y lo deja anotado. Seguir latiendo
      sobre una tarea ajena sería sostener artificialmente una ejecución
      que ya no existe, y haría que la recuperación creyera viva a una
      tarea muerta: justo al revés de para lo que sirve.

    - Se para siempre al terminar la operación, salga bien o mal, porque
      es un gestor de contexto y `__exit__` corre también cuando el
      trabajo principal lanza.

    - No mantiene ninguna conexión SQLite abierta entre latidos: abre,
      escribe y cierra. Mantenerla abierta durante minutos estorbaría a
      todos los demás para ahorrar una apertura que cuesta microsegundos.

    - La espera es cancelable: se usa un `Event.wait`, que vuelve en el
      acto cuando se pide parar, y no un `sleep` que habría que aguantar
      entero. Por eso una prueba puede usar intervalos de milisegundos sin
      quedarse esperando nada.
    """

    def __init__(
        self,
        raiz: Path,
        identificador: str,
        trabajador_id: str,
        generacion: int,
        intervalo_s: float = None,
        reloj=None,
    ):
        self.raiz = Path(raiz)
        self.identificador = identificador
        self.trabajador_id = trabajador_id
        self.generacion = int(generacion)
        self.intervalo_s = (
            INTERVALO_LATIDO_S if intervalo_s is None else float(intervalo_s)
        )

        # Un intervalo de cero o negativo convierte `Event.wait` en un bucle
        # apretado: miles de `BEGIN IMMEDIATE` por segundo sobre la base
        # COMPARTIDA, que es una denegación de servicio para el resto de
        # trabajadores. Se rechaza en vez de dejarlo pasar.
        if self.intervalo_s <= 0:
            raise ErrorSupervisor(
                "El intervalo del latido debe ser mayor que cero; se "
                "recibió: " + repr(intervalo_s) + "."
            )

        self._reloj = reloj or ahora_utc

        self.emitidos = 0
        self.propiedad_perdida = False
        self.detalle = None
        self.motivo = None
        self.error = None
        self.fallos_seguidos = 0
        self.fallos_totales = 0
        self.retrocesos = 0
        self.cierre_incompleto = False

        self._parar = threading.Event()
        self._hilo = None

    def emitir_uno(self) -> bool:
        """
        Un latido. Devuelve False cuando ya no hay que seguir.

        Público a propósito: una prueba puede emitir latidos uno a uno y
        comprobar el efecto sin depender de ningún tiempo de reloj.
        """
        try:
            con = global_.abrir(global_.ruta_base(self.raiz))

            try:
                # El instante se toma con el candado ya pedido y no antes:
                # bajo contención, `busy_timeout` puede hacer esperar
                # segundos, y grabar una marca tomada antes de esa espera
                # equivale a registrar una señal de vida ya rancia.
                momento = self._reloj()

                with global_.transaccion(con):
                    informe = global_.actualizar_si_propietario(
                        con,
                        self.identificador,
                        {"ultimo_latido": momento, "actualizado_en": momento},
                        generacion=self.generacion,
                        momento=momento,
                        trabajador_id=self.trabajador_id,
                        estados_admitidos={str(Estado.EN_EJECUCION)},
                        # El reloj de pared no es monótono. Sin esta guarda,
                        # un salto hacia atrás hacía que el propio latido
                        # REDUJERA la marca de vida y la tarea pareciese
                        # abandonada: el mecanismo que la defiende sería el
                        # que la mata.
                        exigir_no_retroceso={"ultimo_latido": momento},
                    )
            finally:
                con.close()
        except sqlite3.Error as error:
            # `database is locked` es un fallo TRANSITORIO y esperable justo
            # en el escenario para el que existe el latido: varios procesos
            # escribiendo a la vez. Rendirse al primero dejaba la operación
            # sin señal el resto del tiempo, en silencio, hasta que la
            # recuperación la declaraba huérfana.
            self.error = type(error).__name__ + ": " + str(error)
            self.fallos_seguidos += 1
            self.fallos_totales += 1

            return self.fallos_seguidos < FALLOS_LATIDO_SEGUIDOS
        except Exception as error:
            # Cualquier otra cosa no es transitoria: se anota y se para.
            self.error = type(error).__name__ + ": " + str(error)
            self.fallos_totales += 1

            return False

        self.fallos_seguidos = 0

        if informe["resultado"] != global_.ESCRITURA_ACEPTADA:
            # Un rechazo por la guarda de monotonía NO es perder la tarea:
            # significa que ya hay una marca igual o más nueva, que es
            # exactamente lo que el latido quería conseguir.
            if self._sigue_siendo_mio():
                self.retrocesos += 1

                return True

            self.propiedad_perdida = True
            self.detalle = informe["detalle"]
            self.motivo = informe.get("motivo")

            return False

        self.emitidos += 1

        return True

    def _sigue_siendo_mio(self) -> bool:
        """
        Relee la fila para distinguir «reloj atrasado» de «perdí la tarea».

        Sólo se llama tras un rechazo, que es raro: no está en el camino
        normal del latido.
        """
        try:
            con = global_.abrir(global_.ruta_base(self.raiz))

            try:
                fila = global_.obtener_tarea(con, self.identificador)
            finally:
                con.close()
        except Exception:
            return False

        if fila is None:
            return False

        return (
            fila.get("trabajador_id") == self.trabajador_id
            and fila.get("generacion") == self.generacion
            and str(fila.get("estado")) == str(Estado.EN_EJECUCION)
        )

    def _bucle(self) -> None:
        # Se late nada más entrar. Esperando primero, una operación más
        # corta que el intervalo (60 s por omisión) no dejaba ni una sola
        # señal, que es justo lo contrario de lo que se pretende.
        if not self.emitir_uno():
            return

        # `wait` devuelve True en cuanto se pide parar, así que el latido
        # se corta en el acto en vez de esperar a que venza el intervalo.
        while not self._parar.wait(self.intervalo_s):
            if not self.emitir_uno():
                return

    def __enter__(self) -> "LatidoAutomatico":
        if self._hilo is not None:
            # Reutilizar la instancia arrancaba un hilo que salía en la
            # primera vuelta —`_parar` seguía puesto del uso anterior— y
            # emitía CERO latidos sin ninguna señal de avería. Mejor un
            # error ruidoso que una garantía perdida en silencio.
            raise ErrorSupervisor(
                "Un LatidoAutomatico acompaña a UNA operación y no se "
                "reutiliza. Construye otro."
            )

        self._parar.clear()

        self._hilo = threading.Thread(
            target=self._bucle,
            name="latido-" + str(self.identificador),
            daemon=True,
        )

        try:
            self._hilo.start()
        except BaseException:
            # Si `__enter__` no retorna, `__exit__` NO se ejecuta nunca y el
            # hilo quedaría latiendo para siempre sobre una tarea que ya no
            # trabaja nadie: sostener artificialmente una ejecución muerta
            # es justo lo que esto no debe hacer.
            self._parar.set()
            raise

        return self

    def __exit__(self, *_excepcion) -> bool:
        self._parar.set()

        if self._hilo is not None:
            self._hilo.join(timeout=ESPERA_CIERRE_LATIDO_S)

            # Si el `join` se agotó, el hilo sigue vivo y escribirá después
            # de que este bloque haya devuelto el control. Queda anotado:
            # antes no quedaba ningún rastro y `emitidos` se leía en carrera
            # con un hilo que aún lo estaba tocando.
            self.cierre_incompleto = self._hilo.is_alive()

        # No se traga ninguna excepción del trabajo principal.
        return False


def vitalidad(
    fila: dict,
    ahora: datetime | None = None,
    comprobar_proceso=proceso_vivo,
    latido_maximo_s: int = LATIDO_MAXIMO_S,
    latido_gracia_s: int = LATIDO_GRACIA_S,
    latido_abandono_s: int = LATIDO_ABANDONO_S,
) -> dict:
    """
    Vitalidad de una tarea, para INFORMAR. No decide ni cambia nada.

    Devuelve uno de cinco estados, que son distintos del estado de la tarea:

        ACTIVA          hay una ejecución y da señales
        LATIDO_VENCIDO  hay una ejecución, el latido caducó, pero no está
                        demostrada muerta
        HUERFANA        hay una ejecución y está demostrada perdida
        REANUDABLE      no hay ejecución y la tarea se puede tomar
        FINALIZADA      no hay ejecución y la tarea no se puede tomar

    Separar esto de `clasificar_ejecucion` es deliberado: aquélla decide si
    la recuperación toca o no toca una tarea, y sólo mira las que están en
    ejecución. Ésta responde a "¿qué le pasa a esta tarea?" para cualquiera,
    que es lo que necesita el tablero.
    """
    ahora = ahora or ahora_datetime()

    estado = str(fila.get("estado") or "")
    latido = a_datetime(fila.get("ultimo_latido"))
    edad = None if latido is None else int((ahora - latido).total_seconds())

    informe = {
        "estado": estado,
        "trabajador_id": fila.get("trabajador_id"),
        "generacion": fila.get("generacion"),
        "pid": fila.get("pid"),
        "iniciado_en": fila.get("iniciado_en"),
        "ultimo_latido": fila.get("ultimo_latido"),
        "edad_latido_s": edad,
        "worktree": fila.get("worktree"),
        "vitalidad": None,
        "motivo": None,
        # Lo decide el motor, no la interfaz. LATIDO_VENCIDO y HUÉRFANA
        # piden que alguien mire; REANUDABLE y FINALIZADA, no: son el
        # estado normal de una tarea que nadie está ejecutando.
        "requiere_atencion": False,
    }

    if estado != str(Estado.EN_EJECUCION):
        tomable = estado in {str(uno) for uno in ESTADOS_TOMABLES}

        informe["vitalidad"] = (
            VITALIDAD_REANUDABLE if tomable else VITALIDAD_FINALIZADA
        )

        if tomable:
            detalle = "puede tomarse."
        elif estado in ESTADOS_QUE_ESPERAN_A_UNA_PERSONA:
            # PROPUESTO y BLOQUEADO no son finales: son las que MÁS piden
            # atención. Decir de ellas lo mismo que de APROBADO escondía en
            # el tablero justo lo que hay que mirar.
            detalle = "está esperando una decisión humana."
        else:
            detalle = "está cerrada."

        informe["motivo"] = "Sin ejecución en curso; la tarea " + detalle
        informe["requiere_atencion"] = (
            estado in ESTADOS_QUE_ESPERAN_A_UNA_PERSONA
        )

        return informe

    prestada = Ficha(id=str(fila.get("id") or "T-0000"), titulo="")
    prestada.estado = Estado.EN_EJECUCION
    prestada.trabajador_id = fila.get("trabajador_id")
    prestada.pid = fila.get("pid")
    prestada.iniciado_en = fila.get("iniciado_en")
    prestada.ultimo_latido = fila.get("ultimo_latido")

    clase, motivo = clasificar_ejecucion(
        prestada,
        ahora,
        comprobar_proceso,
        latido_maximo_s,
        latido_gracia_s,
        latido_abandono_s,
    )

    # HUÉRFANA significa «demostrada perdida». Una fila incompleta no
    # demuestra nada sobre el trabajador, así que INCONSISTENTE se informa
    # como LATIDO_VENCIDO —hay ejecución y no está demostrada muerta—, que
    # es la categoría de la duda. Y lo desconocido cae del lado seguro: si
    # mañana aparece otra clase, se informará como duda y no como ACTIVA,
    # que era el valor más tranquilizador y el peor por omisión.
    informe["vitalidad"] = {
        CLASE_ACTIVA: VITALIDAD_ACTIVA,
        CLASE_LATIDO_VENCIDO: VITALIDAD_LATIDO_VENCIDO,
        CLASE_HUERFANA: VITALIDAD_HUERFANA,
        CLASE_INCONSISTENTE: VITALIDAD_LATIDO_VENCIDO,
    }.get(clase, VITALIDAD_LATIDO_VENCIDO)
    informe["motivo"] = motivo
    informe["requiere_atencion"] = informe["vitalidad"] in (
        VITALIDAD_LATIDO_VENCIDO,
        VITALIDAD_HUERFANA,
    )

    return informe


def clasificar_ejecucion(
    ficha: Ficha,
    ahora: datetime,
    comprobar_proceso=proceso_vivo,
    latido_maximo_s: int = LATIDO_MAXIMO_S,
    latido_gracia_s: int = LATIDO_GRACIA_S,
    latido_abandono_s: int = LATIDO_ABANDONO_S,
) -> tuple[str, str]:
    """
    Clasifica una tarea EN_EJECUCION como ACTIVA, HUERFANA o INCONSISTENTE.

    Nunca se juzga por UNA sola señal, ni por el PID ni por el latido:

    - un latido vencido NO basta por sí solo: hace falta confirmarlo con el
      proceso, o que la antigüedad pase del umbral de abandono, que es
      holgado a propósito. Si no, se devuelve LATIDO_VENCIDO, que informa
      sin arrebatar;
    - la desaparición del proceso sólo cuenta si además el latido dejó de
      ser reciente, porque el proceso que reclamó la tarea puede haber sido
      un mandato breve de línea de comandos ya terminado;
    - si la ficha proviene de otro equipo, el PID local carece de sentido y
      se juzga únicamente por el latido.
    """
    if (
        not ficha.trabajador_id
        or ficha.pid is None
        or not ficha.iniciado_en
        or not ficha.ultimo_latido
    ):
        return (
            CLASE_INCONSISTENTE,
            "Ficha en ejecución sin identidad completa del trabajador "
            "(trabajador_id, pid, iniciado_en o ultimo_latido ausente).",
        )

    latido = a_datetime(ficha.ultimo_latido)

    if latido is None:
        return (
            CLASE_INCONSISTENTE,
            "El último latido registrado no es una fecha válida: '"
            + str(ficha.ultimo_latido)
            + "'.",
        )

    antiguedad = ahora - latido
    segundos = int(antiguedad.total_seconds())

    # Una marca en el FUTURO no es frescura: es un reloj mal puesto, una
    # hora local escrita sin zona horaria por otra máquina, o una fila
    # manipulada. Tratándola como reciente, la tarea quedaba ACTIVA para
    # siempre —la antigüedad negativa no supera ningún umbral—, bloqueando
    # su ámbito sin que ninguna recuperación pudiera tocarla nunca.
    if antiguedad < -timedelta(seconds=latido_gracia_s):
        return (
            CLASE_INCONSISTENTE,
            "El último latido está " + str(abs(segundos)) + " s en el "
            "FUTURO (" + str(ficha.ultimo_latido) + "). Reloj "
            "desincronizado o marca sin zona horaria: no se puede juzgar "
            "esta ejecución por el tiempo.",
        )

    equipo_ficha = equipo_de(ficha.trabajador_id)
    equipo_actual = socket.gethostname()

    # Ajeno significa "declara OTRA máquina", no "no declara ninguna".
    #
    # `equipo_de` devuelve None cuando el identificador no lleva equipo, y
    # tratar eso como ajeno sería un error con consecuencias: el PID local
    # dejaría de poder confirmar nada y ninguna ejecución con un
    # identificador libre podría recuperarse hasta el umbral de abandono.
    # Desconocido no es ajeno: es desconocido, y entonces la señal del
    # proceso sí vale.
    ajeno = equipo_ficha is not None and equipo_ficha != equipo_actual

    if antiguedad > timedelta(seconds=latido_maximo_s):
        # El latido caducó. Eso NO basta para declarar abandono: es una
        # señal débil, y decidir con ella sola es arrebatarle la tarea a
        # quien quizá sigue trabajando. Se busca una segunda.
        #
        # El umbral de abandono TAMPOCO decide solo. Antes sí lo hacía, y
        # con un `return` colocado por delante de todo: `comprobar_proceso`
        # ni se llegaba a llamar. Un trabajador local con su proceso vivo y
        # comprobable perdía la tarea por llevar una hora sin latir —que es
        # exactamente lo que pasa si el hilo del latido muere—, y un
        # trabajador remoto la perdía con que el reloj de su máquina fuera
        # una hora distinto. Un proceso vivo es una señal FUERTE que
        # contradice al latido: mientras exista, no hay abandono demostrado.
        if not ajeno and not comprobar_proceso(ficha.pid):
            return (
                CLASE_HUERFANA,
                "Latido vencido (" + str(segundos) + " s, máximo "
                + str(latido_maximo_s) + " s) Y el proceso "
                + str(ficha.pid) + " ya no existe: dos señales.",
            )

        if ajeno:
            # Aquí no hay segunda señal posible y no la habrá nunca: el PID
            # es de otra máquina. Se informa y decide una persona; liberar
            # por el reloj solo es justo lo que esta función no debe hacer.
            return (
                CLASE_LATIDO_VENCIDO,
                "Latido vencido (" + str(segundos) + " s, máximo "
                + str(latido_maximo_s) + " s) y el trabajador es de otro "
                "equipo (" + str(equipo_ficha) + "): aquí no se puede "
                "comprobar su proceso, así que no hay segunda señal y no se "
                "libera sola. Decide una persona (`reabrir`).",
            )

        return (
            CLASE_LATIDO_VENCIDO,
            "Latido vencido (" + str(segundos) + " s, máximo "
            + str(latido_maximo_s) + " s) pero el proceso "
            + str(ficha.pid) + " sigue vivo"
            + (
                ", y lleva más del umbral de abandono ("
                + str(latido_abandono_s) + " s) sin latir: probablemente su "
                "latido murió. Míralo"
                if antiguedad > timedelta(seconds=latido_abandono_s)
                else ""
            )
            + ". No se declara abandonada con una sola señal.",
        )

    if ajeno:
        return (
            CLASE_ACTIVA,
            "Trabajador de otro equipo ("
            + str(equipo_ficha)
            + ") con latido reciente: aquí no se puede comprobar su "
            "proceso, y el latido es la señal que sí vale.",
        )

    if comprobar_proceso(ficha.pid):
        return (CLASE_ACTIVA, "Trabajador vivo y con latido reciente.")

    # El proceso ya no está. Sólo cuenta como abandono si el latido tampoco
    # es reciente: un 'tomar' desde la línea de comandos deja siempre un PID
    # muerto, y eso por sí solo no significa que el trabajo se haya perdido.
    if antiguedad > timedelta(seconds=latido_gracia_s):
        return (
            CLASE_HUERFANA,
            "El proceso "
            + str(ficha.pid)
            + " del trabajador ya no existe y el latido tiene "
            + str(int(antiguedad.total_seconds()))
            + " s (margen de cortesía "
            + str(latido_gracia_s)
            + " s).",
        )

    return (
        CLASE_ACTIVA,
        "El proceso que reclamó la tarea ya terminó, pero el latido es de "
        "hace " + str(int(antiguedad.total_seconds())) + " s: se respeta "
        "dentro del margen de cortesía.",
    )


def reanudar(
    raiz: Path,
    ahora: datetime | None = None,
    comprobar_proceso=proceso_vivo,
    latido_maximo_s: int = LATIDO_MAXIMO_S,
    latido_gracia_s: int = LATIDO_GRACIA_S,
    git=None,
    latido_abandono_s: int = LATIDO_ABANDONO_S,
) -> dict:
    """
    Recuperación tras un cierre, un cambio de sesión o un apagón.

    Ninguna tarea se pierde: las ejecuciones interrumpidas se registran como
    tales, conservando el historial, y la tarea vuelve a REABIERTO.

    A2: es una orden manual que juzga por PID y por el latido registrado.
    La detección avanzada de trabajadores huérfanos y los latidos
    automáticos pertenecen a A3/B.
    """
    ahora = ahora or ahora_datetime()

    informe = {
        "fecha": ahora.isoformat(timespec="seconds"),
        "revisadas": 0,
        "activas": [],
        "huerfanas": [],
        "inconsistentes": [],
        "sin_definicion": [],
        "temporales_eliminados": [],
        "fichas_ilegibles": [],
        "reclamadas_mientras_tanto": [],
        # Ejecuciones con el latido caducado que NO se recuperan porque no
        # están demostradas muertas (A3.3).
        "latido_vencido": [],
        # Tareas cuyo worktree registrado ya no resuelve (A3.3). No se
        # borra el dato: el árbol puede volver (una unidad desconectada,
        # un `git worktree` que se rehace). Se avisa, que es lo que una
        # persona necesita para decidir.
        "worktree_ausente": [],
        # Filas en ejecución con la identidad incompleta (A3.3). NO se
        # liberan: una fila rota no demuestra que el trabajador esté
        # muerto, y liberarla le quitaba la tarea a alguien que podía
        # estar vivo y latiendo. Se informan para que una persona decida
        # (la salida es `reabrir`, que es una orden humana).
        "inconsistentes_sin_tocar": [],
        # Tareas recuperadas en la base cuyo espejo JSON no se pudo
        # regenerar. Antes esto abortaba la pasada entera y dejaba sin
        # revisar todo lo que venía detrás.
        "espejo_no_regenerado": [],
    }

    for temporal in temporales_huerfanos(raiz):
        try:
            temporal.unlink()
            informe["temporales_eliminados"].append(temporal.name)
        except OSError:
            pass

    # Las tareas en ejecución se enumeran desde SQLite (autoridad). Para
    # recuperar una hace falta además su definición JSON en este árbol:
    # sin ella no se puede regenerar el espejo ni conocer su contrato, así
    # que se informa sin tocarla.
    fichas, errores = listar_con_errores(raiz)
    definiciones = {ficha.id: ficha for ficha in fichas}

    informe["fichas_ilegibles"] = errores

    with global_.conexion(raiz) as con:
        global_.sincronizar_lista(con, fichas)
        filas = global_.listar_tareas(con)

    for fila in filas:
        if fila["estado"] != str(Estado.EN_EJECUCION):
            continue

        informe["revisadas"] = informe["revisadas"] + 1

        ficha = definiciones.get(fila["id"])

        if ficha is None:
            informe["sin_definicion"].append(
                {
                    "id": fila["id"],
                    "titulo": fila["titulo"],
                    "trabajador_id": fila["trabajador_id"],
                    "pid": fila["pid"],
                    "motivo": "En ejecución según el estado global, pero su "
                    "ficha JSON no está en este árbol de trabajo ("
                    + str(fila["definicion_ruta"]) + "). No se modifica.",
                }
            )
            continue

        global_.aplicar_fila(ficha, fila)
        ficha.eventos_pendientes.clear()

        clase, motivo = clasificar_ejecucion(
            ficha,
            ahora,
            comprobar_proceso,
            latido_maximo_s,
            latido_gracia_s,
            latido_abandono_s,
        )

        if clase == CLASE_ACTIVA:
            informe["activas"].append(
                {
                    "id": ficha.id,
                    "titulo": ficha.titulo,
                    "trabajador_id": ficha.trabajador_id,
                    "pid": ficha.pid,
                    "motivo": motivo,
                }
            )
            continue

        if clase == CLASE_INCONSISTENTE:
            informe["inconsistentes_sin_tocar"].append(
                {
                    "id": ficha.id,
                    "titulo": ficha.titulo,
                    "trabajador_id": ficha.trabajador_id,
                    "pid": ficha.pid,
                    "motivo": motivo,
                }
            )
            continue

        if clase == CLASE_LATIDO_VENCIDO:
            # Caducó el latido pero NO está demostrada muerta. No se toca:
            # recuperarla sería arrebatársela a quien quizá sigue
            # trabajando. Se informa para que una persona lo mire, que es
            # justo lo que hay que hacer con una duda.
            informe["latido_vencido"].append(
                {
                    "id": ficha.id,
                    "titulo": ficha.titulo,
                    "trabajador_id": ficha.trabajador_id,
                    "pid": ficha.pid,
                    "motivo": motivo,
                }
            )
            continue

        # Se conserva todo: quién la tenía, desde cuándo y por qué se cortó.
        ficha.registrar_ejecucion(
            {
                "tipo": "interrupcion",
                "fecha": informe["fecha"],
                "resultado": "INTERRUMPIDA",
                "clase": clase,
                "motivo": motivo,
                "trabajador_id": ficha.trabajador_id,
                "pid": ficha.pid,
                "iniciado_en": ficha.iniciado_en,
                "ultimo_latido": ficha.ultimo_latido,
            }
        )

        ficha.ultima_falla = {
            "fecha": informe["fecha"],
            "intento": ficha.intentos,
            "problemas": ["Ejecución interrumpida: " + motivo],
        }

        ficha.registrar_evento(
            {
                "fecha": informe["fecha"],
                "tipo": global_.EVENTO_RECUPERACION,
                "estado_anterior": str(ficha.estado),
                "estado_nuevo": str(ficha.estado),
                "motivo": "Ejecución " + clase.lower() + ": " + motivo,
                "origen": ORIGEN_AUTOMATICO,
                "datos": {
                    "clase": clase,
                    "trabajador_id": ficha.trabajador_id,
                    "pid": ficha.pid,
                    "iniciado_en": ficha.iniciado_en,
                    "ultimo_latido": ficha.ultimo_latido,
                },
            }
        )

        # Se guarda ANTES de liberar: es la prueba de vida sobre la que se
        # tomó la decisión, y tiene que viajar en el WHERE de la escritura.
        latido_juzgado = ficha.ultimo_latido

        _liberar_trabajador(ficha)

        transicionar(
            ficha,
            Estado.REABIERTO,
            "Recuperación tras interrupción: " + motivo,
            ORIGEN_AUTOMATICO,
        )

        try:
            # A3.2: la recuperación juzga sobre una foto leída antes. Si
            # entre aquella lectura y esta escritura alguien volvió a tomar
            # la tarea, la generación ya no casa y se rechaza. Es lo
            # correcto: una tarea recién reclamada NO está abandonada, y
            # devolverla a REABIERTO se la quitaría a su nuevo dueño.
            # `exigir_iguales` cierra el hueco que la generación no ve: un
            # latido no mueve ni el estado ni la generación, así que si el
            # dueño daba señal de vida justo entre la clasificación y esta
            # escritura, el UPDATE casaba igual y se le quitaba la tarea a
            # alguien que acababa de demostrar que seguía ahí.
            persistir(
                raiz,
                ficha,
                campos_propios=CAMPOS_RECUPERAR,
                exigir_iguales={"ultimo_latido": latido_juzgado},
            )
        except ErrorPropiedad as rechazo:
            informe["reclamadas_mientras_tanto"].append(
                {
                    "id": ficha.id,
                    "titulo": ficha.titulo,
                    "motivo": rechazo.informe["detalle"],
                }
            )
            continue

        try:
            _registrar_en_git(git, ficha, "Recuperación tras interrupción.")
        except ErrorSupervisor as problema:
            # La base ya está bien; lo que falló es el espejo en disco. Se
            # anota y se sigue: abortar aquí dejaba sin revisar todas las
            # tareas que venían detrás, que es un daño mayor.
            informe["espejo_no_regenerado"].append(
                {
                    "id": ficha.id,
                    "titulo": ficha.titulo,
                    "motivo": str(problema),
                }
            )

        destino = (
            informe["huerfanas"]
            if clase == CLASE_HUERFANA
            else informe["inconsistentes"]
        )

        destino.append(
            {
                "id": ficha.id,
                "titulo": ficha.titulo,
                "motivo": motivo,
                "estado_nuevo": str(ficha.estado),
            }
        )

        # La tarea ya está recuperada; lo que sigue es sólo informar. Se
        # comprueba después de persistir para no dejar sin recuperar una
        # tarea por un problema de su árbol: son cosas independientes.
        if ficha.worktree:
            try:
                resolver_worktree(raiz, ficha.worktree)
            except ErrorWorktree as problema:
                informe["worktree_ausente"].append(
                    {
                        "id": ficha.id,
                        "titulo": ficha.titulo,
                        "worktree": ficha.worktree,
                        "motivo": str(problema),
                    }
                )

    return informe


# ----------------------------------------------------------------------
# Lectura para la interfaz: SQLite global como única fuente operativa
# ----------------------------------------------------------------------

def resumen_de_tarea(fila: dict, definicion: Ficha | None) -> dict:
    """
    Vista compacta de una tarea para el tablero.

    `fila` es la autoridad (SQLite). `definicion` es la ficha JSON legible,
    o None si el archivo falta o está corrupto: la tarea se muestra igual,
    con su estado real, y se indica que la definición no es legible.
    """
    declaradas = definicion.requiere_decision_humana if definicion else []

    if definicion is not None:
        decisiones = global_.fusionar_decisiones(
            declaradas, fila.get("decisiones") or []
        )
    else:
        decisiones = [
            dict(operativa, descripcion="(definición JSON no legible)")
            for operativa in fila.get("decisiones") or []
        ]

    pendientes = [una for una in decisiones if not una.get("resuelta")]

    verificacion = fila.get("ultima_verificacion")

    # A3.3 — lo que hace falta para saber QUÉ LE PASA a la tarea, no sólo
    # en qué estado está. Son cosas distintas: REABIERTO es un estado;
    # que nadie la esté ejecutando y desde cuándo, otra cosa.
    #
    # Se calcula aquí y no en la plantilla porque el tablero no debe
    # reimplementar el criterio de vitalidad: si lo hiciera, tarde o
    # temprano diría algo distinto de lo que decide la recuperación.
    senales = vitalidad(fila)

    # Si la verificación guardada es de una ejecución anterior, el verde no
    # dice nada de la actual. Lo decide el motor y no la interfaz: ni el
    # tablero ni la consola deben reimplementar este criterio.
    vigente = None

    if verificacion:
        registrada = verificacion.get("generacion")
        vigente = (
            registrada is not None
            and registrada == fila.get("generacion")
        )

    return {
        "id": fila["id"],
        "vitalidad": senales["vitalidad"],
        "vitalidad_motivo": senales["motivo"],
        "edad_latido_s": senales["edad_latido_s"],
        # Qué merece la atención de una persona. Lo dice el motor: la
        # plantilla lo tenía en una lista propia y marcaba en alerta toda
        # tarea NUEVA o REABIERTA, que es el estado normal de lo que nadie
        # ha tomado todavía. Un tablero donde lo normal está en rojo deja
        # de leerse.
        "requiere_atencion": senales["requiere_atencion"],
        "verificacion_vigente": vigente,
        "verificacion_raiz": (
            verificacion.get("raiz") if verificacion else None
        ),
        "verificacion_commit": (
            verificacion.get("commit") if verificacion else None
        ),
        "verificacion_rama": (
            verificacion.get("rama") if verificacion else None
        ),
        "verificacion_fecha": (
            verificacion.get("fecha") if verificacion else None
        ),
        "titulo": fila["titulo"],
        "objetivo": definicion.objetivo if definicion else "",
        "estado": fila["estado"],
        "rama": fila.get("rama"),
        "worktree": fila.get("worktree"),
        "intentos": fila.get("intentos", 0),
        "max_intentos": fila.get("max_intentos", 0),
        "pruebas_ok": verificacion.get("ok", 0) if verificacion else 0,
        "pruebas_total": verificacion.get("total", 0) if verificacion else 0,
        "pruebas_requeridas": (
            list(definicion.pruebas_requeridas) if definicion else []
        ),
        "ambito_archivos": list(fila.get("ambito_archivos") or []),
        "actualizado_en": fila.get("actualizado_en"),
        "creado_en": fila.get("creado_en"),
        "ultima_falla": fila.get("ultima_falla"),
        "decisiones_pendientes": pendientes,
        "decisiones_totales": len(decisiones),
        "requiere_decision_humana": bool(pendientes),
        "trabajador_id": fila.get("trabajador_id"),
        "generacion": fila.get("generacion"),
        "pid": fila.get("pid"),
        "iniciado_en": fila.get("iniciado_en"),
        "ultimo_latido": fila.get("ultimo_latido"),
        "ultima_verificacion": verificacion,
        "commit_inicial": fila.get("commit_inicial"),
        "definicion_ruta": fila.get("definicion_ruta"),
        "definicion_legible": definicion is not None,
    }


def _resumen_ilegible(fila: dict) -> dict:
    """
    Tarjeta mínima para una fila que no se pudo interpretar.

    Se muestra igual, diciendo la verdad, en vez de hacer desaparecer el
    tablero entero por una fila mala.
    """
    return {
        "id": str(fila.get("id") or "?"),
        "titulo": str(fila.get("titulo") or "(sin título)"),
        "objetivo": "",
        "estado": str(fila.get("estado") or ""),
        "vitalidad": None,
        "vitalidad_motivo": "La fila de la base no se pudo interpretar.",
        "requiere_atencion": True,
        "edad_latido_s": None,
        "verificacion_raiz": None,
        "verificacion_commit": None,
        "verificacion_rama": None,
        "verificacion_fecha": None,
        "verificacion_vigente": None,
        "rama": fila.get("rama"),
        "worktree": fila.get("worktree"),
        "intentos": 0,
        "max_intentos": 0,
        "pruebas_ok": 0,
        "pruebas_total": 0,
        "pruebas_requeridas": [],
        "ambito_archivos": [],
        "actualizado_en": fila.get("actualizado_en"),
        "creado_en": fila.get("creado_en"),
        "ultima_falla": None,
        "decisiones_pendientes": [],
        "decisiones_totales": 0,
        "requiere_decision_humana": False,
        "trabajador_id": fila.get("trabajador_id"),
        "generacion": fila.get("generacion"),
        "pid": fila.get("pid"),
        "iniciado_en": fila.get("iniciado_en"),
        "ultimo_latido": fila.get("ultimo_latido"),
        "ultima_verificacion": None,
        "commit_inicial": fila.get("commit_inicial"),
        "definicion_ruta": fila.get("definicion_ruta"),
        "definicion_legible": False,
        "fila_legible": False,
    }


def _resumen_vacio() -> dict:
    return {
        "agentes_activos": 0,
        "agentes_sin_senal": [],
        "sin_importar": [],
        "totales": 0,
        "nuevas": 0,
        "en_ejecucion": 0,
        "propuestas": 0,
        "requieren_revision": 0,
        "bloqueadas": 0,
        "reabiertas": 0,
        "aprobadas": 0,
        "rechazadas": 0,
        "ultima_actividad": None,
    }


def tablero(raiz: Path, maximo_actividad: int = 20) -> dict:
    """
    Estado completo del Supervisor para la interfaz de sólo lectura.

    Todo el estado operativo proviene de la base SQLite global.

    NO ESCRIBE NADA (auditoría R1). Antes incorporaba a la base las fichas
    que no conocía, es decir: pedía `BEGIN IMMEDIATE` —el candado de
    escritura de toda la base— desde un GET de la API web, y una tarea
    podía nacer con sólo refrescar el tablero. Las fichas que la base no
    conoce se reportan en `sin_importar`, como ya hace `diagnostico`, y se
    incorporan con `sincronizar-definiciones`, que es una orden explícita.

    Nunca lanza excepción: una ficha corrupta se reporta, una FILA
    corrupta se degrada a una tarjeta que lo dice, y si la base global no
    está disponible el tablero lo dice (estado ERROR) en lugar de inventar
    datos a partir de los JSON.
    """
    raiz = Path(raiz)

    fichas, errores = listar_con_errores(raiz)
    definiciones = {ficha.id: ficha for ficha in fichas}

    base = {
        "estado": "ERROR",
        "ruta": None,
        "ubicacion_resumida": None,
        "version_esquema": None,
        "journal_mode": None,
        "detalle": None,
    }

    filas: list[dict] = []
    eventos: list[dict] = []
    ultima_actividad = None

    try:
        ruta = global_.ruta_base(raiz)

        base["ruta"] = str(ruta)
        base["ubicacion_resumida"] = global_.ubicacion_resumida(ruta)

        with global_.conexion(raiz) as con:
            base["version_esquema"] = global_.version_esquema(con)
            base["journal_mode"] = con.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]

            filas = global_.listar_tareas(con)
            eventos = global_.listar_eventos(con, maximo=maximo_actividad)

            ultima_actualizacion = global_.ultima_actualizacion(con)
            ultimo_evento = eventos[0]["fecha"] if eventos else None

            candidatos = [
                valor for valor in (ultima_actualizacion, ultimo_evento)
                if valor
            ]
            ultima_actividad = max(candidatos) if candidatos else None

        base["estado"] = "ACTIVA"
        base["detalle"] = "Base global operativa."

    except (global_.ErrorEstadoGlobal, sqlite3.Error, OSError) as error:
        base["estado"] = "ERROR"
        base["detalle"] = str(error)

    tareas = []

    for fila in filas:
        try:
            tareas.append(
                resumen_de_tarea(fila, definiciones.get(fila["id"]))
            )
        except Exception as problema:
            # Una sola fila con un JSON operativo malformado tumbaba el
            # tablero ENTERO —desaparecían todas las tareas— y el navegador
            # mostraba «sin conexión con el motor local», que además es
            # falso: el motor contestó perfectamente. Se degrada esa fila y
            # las demás se ven.
            errores.append(
                {
                    "archivo": str(fila.get("definicion_ruta") or fila["id"]),
                    "motivo": "La fila de '" + str(fila.get("id"))
                    + "' no se pudo interpretar: "
                    + type(problema).__name__ + ": " + str(problema),
                }
            )
            tareas.append(_resumen_ilegible(fila))

    def contar(estado: Estado) -> int:
        return sum(1 for una in tareas if una["estado"] == str(estado))

    # «Agente activo» significa que hay alguien trabajando, no que quede
    # una fila con nombre de dueño. Una ejecución muerta hace horas contaba
    # igual que una viva, y el resumen decía que había gente trabajando
    # cuando no había nadie.
    agentes = {
        una["trabajador_id"]
        for una in tareas
        if una["estado"] == str(Estado.EN_EJECUCION)
        and una["trabajador_id"]
        and una.get("vitalidad") == VITALIDAD_ACTIVA
    }

    sin_senal = sorted(
        {
            una["trabajador_id"]
            for una in tareas
            if una["estado"] == str(Estado.EN_EJECUCION)
            and una["trabajador_id"]
            and una.get("vitalidad") != VITALIDAD_ACTIVA
        }
    )

    conocidas = {fila["id"] for fila in filas}
    sin_importar = sorted(
        ficha.id for ficha in fichas if ficha.id not in conocidas
    )

    actividad = [
        {
            "fecha": evento.get("fecha"),
            "tarea": evento.get("tarea_id"),
            "titulo": evento.get("titulo"),
            "tipo": evento.get("tipo"),
            "estado_anterior": evento.get("estado_anterior"),
            "estado_nuevo": evento.get("estado_nuevo"),
            "motivo": evento.get("motivo"),
            "origen": evento.get("origen"),
        }
        for evento in eventos
    ]

    resumen = _resumen_vacio()

    if base["estado"] == "ACTIVA":
        resumen.update(
            {
                "agentes_activos": len(agentes),
                "agentes_sin_senal": sin_senal,
                "sin_importar": sin_importar,
                "totales": len(tareas),
                "nuevas": contar(Estado.NUEVO),
                "en_ejecucion": contar(Estado.EN_EJECUCION),
                "propuestas": contar(Estado.PROPUESTO),
                "requieren_revision": contar(Estado.REQUIERE_REVISION),
                "bloqueadas": contar(Estado.BLOQUEADO),
                "reabiertas": contar(Estado.REABIERTO),
                "aprobadas": contar(Estado.APROBADO),
                "rechazadas": contar(Estado.RECHAZADO),
                "ultima_actividad": ultima_actividad,
            }
        )

    return {
        "generado_en": ahora_utc(),
        "base_global": base,
        "resumen": resumen,
        "tareas": tareas,
        "actividad": actividad,
        "fichas_ilegibles": errores,
    }
