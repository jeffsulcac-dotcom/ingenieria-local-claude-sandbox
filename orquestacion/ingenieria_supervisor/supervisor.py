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


def _regenerar_espejo(raiz: Path, ficha: Ficha) -> None:
    """
    Reescribe el JSON como espejo de lo que SQLite ya confirmó.

    Si el espejo no se pudiera escribir, el estado global ya quedó
    confirmado y la siguiente persistencia lo regenera: nunca hay dos
    escrituras contradictorias, porque el JSON siempre sale de SQLite.
    """
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

class ErrorWorktree(ErrorSupervisor):
    """
    La ruta registrada como worktree de una tarea no se puede usar.

    No existe, no es un directorio, o pertenece a otro repositorio. Es un
    error propio y no genérico porque la respuesta del operador es distinta
    en cada caso y porque una ruta ajena NUNCA debe ejecutarse por el
    hecho de existir.
    """


def resolver_worktree(raiz: Path, declarado: str | None) -> Path:
    """
    Convierte la ruta registrada de un worktree en una raíz utilizable.

    Devuelve `raiz` cuando la tarea no declara worktree: es el
    comportamiento de siempre y el caso normal hoy.

    Qué se comprueba, y por qué cada cosa
    -------------------------------------
    - Que exista y sea un DIRECTORIO. Un archivo con ese nombre, o una ruta
      borrada, no es un árbol de trabajo.

    - Que pertenezca AL MISMO repositorio, comparando el directorio común de
      Git. Es la comprobación que impide ejecutar una ruta ajena: que un
      directorio exista y hasta que sea un repositorio Git válido no
      autoriza a correr sus pruebas. Un worktree registrado apuntando fuera
      del proyecto —por error o a propósito— haría que `verificar` ejecutara
      código de otro sitio y grabara su resultado como si fuera el de esta
      tarea.

    Sobre las rutas, que es donde se esconden los disgustos:

    - Una ruta RELATIVA se interpreta contra `raiz`, nunca contra el
      directorio desde el que se invocó el Supervisor, que puede ser
      cualquiera.
    - `expanduser` resuelve `~`; `resolve` normaliza `..`, los enlaces
      simbólicos y las junctions de Windows, y en Windows además unifica la
      letra de unidad y el caso del sistema de archivos. Por eso la
      comparación se hace SIEMPRE entre rutas resueltas: comparar cadenas
      dejaría pasar `C:\repo` frente a `c:\repo\` y dos formas del mismo
      directorio parecerían sitios distintos.
    - Los espacios no necesitan nada especial porque nunca se construye una
      línea de órdenes de texto: `subprocess` recibe una lista.
    """
    if declarado is None or not str(declarado).strip():
        return Path(raiz)

    candidato = Path(str(declarado).strip()).expanduser()

    if not candidato.is_absolute():
        candidato = Path(raiz) / candidato

    try:
        candidato = candidato.resolve()
    except OSError as error:
        raise ErrorWorktree(
            "No se pudo resolver la ruta del worktree '" + str(declarado)
            + "': " + str(error)
        ) from None

    if not candidato.exists():
        raise ErrorWorktree(
            "El worktree registrado no existe: '" + str(candidato) + "'."
        )

    if not candidato.is_dir():
        raise ErrorWorktree(
            "El worktree registrado no es un directorio: '"
            + str(candidato) + "'."
        )

    try:
        comun_tarea = global_.git_common_dir(candidato)
    except global_.ErrorEstadoGlobal as error:
        raise ErrorWorktree(
            "La ruta '" + str(candidato) + "' no pertenece a ningún "
            "repositorio Git, así que no se puede verificar ahí: "
            + str(error)
        ) from None

    comun_propio = global_.git_common_dir(Path(raiz))

    if comun_tarea != comun_propio:
        raise ErrorWorktree(
            "El worktree '" + str(candidato) + "' pertenece a OTRO "
            "repositorio (" + str(comun_tarea) + " en vez de "
            + str(comun_propio) + "). No se ejecuta una ruta ajena por el "
            "hecho de que exista."
        )

    return candidato


class Git:
    """Acceso mínimo a Git, con los límites del Supervisor incorporados."""

    def __init__(self, raiz: Path):
        self.raiz = Path(raiz)

    def _ejecutar(self, *argumentos: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *argumentos],
            cwd=str(self.raiz),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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

    if existe(raiz, identificador):
        raise ErrorSupervisor(
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

    ficha.registrar_evento(
        {
            "fecha": ahora_utc(),
            "tipo": global_.EVENTO_CREACION,
            "estado_anterior": None,
            "estado_nuevo": str(Estado.NUEVO),
            "motivo": "Ficha creada.",
            "origen": ORIGEN_HUMANO,
        }
    )

    # La definición es el contrato: primero el JSON versionable, después su
    # incorporación al estado global (misma vía que el bootstrap).
    with global_.conexion(raiz) as con:
        if global_.obtener_tarea(con, identificador) is not None:
            raise ErrorSupervisor(
                "La tarea '" + identificador + "' ya existe en el estado "
                "global aunque su ficha JSON no esté: no se puede volver a "
                "crear con el mismo identificador."
            )

        guardar(raiz, ficha)

        with global_.transaccion(con):
            global_.importar_ficha(con, ficha, evento_importacion=False)

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
    arbol_declarado = (
        str(resolver_worktree(raiz, worktree)) if worktree else ficha.worktree
    )

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

    A2: las pruebas se ejecutan sobre `raiz`, no sobre el worktree de la
    tarea. La verificación consciente del worktree pertenece a A3/B.
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

    corrida = corredor.ejecutar_todas(arbol, tiempo_limite_s, ejecutable)

    # La evidencia de DÓNDE se ejecutó viaja con el resultado. Sin esto,
    # dos corridas idénticas de árboles distintos son indistinguibles en el
    # historial, y no se puede auditar después si se verificó lo correcto.
    corrida["raiz"] = str(arbol)
    corrida["es_worktree"] = arbol != Path(raiz).resolve()
    corrida["rama"] = testigo.rama_actual()
    corrida["commit"] = testigo.hash_actual()

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
        "es_worktree": corrida["es_worktree"],
        "rama": corrida["rama"],
        "commit": corrida["commit"],
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

    No genera commit automático: no es una transición de estado.
    """
    ficha = cargar(raiz, identificador)

    encontrada = None

    for decision in ficha.requiere_decision_humana:
        if str(decision.get("clave")) == str(clave):
            encontrada = decision
            break

    if encontrada is None:
        raise ErrorSupervisor(
            "La tarea no tiene ninguna decisión con clave '"
            + str(clave)
            + "'."
        )

    if encontrada.get("resuelta"):
        raise ErrorSupervisor(
            "La decisión '" + str(clave) + "' ya estaba resuelta."
        )

    encontrada["resuelta"] = True
    encontrada["resolucion"] = resolucion
    encontrada["resuelta_en"] = ahora_utc()
    encontrada["origen"] = ORIGEN_HUMANO

    # Resolver una decisión no es una transición de estado, y el commit
    # automático está reservado a las transiciones. La resolución queda en
    # SQLite; el espejo JSON queda escrito en disco y su versionado
    # corresponde al humano que decidió.

    ficha.registrar_evento(
        {
            "fecha": ahora_utc(),
            "tipo": global_.EVENTO_DECISION,
            "estado_anterior": str(ficha.estado),
            "estado_nuevo": str(ficha.estado),
            "motivo": "Decisión humana '" + str(clave) + "' resuelta.",
            "origen": ORIGEN_HUMANO,
            "datos": {"clave": str(clave), "resolucion": resolucion},
        }
    )

    persistir(raiz, ficha, campos_propios=CAMPOS_DECIDIR)

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

def clasificar_ejecucion(
    ficha: Ficha,
    ahora: datetime,
    comprobar_proceso=proceso_vivo,
    latido_maximo_s: int = LATIDO_MAXIMO_S,
    latido_gracia_s: int = LATIDO_GRACIA_S,
) -> tuple[str, str]:
    """
    Clasifica una tarea EN_EJECUCION como ACTIVA, HUERFANA o INCONSISTENTE.

    Nunca se juzga sólo por el PID:

    - un latido vencido basta por sí solo para declarar abandono;
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

    if antiguedad > timedelta(seconds=latido_maximo_s):
        return (
            CLASE_HUERFANA,
            "Latido vencido: "
            + str(int(antiguedad.total_seconds()))
            + " s sin señal (máximo permitido "
            + str(latido_maximo_s)
            + " s).",
        )

    equipo_ficha = equipo_de(ficha.trabajador_id)
    equipo_actual = socket.gethostname()

    if equipo_ficha != equipo_actual:
        return (
            CLASE_ACTIVA,
            "Trabajador de otro equipo ("
            + str(equipo_ficha)
            + ") con latido reciente.",
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
            ficha, ahora, comprobar_proceso, latido_maximo_s, latido_gracia_s
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
            persistir(raiz, ficha, campos_propios=CAMPOS_RECUPERAR)
        except ErrorPropiedad as rechazo:
            informe["reclamadas_mientras_tanto"].append(
                {
                    "id": ficha.id,
                    "titulo": ficha.titulo,
                    "motivo": rechazo.informe["detalle"],
                }
            )
            continue

        _registrar_en_git(git, ficha, "Recuperación tras interrupción.")

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

    return {
        "id": fila["id"],
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


def _resumen_vacio() -> dict:
    return {
        "agentes_activos": 0,
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

    Todo el estado operativo proviene de la base SQLite global. Antes de
    leer se sincronizan las definiciones JSON legibles (idempotente), de
    modo que una ficha recién añadida aparece sin pasos manuales.

    Nunca lanza excepción: una ficha corrupta se reporta, y si la base
    global no está disponible el tablero lo dice (estado ERROR) en lugar de
    inventar datos a partir de los JSON.
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
            # Camino de sólo lectura: incorpora tareas ausentes, no
            # refresca definiciones (ver sincronizar_lista).
            global_.sincronizar_lista(con, fichas, solo_importar=True)

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

    tareas = [
        resumen_de_tarea(fila, definiciones.get(fila["id"]))
        for fila in filas
    ]

    def contar(estado: Estado) -> int:
        return sum(1 for una in tareas if una["estado"] == str(estado))

    agentes = {
        una["trabajador_id"]
        for una in tareas
        if una["estado"] == str(Estado.EN_EJECUCION) and una["trabajador_id"]
    }

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
