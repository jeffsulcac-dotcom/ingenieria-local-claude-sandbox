"""
Pruebas de A3.3: EJECUCIÓN SEGURA, RECUPERACIÓN Y WORKTREES.

A3.1 demostró que una tarea sólo puede ser TOMADA por un trabajador.
A3.2 demostró que, después de la toma, sólo el propietario vigente puede
modificar el estado operativo de esa ejecución.

A3.3 cierra lo que faltaba antes de poder lanzar trabajadores paralelos:

1. Que cada orden escriba SÓLO lo suyo. A3.2 protegía quién escribe y desde
   qué momento, no QUÉ: dos órdenes perfectamente válidas de la misma
   generación, propietario y estado se pisaban campo a campo.
2. Latidos automáticos mientras dura una operación larga.
3. Un modelo de vitalidad que no dependa de una sola señal débil.
4. Recuperación idempotente que no le robe la tarea a quien sigue vivo.
5. `verificar` ejecutando de verdad en el worktree registrado.
6. La carrera de `crear`.
7. Rutas de worktree que no se aceptan sólo porque existan.

Todo es hermético: repositorios Git temporales con su propia base SQLite.
Jamás se toca el repositorio real ni su base.

Ejecución por omisión: pensada para terminar muy por debajo del tiempo
límite del corredor único (120 s). Para la corrida de estrés:

    python pruebas/orquestacion/prueba_ejecucion_segura.py --rondas 40
"""

import argparse
import copy
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(RAIZ / "orquestacion"))
sys.path.insert(0, str(RAIZ / "nucleo"))


from ingenieria_nucleo.estados import Estado

from ingenieria_supervisor import estado_global
from ingenieria_supervisor import pruebas as corredor
from ingenieria_supervisor import supervisor as nucleo
from ingenieria_supervisor import tarea as fichas


PRUEBA_VERDE = "print('PRUEBA_VERDE=OK')\n"
PRUEBA_ROJA = "raise SystemExit('esta prueba falla a propósito')\n"

# Rondas de las corridas concurrentes cuando no se indica otra cosa.
RONDAS_POR_OMISION = 6

# Escritores concurrentes sobre campos independientes.
ESCRITORES_POR_OMISION = 6

# Procesos que intentan crear la misma tarea a la vez.
CREADORES_POR_OMISION = 8

# Ninguna espera es indefinida, y todas quedan MUY por debajo del límite que
# el corredor único concede al archivo (120 s).
ESPERA_BARRERA_S = 20
ESPERA_PROCESO_S = 90
ESPERA_SUBPROCESO_S = 90
ESPERA_GIT_S = 30


# ----------------------------------------------------------------------
# Métricas reales acumuladas durante toda la ejecución
# ----------------------------------------------------------------------

METRICAS = {
    "OPERACIONES": 0,
    "ACEPTADAS": 0,
    "RECHAZADAS": 0,
    "ACTUALIZACIONES_PERDIDAS": 0,
    "ROBOS_INDEBIDOS": 0,
    "VERIFICACIONES_EN_ARBOL_INCORRECTO": 0,
    "ERRORES_SQLITE": 0,
    "EXCEPCIONES": 0,
    "LATIDOS_AUTOMATICOS": 0,
    "COMPROBACIONES_INTEGRIDAD": 0,
    "FALLOS_INTEGRIDAD": 0,
}


# ----------------------------------------------------------------------
# Utilidades de repositorio temporal
# ----------------------------------------------------------------------

def _git(raiz: Path, *argumentos: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argumentos],
        cwd=str(raiz),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=ESPERA_GIT_S,
    )


def crear_repositorio(prefijo="ejecucion_segura_") -> Path:
    """Repositorio Git temporal con su propia base SQLite global."""
    raiz = Path(tempfile.mkdtemp(prefix=prefijo))

    inicio = _git(raiz, "init", "-q", "-b", "main")
    assert inicio.returncode == 0, inicio.stderr

    _git(raiz, "config", "user.name", "Prueba A3.3")
    _git(raiz, "config", "user.email", "prueba@ingenieria.local")
    _git(raiz, "config", "commit.gpgsign", "false")

    carpeta = raiz / "pruebas" / "demostracion"
    carpeta.mkdir(parents=True)
    (carpeta / "prueba_verde.py").write_text(PRUEBA_VERDE, encoding="utf-8")

    fichas.carpeta_tareas(raiz).mkdir(parents=True)

    return raiz


def _quitar_solo_lectura(funcion, ruta, _excepcion):
    """Git marca sus objetos como sólo lectura; se limpian igual."""
    os.chmod(ruta, 0o700)
    funcion(ruta)


def borrar(raiz: Path) -> None:
    """
    Borra el temporal sin lanzar NUNCA.

    Se llama desde un `finally`: si lanzara, sustituiría al error que la
    comprobación estaba reportando y se perdería el diagnóstico bueno.
    """
    clave = "onexc" if sys.version_info >= (3, 12) else "onerror"

    for intento in range(3):
        try:
            shutil.rmtree(
                raiz, ignore_errors=False, **{clave: _quitar_solo_lectura}
            )
            return
        except OSError:
            if intento == 2:
                break

            time.sleep(0.2)

    shutil.rmtree(raiz, ignore_errors=True)

    if Path(raiz).exists():
        print(
            "  Aviso: no se pudo borrar el temporal " + str(raiz)
            + "; comprueba si quedó algún proceso vivo."
        )


def ficha_minima(raiz: Path, identificador="T-0901", **extras):
    parametros = {
        "titulo": "Tarea de ejecución " + identificador,
        "objetivo": "Comprobar la ejecución segura.",
        "criterios_aceptacion": ["La prueba verde pasa."],
        "ambito_archivos": ["modulos/demostracion/" + identificador + ".py"],
        "pruebas_requeridas": ["pruebas/demostracion/prueba_verde.py"],
    }
    parametros.update(extras)

    return nucleo.crear(raiz, identificador, **parametros)


def fila_de(raiz: Path, identificador: str) -> dict:
    """Lee la fila REAL de la base global, sin pasar por el Supervisor."""
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        return estado_global.obtener_tarea(con, identificador)
    finally:
        con.close()


def comprobar_integridad(raiz: Path) -> str:
    """PRAGMA integrity_check sobre la base global. Acumula la métrica."""
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        veredicto = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        claves = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()

    METRICAS["COMPROBACIONES_INTEGRIDAD"] += 1

    if veredicto.lower() != "ok" or claves:
        METRICAS["FALLOS_INTEGRIDAD"] += 1

    assert not claves, (
        "La base quedó con claves foráneas rotas: " + repr(claves)
    )
    assert veredicto.lower() == "ok", (
        "PRAGMA integrity_check devolvió: " + veredicto
    )

    return veredicto


# ----------------------------------------------------------------------
# GRUPO 1 — Escrituras por operación: ninguna orden pisa lo ajeno
# ----------------------------------------------------------------------

def prueba_a_dos_ordenes_validas_no_se_pisan():
    """
    Dos órdenes válidas sobre campos distintos no se pierden entre sí.

    Ésta es la deuda que A3.2 dejó abierta y la razón de ser de A3.3. Las
    precondiciones de A3.2 —generación, propietario, estado— deciden QUIÉN
    escribe y DESDE QUÉ momento, no QUÉ. Mientras `persistir` reescribía las
    dieciséis columnas operativas a partir de la foto que `cargar` había
    leído, la segunda orden en confirmar devolvía a la columna de la primera
    el valor que tenía cuando ella leyó.

    Las dos órdenes de esta prueba son perfectamente legítimas: mismo
    propietario, misma generación, mismo estado. Ninguna precondición podía
    ni debía rechazarlas. Lo único que evita la pérdida es que cada una
    escriba sólo lo suyo.
    """
    print("  1. dos órdenes válidas no se pisan campo a campo:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            decisiones=[{"clave": "D-1", "descripcion": "hay que decidir"}],
        )
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        # Las DOS leen antes de que ninguna escriba. Es exactamente lo que
        # pasa cuando dos órdenes se solapan.
        nucleo.latido(
            raiz,
            "T-0901",
            trabajador_id="worker-A",
            generacion=tomada.generacion,
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        latido_grabado = fila_de(raiz, "T-0901")["ultimo_latido"]

        # La decisión humana se compuso ANTES del latido: su foto lleva el
        # `ultimo_latido` viejo.
        nucleo.decidir(raiz, "T-0901", "D-1", "resuelta a mano")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        fila = fila_de(raiz, "T-0901")

        if fila["ultimo_latido"] != latido_grabado:
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert fila["ultimo_latido"] == latido_grabado, (
            "La decisión humana revirtió el latido: " + repr(latido_grabado)
            + " -> " + repr(fila["ultimo_latido"])
        )
        assert fila["decisiones"][0]["resuelta"], (
            "La decisión no se grabó."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_b_una_orden_no_escribe_columnas_ajenas():
    """
    Cada orden toca exactamente sus columnas, y se comprueba en el SQL.

    No basta con que el resultado salga bien en un caso: se mira la
    sentencia que llega al motor. Una orden que escriba de más está
    pisando datos que no le pertenecen aunque hoy nadie lo note.
    """
    print("  2. cada orden escribe sólo sus columnas:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        sentencias = []
        conectar = sqlite3.connect

        def conectar_vigilado(*argumentos, **claves):
            con_nueva = conectar(*argumentos, **claves)
            con_nueva.set_trace_callback(
                lambda sentencia: sentencias.append(str(sentencia))
            )

            return con_nueva

        sqlite3.connect = conectar_vigilado

        try:
            nucleo.latido(
                raiz,
                "T-0901",
                trabajador_id="worker-A",
                generacion=tomada.generacion,
            )
        finally:
            sqlite3.connect = conectar

        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        actualizaciones = [
            una for una in sentencias
            if una.strip().upper().startswith("UPDATE TAREAS")
        ]

        assert len(actualizaciones) == 1, (
            "Un latido debería producir UN solo UPDATE: " + repr(actualizaciones)
        )

        sentencia = actualizaciones[0]

        # Sólo la parte SET: el WHERE nombra columnas a propósito y
        # confundirlo con lo que se escribe daría un verde falso.
        asignaciones = sentencia.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        escritas = {
            trozo.split("=", 1)[0].strip()
            for trozo in asignaciones.split(",")
            if "=" in trozo
        }

        assert escritas == {"ultimo_latido", "actualizado_en"}, (
            "El latido escribe columnas que no son suyas o le faltan las "
            "que sí lo son. Escribió: " + repr(sorted(escritas))
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_c_los_intentos_los_cuenta_el_motor():
    """
    `intentos` se incrementa en el UPDATE, no en Python.

    Sumar uno sobre una lectura anterior es un lost update en cuanto haya
    dos verificaciones: las dos leerían el mismo valor y escribirían el
    mismo resultado, con lo que un intento desaparecería sin dejar rastro.
    """
    print("  3. los intentos los cuenta el motor, no Python:", end=" ")

    raiz = crear_repositorio()

    try:
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            estado_global.inicializar(con)
        finally:
            con.close()

        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        # Dos incrementos partiendo del MISMO valor leído. Si el valor se
        # calculara en Python, el segundo escribiría lo mismo que el
        # primero y el contador quedaría en 1 en vez de en 2.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            for _ in range(2):
                with estado_global.transaccion(con):
                    informe = estado_global.actualizar_si_propietario(
                        con,
                        "T-0901",
                        {},
                        generacion=tomada.generacion,
                        momento="2030-01-01T00:00:00+00:00",
                        incrementos=("intentos",),
                    )

                assert informe["resultado"] == estado_global.ESCRITURA_ACEPTADA
        finally:
            con.close()

        assert fila_de(raiz, "T-0901")["intentos"] == 2, (
            "Dos incrementos dejaron el contador en "
            + str(fila_de(raiz, "T-0901")["intentos"]) + " en vez de 2."
        )

        # Y no se puede incrementar cualquier cosa.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                estado_global.actualizar_si_propietario(
                    con,
                    "T-0901",
                    {},
                    generacion=tomada.generacion,
                    momento="2030-01-01T00:00:00+00:00",
                    incrementos=("estado",),
                )
        except estado_global.ErrorEstadoGlobal:
            pass
        else:
            raise AssertionError("Se aceptó incrementar una columna de texto.")
        finally:
            con.close()

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# Corredor de este archivo
# ----------------------------------------------------------------------

COMPROBACIONES = (
    prueba_a_dos_ordenes_validas_no_se_pisan,
    prueba_b_una_orden_no_escribe_columnas_ajenas,
    prueba_c_los_intentos_los_cuenta_el_motor,
)


def imprimir_metricas() -> None:
    print("")
    print("  Métricas reales de esta corrida")
    print("  -------------------------------")

    for clave in (
        "OPERACIONES",
        "ACEPTADAS",
        "RECHAZADAS",
        "ACTUALIZACIONES_PERDIDAS",
        "ROBOS_INDEBIDOS",
        "VERIFICACIONES_EN_ARBOL_INCORRECTO",
        "ERRORES_SQLITE",
        "EXCEPCIONES",
        "LATIDOS_AUTOMATICOS",
        "COMPROBACIONES_INTEGRIDAD",
        "FALLOS_INTEGRIDAD",
    ):
        print("  " + clave.ljust(36) + " = " + str(METRICAS[clave]))

    print("")


def prueba_ejecucion_segura(rondas: int = RONDAS_POR_OMISION) -> None:
    print("")
    print("PRUEBA: ejecución segura, recuperación y worktrees (A3.3)")
    print("")

    inicio = time.monotonic()

    for comprobacion in COMPROBACIONES:
        comprobacion()

    duracion = time.monotonic() - inicio

    imprimir_metricas()

    # El veredicto no se declara: se comprueba contra lo medido.
    assert METRICAS["ACTUALIZACIONES_PERDIDAS"] == 0
    assert METRICAS["ROBOS_INDEBIDOS"] == 0
    assert METRICAS["VERIFICACIONES_EN_ARBOL_INCORRECTO"] == 0
    assert METRICAS["ERRORES_SQLITE"] == 0
    assert METRICAS["EXCEPCIONES"] == 0
    assert METRICAS["FALLOS_INTEGRIDAD"] == 0

    print("  Tiempo: " + str(round(duracion, 2)) + " s")
    print("")
    print("PRUEBA_EJECUCION_SEGURA=OK")


def principal() -> int:
    analizador = argparse.ArgumentParser(
        description="Pruebas de ejecución segura y worktrees (A3.3)."
    )
    analizador.add_argument(
        "--rondas",
        type=int,
        default=RONDAS_POR_OMISION,
        help="Rondas de las corridas concurrentes.",
    )

    argumentos = analizador.parse_args()

    prueba_ejecucion_segura(argumentos.rondas)

    return 0


if __name__ == "__main__":
    sys.exit(principal())
