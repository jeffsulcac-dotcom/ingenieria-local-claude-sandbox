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

Este módulo no realiza cálculos de ingeniería.
"""

from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ingenieria_nucleo.estados import Estado

from . import pruebas as corredor
from .tarea import (
    Ficha,
    ahora_utc,
    existe,
    guardar,
    leer,
    listar,
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
# No basta con EN_EJECUCION: una tarea que quedó en requiere_revision o en
# propuesto conserva cambios sin confirmar en el árbol de trabajo, así que
# sigue siendo la dueña de esos archivos hasta que un humano la cierre.
ESTADOS_QUE_RETIENEN_AMBITO = frozenset(
    {Estado.EN_EJECUCION, Estado.REQUIERE_REVISION, Estado.PROPUESTO}
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
    raiz: Path,
    ficha: Ficha,
    estados_activos: frozenset = ESTADOS_QUE_RETIENEN_AMBITO,
) -> list[dict]:
    """Conflictos de esta ficha contra todas las tareas que retienen ámbito."""
    conflictos = []

    for otra in listar(raiz):
        if otra.id == ficha.id:
            continue

        if otra.estado not in estados_activos:
            continue

        pares = solapamientos(ficha.ambito_archivos, otra.ambito_archivos)

        if pares:
            conflictos.append(
                {
                    "tarea": otra.id,
                    "titulo": otra.titulo,
                    "estado": str(otra.estado),
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
            "estado_anterior": str(anterior),
            "estado_nuevo": str(destino),
            "motivo": motivo,
            "origen": origen,
        }
    )

    return ficha


# ----------------------------------------------------------------------
# Git: commits automáticos estrictamente limitados
# ----------------------------------------------------------------------

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
            "estado_anterior": None,
            "estado_nuevo": str(Estado.NUEVO),
            "motivo": "Ficha creada.",
            "origen": ORIGEN_HUMANO,
        }
    )

    guardar(raiz, ficha)

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
) -> Ficha:
    """
    Reclama una tarea para trabajarla.

    Aquí se aplica la regla de un solo escritor: si otra tarea activa declara
    un ámbito que se solapa, la toma se rechaza.
    """
    ficha = leer(raiz, identificador)

    if ficha.estado not in ESTADOS_TOMABLES:
        raise ErrorSupervisor(
            "No se puede tomar una tarea en estado '"
            + str(ficha.estado)
            + "'."
        )

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

    conflictos = conflictos_de_ambito(raiz, ficha)

    if conflictos:
        detalle = "; ".join(
            uno["tarea"]
            + " ("
            + ", ".join(
                par["propio"] + " <-> " + par["ajeno"] for par in uno["pares"]
            )
            + ")"
            for uno in conflictos
        )

        raise ErrorSolapamiento(
            "Ámbito en conflicto con tareas activas: "
            + detalle
            + ". Un solo escritor por archivo."
        )

    momento = (ahora or ahora_datetime()).isoformat(timespec="seconds")

    ficha.trabajador_id = trabajador_id or nuevo_trabajador_id()
    ficha.pid = pid if pid is not None else os.getpid()
    ficha.iniciado_en = momento
    ficha.ultimo_latido = momento

    if not ficha.rama:
        ficha.rama = PREFIJO_RAMA + ficha.id

    if git is not None and ficha.commit_inicial is None:
        ficha.commit_inicial = git.hash_actual()

    transicionar(
        ficha,
        Estado.EN_EJECUCION,
        "Tarea tomada por " + ficha.trabajador_id + ".",
        ORIGEN_AUTOMATICO,
    )

    guardar(raiz, ficha)

    _registrar_en_git(git, ficha, "Tarea tomada.")

    return ficha


def latido(
    raiz: Path,
    identificador: str,
    ahora: datetime | None = None,
) -> Ficha:
    """Señal de vida del trabajador que sostiene la tarea."""
    ficha = leer(raiz, identificador)

    if ficha.estado != Estado.EN_EJECUCION:
        raise ErrorSupervisor(
            "Sólo una tarea en ejecución puede emitir latido."
        )

    ficha.ultimo_latido = (
        ahora or ahora_datetime()
    ).isoformat(timespec="seconds")

    guardar(raiz, ficha)

    return ficha


def devolver(
    raiz: Path,
    identificador: str,
    motivo: str = "Tarea devuelta por el trabajador.",
    git=None,
) -> Ficha:
    """El trabajador suelta la tarea sin haberla terminado."""
    ficha = leer(raiz, identificador)

    if ficha.estado != Estado.EN_EJECUCION:
        raise ErrorSupervisor(
            "Sólo se puede devolver una tarea en ejecución."
        )

    _liberar_trabajador(ficha)

    transicionar(ficha, Estado.REABIERTO, motivo, ORIGEN_AUTOMATICO)

    guardar(raiz, ficha)

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
    """
    ficha = leer(raiz, identificador)

    if ficha.estado != Estado.EN_EJECUCION:
        raise ErrorSupervisor(
            "Sólo se puede verificar una tarea en ejecución. Estado actual: '"
            + str(ficha.estado)
            + "'."
        )

    corrida = corredor.ejecutar_todas(raiz, tiempo_limite_s, ejecutable)

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

    if problemas:
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

    guardar(raiz, ficha)

    registro = _registrar_en_git(git, ficha, motivo)

    return {
        "ficha": ficha,
        "corrida": corrida,
        "problemas": problemas,
        "decisiones_pendientes": pendientes,
        "estado": str(ficha.estado),
        "motivo": motivo,
        "git": registro,
    }


def decidir(
    raiz: Path,
    identificador: str,
    clave: str,
    resolucion: str,
) -> Ficha:
    """
    Resuelve explícitamente una decisión humana pendiente.

    No genera commit automático: no es una transición de estado.
    """
    ficha = leer(raiz, identificador)

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

    # Resolver una decisión no es una transición de estado, y el commit
    # automático está reservado a las transiciones. La ficha queda escrita
    # en disco y su versionado corresponde al humano que decidió.

    ficha.registrar_evento(
        {
            "fecha": ahora_utc(),
            "estado_anterior": str(ficha.estado),
            "estado_nuevo": str(ficha.estado),
            "motivo": "Decisión humana '" + str(clave) + "' resuelta.",
            "origen": ORIGEN_HUMANO,
        }
    )

    guardar(raiz, ficha)

    return ficha


def aprobar(
    raiz: Path,
    identificador: str,
    comentario: str = "",
    git=None,
) -> Ficha:
    """Aprobación humana. El Supervisor nunca puede ejecutar esto solo."""
    ficha = leer(raiz, identificador)

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

    guardar(raiz, ficha)

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

    ficha = leer(raiz, identificador)

    _liberar_trabajador(ficha)

    transicionar(ficha, Estado.RECHAZADO, motivo, ORIGEN_HUMANO)

    guardar(raiz, ficha)

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
    ficha = leer(raiz, identificador)

    _liberar_trabajador(ficha)

    ficha.intentos = 0

    transicionar(ficha, Estado.REABIERTO, motivo, ORIGEN_HUMANO)

    guardar(raiz, ficha)

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

    ficha = leer(raiz, identificador)

    _liberar_trabajador(ficha)

    transicionar(ficha, Estado.BLOQUEADO, motivo, origen)

    guardar(raiz, ficha)

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
    """
    ahora = ahora or ahora_datetime()

    informe = {
        "fecha": ahora.isoformat(timespec="seconds"),
        "revisadas": 0,
        "activas": [],
        "huerfanas": [],
        "inconsistentes": [],
        "temporales_eliminados": [],
        "fichas_ilegibles": [],
    }

    for temporal in temporales_huerfanos(raiz):
        try:
            temporal.unlink()
            informe["temporales_eliminados"].append(temporal.name)
        except OSError:
            pass

    fichas, errores = listar_con_errores(raiz)

    informe["fichas_ilegibles"] = errores

    for ficha in fichas:
        if ficha.estado != Estado.EN_EJECUCION:
            continue

        informe["revisadas"] = informe["revisadas"] + 1

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

        _liberar_trabajador(ficha)

        transicionar(
            ficha,
            Estado.REABIERTO,
            "Recuperación tras interrupción: " + motivo,
            ORIGEN_AUTOMATICO,
        )

        guardar(raiz, ficha)

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
# Lectura para la interfaz
# ----------------------------------------------------------------------

def _ultima_corrida(ficha: Ficha) -> dict | None:
    for ejecucion in reversed(ficha.ejecuciones):
        if ejecucion.get("tipo") == "corrida":
            return ejecucion

    return None


def resumen_de_ficha(ficha: Ficha) -> dict:
    """Vista compacta de una ficha, pensada para el tablero."""
    corrida = _ultima_corrida(ficha)

    return {
        "id": ficha.id,
        "titulo": ficha.titulo,
        "objetivo": ficha.objetivo,
        "estado": str(ficha.estado),
        "rama": ficha.rama,
        "worktree": ficha.worktree,
        "intentos": ficha.intentos,
        "max_intentos": ficha.max_intentos,
        "pruebas_ok": corrida.get("ok", 0) if corrida else 0,
        "pruebas_total": corrida.get("total", 0) if corrida else 0,
        "pruebas_requeridas": list(ficha.pruebas_requeridas),
        "ambito_archivos": list(ficha.ambito_archivos),
        "actualizado_en": ficha.actualizado_en,
        "creado_en": ficha.creado_en,
        "ultima_falla": ficha.ultima_falla,
        "decisiones_pendientes": ficha.decisiones_pendientes(),
        "decisiones_totales": len(ficha.requiere_decision_humana),
        "trabajador_id": ficha.trabajador_id,
        "pid": ficha.pid,
        "iniciado_en": ficha.iniciado_en,
        "ultimo_latido": ficha.ultimo_latido,
    }


def tablero(raiz: Path, maximo_actividad: int = 20) -> dict:
    """
    Estado completo del Supervisor para la interfaz de sólo lectura.

    Nunca lanza excepción por una ficha corrupta: la reporta.
    """
    fichas, errores = listar_con_errores(raiz)

    tareas = [resumen_de_ficha(ficha) for ficha in fichas]

    def contar(estado: Estado) -> int:
        return sum(1 for una in tareas if una["estado"] == str(estado))

    agentes = {
        una["trabajador_id"]
        for una in tareas
        if una["estado"] == str(Estado.EN_EJECUCION) and una["trabajador_id"]
    }

    # Varios eventos pueden compartir el mismo segundo. El orden dentro de
    # la ficha desempata, para que lo más reciente aparezca siempre arriba.
    ordenados = []

    for ficha in fichas:
        for posicion, evento in enumerate(ficha.historial):
            ordenados.append(
                (
                    evento.get("fecha") or "",
                    posicion,
                    {
                        "fecha": evento.get("fecha"),
                        "tarea": ficha.id,
                        "titulo": ficha.titulo,
                        "estado_anterior": evento.get("estado_anterior"),
                        "estado_nuevo": evento.get("estado_nuevo"),
                        "motivo": evento.get("motivo"),
                        "origen": evento.get("origen"),
                    },
                )
            )

    ordenados.sort(key=lambda uno: (uno[0], uno[1]), reverse=True)

    actividad = [uno[2] for uno in ordenados]

    return {
        "generado_en": ahora_utc(),
        "resumen": {
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
        },
        "tareas": tareas,
        "actividad": actividad[:maximo_actividad],
        "fichas_ilegibles": errores,
    }
