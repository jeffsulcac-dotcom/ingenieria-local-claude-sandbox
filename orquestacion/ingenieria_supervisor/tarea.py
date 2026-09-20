"""
Contrato de la ficha de tarea del Supervisor.

Cada tarea es un archivo JSON versionado en Git:

    orquestacion/tareas/<id>.json

La ficha es el único contrato que necesita un trabajador para ejecutar la
tarea. No depende del historial de ninguna conversación.

La escritura es atómica: temporal -> validación -> reemplazo.
Un corte de energía no puede dejar una ficha JSON corrupta.

Autoridad desde A2
------------------
El JSON es la DEFINICIÓN versionada de la tarea: id, título, objetivo,
criterios, ámbito, pruebas requeridas y decisiones humanas declaradas
(clave y descripción).

El ESTADO OPERATIVO (estado, rama, worktree, intentos, trabajador, latido,
fallas, resolución de decisiones y ejecuciones) lo gobierna la base SQLite
global (ver `estado_global.py`). Esos campos siguen presentes en el JSON
como ESPEJO derivado: se regeneran a partir de SQLite después de cada
operación, por compatibilidad con el Supervisor V1 y para que el commit
automático de la ficha siga dejando rastro en Git. Nunca son entrada: al
cargar una tarea, SQLite se superpone a lo que diga el JSON.

El `historial` del JSON es la excepción: no se reconstruye desde SQLite.
Cada evento se registra a la vez en esta lista (recortada a los últimos
MAXIMO_HISTORIAL) y en la tabla `eventos` de la base, que es el historial
completo y global.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ingenieria_nucleo.estados import Estado


# Identificador obligatorio: T-0001, T-0002, ...
# Se valida siempre antes de construir una ruta, para que un identificador
# manipulado no pueda escribir fuera de la carpeta de tareas.
FORMATO_ID = re.compile(r"^T-\d{4}$")

CARPETA_TAREAS = ("orquestacion", "tareas")

PREFIJO_TEMPORAL = "_temporal_"
SUFIJO_TEMPORAL = ".json.tmp"

# Cantidad de ejecuciones recientes conservadas en la ficha.
MAXIMO_EJECUCIONES = 5

# Cantidad de eventos recientes conservados en la ficha.
MAXIMO_HISTORIAL = 20


class ErrorFicha(Exception):
    """La ficha no cumple el contrato."""


def ahora_utc() -> str:
    """Marca de tiempo UTC en ISO 8601, ordenable y sin ambigüedad."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _texto(valor, campo: str, obligatorio: bool = False) -> str | None:
    if valor is None:
        if obligatorio:
            raise ErrorFicha("El campo '" + campo + "' es obligatorio.")
        return None

    if not isinstance(valor, str):
        raise ErrorFicha("El campo '" + campo + "' debe ser texto.")

    valor = valor.strip()

    if obligatorio and not valor:
        raise ErrorFicha("El campo '" + campo + "' no puede estar vacío.")

    return valor


def _lista_de_texto(valor, campo: str) -> list[str]:
    if valor is None:
        return []

    if not isinstance(valor, list):
        raise ErrorFicha("El campo '" + campo + "' debe ser una lista.")

    resultado = []

    for elemento in valor:
        if not isinstance(elemento, str):
            raise ErrorFicha("El campo '" + campo + "' sólo admite texto.")
        resultado.append(elemento.strip())

    return resultado


def _lista_de_diccionarios(valor, campo: str) -> list[dict]:
    if valor is None:
        return []

    if not isinstance(valor, list):
        raise ErrorFicha("El campo '" + campo + "' debe ser una lista.")

    for elemento in valor:
        if not isinstance(elemento, dict):
            raise ErrorFicha("El campo '" + campo + "' sólo admite objetos.")

    return list(valor)


def _entero(valor, campo: str, minimo: int = 0) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorFicha("El campo '" + campo + "' debe ser un número entero.")

    if valor < minimo:
        raise ErrorFicha(
            "El campo '" + campo + "' no puede ser menor que "
            + str(minimo) + "."
        )

    return valor


@dataclass(slots=True)
class Ficha:
    """Contrato completo y autosuficiente de una tarea de desarrollo."""

    id: str
    titulo: str

    objetivo: str = ""
    criterios_aceptacion: list[str] = field(default_factory=list)
    ambito_archivos: list[str] = field(default_factory=list)
    pruebas_requeridas: list[str] = field(default_factory=list)

    estado: Estado = Estado.NUEVO

    rama: str | None = None
    worktree: str | None = None

    intentos: int = 0
    max_intentos: int = 3

    commit_inicial: str | None = None

    creado_en: str = ""
    actualizado_en: str = ""

    requiere_decision_humana: list[dict] = field(default_factory=list)

    trabajador_id: str | None = None
    pid: int | None = None
    iniciado_en: str | None = None
    ultimo_latido: str | None = None

    # A3.2 — generación de propiedad con la que se leyó esta ficha.
    #
    # Es el testigo que acompaña a cada orden del ciclo: viaja en el WHERE
    # de la escritura y la invalida si entretanto hubo una toma nueva.
    #
    # NO SE SERIALIZA. No aparece en `a_dict` ni se lee en `desde_dict`, y
    # por tanto nunca llega al JSON versionado. La única forma de poblarla
    # es `estado_global.aplicar_fila`, es decir, leyéndola de SQLite.
    #
    # La razón es que el JSON es un archivo del árbol de trabajo que
    # cualquiera edita y que Git versiona. Si el testigo viajara ahí, se
    # podría FIJAR a un valor cualquiera —o hacerlo RETROCEDER— y dos
    # ejecuciones distintas volverían a ser indistinguibles: exactamente el
    # problema ABA que esta columna existe para cerrar. Comprobado antes de
    # cerrarlo: bastaba escribir "generacion": 999 en la ficha.
    generacion: int = 0

    ultima_falla: dict | None = None

    ejecuciones: list[dict] = field(default_factory=list)
    historial: list[dict] = field(default_factory=list)

    # Eventos registrados en memoria y todavía no confirmados en SQLite.
    # No forman parte del JSON: `persistir()` los inserta y los vacía.
    eventos_pendientes: list[dict] = field(default_factory=list, repr=False)

    # A3.2 — estado que tenía la fila cuando se leyó esta ficha.
    #
    # Igual que `generacion`, no se serializa: sólo lo pone `aplicar_fila`.
    # Sirve para que la escritura pueda exigir que la tarea SIGA en el
    # estado sobre el que la orden decidió. Sin eso, una orden lenta que
    # decidió sobre una foto vieja revierte una transición ya confirmada:
    # comprobado, una orden humana rezagada resucitaba a PROPUESTO una
    # tarea que entretanto había quedado BLOQUEADA.
    #
    # `None` significa "ficha no leída de la base" (recién construida), y
    # entonces no se exige ningún estado: es lo que hacía V1.
    estado_leido: Estado | None = field(default=None, repr=False)

    # A3.2 — ámbito que la base tiene CONCEDIDO a esta tarea.
    #
    # Tampoco se serializa. Normalmente coincide con `ambito_archivos`; si
    # difieren, es que la ficha declara un ámbito que la base se negó a
    # aplicar por estar la tarea viva. El que manda para la regla de un solo
    # escritor es éste: se posee lo que la base concedió, no lo que un
    # archivo del árbol de trabajo diga.
    #
    # `None` significa "ficha no leída de la base".
    ambito_vigente: list[str] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Decisiones humanas
    # ------------------------------------------------------------------

    def decisiones_pendientes(self) -> list[dict]:
        """Decisiones de ingeniería que todavía nadie resolvió."""
        return [
            decision
            for decision in self.requiere_decision_humana
            if not decision.get("resuelta", False)
        ]

    def tiene_decisiones_pendientes(self) -> bool:
        return bool(self.decisiones_pendientes())

    # ------------------------------------------------------------------
    # Control de crecimiento de la ficha
    # ------------------------------------------------------------------

    def registrar_ejecucion(self, ejecucion: dict) -> None:
        self.ejecuciones.append(ejecucion)
        del self.ejecuciones[:-MAXIMO_EJECUCIONES]

    def registrar_evento(self, evento: dict) -> None:
        self.historial.append(evento)
        del self.historial[:-MAXIMO_HISTORIAL]
        self.eventos_pendientes.append(evento)

    # ------------------------------------------------------------------
    # Serialización
    # ------------------------------------------------------------------

    def a_dict(self) -> dict:
        return {
            "id": self.id,
            "titulo": self.titulo,
            "objetivo": self.objetivo,
            "criterios_aceptacion": list(self.criterios_aceptacion),
            "ambito_archivos": list(self.ambito_archivos),
            "pruebas_requeridas": list(self.pruebas_requeridas),
            "estado": str(self.estado),
            "rama": self.rama,
            "worktree": self.worktree,
            "intentos": self.intentos,
            "max_intentos": self.max_intentos,
            "commit_inicial": self.commit_inicial,
            "creado_en": self.creado_en,
            "actualizado_en": self.actualizado_en,
            "requiere_decision_humana": list(self.requiere_decision_humana),
            "trabajador_id": self.trabajador_id,
            "pid": self.pid,
            "iniciado_en": self.iniciado_en,
            "ultimo_latido": self.ultimo_latido,
            "ultima_falla": self.ultima_falla,
            "ejecuciones": list(self.ejecuciones),
            "historial": list(self.historial),
        }

    @classmethod
    def desde_dict(cls, datos) -> "Ficha":
        if not isinstance(datos, dict):
            raise ErrorFicha("La ficha debe ser un objeto JSON.")

        identificador = _texto(datos.get("id"), "id", obligatorio=True)
        validar_id(identificador)

        estado_texto = _texto(datos.get("estado"), "estado") or str(Estado.NUEVO)

        try:
            estado = Estado(estado_texto)
        except ValueError:
            raise ErrorFicha(
                "Estado desconocido: '" + estado_texto + "'."
            ) from None

        pid = datos.get("pid")
        if pid is not None:
            pid = _entero(pid, "pid", minimo=1)

        ultima_falla = datos.get("ultima_falla")
        if ultima_falla is not None and not isinstance(ultima_falla, dict):
            raise ErrorFicha("El campo 'ultima_falla' debe ser un objeto.")

        return cls(
            id=identificador,
            titulo=_texto(datos.get("titulo"), "titulo", obligatorio=True),
            objetivo=_texto(datos.get("objetivo"), "objetivo") or "",
            criterios_aceptacion=_lista_de_texto(
                datos.get("criterios_aceptacion"), "criterios_aceptacion"
            ),
            ambito_archivos=_lista_de_texto(
                datos.get("ambito_archivos"), "ambito_archivos"
            ),
            pruebas_requeridas=_lista_de_texto(
                datos.get("pruebas_requeridas"), "pruebas_requeridas"
            ),
            estado=estado,
            rama=_texto(datos.get("rama"), "rama"),
            worktree=_texto(datos.get("worktree"), "worktree"),
            # `generacion` NO se lee del JSON a propósito: ver el campo.
            # Un valor inyectado ahí se descarta en silencio, que es lo que
            # debe pasar con un dato cuya autoridad es sólo SQLite.
            intentos=_entero(datos.get("intentos", 0), "intentos"),
            max_intentos=_entero(
                datos.get("max_intentos", 3), "max_intentos", minimo=1
            ),
            commit_inicial=_texto(
                datos.get("commit_inicial"), "commit_inicial"
            ),
            creado_en=_texto(datos.get("creado_en"), "creado_en") or "",
            actualizado_en=_texto(
                datos.get("actualizado_en"), "actualizado_en"
            ) or "",
            requiere_decision_humana=_lista_de_diccionarios(
                datos.get("requiere_decision_humana"),
                "requiere_decision_humana",
            ),
            trabajador_id=_texto(datos.get("trabajador_id"), "trabajador_id"),
            pid=pid,
            iniciado_en=_texto(datos.get("iniciado_en"), "iniciado_en"),
            ultimo_latido=_texto(datos.get("ultimo_latido"), "ultimo_latido"),
            ultima_falla=ultima_falla,
            ejecuciones=_lista_de_diccionarios(
                datos.get("ejecuciones"), "ejecuciones"
            ),
            historial=_lista_de_diccionarios(
                datos.get("historial"), "historial"
            ),
        )


# ----------------------------------------------------------------------
# Rutas
# ----------------------------------------------------------------------

def validar_id(identificador: str) -> str:
    if not FORMATO_ID.match(identificador or ""):
        raise ErrorFicha(
            "Identificador inválido: '" + str(identificador) + "'. "
            "Debe tener la forma T-0001."
        )
    return identificador


def carpeta_tareas(raiz: Path) -> Path:
    return Path(raiz).joinpath(*CARPETA_TAREAS)


def ruta_ficha(raiz: Path, identificador: str) -> Path:
    validar_id(identificador)
    return carpeta_tareas(raiz) / (identificador + ".json")


def ruta_relativa_ficha(identificador: str) -> str:
    validar_id(identificador)
    return "/".join(CARPETA_TAREAS) + "/" + identificador + ".json"


# ----------------------------------------------------------------------
# Lectura y escritura
# ----------------------------------------------------------------------

def existe(raiz: Path, identificador: str) -> bool:
    return ruta_ficha(raiz, identificador).is_file()


def leer(raiz: Path, identificador: str) -> Ficha:
    ruta = ruta_ficha(raiz, identificador)

    if not ruta.is_file():
        raise ErrorFicha("No existe la ficha '" + identificador + "'.")

    try:
        crudo = ruta.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ErrorFicha(
            "La ficha '" + identificador + "' no está codificada en UTF-8: "
            + str(error)
        ) from None
    except OSError as error:
        raise ErrorFicha(
            "No se pudo leer la ficha '" + identificador + "': " + str(error)
        ) from None

    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError as error:
        raise ErrorFicha(
            "La ficha '" + identificador + "' no es JSON válido: "
            + str(error)
        ) from None

    ficha = Ficha.desde_dict(datos)

    # El nombre del archivo manda. Si no coinciden, un guardado posterior
    # escribiría sobre OTRA ficha y la destruiría sin aviso.
    if ficha.id != identificador:
        raise ErrorFicha(
            "La ficha del archivo '" + identificador + ".json' declara el "
            "identificador '" + ficha.id + "'. Deben coincidir."
        )

    return ficha


def guardar(
    raiz: Path,
    ficha: Ficha,
    marcar_actualizacion: bool = True,
) -> Path:
    """
    Escritura atómica en tres pasos:

    1. escribir a un archivo temporal en la misma carpeta y sincronizarlo
       a disco;
    2. releer ese temporal y reconstruir la ficha, para validar lo que
       realmente quedó escrito;
    3. reemplazar el archivo definitivo con os.replace, que es atómico.

    Si algo falla, el temporal se elimina y el archivo definitivo anterior
    permanece intacto.
    """
    validar_id(ficha.id)

    if marcar_actualizacion:
        ficha.actualizado_en = ahora_utc()

    if not ficha.creado_en:
        ficha.creado_en = ficha.actualizado_en or ahora_utc()

    destino = ruta_ficha(raiz, ficha.id)
    destino.parent.mkdir(parents=True, exist_ok=True)

    contenido = json.dumps(
        ficha.a_dict(),
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    descriptor, temporal_texto = tempfile.mkstemp(
        dir=str(destino.parent),
        prefix=PREFIJO_TEMPORAL,
        suffix=SUFIJO_TEMPORAL,
    )

    temporal = Path(temporal_texto)

    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline="\n"
        ) as manejador:
            manejador.write(contenido)
            manejador.flush()
            os.fsync(manejador.fileno())

        # Validación: lo escrito debe volver a ser una ficha legal.
        comprobacion = json.loads(temporal.read_text(encoding="utf-8"))
        Ficha.desde_dict(comprobacion)

        os.replace(temporal, destino)

    except BaseException:
        temporal.unlink(missing_ok=True)
        raise

    return destino


def listar(raiz: Path) -> list[Ficha]:
    """
    Todas las fichas, ordenadas por identificador.

    Los archivos .json que no siguen el formato de nombre de una ficha se
    ignoran: son ajenos a la carpeta y no deben paralizar el Supervisor.

    En cambio, una ficha con nombre correcto pero contenido ilegible SÍ
    interrumpe la lectura: sin conocer su ámbito no se puede garantizar la
    regla de un solo escritor, y es preferible detenerse a arriesgarse.
    """
    carpeta = carpeta_tareas(raiz)

    if not carpeta.is_dir():
        return []

    fichas = []

    for ruta in sorted(carpeta.glob("*.json")):
        if not FORMATO_ID.match(ruta.stem):
            continue

        fichas.append(leer(raiz, ruta.stem))

    return fichas


def listar_con_errores(raiz: Path) -> tuple[list[Ficha], list[dict]]:
    """
    Igual que listar(), pero no se detiene ante una ficha ilegible.

    Devuelve (fichas_correctas, errores) para que la interfaz pueda mostrar
    el problema en lugar de caerse.
    """
    carpeta = carpeta_tareas(raiz)

    if not carpeta.is_dir():
        return [], []

    fichas = []
    errores = []

    for ruta in sorted(carpeta.glob("*.json")):
        if not FORMATO_ID.match(ruta.stem):
            errores.append(
                {
                    "archivo": ruta.name,
                    "motivo": "El nombre no corresponde a una ficha de tarea.",
                }
            )
            continue

        try:
            fichas.append(leer(raiz, ruta.stem))
        except ErrorFicha as error:
            errores.append({"archivo": ruta.name, "motivo": str(error)})

    return fichas, errores


# Un temporal recién creado NO está abandonado: es una escritura en vuelo.
# Sin este margen, la recuperación borraba el temporal de OTRO proceso que
# estaba guardando en ese instante, su `os.replace` fallaba con ENOENT y la
# pasada entera se abortaba a medias.
EDAD_TEMPORAL_HUERFANO_S = 300


def temporales_huerfanos(
    raiz: Path, edad_minima_s: int = EDAD_TEMPORAL_HUERFANO_S
) -> list[Path]:
    """
    Temporales abandonados por un corte ocurrido durante una escritura.

    Sólo cuentan los que llevan parados más de `edad_minima_s`. Un temporal
    joven pertenece con toda probabilidad a una escritura en curso, y
    borrarlo rompería a quien la está haciendo.
    """
    carpeta = carpeta_tareas(raiz)

    if not carpeta.is_dir():
        return []

    limite = time.time() - max(0, edad_minima_s)
    abandonados = []

    for temporal in sorted(
        carpeta.glob(PREFIJO_TEMPORAL + "*" + SUFIJO_TEMPORAL)
    ):
        try:
            if temporal.stat().st_mtime <= limite:
                abandonados.append(temporal)
        except OSError:
            # Desapareció mientras mirábamos: no es asunto nuestro.
            continue

    return abandonados
