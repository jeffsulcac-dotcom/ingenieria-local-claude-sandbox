"""
Interfaz de línea de comandos del Supervisor de Desarrollo.

Toda la experiencia visible está en español.

Uso desde la raíz del repositorio:

    python -m orquestacion.ingenieria_supervisor estado
    python -m orquestacion.ingenieria_supervisor verificar T-0001
    python -m orquestacion.ingenieria_supervisor reanudar
    python -m orquestacion.ingenieria_supervisor aprobar T-0001
    python -m orquestacion.ingenieria_supervisor rechazar T-0001 --motivo "..."

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
from . import pruebas as corredor
from . import supervisor as nucleo
from .tarea import ErrorFicha, leer


ANCHO = 74


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

    _titulo("DESARROLLO — INGENIERÍA LOCAL")

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
        _linea("PID", tarea["pid"])
        _linea("Último latido", tarea["ultimo_latido"])

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
                + "  "
                + anterior
                + " -> "
                + str(evento.get("estado_nuevo"))
            )
            print("      " + str(evento.get("motivo")))

    print("")

    return 0


def mostrar_ficha(raiz: Path, identificador: str) -> int:
    ficha = leer(raiz, identificador)

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
    _linea("PID", ficha.pid)
    _linea("Último latido", ficha.ultimo_latido)

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
                print(
                    "      · "
                    + str(ejecucion.get("fecha"))
                    + "  "
                    + str(ejecucion.get("resultado"))
                    + "  OK "
                    + str(ejecucion.get("ok"))
                    + " de "
                    + str(ejecucion.get("total"))
                )

    print("")

    return 0


# ----------------------------------------------------------------------
# Órdenes
# ----------------------------------------------------------------------

def orden_estado(raiz: Path, argumentos) -> int:
    if argumentos.json:
        print(
            json.dumps(nucleo.tablero(raiz), ensure_ascii=False, indent=2)
        )
        return 0

    return mostrar_tablero(raiz)


def orden_ver(raiz: Path, argumentos) -> int:
    return mostrar_ficha(raiz, argumentos.tarea)


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


def orden_tomar(raiz: Path, argumentos) -> int:
    ficha = nucleo.tomar(
        raiz,
        argumentos.tarea,
        trabajador_id=argumentos.trabajador,
        pid=argumentos.pid,
        git=_git(raiz, argumentos),
    )

    print("Tarea tomada: " + ficha.id)
    print("Estado: " + str(ficha.estado))
    print("Trabajador: " + str(ficha.trabajador_id))
    print("Rama exigida: " + str(ficha.rama))

    return 0


def orden_latido(raiz: Path, argumentos) -> int:
    ficha = nucleo.latido(raiz, argumentos.tarea)

    print("Latido registrado: " + str(ficha.ultimo_latido))

    return 0


def orden_devolver(raiz: Path, argumentos) -> int:
    ficha = nucleo.devolver(
        raiz,
        argumentos.tarea,
        argumentos.motivo or "Tarea devuelta por el trabajador.",
        git=_git(raiz, argumentos),
    )

    print("Tarea devuelta: " + ficha.id + " -> " + str(ficha.estado))

    return 0


def orden_verificar(raiz: Path, argumentos) -> int:
    informe = nucleo.verificar(
        raiz,
        argumentos.tarea,
        git=_git(raiz, argumentos),
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
    _linea("Inconsistentes recuperadas", len(informe["inconsistentes"]))
    _linea("Temporales eliminados", len(informe["temporales_eliminados"]))

    for grupo, etiqueta in (
        ("activas", "ACTIVAS"),
        ("huerfanas", "HUÉRFANAS RECUPERADAS"),
        ("inconsistentes", "INCONSISTENTES RECUPERADAS"),
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

    return 0


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

def construir_analizador() -> argparse.ArgumentParser:
    analizador = argparse.ArgumentParser(
        prog="ingenieria_supervisor",
        description="Supervisor de Desarrollo de Ingeniería Local.",
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

    ordenes = analizador.add_subparsers(dest="orden", required=True)

    estado = ordenes.add_parser("estado", help="Tablero de tareas.")
    estado.add_argument(
        "--json", action="store_true", help="Salida en formato JSON."
    )
    estado.set_defaults(funcion=orden_estado)

    ver = ordenes.add_parser("ver", help="Detalle de una tarea.")
    ver.add_argument("tarea")
    ver.set_defaults(funcion=orden_ver)

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
    tomar.set_defaults(funcion=orden_tomar)

    latido = ordenes.add_parser("latido", help="Señal de vida del trabajador.")
    latido.add_argument("tarea")
    latido.set_defaults(funcion=orden_latido)

    devolver = ordenes.add_parser("devolver", help="Soltar una tarea tomada.")
    devolver.add_argument("tarea")
    devolver.add_argument("--motivo")
    devolver.set_defaults(funcion=orden_devolver)

    verificar = ordenes.add_parser(
        "verificar", help="Ejecutar el filtro de pruebas y decidir el estado."
    )
    verificar.add_argument("tarea")
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
    except (nucleo.ErrorSupervisor, ErrorFicha) as error:
        print("")
        print("  ERROR: " + str(error))
        print("")
        return 2


if __name__ == "__main__":
    sys.exit(principal())
