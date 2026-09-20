"""
Interfaz de línea de comandos del Supervisor de Desarrollo.

Toda la experiencia visible está en español.

Uso desde la raíz del repositorio:

    python -m orquestacion.ingenieria_supervisor estado
    python -m orquestacion.ingenieria_supervisor diagnostico
    python -m orquestacion.ingenieria_supervisor inicializar-estado
    python -m orquestacion.ingenieria_supervisor sincronizar-definiciones
    python -m orquestacion.ingenieria_supervisor verificar T-0001
    python -m orquestacion.ingenieria_supervisor reanudar
    python -m orquestacion.ingenieria_supervisor aprobar T-0001
    python -m orquestacion.ingenieria_supervisor rechazar T-0001 --motivo "..."

Desde A2 las órdenes de consulta leen la base SQLite global del
repositorio; las órdenes que cambian estado escriben en ella.

Esta misma CLI es la que podrá invocar n8n más adelante. La lógica vive en
Python local: si n8n desaparece, el Supervisor sigue funcionando.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ingenieria_nucleo.estados import Estado

from . import RAIZ
from . import estado_global as global_
from . import pruebas as corredor
from . import supervisor as nucleo
from .tarea import ErrorFicha


ANCHO = 74

# Las claves son las del motor; lo que se imprime va acentuado porque es
# texto para una persona. El criterio no se toca aquí: sólo la etiqueta.
ETIQUETAS_VITALIDAD = {
    "ACTIVA": "ACTIVA",
    "LATIDO_VENCIDO": "LATIDO VENCIDO",
    "HUERFANA": "HUÉRFANA",
    "REANUDABLE": "REANUDABLE",
    "ESPERA_HUMANA": "ESPERA HUMANA",
    "FINALIZADA": "FINALIZADA",
}

# PRAGMA synchronous devuelve un número; el nombre es el de SQLite.
SYNCHRONOUS_LEGIBLE = {"0": "OFF", "1": "NORMAL", "2": "FULL", "3": "EXTRA"}


def _preparar_salida() -> None:
    """La consola debe poder mostrar acentos sin depender de su codificación."""
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _titulo(texto: str) -> None:
    print("")
    print("=" * ANCHO)
    print("  " + texto)
    print("=" * ANCHO)
    print("")


def _linea(etiqueta: str, valor) -> None:
    if valor is None or valor == "":
        valor = "—"

    print("  " + (etiqueta + " ").ljust(26, ".") + " " + str(valor))


# ----------------------------------------------------------------------
# Presentación
# ----------------------------------------------------------------------

def mostrar_tablero(raiz: Path) -> int:
    datos = nucleo.tablero(raiz)
    resumen = datos["resumen"]
    base = datos["base_global"]

    _titulo("DESARROLLO — INGENIERÍA LOCAL")

    _linea("Base global SQLite", base["estado"])
    _linea("Ubicación", base["ubicacion_resumida"])
    _linea("Versión de esquema", base["version_esquema"])
    _linea("Última actividad global", resumen["ultima_actividad"])

    if base["estado"] != "ACTIVA":
        print("")
        print("  La base global no está disponible: " + str(base["detalle"]))
        print("")

    print("")
    _linea("Agentes activos", resumen["agentes_activos"])
    _linea("Tareas totales", resumen["totales"])
    _linea("Nuevas", resumen["nuevas"])
    _linea("En ejecución", resumen["en_ejecucion"])
    _linea("Propuestas", resumen["propuestas"])
    _linea("Requieren revisión", resumen["requieren_revision"])
    _linea("Bloqueadas", resumen["bloqueadas"])
    _linea("Reabiertas", resumen["reabiertas"])
    _linea("Aprobadas", resumen["aprobadas"])
    _linea("Rechazadas", resumen["rechazadas"])

    if resumen.get("agentes_sin_senal"):
        print("")
        print(
            "  AVISO: hay ejecuciones sin señal de vida ("
            + ", ".join(resumen["agentes_sin_senal"])
            + "). Mira su vitalidad más abajo."
        )

    if resumen.get("sin_importar"):
        print("")
        print(
            "  FICHAS NO REGISTRADAS EN LA BASE: "
            + ", ".join(resumen["sin_importar"])
        )
        print(
            "      El tablero no escribe nada. Para incorporarlas: "
            "`sincronizar-definiciones`."
        )

    if not datos["tareas"]:
        print("")
        print("  Todavía no hay ninguna ficha de tarea.")

    for tarea in datos["tareas"]:
        print("")
        print("-" * ANCHO)
        print("  " + tarea["id"] + "  " + tarea["titulo"])
        print("-" * ANCHO)

        _linea("Estado", tarea["estado"].upper())
        _linea("Rama", tarea["rama"])
        _linea("Worktree", tarea["worktree"])
        _linea(
            "Intentos",
            str(tarea["intentos"]) + " / " + str(tarea["max_intentos"]),
        )
        _linea(
            "Pruebas",
            str(tarea["pruebas_ok"]) + " / " + str(tarea["pruebas_total"]),
        )
        _linea("Última actualización", tarea["actualizado_en"])
        _linea("Trabajador", tarea["trabajador_id"])
        _linea("Generación", tarea["generacion"])
        _linea("PID", tarea["pid"])
        _linea("Último latido", tarea["ultimo_latido"])

        # El estado dice en qué punto del ciclo está la tarea; la vitalidad,
        # si alguien la está ejecutando AHORA. Una tarea puede quedarse en
        # EN EJECUCIÓN para siempre porque el proceso que la tomó murió, y
        # sin esta línea la consola la muestra igual que una viva.
        _linea(
            "Vitalidad",
            ETIQUETAS_VITALIDAD.get(
                tarea["vitalidad"],
                "—" if tarea["vitalidad"] is None else str(tarea["vitalidad"]),
            )
            + "  "
            + str(tarea["vitalidad_motivo"]),
        )

        # Una FILA de la base que no se pudo interpretar no es un JSON roto:
        # decirlo con el rótulo del archivo mandaba al operador a arreglar
        # una ficha que estaba bien.
        if tarea.get("fila_legible") is False:
            _linea(
                "Fila de la base",
                "NO LEGIBLE (la definición JSON no es el problema; mira "
                "la fila en SQLite)",
            )

        verificacion = tarea.get("ultima_verificacion")

        _linea(
            "Última verificación",
            (
                str(verificacion.get("fecha"))
                + "  "
                + str(verificacion.get("resultado"))
                + (
                    ""
                    if tarea.get("verificacion_vigente")
                    else "  (DE OTRA EJECUCIÓN)"
                )
            )
            if verificacion
            else None,
        )

        # Dónde se verificó. Sin esto, «8 de 8» no dice nada: las pruebas
        # pudieron correr en otro árbol y sobre otro commit.
        if verificacion:
            _linea(
                "Verificado en",
                str(tarea["verificacion_raiz"])
                + "  ("
                + str(tarea["verificacion_rama"])
                + " @ "
                + str(tarea["verificacion_commit"])
                + ")"
                + (
                    "  + CAMBIOS SIN CONFIRMAR"
                    if tarea.get("verificacion_sin_confirmar")
                    else ""
                ),
            )

        if not tarea.get("definicion_legible", True):
            _linea("Definición JSON", "NO LEGIBLE (" + str(tarea["definicion_ruta"]) + ")")

        pendientes = tarea["decisiones_pendientes"]

        _linea(
            "Decisión humana",
            "PENDIENTE (" + str(len(pendientes)) + ")"
            if pendientes
            else ("resueltas" if tarea["decisiones_totales"] else "—"),
        )

        for decision in pendientes:
            print(
                "      · "
                + str(decision.get("clave"))
                + ": "
                + str(decision.get("descripcion"))
            )

        if tarea["ultima_falla"]:
            print("")
            print("  Última falla:")
            for problema in tarea["ultima_falla"].get("problemas", []):
                print("      · " + str(problema))

    if datos["fichas_ilegibles"]:
        print("")
        print("  FICHAS ILEGIBLES:")
        for error in datos["fichas_ilegibles"]:
            print("      · " + error["archivo"] + ": " + error["motivo"])

    if datos["actividad"]:
        print("")
        print("=" * ANCHO)
        print("  ACTIVIDAD RECIENTE")
        print("=" * ANCHO)
        print("")

        for evento in datos["actividad"][:10]:
            anterior = evento.get("estado_anterior") or "—"
            print(
                "  "
                + str(evento.get("fecha"))
                + "  "
                + str(evento.get("tarea"))
                + "  ["
                + str(evento.get("tipo"))
                + "]  "
                + anterior
                + " -> "
                + str(evento.get("estado_nuevo"))
            )
            print("      " + str(evento.get("motivo")))

    print("")

    # Mismo criterio que `diagnostico`: si no se pudo leer la base global,
    # el tablero mostrado no es el estado real y el código de salida lo dice.
    return 0 if base["estado"] == "ACTIVA" else 1


def mostrar_diagnostico(raiz: Path) -> int:
    informe = global_.diagnostico(raiz)

    _titulo("DIAGNÓSTICO DE LA BASE GLOBAL SQLITE")

    _linea("Raíz consultada", informe["raiz"])
    _linea("Git common dir", informe["git_common_dir"])
    _linea("Ruta real de SQLite", informe["ruta"])
    _linea("Existe", "sí" if informe["existe"] else "no")
    _linea("Tamaño (bytes)", informe["tamano_bytes"])
    _linea("Estado de la base", informe["estado"])
    _linea("Detalle", informe["detalle"])
    _linea(
        "Versión de esquema",
        str(informe["version_esquema"])
        + " (esperada "
        + str(informe["version_esperada"])
        + ")",
    )
    _linea("journal_mode", informe["journal_mode"])
    _linea(
        "synchronous",
        None if informe["synchronous"] is None
        else str(informe["synchronous"]) + " (" + SYNCHRONOUS_LEGIBLE.get(
            str(informe["synchronous"]), "desconocido"
        ) + ")",
    )
    _linea("busy_timeout (ms)", informe["busy_timeout_ms"])
    _linea(
        "foreign_keys",
        None if informe["foreign_keys"] is None
        else ("activas" if informe["foreign_keys"] else "inactivas"),
    )
    _linea("integrity_check", informe["integridad"])
    _linea("Tareas registradas", informe["tareas"])
    _linea("Eventos registrados", informe["eventos"])
    _linea("Última actualización", informe["ultima_actualizacion"])

    ultimo = informe["ultimo_evento"]

    _linea(
        "Último evento",
        (
            str(ultimo["fecha"])
            + "  "
            + str(ultimo["tarea"])
            + "  ["
            + str(ultimo["tipo"])
            + "]  "
            + str(ultimo["motivo"])
        )
        if ultimo
        else None,
    )

    _linea("Definiciones JSON", informe["definiciones_json"])
    _linea(
        "Sin importar",
        ", ".join(informe["sin_importar"]) if informe["sin_importar"] else "ninguna",
    )
    _linea(
        "Definición desactualizada",
        ", ".join(informe["desactualizadas"])
        if informe["desactualizadas"]
        else "ninguna",
    )
    _linea(
        "Definición congelada (tarea viva)",
        ", ".join(informe.get("congeladas") or []) or "ninguna",
    )

    if informe["fichas_ilegibles"]:
        print("")
        print("  FICHAS ILEGIBLES:")
        for error in informe["fichas_ilegibles"]:
            print("      · " + error["archivo"] + ": " + error["motivo"])

    print("")

    return 0 if informe["estado"] == "ACTIVA" else 1


def mostrar_ficha(raiz: Path, identificador: str) -> int:
    ficha = nucleo.cargar(raiz, identificador)

    _titulo(ficha.id + " — " + ficha.titulo)

    _linea("Estado", str(ficha.estado).upper())
    _linea("Rama", ficha.rama)
    _linea("Worktree", ficha.worktree)
    _linea(
        "Intentos",
        str(ficha.intentos) + " / " + str(ficha.max_intentos),
    )
    _linea("Commit inicial", ficha.commit_inicial)
    _linea("Creada", ficha.creado_en)
    _linea("Actualizada", ficha.actualizado_en)
    _linea("Trabajador", ficha.trabajador_id)
    # La generación se muestra aquí porque es la credencial con la que una
    # orden se acredita, y hasta ahora sólo la imprimía `tomar`. Si esa
    # salida se perdía —consola cerrada, guion que no la capturó, retoma
    # tras un apagón— no había forma de recuperarla, y la vía declarada,
    # que es la única que detiene a una orden rezagada, quedaba inservible.
    _linea("Generación", ficha.generacion)
    _linea("PID", ficha.pid)
    _linea("Último latido", ficha.ultimo_latido)

    # Por qué no está en PROPUESTO, cuando no lo está. Sin esto, tras una
    # prueba requerida ausente `ver` enseñaba «APROBADO OK 1 de 1» y
    # REQUIERE_REVISION sin ninguna explicación.
    if ficha.ultima_falla:
        print("")
        print("  Última falla:")
        for problema in (ficha.ultima_falla or {}).get("problemas", []):
            print("      · " + str(problema))

    # Si la ficha declara un ámbito que la base no aplicó por estar la tarea
    # viva, hay que decirlo aquí: quien trabaje la tarea posee lo que la
    # base concedió, no lo que diga el archivo que acaba de editar.
    if (
        ficha.ambito_vigente is not None
        and set(ficha.ambito_vigente) != set(ficha.ambito_archivos)
    ):
        print("")
        print("  AVISO: la ficha declara un ámbito distinto del vigente.")
        print("      Vigente (lo que la base concedió y lo único que cuenta")
        print("      para la regla de un solo escritor):")
        for patron in ficha.ambito_vigente:
            print("          · " + patron)
        print("      Declarado en el JSON, en espera de que la tarea deje de")
        print("      estar viva:")
        for patron in ficha.ambito_archivos:
            print("          · " + patron)

    print("")
    print("  Objetivo:")
    print("      " + (ficha.objetivo or "—"))

    if ficha.criterios_aceptacion:
        print("")
        print("  Criterios de aceptación:")
        for criterio in ficha.criterios_aceptacion:
            print("      · " + criterio)

    if ficha.ambito_archivos:
        print("")
        print("  Ámbito de archivos:")
        for patron in ficha.ambito_archivos:
            print("      · " + patron)

    if ficha.pruebas_requeridas:
        print("")
        print("  Pruebas requeridas:")
        for prueba in ficha.pruebas_requeridas:
            print("      · " + prueba)

    if ficha.requiere_decision_humana:
        print("")
        print("  Decisiones humanas:")
        for decision in ficha.requiere_decision_humana:
            marca = "RESUELTA" if decision.get("resuelta") else "PENDIENTE"
            print(
                "      ["
                + marca
                + "] "
                + str(decision.get("clave"))
                + ": "
                + str(decision.get("descripcion"))
            )
            if decision.get("resolucion"):
                print("            resolución: " + str(decision["resolucion"]))

    if ficha.ejecuciones:
        print("")
        print("  Ejecuciones recientes:")
        for ejecucion in ficha.ejecuciones:
            if ejecucion.get("tipo") == "interrupcion":
                print(
                    "      · "
                    + str(ejecucion.get("fecha"))
                    + "  INTERRUMPIDA  "
                    + str(ejecucion.get("motivo"))
                )
            else:
                # Dónde corrió y de qué ejecución es: «8 de 8» a secas no
                # distingue una corrida en el worktree de la tarea de una
                # en otro árbol, ni el verde vigente del de una ejecución
                # que ya no existe.
                donde = ""

                if ejecucion.get("raiz"):
                    donde = (
                        "  en " + str(ejecucion.get("raiz")) + " ("
                        + str(ejecucion.get("rama")) + " @ "
                        + str(ejecucion.get("commit")) + ")"
                        + (
                            "  + CAMBIOS SIN CONFIRMAR"
                            if ejecucion.get("sin_confirmar")
                            else ""
                        )
                        + (
                            "  (DE OTRA EJECUCIÓN)"
                            if ejecucion.get("generacion") is not None
                            and ejecucion.get("generacion") != ficha.generacion
                            else ""
                        )
                    )

                print(
                    "      · "
                    + str(ejecucion.get("fecha"))
                    + "  "
                    + str(ejecucion.get("resultado"))
                    + "  OK "
                    + str(ejecucion.get("ok"))
                    + " de "
                    + str(ejecucion.get("total"))
                    + donde
                )

    print("")

    return 0


# ----------------------------------------------------------------------
# Órdenes
# ----------------------------------------------------------------------

def orden_estado(raiz: Path, argumentos) -> int:
    if argumentos.json:
        datos = nucleo.tablero(raiz)

        print(json.dumps(datos, ensure_ascii=False, indent=2))

        # Mismo criterio que la salida de texto y que `diagnostico --json`:
        # si la base no se pudo leer, lo publicado no es el estado real y
        # el código de salida lo dice. Justo el modo que consume un guion
        # era el que devolvía 0 con la base en ERROR.
        return 0 if datos["base_global"]["estado"] == "ACTIVA" else 1

    return mostrar_tablero(raiz)


def orden_ver(raiz: Path, argumentos) -> int:
    return mostrar_ficha(raiz, argumentos.tarea)


def orden_diagnostico(raiz: Path, argumentos) -> int:
    if argumentos.json:
        informe = global_.diagnostico(raiz)
        print(json.dumps(informe, ensure_ascii=False, indent=2))
        return 0 if informe["estado"] == "ACTIVA" else 1

    return mostrar_diagnostico(raiz)


def orden_inicializar_estado(raiz: Path, argumentos) -> int:
    informe = global_.inicializar_base(raiz)

    esquema = informe["esquema"]
    sincronizacion = informe["sincronizacion"]

    _titulo("INICIALIZACIÓN DEL ESTADO GLOBAL")

    _linea("Ruta de SQLite", informe["ruta"])
    _linea(
        "Esquema",
        "creado en versión " + str(esquema["version_actual"])
        if esquema["version_anterior"] == 0
        else (
            "migrado de "
            + str(esquema["version_anterior"])
            + " a "
            + str(esquema["version_actual"])
            if esquema["aplicadas"]
            else "ya estaba en versión " + str(esquema["version_actual"])
        ),
    )
    _linea("Definiciones importadas", ", ".join(sincronizacion["importadas"]) or "ninguna")
    _linea("Definiciones actualizadas", ", ".join(sincronizacion["actualizadas"]) or "ninguna")
    _linea("Sin cambios", ", ".join(sincronizacion["sin_cambios"]) or "ninguna")

    if sincronizacion["fichas_ilegibles"]:
        print("")
        print("  FICHAS ILEGIBLES:")
        for error in sincronizacion["fichas_ilegibles"]:
            print("      · " + error["archivo"] + ": " + error["motivo"])

    print("")

    return 0


def orden_sincronizar_definiciones(raiz: Path, argumentos) -> int:
    informe = global_.sincronizar_definiciones(raiz)

    _titulo("SINCRONIZACIÓN DE DEFINICIONES")

    _linea("Importadas", ", ".join(informe["importadas"]) or "ninguna")
    _linea("Actualizadas", ", ".join(informe["actualizadas"]) or "ninguna")
    _linea("Sin cambios", ", ".join(informe["sin_cambios"]) or "ninguna")

    # A3.2: no se puede informar como "sin cambios" una edición que está
    # esperando. El usuario editó el ámbito y tiene que saber que no se
    # aplicó, por qué, y que se aplicará sola cuando la tarea se cierre.
    if informe.get("ambito_congelado"):
        print("")
        print("  ÁMBITO NO APLICADO (tareas vivas):")
        for uno in informe["ambito_congelado"]:
            print("      · " + uno["id"] + " [" + str(uno["estado"]) + "]: "
                  + uno["detalle"])

    if informe["fichas_ilegibles"]:
        print("")
        print("  FICHAS ILEGIBLES:")
        for error in informe["fichas_ilegibles"]:
            print("      · " + error["archivo"] + ": " + error["motivo"])

    print("")

    return 0


def orden_crear(raiz: Path, argumentos) -> int:
    ficha = nucleo.crear(
        raiz,
        identificador=argumentos.tarea,
        titulo=argumentos.titulo,
        objetivo=argumentos.objetivo or "",
        criterios_aceptacion=argumentos.criterio or [],
        ambito_archivos=argumentos.ambito or [],
        pruebas_requeridas=argumentos.prueba or [],
        max_intentos=argumentos.max_intentos,
    )

    print("Ficha creada: " + ficha.id + " (" + str(ficha.estado) + ")")
    print("Rama prevista: " + str(ficha.rama))

    return 0


# Código de salida propio de una toma rechazada: no es una avería del
# Supervisor, es el resultado normal de perder una carrera. Quien invoque
# la orden (una persona, un guion o n8n) puede distinguirla de un error.
AYUDA_TRABAJADOR = (
    "Identidad con la que se acredita quien ordena. Sin ella se usa la del propietario que figure en la base, que NO detiene a una orden rezagada: quien la lee estaría suplantando al dueño actual."
)

AYUDA_GENERACION = (
    "Generación de propiedad con la que se acredita la orden. Es lo único que distingue dos ejecuciones del MISMO trabajador. La imprime 'tomar'."
)

CODIGO_TOMA_RECHAZADA = 3

# Código propio de una orden rechazada por propiedad (A3.2): quien la emitió
# ya no es el dueño vigente de la ejecución, o su generación quedó atrás.
#
# Tampoco es una avería: es el resultado normal de llegar tarde, y quien
# invoque la orden necesita distinguirlo del error genérico (2), de la toma
# perdida (3) y del "resultado no deseado" (1).
#
# Se elige el 4 porque 0, 1, 2 y 3 ya están tomados, y el 2 lo está dos
# veces: también lo usa argparse ante un error de uso.
CODIGO_PROPIEDAD_INVALIDA = 4

# Código propio de una creación que llega segunda (A3.3). Perder una carrera
# de creación es un resultado NORMAL, igual que perder una toma: un guion
# que crea tareas en lote necesita distinguir "ya existía" de una avería.
CODIGO_YA_EXISTE = 5

# El árbol de trabajo declarado o heredado no sirve. Se distingue del 2
# (avería genérica) porque lo que tiene que hacer el operador es otra cosa:
# mirar el worktree, no el Supervisor.
CODIGO_WORKTREE_INVALIDO = 6


def orden_tomar(raiz: Path, argumentos) -> int:
    try:
        ficha = nucleo.tomar(
            raiz,
            argumentos.tarea,
            trabajador_id=argumentos.trabajador,
            pid=argumentos.pid,
            git=_git(raiz, argumentos),
            worktree=argumentos.worktree,
        )
    except nucleo.ErrorToma as rechazo:
        print("")
        print("  TOMA RECHAZADA: " + str(rechazo))
        print("")
        _linea("Tarea", rechazo.tarea)
        _linea("Motivo", rechazo.motivo)
        _linea("Estado actual", rechazo.estado)
        _linea("Trabajador actual", rechazo.propietario)
        print("")

        return CODIGO_TOMA_RECHAZADA

    print("Tarea tomada: " + ficha.id)
    print("Estado: " + str(ficha.estado))
    print("Trabajador: " + str(ficha.trabajador_id))
    print("Generación: " + str(ficha.generacion))
    print("Rama exigida: " + str(ficha.rama))
    print("Árbol de ejecución: " + str(ficha.worktree))

    return 0


def orden_latido(raiz: Path, argumentos) -> int:
    ficha = nucleo.latido(
        raiz,
        argumentos.tarea,
        trabajador_id=argumentos.trabajador,
        generacion=argumentos.generacion,
    )

    print("Latido registrado: " + str(ficha.ultimo_latido))

    return 0


def orden_devolver(raiz: Path, argumentos) -> int:
    ficha = nucleo.devolver(
        raiz,
        argumentos.tarea,
        argumentos.motivo or "Tarea devuelta por el trabajador.",
        git=_git(raiz, argumentos),
        trabajador_id=argumentos.trabajador,
        generacion=argumentos.generacion,
    )

    print("Tarea devuelta: " + ficha.id + " -> " + str(ficha.estado))

    return 0


def orden_verificar(raiz: Path, argumentos) -> int:
    informe = nucleo.verificar(
        raiz,
        argumentos.tarea,
        git=_git(raiz, argumentos),
        trabajador_id=argumentos.trabajador,
        generacion=argumentos.generacion,
    )

    _titulo("VERIFICACIÓN DE " + argumentos.tarea)

    print(corredor.resumen_en_texto(informe["corrida"]))

    if informe["problemas"]:
        print("")
        print("  Problemas detectados:")
        for problema in informe["problemas"]:
            print("      · " + problema)

    if informe["decisiones_pendientes"]:
        print("")
        print("  Decisiones humanas pendientes:")
        for decision in informe["decisiones_pendientes"]:
            print(
                "      · "
                + str(decision.get("clave"))
                + ": "
                + str(decision.get("descripcion"))
            )

    print("")
    print("  Estado resultante: " + informe["estado"].upper())
    print("  " + informe["motivo"])

    # El latido acompaña a la corrida y hasta ahora no se veía. Una
    # verificación con cero latidos y otra con veinticinco se mostraban
    # exactamente igual, y la primera deja la tarea sin señal todo el rato.
    print("  Latidos emitidos: " + str(informe.get("latidos")))

    # Dónde corrió: es la funcionalidad central de A3.3 y hasta ahora sólo
    # se veía ejecutando `estado` después.
    _linea(
        "Verificado en",
        str(informe["raiz"])
        + ("  (worktree)" if informe["es_worktree"] else "  (la raíz)"),
    )
    _linea(
        "Rama @ commit",
        str(informe["rama"]) + " @ " + str(informe["commit"])
        + (
            "  + CAMBIOS SIN CONFIRMAR"
            if informe.get("sin_confirmar")
            else ""
        ),
    )

    if informe.get("latido_error"):
        print("  AVISO: el latido falló: " + str(informe["latido_error"]))

    if informe.get("latido_cierre_incompleto"):
        print(
            "  AVISO: el hilo del latido no cerró dentro del plazo; puede "
            "haber escrito después de terminar la verificación."
        )

    if informe["git"]:
        print("")
        print("  Registro en Git: " + informe["git"]["motivo"])

    print("")

    # Código de salida distinto de cero cuando la tarea no quedó propuesta,
    # para que un orquestador externo pueda decidir sin leer el texto.
    return 0 if informe["estado"] == str(Estado.PROPUESTO) else 1


def orden_decidir(raiz: Path, argumentos) -> int:
    ficha = nucleo.decidir(
        raiz,
        argumentos.tarea,
        argumentos.clave,
        argumentos.resolucion,
    )

    pendientes = len(ficha.decisiones_pendientes())

    print("Decisión '" + argumentos.clave + "' resuelta.")
    print("Decisiones pendientes: " + str(pendientes))

    return 0


def orden_aprobar(raiz: Path, argumentos) -> int:
    ficha = nucleo.aprobar(
        raiz,
        argumentos.tarea,
        argumentos.comentario or "",
        git=_git(raiz, argumentos),
    )

    print("Tarea aprobada por decisión humana: " + ficha.id)
    print("Estado: " + str(ficha.estado))

    return 0


def orden_rechazar(raiz: Path, argumentos) -> int:
    ficha = nucleo.rechazar(
        raiz,
        argumentos.tarea,
        argumentos.motivo,
        git=_git(raiz, argumentos),
    )

    print("Tarea rechazada: " + ficha.id + " -> " + str(ficha.estado))

    return 0


def orden_reabrir(raiz: Path, argumentos) -> int:
    ficha = nucleo.reabrir(
        raiz,
        argumentos.tarea,
        argumentos.motivo or "Reapertura humana.",
        git=_git(raiz, argumentos),
    )

    print("Tarea reabierta: " + ficha.id + " -> " + str(ficha.estado))

    return 0


def orden_bloquear(raiz: Path, argumentos) -> int:
    ficha = nucleo.bloquear(
        raiz,
        argumentos.tarea,
        argumentos.motivo,
        git=_git(raiz, argumentos),
    )

    print("Tarea bloqueada: " + ficha.id + " -> " + str(ficha.estado))

    return 0


def orden_reanudar(raiz: Path, argumentos) -> int:
    informe = nucleo.reanudar(raiz, git=_git(raiz, argumentos))

    _titulo("REANUDACIÓN")

    _linea("Tareas en ejecución revisadas", informe["revisadas"])
    _linea("Siguen activas", len(informe["activas"]))
    _linea("Huérfanas recuperadas", len(informe["huerfanas"]))
    # Desde A3.3 una fila incompleta NO se recupera: se informa y decide
    # una persona. La línea «Inconsistentes recuperadas» salía siempre 0 y
    # el grupo que sí importa no se imprimía: el operador no podía dar la
    # orden humana que el motor le pedía, porque no sabía que hacía falta.
    _linea(
        "Inconsistentes (sin tocar)", len(informe["inconsistentes_sin_tocar"])
    )
    _linea("Latido vencido (sin tocar)", len(informe["latido_vencido"]))
    _linea("Sin definición en este árbol", len(informe["sin_definicion"]))
    _linea("Temporales eliminados", len(informe["temporales_eliminados"]))

    for grupo, etiqueta in (
        ("activas", "ACTIVAS"),
        ("huerfanas", "HUÉRFANAS RECUPERADAS"),
        (
            "inconsistentes_sin_tocar",
            "INCONSISTENTES (no recuperadas: una fila incompleta no "
            "demuestra abandono; decide una persona con `reabrir`)",
        ),
        ("sin_definicion", "SIN DEFINICIÓN EN ESTE ÁRBOL (no modificadas)"),
        (
            "reclamadas_mientras_tanto",
            "NO RECUPERADAS: RETOMADAS O CON SEÑAL DE VIDA DURANTE LA "
            "RECUPERACIÓN",
        ),
        ("latido_vencido", "LATIDO VENCIDO (no recuperadas: mirar a mano)"),
        ("worktree_ausente", "WORKTREE REGISTRADO QUE YA NO EXISTE"),
        (
            "espejo_no_regenerado",
            "RECUPERADAS EN LA BASE PERO SIN ESPEJO JSON (la siguiente "
            "orden lo regenera)",
        ),
    ):
        if informe[grupo]:
            print("")
            print("  " + etiqueta + ":")
            for elemento in informe[grupo]:
                print("      · " + elemento["id"] + ": " + elemento["motivo"])

    if informe["fichas_ilegibles"]:
        print("")
        print("  FICHAS ILEGIBLES:")
        for error in informe["fichas_ilegibles"]:
            print("      · " + error["archivo"] + ": " + error["motivo"])

    print("")

    # 1 cuando quedan tareas que piden a una persona: latido vencido, árbol
    # ausente, fila incompleta, espejo sin regenerar o sin definición en
    # este árbol. Un guion de arranque no podía distinguir «todo
    # recuperado» de «hay algo que mirar» sin leer el texto.
    pendientes_de_persona = any(
        informe[grupo]
        for grupo in (
            "latido_vencido",
            "worktree_ausente",
            "inconsistentes_sin_tocar",
            "espejo_no_regenerado",
            "sin_definicion",
            "fichas_ilegibles",
        )
    )

    return 1 if pendientes_de_persona else 0


def orden_pruebas(raiz: Path, argumentos) -> int:
    corrida = corredor.ejecutar_todas(raiz)

    _titulo("CORREDOR ÚNICO DE PRUEBAS")

    print(corredor.resumen_en_texto(corrida))

    if argumentos.detalle:
        for uno in corrida["detalle"]:
            if uno["veredicto"] != corredor.VEREDICTO_OK and uno["salida"]:
                print("")
                print("-" * ANCHO)
                print("  " + uno["prueba"] + "  [" + uno["veredicto"] + "]")
                print("-" * ANCHO)
                print(uno["salida"])

    print("")

    return 0 if corrida["resultado"] == corredor.RESULTADO_APROBADO else 1


def _git(raiz: Path, argumentos):
    if getattr(argumentos, "sin_git", False):
        return None

    return nucleo.Git(raiz)


# ----------------------------------------------------------------------
# Análisis de argumentos
# ----------------------------------------------------------------------

CODIGOS_DE_SALIDA = """\
Códigos de salida:
  0   la orden se completó.
  1   la orden se completó pero el resultado no es el deseado (por ejemplo,
      una verificación que no deja la tarea en PROPUESTO, o una
      reanudación que deja tareas que una persona debe mirar).
  2   error de uso o avería del Supervisor.
  3   toma rechazada: la tarea ya la tiene otro, o su estado no la admite.
  4   orden rechazada por propiedad: identidad, generación o estado no
      coinciden con los de la ejecución vigente.
  5   la tarea ya existe.
  6   el árbol de trabajo declarado o heredado no sirve.
"""


class AyudaEnEspanol(argparse.RawDescriptionHelpFormatter):
    """
    Formateador con los rótulos de `argparse` en español.

    La regla de idioma del proyecto vale también para la primera pantalla
    que ve cualquiera. `usage`, `positional arguments` y `options` son
    cadenas de la biblioteca, no nombres exigidos por ninguna API.
    """

    def add_usage(self, uso, acciones, grupos, prefijo=None):
        # `None` significa «pon el rótulo»; la cadena vacía la usa argparse
        # internamente para calcular el `prog` de los subanalizadores, y
        # ahí no debe aparecer ningún rótulo (salía «Uso: Uso: ...»).
        if prefijo is None:
            prefijo = "Uso: "

        return super().add_usage(uso, acciones, grupos, prefijo)


def _en_espanol(analizador: argparse.ArgumentParser) -> argparse.ArgumentParser:
    analizador._positionals.title = "Órdenes y argumentos"
    analizador._optionals.title = "Opciones"

    return analizador


def construir_analizador() -> argparse.ArgumentParser:
    analizador = argparse.ArgumentParser(
        prog="ingenieria_supervisor",
        description="Supervisor de Desarrollo de Ingeniería Local.",
        formatter_class=AyudaEnEspanol,
        epilog=CODIGOS_DE_SALIDA,
        add_help=False,
    )

    _en_espanol(analizador)

    # `argparse` rotula su ayuda en inglés («options», «show this help
    # message and exit»). Toda la interfaz de este proyecto va en español,
    # así que la opción se declara a mano.
    analizador.add_argument(
        "-h",
        "--ayuda",
        action="help",
        help="Mostrar esta ayuda y salir.",
    )

    analizador.add_argument(
        "--raiz",
        default=str(RAIZ),
        help="Raíz del repositorio (por omisión, la del propio Supervisor).",
    )

    analizador.add_argument(
        "--sin-git",
        dest="sin_git",
        action="store_true",
        help="No registrar la transición de la ficha en Git.",
    )

    ordenes = analizador.add_subparsers(
        dest="orden", required=True, title="Órdenes"
    )

    # Cada subanalizador trae sus propios rótulos en inglés y su propia
    # `-h`. Se envuelve `add_parser` para no repetir lo mismo diecisiete
    # veces ni olvidarlo en la siguiente orden que se añada.
    _crudo = ordenes.add_parser

    def add_parser(nombre, **extras):
        extras.setdefault("add_help", False)
        extras.setdefault("formatter_class", AyudaEnEspanol)

        sub = _crudo(nombre, **extras)
        sub.add_argument(
            "-h", "--ayuda", action="help", help="Mostrar esta ayuda y salir."
        )

        return _en_espanol(sub)

    ordenes.add_parser = add_parser

    estado = ordenes.add_parser("estado", help="Tablero de tareas.")
    estado.add_argument(
        "--json", action="store_true", help="Salida en formato JSON."
    )
    estado.set_defaults(funcion=orden_estado)

    ver = ordenes.add_parser("ver", help="Detalle de una tarea.")
    ver.add_argument("tarea")
    ver.set_defaults(funcion=orden_ver)

    diagnostico = ordenes.add_parser(
        "diagnostico", help="Diagnóstico de la base SQLite global."
    )
    diagnostico.add_argument(
        "--json", action="store_true", help="Salida en formato JSON."
    )
    diagnostico.set_defaults(funcion=orden_diagnostico)

    inicializar = ordenes.add_parser(
        "inicializar-estado",
        help="Crear la base SQLite global e importar las definiciones.",
    )
    inicializar.set_defaults(funcion=orden_inicializar_estado)

    sincronizar = ordenes.add_parser(
        "sincronizar-definiciones",
        help="Importar o refrescar las fichas JSON en la base global.",
    )
    sincronizar.set_defaults(funcion=orden_sincronizar_definiciones)

    crear = ordenes.add_parser("crear", help="Crear una ficha de tarea.")
    crear.add_argument("tarea")
    crear.add_argument("--titulo", required=True)
    crear.add_argument("--objetivo")
    crear.add_argument("--criterio", action="append")
    crear.add_argument("--ambito", action="append")
    crear.add_argument("--prueba", action="append")
    crear.add_argument("--max-intentos", dest="max_intentos", type=int, default=3)
    crear.set_defaults(funcion=orden_crear)

    tomar = ordenes.add_parser("tomar", help="Reclamar una tarea.")
    tomar.add_argument("tarea")
    tomar.add_argument("--trabajador")
    tomar.add_argument(
        "--pid",
        type=int,
        help=(
            "PID del trabajador real y duradero. Sin esta opción se registra "
            "el del propio mandato, que termina de inmediato."
        ),
    )
    tomar.add_argument(
        "--worktree",
        help=(
            "Árbol de trabajo donde se ejecutará la tarea. Debe ser un "
            "worktree que Git tenga registrado para este repositorio "
            "(`git worktree list`). Sin esta opción, el árbol de la "
            "ejecución es la raíz desde la que se toma (--raiz), y "
            "`verificar` correrá allí aunque se invoque desde otro árbol."
        ),
    )
    tomar.set_defaults(funcion=orden_tomar)

    latido = ordenes.add_parser("latido", help="Señal de vida del trabajador.")
    latido.add_argument("tarea")
    latido.add_argument("--trabajador", help=AYUDA_TRABAJADOR)
    latido.add_argument("--generacion", type=int, help=AYUDA_GENERACION)
    latido.set_defaults(funcion=orden_latido)

    devolver = ordenes.add_parser("devolver", help="Soltar una tarea tomada.")
    devolver.add_argument("tarea")
    devolver.add_argument("--motivo")
    devolver.add_argument("--trabajador", help=AYUDA_TRABAJADOR)
    devolver.add_argument("--generacion", type=int, help=AYUDA_GENERACION)
    devolver.set_defaults(funcion=orden_devolver)

    verificar = ordenes.add_parser(
        "verificar", help="Ejecutar el filtro de pruebas y decidir el estado."
    )
    verificar.add_argument("tarea")
    verificar.add_argument("--trabajador", help=AYUDA_TRABAJADOR)
    verificar.add_argument("--generacion", type=int, help=AYUDA_GENERACION)
    verificar.set_defaults(funcion=orden_verificar)

    decidir = ordenes.add_parser(
        "decidir", help="Resolver una decisión humana pendiente."
    )
    decidir.add_argument("tarea")
    decidir.add_argument("--clave", required=True)
    decidir.add_argument("--resolucion", required=True)
    decidir.set_defaults(funcion=orden_decidir)

    aprobar = ordenes.add_parser("aprobar", help="Aprobación humana.")
    aprobar.add_argument("tarea")
    aprobar.add_argument("--comentario")
    aprobar.set_defaults(funcion=orden_aprobar)

    rechazar = ordenes.add_parser("rechazar", help="Rechazo humano.")
    rechazar.add_argument("tarea")
    rechazar.add_argument("--motivo", required=True)
    rechazar.set_defaults(funcion=orden_rechazar)

    reabrir = ordenes.add_parser("reabrir", help="Reabrir una tarea.")
    reabrir.add_argument("tarea")
    reabrir.add_argument("--motivo")
    reabrir.set_defaults(funcion=orden_reabrir)

    bloquear = ordenes.add_parser("bloquear", help="Bloquear una tarea.")
    bloquear.add_argument("tarea")
    bloquear.add_argument("--motivo", required=True)
    bloquear.set_defaults(funcion=orden_bloquear)

    reanudar = ordenes.add_parser(
        "reanudar", help="Recuperar tareas tras un cierre o apagón."
    )
    reanudar.set_defaults(funcion=orden_reanudar)

    pruebas = ordenes.add_parser(
        "pruebas", help="Ejecutar el corredor único de pruebas."
    )
    pruebas.add_argument(
        "--detalle",
        action="store_true",
        help="Mostrar la salida real de las pruebas que no quedaron en OK.",
    )
    pruebas.set_defaults(funcion=orden_pruebas)

    return analizador


def principal(argumentos_crudos: list[str] | None = None) -> int:
    _preparar_salida()

    analizador = construir_analizador()
    argumentos = analizador.parse_args(argumentos_crudos)

    raiz = Path(argumentos.raiz).resolve()

    try:
        return argumentos.funcion(raiz, argumentos)
    except nucleo.ErrorCreacion as choque:
        # Antes que el genérico, por el mismo motivo que ErrorPropiedad:
        # hereda de ErrorSupervisor y si no se capturase aquí colapsaría en
        # el código 2, indistinguible de una avería.
        print("")
        print("  NO CREADA: " + str(choque))
        print("")

        return CODIGO_YA_EXISTE
    except nucleo.ErrorPropiedad as rechazo:
        # Antes que el genérico: ErrorPropiedad hereda de ErrorSupervisor y
        # si no se capturase aquí colapsaría en el código 2, indistinguible
        # de una avería. Se atiende en un solo sitio para que TODAS las
        # órdenes del ciclo den el mismo código, no sólo las que hoy existen.
        print("")
        print("  ORDEN RECHAZADA: " + str(rechazo))
        print("")
        _linea("Tarea", rechazo.tarea)
        _linea("Motivo", rechazo.motivo)
        _linea("Estado actual", rechazo.estado)
        _linea("Propietario que ordenó", rechazo.propietario)
        _linea("Propietario vigente", rechazo.propietario_vigente)
        _linea("Generación de la orden", rechazo.generacion)
        _linea("Generación vigente", rechazo.generacion_vigente)
        print("")

        return CODIGO_PROPIEDAD_INVALIDA
    except nucleo.ErrorWorktree as problema:
        # Código propio: la respuesta del operador no es la misma que ante
        # una avería del Supervisor. Aquí hay que mirar el árbol.
        print("")
        print("  ÁRBOL DE TRABAJO NO VÁLIDO: " + str(problema))
        print("")

        return CODIGO_WORKTREE_INVALIDO
    except (
        nucleo.ErrorSupervisor,
        ErrorFicha,
        global_.ErrorEstadoGlobal,
    ) as error:
        print("")
        print("  ERROR: " + str(error))
        print("")
        return 2
    except Exception as error:
        # Una avería que no es de las conocidas —una fila de la base que no
        # se puede interpretar, un error de programación— salía como
        # traceback en inglés con código 1, que la ayuda define como «se
        # completó pero el resultado no es el deseado». No se completó: es
        # el 2, y en español.
        print("")
        print(
            "  AVERÍA DEL SUPERVISOR: " + type(error).__name__ + ": "
            + str(error)
        )
        print("")
        return 2


if __name__ == "__main__":
    sys.exit(principal())
