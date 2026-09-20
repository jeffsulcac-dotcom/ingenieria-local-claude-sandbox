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
                tarea["vitalidad"], str(tarea["vitalidad"])
            )
            + "  "
            + str(tarea["vitalidad_motivo"]),
        )

        verificacion = tarea.get("ultima_verificacion")

        _linea(
            "Última verificación",
            (
                str(verificacion.get("fecha"))
                + "  "
                + str(verificacion.get("resultado"))
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
                + ")",
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


def orden_tomar(raiz: Path, argumentos) -> int:
    try:
        ficha = nucleo.tomar(
            raiz,
            argumentos.tarea,
            trabajador_id=argumentos.trabajador,
            pid=argumentos.pid,
            git=_git(raiz, argumentos),
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
    _linea("Sin definición en este árbol", len(informe["sin_definicion"]))
    _linea("Temporales eliminados", len(informe["temporales_eliminados"]))

    for grupo, etiqueta in (
        ("activas", "ACTIVAS"),
        ("huerfanas", "HUÉRFANAS RECUPERADAS"),
        ("inconsistentes", "INCONSISTENTES RECUPERADAS"),
        ("sin_definicion", "SIN DEFINICIÓN EN ESTE ÁRBOL (no modificadas)"),
        ("reclamadas_mientras_tanto", "RECLAMADAS DURANTE LA RECUPERACIÓN"),
        ("latido_vencido", "LATIDO VENCIDO (no recuperadas: mirar a mano)"),
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
    except (
        nucleo.ErrorSupervisor,
        ErrorFicha,
        global_.ErrorEstadoGlobal,
    ) as error:
        print("")
        print("  ERROR: " + str(error))
        print("")
        return 2


if __name__ == "__main__":
    sys.exit(principal())
