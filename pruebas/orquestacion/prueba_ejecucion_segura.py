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
import json
import multiprocessing
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
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

# Casos que una corrida concreta no pudo ejercitar (el sistema no permite
# enlaces simbólicos, por ejemplo). Se imprimen al final: una comprobación
# que se salta un caso en silencio y sigue diciendo «OK» miente.
OMITIDAS: list[str] = []

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

        # Las dos órdenes van UNA TRAS OTRA: esta comprobación demuestra el
        # efecto (ningún campo vuelve atrás). El solapamiento real lo
        # prueban la 2, que mira el SET de cada orden, y la 37, con procesos
        # escribiendo a la vez.
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

        # `decidir` no pasa por `persistir`, así que se mira aparte. Sus
        # columnas son las decisiones, la bandera y la marca; nada más. Un
        # `decidir` que reescribiera el latido o los intentos desde su foto
        # sobrevivía a la batería porque la 1 va en serie.
        ficha_minima(
            raiz, "T-0902",
            decisiones=[{"clave": "D-1", "descripcion": "una"}],
        )
        sentencias.clear()
        sqlite3.connect = conectar_vigilado

        try:
            nucleo.decidir(raiz, "T-0902", "D-1", "resuelta")
        finally:
            sqlite3.connect = conectar

        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        actualizaciones = [
            una for una in sentencias
            if una.strip().upper().startswith("UPDATE TAREAS")
        ]

        assert len(actualizaciones) == 1, (
            "Decidir debería producir UN solo UPDATE: " + repr(actualizaciones)
        )

        asignaciones = actualizaciones[0].split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        escritas = {
            trozo.split("=", 1)[0].strip()
            for trozo in asignaciones.split(",")
            if "=" in trozo
        }

        assert escritas == {
            "decisiones", "requiere_decision_humana", "actualizado_en"
        }, (
            "Decidir escribe columnas que no son suyas o le faltan las que "
            "sí lo son. Escribió: " + repr(sorted(escritas))
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

        # Y el presupuesto con el que se decide BLOQUEADO es el VIGENTE.
        # `max_intentos` es declarativo y cualquier `cargar` de otro
        # proceso —incluido `ver`— lo sincroniza desde el JSON: decidir
        # con la foto de antes de correr y grabar la fila con el valor
        # nuevo dejaba «bloqueada con 1 de 5».
        (raiz / "pruebas" / "demostracion" / "prueba_roja_lenta.py").write_text(
            "import time\ntime.sleep(1.5)\nraise SystemExit('falla')\n",
            encoding="utf-8",
        )
        ficha_minima(
            raiz, "T-0902",
            pruebas_requeridas=["pruebas/demostracion/prueba_roja_lenta.py"],
            max_intentos=1,
        )
        segunda = nucleo.tomar(raiz, "T-0902", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        def ampliar_a_mitad():
            time.sleep(0.5)
            archivo = fichas.carpeta_tareas(raiz) / "T-0902.json"
            datos = json.loads(archivo.read_text(encoding="utf-8"))
            datos["max_intentos"] = 5
            archivo.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            nucleo.cargar(raiz, "T-0902")

        hilo = threading.Thread(target=ampliar_a_mitad)
        hilo.start()

        try:
            METRICAS["OPERACIONES"] += 1
            informe = nucleo.verificar(
                raiz, "T-0902",
                trabajador_id="worker-A", generacion=segunda.generacion,
            )
        finally:
            hilo.join()

        METRICAS["ACEPTADAS"] += 1

        fila = fila_de(raiz, "T-0902")

        assert fila["max_intentos"] == 5, (
            "El hilo no llegó a ampliar el presupuesto: la prueba no probó nada."
        )
        assert informe["estado"] == str(Estado.REQUIERE_REVISION), (
            "Se bloqueó con un presupuesto que ya no era el vigente: "
            + informe["estado"] + " / " + informe["motivo"]
        )
        assert fila["intentos"] == 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 2 — verificar() ejecuta en el worktree de la tarea, no donde se le
#           invocó
# ----------------------------------------------------------------------

def _prueba_con_marca(marca: str) -> str:
    return "print('PRUEBA_" + marca + "=OK')\n"


def montar_tres_arboles():
    """
    Repositorio con main y dos worktrees enlazados, cada uno distinguible.

    Los tres tienen el MISMO archivo de prueba en la misma ruta, pero con
    una marca distinta. Así, mirar qué marca quedó registrada dice sin
    ambigüedad qué árbol se ejecutó: no hay que fiarse de rutas ni de
    mensajes, lo dice el resultado.
    """
    principal = crear_repositorio("arboles_")
    aparte = Path(tempfile.mkdtemp(prefix="arboles_wt_"))

    demo = principal / "pruebas" / "demostracion"
    (demo / "prueba_arbol.py").write_text(
        _prueba_con_marca("ARBOL_MAIN"), encoding="utf-8"
    )

    _git(principal, "add", "-A")
    hecho = _git(principal, "commit", "-q", "-m", "base")
    assert hecho.returncode == 0, hecho.stderr

    arboles = {"main": principal}

    for nombre in ("A", "B"):
        destino = aparte / nombre
        creado = _git(
            principal, "worktree", "add", "-q", str(destino),
            "-b", "rama-" + nombre.lower(),
        )
        assert creado.returncode == 0, creado.stderr

        (destino / "pruebas" / "demostracion" / "prueba_arbol.py").write_text(
            _prueba_con_marca("ARBOL_" + nombre), encoding="utf-8"
        )

        _git(destino, "add", "-A")
        hecho = _git(destino, "commit", "-q", "-m", "arbol " + nombre)
        assert hecho.returncode == 0, hecho.stderr

        arboles[nombre] = destino

    return principal, aparte, arboles


def _commit_de(raiz: Path) -> str:
    resultado = _git(raiz, "rev-parse", "--short", "HEAD")
    assert resultado.returncode == 0, resultado.stderr

    return resultado.stdout.strip()


def prueba_d_verificar_ejecuta_en_el_worktree_de_la_tarea():
    """
    Una tarea con worktree se verifica EN ese worktree, no donde se invocó.

    Es el gate crítico de A3.3. Antes, `verificar` corría el corredor sobre
    la raíz desde la que se llamó al Supervisor, así que una tarea que vivía
    en el worktree A y se verificaba desde main ejecutaba las pruebas de
    MAIN y grababa ese resultado como suyo. Un verde que no dice nada del
    trabajo que se estaba juzgando, y que además habría dado por bueno un
    árbol que nadie miró.

    Los tres árboles tienen el mismo archivo de prueba con marcas distintas,
    de modo que el resultado registrado identifica sin ambigüedad cuál se
    ejecutó. Se comprueba por partida doble: la marca y el commit.
    """
    print("  4. verificar ejecuta en el worktree de la tarea:", end=" ")

    principal, aparte, arboles = montar_tres_arboles()

    try:
        esperado = {
            "A": ("PRUEBA_ARBOL_A", _commit_de(arboles["A"])),
            "B": ("PRUEBA_ARBOL_B", _commit_de(arboles["B"])),
        }
        commit_main = _commit_de(principal)

        for nombre in ("A", "B"):
            identificador = "T-090" + ("1" if nombre == "A" else "2")

            nucleo.crear(
                principal,
                identificador,
                titulo="Tarea del árbol " + nombre,
                ambito_archivos=["modulos/" + nombre.lower() + "/*.py"],
                pruebas_requeridas=["pruebas/demostracion/prueba_arbol.py"],
            )

            # La toma declara el árbol: pertenece a la ejecución.
            tomada = nucleo.tomar(
                principal,
                identificador,
                trabajador_id="worker-" + nombre,
                worktree=str(arboles[nombre]),
            )
            METRICAS["OPERACIONES"] += 1
            METRICAS["ACEPTADAS"] += 1

            assert tomada.worktree == str(arboles[nombre].resolve()), (
                "La toma no registró el worktree: " + repr(tomada.worktree)
            )

            # Se verifica desde la raíz PRINCIPAL, a propósito.
            informe = nucleo.verificar(
                principal,
                identificador,
                trabajador_id="worker-" + nombre,
                generacion=tomada.generacion,
            )
            METRICAS["OPERACIONES"] += 1
            METRICAS["ACEPTADAS"] += 1

            marca_esperada, commit_esperado = esperado[nombre]

            marcas = {
                una["marca"] for una in informe["corrida"]["detalle"]
                if una["marca"]
            }

            if marca_esperada not in marcas:
                METRICAS["VERIFICACIONES_EN_ARBOL_INCORRECTO"] += 1

            assert marca_esperada in marcas, (
                "No se ejecutó el árbol " + nombre + ". Marcas obtenidas: "
                + repr(sorted(marcas))
            )

            for ajena in ("PRUEBA_ARBOL_MAIN", "PRUEBA_ARBOL_"
                          + ("B" if nombre == "A" else "A")):
                if ajena in marcas:
                    METRICAS["VERIFICACIONES_EN_ARBOL_INCORRECTO"] += 1

                assert ajena not in marcas, (
                    "Se ejecutó el árbol equivocado (" + ajena + ") al "
                    "verificar la tarea del árbol " + nombre + "."
                )

            assert informe["commit"] == commit_esperado, (
                "El commit registrado no es el del árbol " + nombre + ": "
                + repr(informe["commit"]) + " en vez de "
                + repr(commit_esperado)
            )
            assert informe["commit"] != commit_main, (
                "Se registró el commit de main."
            )
            assert informe["rama"] == "rama-" + nombre.lower(), (
                "La rama registrada no es la del árbol: "
                + repr(informe["rama"])
            )
            assert informe["es_worktree"] is True
            assert Path(informe["raiz"]) == arboles[nombre].resolve()

            # Y queda grabado en la base, no sólo devuelto.
            grabado = fila_de(principal, identificador)["ultima_verificacion"]

            assert grabado["commit"] == commit_esperado, (
                "La verificación grabada no conserva el commit del árbol: "
                + repr(grabado)
            )
            assert grabado["raiz"] == str(arboles[nombre].resolve())

        # Sin `--worktree`, el árbol de la ejecución es la raíz desde la
        # que se TOMA. Grabar None significaba «la raíz de quien invoque la
        # siguiente orden»: un trabajador que tomaba desde su worktree y un
        # operador que verificaba desde main proponían una tarea cuyo
        # trabajo nunca se ejecutó.
        nucleo.crear(
            principal, "T-0903",
            titulo="Tomada desde A sin declarar",
            ambito_archivos=["modulos/c/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_arbol.py"],
        )
        destino = fichas.carpeta_tareas(arboles["A"])
        destino.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            fichas.carpeta_tareas(principal) / "T-0903.json",
            destino / "T-0903.json",
        )

        tomada = nucleo.tomar(arboles["A"], "T-0903", trabajador_id="worker-C")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert tomada.worktree == str(arboles["A"].resolve()), (
            "La toma sin worktree no grabó la raíz desde la que se tomó: "
            + repr(tomada.worktree)
        )

        # Se verifica desde la raíz PRINCIPAL: tiene que correr en A.
        informe = nucleo.verificar(
            principal, "T-0903",
            trabajador_id="worker-C", generacion=tomada.generacion,
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        marcas = {
            una["marca"] for una in informe["corrida"]["detalle"] if una["marca"]
        }

        if "PRUEBA_ARBOL_A" not in marcas or "PRUEBA_ARBOL_MAIN" in marcas:
            METRICAS["VERIFICACIONES_EN_ARBOL_INCORRECTO"] += 1

        assert "PRUEBA_ARBOL_A" in marcas and "PRUEBA_ARBOL_MAIN" not in marcas, (
            "Una tarea tomada desde A sin declarar worktree se verificó en "
            "otro árbol: " + repr(sorted(marcas))
        )
        assert Path(informe["raiz"]) == arboles["A"].resolve()

        comprobar_integridad(principal)
    finally:
        _git(principal, "worktree", "prune")
        borrar(aparte)
        borrar(principal)

    print("OK")


def prueba_e_una_ruta_ajena_no_se_ejecuta():
    """
    Sólo un worktree REGISTRADO en Git sirve como árbol de ejecución.

    Que una ruta exista, y hasta que sea un repositorio Git válido, no
    autoriza a correr sus pruebas y grabar el resultado como si fuera el de
    esta tarea.

    La comprobación es la lista canónica de `git worktree list`, y no
    comparar el directorio común. Ese valor no lo decide el repositorio: lo
    decide un archivo `.git` de una línea que vive en el directorio
    candidato. Copiar un worktree con `cp -a`, moverlo sin
    `git worktree repair`, o escribir a mano `gitdir: …` en cualquier
    carpeta, producía un directorio que se aceptaba y que Git no ha listado
    nunca; y como la rama y el commit se leían de ese `.git` prestado, la
    evidencia grabada era la del worktree legítimo. El historial afirmaba
    haber verificado un commit que nadie ejecutó.

    Un subdirectorio cualquiera tampoco vale: el corredor descubre
    `<arbol>/pruebas/**/prueba_*.py`, así que un subdirectorio con una sola
    prueba verde dentro bastaba para llegar a PROPUESTO saltándose la
    batería entera.
    """
    print("  5. sólo un worktree registrado sirve de árbol:", end=" ")

    principal = crear_repositorio("ajena_")
    ajeno = crear_repositorio("ajeno_otro_")

    try:
        _git(principal, "add", "-A")
        _git(principal, "commit", "-q", "-m", "base")

        # Un worktree legítimo, para tener con qué comparar.
        legitimo = principal.parent / (principal.name + "_wt")
        alta = _git(principal, "worktree", "add", str(legitimo), "-b", "rama-l")
        assert alta.returncode == 0, alta.stderr

        # Y una COPIA de ese worktree en otro sitio: conserva el archivo
        # `.git`, así que declara el mismo directorio común. Git no la ha
        # listado nunca.
        impostor = principal.parent / (principal.name + "_impostor")
        shutil.copytree(legitimo, impostor, symlinks=True)

        # Un worktree registrado cuyo directorio se borró con `rm -rf` y se
        # volvió a crear como carpeta corriente, con una prueba verde
        # dentro. Git lo sigue listando, marcado `prunable`; esa línea se
        # ignoraba y la carpeta se aceptaba como árbol de ejecución.
        fantasma = principal.parent / (principal.name + "_fantasma")
        alta = _git(principal, "worktree", "add", str(fantasma), "-b", "rama-f")
        assert alta.returncode == 0, alta.stderr
        shutil.rmtree(fantasma)
        (fantasma / "pruebas").mkdir(parents=True)
        (fantasma / "pruebas" / "prueba_verde.py").write_text(
            PRUEBA_VERDE, encoding="utf-8"
        )

        # Y una ruta que ESTE repositorio registró y borró, y que OTRO
        # repositorio reutilizó después para un worktree suyo. Git sólo
        # mira que `<ruta>/.git` exista, así que aquí la sigue listando
        # como válida (sin `prunable`), pero el checkout —rama, commit,
        # pruebas— es del otro.
        _git(ajeno, "add", "-A")
        hecho = _git(ajeno, "commit", "-q", "-m", "base ajena")
        assert hecho.returncode == 0, hecho.stderr

        reutilizada = principal.parent / (principal.name + "_reutilizada")
        alta = _git(principal, "worktree", "add", str(reutilizada), "-b", "rama-r")
        assert alta.returncode == 0, alta.stderr
        shutil.rmtree(reutilizada)
        alta = _git(ajeno, "worktree", "add", str(reutilizada), "-b", "rama-ajena")
        assert alta.returncode == 0, alta.stderr

        # Un worktree ANIDADO en la raíz al que le falta el archivo `.git`.
        # Git lo lista `prunable`, y desde dentro `git` SUBE y responde por
        # la raíz: rama y commit de main sobre las pruebas del worktree.
        anidado = principal / ".arboles" / "wb"
        alta = _git(
            principal, "worktree", "add", str(anidado), "-b", "rama-anidada"
        )
        assert alta.returncode == 0, alta.stderr
        (anidado / ".git").unlink()

        listado = _git(principal, "worktree", "list", "--porcelain").stdout
        assert "worktree " + str(reutilizada) + "\n" in listado, listado
        assert "prunable" not in listado.split(str(reutilizada), 1)[1].split("\n\n")[0], (
            "Git marcó la ruta reutilizada como prunable: el caso ya no "
            "prueba lo que dice."
        )

        casos = (
            ("otro repositorio", str(ajeno), "no es un worktree registrado"),
            ("no existe", str(principal / "no_existe"), "no existe"),
            (
                "no es un directorio",
                str(principal / "pruebas" / "demostracion" / "prueba_verde.py"),
                "no es un directorio",
            ),
            (
                "copia con el .git prestado",
                str(impostor),
                "no es un worktree registrado",
            ),
            (
                "subdirectorio cualquiera",
                str(principal / "pruebas"),
                "no es un worktree registrado",
            ),
            (
                "la carpeta .git",
                str(principal / ".git"),
                "no es un worktree registrado",
            ),
            (
                "worktree prunable (directorio recreado sin .git)",
                str(fantasma),
                "no es utilizable",
            ),
            (
                "ruta reutilizada por otro repositorio",
                str(reutilizada),
                "responde por otro repositorio",
            ),
            (
                "worktree anidado sin .git",
                str(anidado),
                "no es utilizable",
            ),
            # `~usuario` de un usuario que no existe: pathlib lanza
            # RuntimeError y salía como traceback (código 1). En Windows
            # `expanduser` construye la ruta sin comprobar el usuario y el
            # rechazo llega por «no existe»; en los dos casos es
            # ErrorWorktree, que es lo que se exige.
            (
                "usuario inexistente en ~",
                "~usuario_que_no_existe_a33/wt",
                "",
            ),
        )

        for caso, ruta, fragmento in casos:
            METRICAS["OPERACIONES"] += 1

            try:
                nucleo.resolver_worktree(principal, ruta)
            except nucleo.ErrorWorktree as error:
                METRICAS["RECHAZADAS"] += 1

                assert fragmento in str(error), (
                    "El rechazo de '" + caso + "' no explica el motivo: "
                    + str(error)
                )
            else:
                METRICAS["ACEPTADAS"] += 1
                raise AssertionError(
                    "Se aceptó una ruta que no debía: " + caso
                )

        # El worktree legítimo sí se acepta: la validación no es un muro.
        assert nucleo.resolver_worktree(principal, str(legitimo)) == (
            legitimo.resolve()
        ), "Se rechazó un worktree que Git sí tiene registrado."
        METRICAS["ACEPTADAS"] += 1

        # Sin worktree declarado se usa la raíz, que es el caso normal.
        assert nucleo.resolver_worktree(principal, None) == principal.resolve()
        assert nucleo.resolver_worktree(principal, "  ") == principal.resolve()

        # GIT_DIR heredado —siempre puesto dentro de un hook de Git, y
        # también en `git rebase --exec` o `git bisect run`— no puede
        # anular la comprobación. Antes sí lo hacía: con él, `git` ignoraba
        # el directorio de trabajo, las dos consultas devolvían lo mismo y
        # se llegaba a aceptar cualquier ruta del sistema.
        previo = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(principal / ".git")

        try:
            METRICAS["OPERACIONES"] += 1

            try:
                nucleo.resolver_worktree(principal, str(ajeno))
            except nucleo.ErrorWorktree:
                METRICAS["RECHAZADAS"] += 1
            else:
                METRICAS["ACEPTADAS"] += 1
                raise AssertionError(
                    "Con GIT_DIR en el entorno se aceptó un repositorio "
                    "ajeno: la comprobación de pertenencia no comprueba nada."
                )
        finally:
            if previo is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = previo
    finally:
        borrar(ajeno)
        borrar(principal.parent / (principal.name + "_impostor"))
        borrar(principal.parent / (principal.name + "_wt"))
        borrar(principal.parent / (principal.name + "_fantasma"))
        borrar(principal.parent / (principal.name + "_reutilizada"))
        borrar(principal)

    print("OK")


def prueba_f_el_worktree_se_valida_al_tomar():
    """
    La toma no concede una ejecución sobre un árbol que no vale.

    Es el momento correcto para comprobarlo: si se dejara para `verificar`,
    la tarea ya estaría tomada y el trabajador ya habría trabajado en algún
    sitio antes de que nadie mirara si ese sitio era legítimo.
    """
    print("  6. la toma valida el worktree antes de conceder:", end=" ")

    principal = crear_repositorio("valida_")
    ajeno = crear_repositorio("valida_ajeno_")

    try:
        ficha_minima(principal, "T-0901")

        METRICAS["OPERACIONES"] += 1

        try:
            nucleo.tomar(
                principal,
                "T-0901",
                trabajador_id="worker-A",
                worktree=str(ajeno),
            )
        except nucleo.ErrorWorktree:
            METRICAS["RECHAZADAS"] += 1
        else:
            METRICAS["ACEPTADAS"] += 1
            raise AssertionError(
                "La toma concedió una ejecución sobre un repositorio ajeno."
            )

        # Y la tarea sigue libre: un rechazo no deja rastro.
        fila = fila_de(principal, "T-0901")

        assert fila["trabajador_id"] is None, (
            "La toma rechazada dejó propietario: " + repr(fila["trabajador_id"])
        )
        assert fila["estado"] == str(Estado.NUEVO)
        assert fila["generacion"] == 0, (
            "La toma rechazada consumió una generación."
        )
        assert not fila["worktree"]

        comprobar_integridad(principal)
    finally:
        borrar(ajeno)
        borrar(principal)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 4 — Latido automático
# ----------------------------------------------------------------------

def prueba_h_el_latido_automatico_mantiene_viva_la_ejecucion():
    """
    Mientras dura una operación larga, la tarea sigue dando señales.

    Sin esto, una verificación de diez minutos deja la tarea sin latido todo
    ese rato y la recuperación la ve caducada: el trabajo honesto parece
    abandono, y la tarea se le arrebata a quien la está haciendo bien.

    Los latidos se emiten uno a uno, sin esperar ningún reloj: una prueba
    que dependiera de tiempos reales sería lenta y frágil a la vez.
    """
    print("  7. el latido automático mantiene viva la ejecución:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        antes = fila_de(raiz, "T-0901")["ultimo_latido"]

        acompanante = nucleo.LatidoAutomatico(
            raiz,
            "T-0901",
            "worker-A",
            tomada.generacion,
            reloj=lambda: "2030-01-01T00:00:0"
            + str(acompanante.emitidos) + "+00:00",
        )

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
            for esperados in (1, 2, 3):
                assert acompanante.emitir_uno() is True
                assert acompanante.emitidos == esperados
        finally:
            sqlite3.connect = conectar

        # Un latido toca UNA columna, y se mira la sentencia: comprobar sólo
        # el resultado dejaría pasar una escritura ancha que hoy reescribe
        # los mismos valores y mañana, con otra orden en vuelo, no.
        for sentencia in sentencias:
            if not sentencia.strip().upper().startswith("UPDATE TAREAS"):
                continue

            asignaciones = sentencia.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            escritas = {
                trozo.split("=", 1)[0].strip()
                for trozo in asignaciones.split(",")
                if "=" in trozo
            }

            assert escritas == {"ultimo_latido", "actualizado_en"}, (
                "El latido automático escribe columnas que no son suyas: "
                + repr(sorted(escritas))
            )

        METRICAS["LATIDOS_AUTOMATICOS"] += acompanante.emitidos

        despues = fila_de(raiz, "T-0901")["ultimo_latido"]

        assert despues != antes, "El latido automático no escribió nada."
        assert despues.startswith("2030-"), (
            "El latido no dejó la marca esperada: " + repr(despues)
        )
        assert not acompanante.propiedad_perdida
        assert acompanante.error is None

        # Escribe SÓLO el latido: el resto de la ejecución queda intacto.
        fila = fila_de(raiz, "T-0901")

        assert fila["trabajador_id"] == "worker-A"
        assert fila["generacion"] == tomada.generacion
        assert fila["estado"] == str(Estado.EN_EJECUCION)
        assert fila["intentos"] == 0

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_i_el_latido_se_para_al_perder_la_propiedad():
    """
    Un latido no sostiene artificialmente una ejecución que ya no existe.

    Si siguiera latiendo sobre una tarea que cambió de manos, la
    recuperación creería viva a una ejecución muerta y, peor, estaría
    escribiendo sobre el turno de otro. Se para en el primer rechazo y lo
    deja anotado.
    """
    print("  8. el latido se para al perder la propiedad:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        acompanante = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", primera.generacion
        )

        assert acompanante.emitir_uno() is True

        # La tarea cambia de manos por la vía legítima.
        nucleo.devolver(raiz, "T-0901", trabajador_id="worker-A")
        segunda = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        testigo_b = fila_de(raiz, "T-0901")["ultimo_latido"]

        assert acompanante.emitir_uno() is False, (
            "El latido siguió sobre una tarea que ya no era suya."
        )
        assert acompanante.propiedad_perdida is True
        assert acompanante.emitidos == 1, (
            "Se contó como emitido un latido rechazado."
        )
        METRICAS["RECHAZADAS"] += 1

        # Y no tocó nada del dueño nuevo.
        fila = fila_de(raiz, "T-0901")

        if fila["ultimo_latido"] != testigo_b:
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert fila["ultimo_latido"] == testigo_b
        assert fila["trabajador_id"] == "worker-B"
        assert fila["generacion"] == segunda.generacion

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_i2_el_latido_viejo_no_vale_para_la_ejecucion_nueva():
    """
    El mismo trabajador, ejecución nueva: el latido viejo NO cuenta.

    Éste es el caso que la identidad sola no cubre. Un trabajador
    devuelve la tarea y la vuelve a tomar: el `trabajador_id` es el
    mismo y el estado vuelve a ser EN_EJECUCION, así que las dos
    precondiciones evidentes coinciden. Sólo la generación distingue la
    ejecución vieja de la nueva.

    Importa porque un latido rezagado del turno anterior estaría
    certificando como viva una ejecución que nadie está haciendo. El
    reloj de abandono no volvería a vencer nunca y la recuperación no
    llegaría a mirar la tarea.

    Lo descubrió el arnés de mutación: al hacer que el latido leyera la
    generación de la fila en vez de usar la suya, TODA la batería seguía
    en verde. Esta comprobación es la que faltaba.
    """
    print("  9. un latido del turno anterior no vale para el nuevo:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        rezagado = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", primera.generacion
        )

        assert rezagado.emitir_uno() is True, (
            "El latido no funcionaba ni en su propia ejecución."
        )

        # Mismo trabajador, ejecución nueva.
        nucleo.devolver(raiz, "T-0901", trabajador_id="worker-A")
        segunda = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        assert segunda.generacion > primera.generacion, (
            "La retoma no avanzó la generación: la prueba no probaría nada."
        )

        # Marca imposible de confundir: si el latido viejo escribe, se ve.
        testigo = "2001-02-03T04:05:06+00:00"
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                con.execute(
                    "UPDATE tareas SET ultimo_latido = ? WHERE id = ?",
                    (testigo, "T-0901"),
                )
        finally:
            con.close()

        fila_previa = fila_de(raiz, "T-0901")

        assert fila_previa["trabajador_id"] == "worker-A"
        assert fila_previa["estado"] == str(Estado.EN_EJECUCION)
        assert fila_previa["ultimo_latido"] == testigo

        # Identidad y estado coinciden. Sólo la generación debe frenarlo.
        assert rezagado.emitir_uno() is False, (
            "Un latido de la ejecución anterior se aceptó en la nueva."
        )
        assert rezagado.propiedad_perdida is True
        assert rezagado.emitidos == 1, (
            "Se contó como emitido un latido rechazado."
        )
        METRICAS["RECHAZADAS"] += 1

        fila = fila_de(raiz, "T-0901")

        if fila["ultimo_latido"] != testigo:
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert fila["ultimo_latido"] == testigo, (
            "El latido viejo escribió sobre la ejecución nueva: "
            + repr(fila["ultimo_latido"])
        )
        assert fila["generacion"] == segunda.generacion

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_j_el_latido_no_resucita_una_ejecucion_terminada():
    """
    Terminada la ejecución, un latido rezagado no la revive.

    El acompañante se para solo al salir del bloque, pero si uno quedara
    en vuelo tampoco podría hacer daño: su escritura exige estado
    EN_EJECUCION, y una tarea ya cerrada no lo está.
    """
    print(" 10. un latido rezagado no resucita lo terminado:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        acompanante = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion
        )

        nucleo.devolver(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        antes = fila_de(raiz, "T-0901")

        assert acompanante.emitir_uno() is False, (
            "Un latido revivió una ejecución ya terminada."
        )
        METRICAS["RECHAZADAS"] += 1

        despues = fila_de(raiz, "T-0901")

        assert despues["estado"] == str(Estado.REABIERTO)
        assert despues["ultimo_latido"] == antes["ultimo_latido"]
        assert despues["trabajador_id"] is None

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_k_el_acompanante_se_para_aunque_el_trabajo_falle():
    """
    El latido termina con la operación, salga bien o mal.

    Un hilo que sobreviviera al trabajo seguiría escribiendo sobre una
    ejecución que nadie está haciendo. Se comprueba con una excepción
    deliberada, que es el caso en el que un `finally` mal puesto se nota.
    """
    print(" 11. el acompañante se para aunque el trabajo falle:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        acompanante = nucleo.LatidoAutomatico(
            raiz,
            "T-0901",
            "worker-A",
            tomada.generacion,
            intervalo_s=0.005,
        )

        class FalloDelTrabajo(Exception):
            pass

        try:
            with acompanante:
                time.sleep(0.08)
                raise FalloDelTrabajo("el trabajo principal se rompió")
        except FalloDelTrabajo:
            pass
        else:
            raise AssertionError(
                "El gestor de contexto se tragó la excepción del trabajo."
            )

        assert acompanante._hilo is not None
        assert not acompanante._hilo.is_alive(), (
            "El hilo del latido sobrevivió al fallo del trabajo."
        )
        # Con 80 ms de trabajo y 5 ms de intervalo, un acompañante que
        # repite el bucle late muchas veces; uno que sólo late al entrar,
        # una. Exigir «más de cero» no distinguía las dos cosas.
        assert acompanante.emitidos >= 3, (
            "El acompañante no repite el bucle: " + str(acompanante.emitidos)
            + " latidos en 80 ms con intervalo de 5 ms."
        )

        METRICAS["LATIDOS_AUTOMATICOS"] += acompanante.emitidos

        # Reutilizar la instancia es un error ruidoso, no cero latidos en
        # silencio.
        try:
            with acompanante:
                pass
        except nucleo.ErrorSupervisor:
            pass
        else:
            raise AssertionError(
                "Se pudo reutilizar un acompañante ya cerrado."
            )

        # Y un intervalo de cero —miles de BEGIN IMMEDIATE por segundo sobre
        # la base compartida— se rechaza al construir.
        try:
            nucleo.LatidoAutomatico(
                raiz, "T-0901", "worker-A", tomada.generacion, intervalo_s=0
            )
        except nucleo.ErrorSupervisor:
            pass
        else:
            raise AssertionError("Se admitió un intervalo de latido de cero.")

        # Y deja de escribir de verdad: la marca no se mueve más.
        quieto = fila_de(raiz, "T-0901")["ultimo_latido"]
        time.sleep(0.08)

        assert fila_de(raiz, "T-0901")["ultimo_latido"] == quieto, (
            "El latido siguió escribiendo después de cerrarse."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_l_verificar_late_mientras_corre():
    """
    Una verificación de verdad emite latidos mientras dura.

    Es la operación larga que motivó todo esto, así que se comprueba sobre
    ella y no sólo sobre el componente suelto.
    """
    print(" 12. verificar late mientras corre la batería:", end=" ")

    raiz = crear_repositorio()

    try:
        # Una prueba que tarda lo justo para que quepan varios latidos.
        lenta = raiz / "pruebas" / "demostracion" / "prueba_lenta.py"
        lenta.write_text(
            "import time\ntime.sleep(0.25)\nprint('PRUEBA_LENTA=OK')\n",
            encoding="utf-8",
        )

        ficha_minima(
            raiz,
            "T-0901",
            pruebas_requeridas=["pruebas/demostracion/prueba_lenta.py"],
        )
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        # Un tropiezo transitorio en el PRIMER latido de la corrida: tiene
        # que verse en el informe («hasta ahora se calculaba y se tiraba»)
        # sin invalidar la corrida ni parar el acompañante.
        abrir_real = estado_global.abrir
        tropiezo = {"pendiente": True}

        def abrir_con_tropiezo_en_el_latido(*argumentos, **claves):
            if (
                tropiezo["pendiente"]
                and threading.current_thread().name.startswith("latido-")
            ):
                tropiezo["pendiente"] = False
                raise sqlite3.OperationalError("database is locked")

            return abrir_real(*argumentos, **claves)

        estado_global.abrir = abrir_con_tropiezo_en_el_latido

        try:
            informe = nucleo.verificar(
                raiz,
                "T-0901",
                trabajador_id="worker-A",
                generacion=tomada.generacion,
                intervalo_latido_s=0.02,
            )
        finally:
            estado_global.abrir = abrir_real

        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1
        METRICAS["LATIDOS_AUTOMATICOS"] += informe["latidos"]

        assert not tropiezo["pendiente"], (
            "El tropiezo no llegó a ocurrir: la prueba no probó nada."
        )
        # Con 250 ms de prueba e intervalo de 20 ms caben muchos latidos; un
        # acompañante que sólo latiera al entrar daría uno (o cero, tras el
        # tropiezo). Exigir «más de cero» no distinguía nada.
        assert informe["latidos"] >= 3, (
            "La verificación emitió " + str(informe["latidos"]) + " latidos "
            "en 250 ms con intervalo de 20 ms: una corrida larga seguiría "
            "pareciendo abandono."
        )
        assert informe["estado"] == str(Estado.PROPUESTO), informe["motivo"]
        assert informe["latido_error"] and "locked" in informe["latido_error"], (
            "El fallo del latido no llegó al informe: "
            + repr(informe["latido_error"])
        )
        assert informe["latido_cierre_incompleto"] is False, (
            "El hilo del latido no cerró dentro del plazo."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK (" + str(informe["latidos"]) + " latidos)")


# ----------------------------------------------------------------------
# GRUPO 3 — Crear es un acto único
# ----------------------------------------------------------------------

def _creador(ruta_raiz: str, identificador: str, marca: str, barrera) -> dict:
    """
    Un proceso que intenta crear la tarea a la vez que los demás.

    Cada uno escribe un TÍTULO distinto, para que al final se pueda saber
    de quién es la definición que quedó. Si dos escribieran lo mismo, una
    sobrescritura pasaría inadvertida.
    """
    import sys as _sys
    from pathlib import Path as _Path

    for sufijo in ("orquestacion", "nucleo"):
        destino = str(_Path(__file__).resolve().parents[2] / sufijo)
        if destino not in _sys.path:
            _sys.path.insert(0, destino)

    from ingenieria_supervisor import supervisor as _nucleo

    raiz = _Path(ruta_raiz)

    try:
        barrera.wait(timeout=ESPERA_BARRERA_S)
    except Exception as error:
        return {"clase": "barrera", "marca": marca, "detalle": str(error)}

    try:
        _nucleo.crear(
            raiz,
            identificador,
            titulo="Creada por " + marca,
            ambito_archivos=["modulos/" + marca + ".py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
        )

        return {"clase": "creada", "marca": marca, "detalle": None}
    except _nucleo.ErrorCreacion as choque:
        return {"clase": "rechazada", "marca": marca, "detalle": str(choque)}
    except sqlite3.Error as error:
        return {"clase": "sqlite", "marca": marca, "detalle": str(error)}
    except Exception as error:
        return {
            "clase": "inesperada",
            "marca": marca,
            "detalle": type(error).__name__ + ": " + str(error),
        }


def prueba_g_crear_concurrente_tiene_un_solo_ganador(creadores: int):
    """
    Varios procesos creando la misma tarea a la vez: crea exactamente uno.

    La carrera era real y de las feas. La comprobación de existencia se
    hacía en autocommit y el JSON se escribía ANTES del INSERT, así que dos
    procesos pasaban los dos la comprobación, los dos escribían su
    definición —el segundo pisando la del primero— y sólo entonces la clave
    primaria rechazaba a uno. El perdedor se iba con un error habiendo
    dejado su ficha encima de la del ganador.

    Por eso cada proceso escribe un título distinto: al final se comprueba
    que la definición que quedó, en SQLite Y en el JSON, es la del que
    ganó. Contar ganadores no bastaría.
    """
    print(
        " 36. crear concurrente (" + str(creadores) + " procesos): ",
        end="",
    )

    contexto = multiprocessing.get_context("spawn")
    raiz = crear_repositorio("crear_")

    try:
        # La base se prepara antes: lo que se prueba aquí es la carrera de
        # creación, no la de arranque, que ya cubre A3.2.
        estado_global.inicializar_base(raiz)

        # Antes de la carrera, la comprobación DETERMINISTA: la existencia
        # se consulta con el bloqueo de escritura ya tomado.
        #
        # Hace falta porque la carrera con procesos es probabilística: la
        # ventana entre comprobar e insertar es de microsegundos y puede no
        # abrirse en una corrida concreta. Un gate que sólo salta a veces no
        # es un gate. Esto sí salta siempre, y encima dice qué está mal.
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
            nucleo.crear(
                raiz,
                "T-0900",
                titulo="Testigo del orden",
                ambito_archivos=["modulos/testigo.py"],
                pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            )
        finally:
            sqlite3.connect = conectar

        candado = None
        consulta = None
        insercion = None

        for numero, sentencia in enumerate(sentencias):
            texto = sentencia.strip().upper()

            if candado is None and texto.startswith("BEGIN IMMEDIATE"):
                candado = numero
            elif consulta is None and "SELECT * FROM TAREAS WHERE ID" in texto:
                consulta = numero
            elif insercion is None and texto.startswith("INSERT INTO TAREAS"):
                insercion = numero

        assert candado is not None, "Crear no abrió ninguna transacción."
        assert consulta is not None, "Crear no comprobó la existencia."
        assert insercion is not None, "Crear no insertó."
        assert candado < consulta < insercion, (
            "La comprobación de existencia y el INSERT tienen que ocurrir "
            "DENTRO de la transacción y en ese orden. Posiciones: "
            "BEGIN=" + str(candado) + " SELECT=" + str(consulta)
            + " INSERT=" + str(insercion)
        )

        with contexto.Manager() as gestor:
            barrera = gestor.Barrier(creadores)
            reserva = gestor.Pool(processes=creadores)

            try:
                pendientes = [
                    reserva.apply_async(
                        _creador,
                        (str(raiz), "T-0901", "worker" + str(numero), barrera),
                    )
                    for numero in range(creadores)
                ]

                resultados = [
                    uno.get(timeout=ESPERA_PROCESO_S) for uno in pendientes
                ]
            finally:
                reserva.close()
                reserva.join()

        METRICAS["OPERACIONES"] += creadores

        creadas = [uno for uno in resultados if uno["clase"] == "creada"]
        rechazadas = [uno for uno in resultados if uno["clase"] == "rechazada"]
        errores = [uno for uno in resultados if uno["clase"] == "sqlite"]
        raras = [
            uno for uno in resultados
            if uno["clase"] in ("inesperada", "barrera")
        ]

        METRICAS["ACEPTADAS"] += len(creadas)
        METRICAS["RECHAZADAS"] += len(rechazadas)
        METRICAS["ERRORES_SQLITE"] += len(errores)
        METRICAS["EXCEPCIONES"] += len(raras)

        assert not errores, "Errores de SQLite: " + repr(errores)
        assert not raras, "Excepciones inesperadas: " + repr(raras)
        assert len(creadas) == 1, (
            "Crearon " + str(len(creadas)) + " procesos en vez de uno: "
            + repr([uno["marca"] for uno in creadas])
        )
        assert len(rechazadas) == creadores - 1, (
            "No todos los perdedores recibieron un rechazo controlado: "
            + repr(resultados)
        )

        ganador = creadas[0]["marca"]
        titulo_esperado = "Creada por " + ganador

        # La definición que quedó es la del ganador, en los dos sitios.
        fila = fila_de(raiz, "T-0901")

        assert fila["titulo"] == titulo_esperado, (
            "La fila quedó con la definición de otro: " + repr(fila["titulo"])
            + " en vez de " + repr(titulo_esperado)
        )

        import json

        espejo = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        assert espejo["titulo"] == titulo_esperado, (
            "El JSON quedó con la definición de un perdedor: "
            + repr(espejo["titulo"])
        )
        assert espejo["ambito_archivos"] == ["modulos/" + ganador + ".py"]

        # Una sola tarea, un solo evento de creación.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            # Dos: el testigo del orden y la tarea en disputa. Lo que
            # importa es que la disputada exista UNA vez.
            assert estado_global.contar_tareas(con) == 2
            eventos = estado_global.listar_eventos(con, "T-0901")
        finally:
            con.close()

        creaciones = [
            uno for uno in eventos
            if uno["tipo"] == estado_global.EVENTO_CREACION
        ]

        assert len(creaciones) == 1, (
            "Se registraron " + str(len(creaciones)) + " creaciones."
        )

        # Sin temporales huérfanos de la escritura atómica.
        sobrantes = list(fichas.carpeta_tareas(raiz).glob("*.tmp*"))

        assert not sobrantes, "Quedaron temporales: " + repr(sobrantes)

        comprobar_integridad(raiz)

        print(
            "OK (1 creada, " + str(len(rechazadas)) + " rechazadas, "
            "ganador " + ganador + ")"
        )
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# GRUPO 5 — Vitalidad y recuperación
# ----------------------------------------------------------------------

def prueba_m_un_latido_vencido_no_basta_para_declarar_abandono():
    """
    Parecer muerta no es estarlo: con una sola señal no se arrebata nada.

    El latido es una señal DÉBIL. Puede faltar porque el trabajador murió,
    pero también porque estuvo una hora compilando, porque el reloj de la
    otra máquina va adelantado o porque nadie emitió latidos a mano.
    Declarar huérfana una tarea por eso es quitársela a alguien que sigue
    trabajando, y ése es el peor fallo posible en un sistema que va a
    tener varios trabajadores a la vez.
    """
    print(" 13. un latido vencido no basta para declarar abandono:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A", pid=os.getpid())

        antes = fila_de(raiz, "T-0901")

        # El latido caducó de largo, pero el proceso SIGUE VIVO.
        futuro = nucleo.ahora_datetime() + timedelta(minutes=30)

        informe = nucleo.reanudar(
            raiz,
            ahora=futuro,
            comprobar_proceso=lambda _pid: True,
            latido_maximo_s=60,
        )
        METRICAS["OPERACIONES"] += 1

        assert not informe["huerfanas"], (
            "Se declaró huérfana una ejecución con el proceso vivo: "
            + repr(informe["huerfanas"])
        )
        assert [uno["id"] for uno in informe["latido_vencido"]] == ["T-0901"], (
            "No se informó del latido vencido: " + repr(informe)
        )

        despues = fila_de(raiz, "T-0901")

        if despues["trabajador_id"] != antes["trabajador_id"]:
            METRICAS["ROBOS_INDEBIDOS"] += 1

        assert despues["trabajador_id"] == "worker-A", (
            "Se le arrebató la tarea a un trabajador vivo."
        )
        assert despues["estado"] == str(Estado.EN_EJECUCION)
        assert despues["generacion"] == antes["generacion"]

        # Con la SEGUNDA señal —el proceso ya no está— sí se recupera.
        segundo = nucleo.reanudar(
            raiz,
            ahora=futuro,
            comprobar_proceso=lambda _pid: False,
            latido_maximo_s=60,
        )
        METRICAS["OPERACIONES"] += 1

        assert [uno["id"] for uno in segundo["huerfanas"]] == ["T-0901"], (
            "Con dos señales tenía que recuperarse: " + repr(segundo)
        )
        assert "dos señales" in segundo["huerfanas"][0]["motivo"]

        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_n_la_vitalidad_distingue_los_cinco_estados():
    """
    El tablero puede decir qué le pasa a una tarea, no sólo en qué estado
    está.

    Son cosas distintas: REABIERTO es un estado de la tarea; que nadie la
    esté ejecutando es otra cosa, y es la que hace falta para decidir si
    hay que mirar algo.
    """
    print(" 14. la vitalidad distingue los seis estados:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        # REANUDABLE: no hay ejecución y se puede tomar.
        informe = nucleo.vitalidad(fila_de(raiz, "T-0901"))

        assert informe["vitalidad"] == nucleo.VITALIDAD_REANUDABLE, informe

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A", pid=os.getpid())

        # ACTIVA: ejecución con señal.
        informe = nucleo.vitalidad(
            fila_de(raiz, "T-0901"), comprobar_proceso=lambda _pid: True
        )

        assert informe["vitalidad"] == nucleo.VITALIDAD_ACTIVA, informe
        assert informe["edad_latido_s"] is not None
        assert informe["generacion"] == 1

        futuro = nucleo.ahora_datetime() + timedelta(minutes=30)

        # LATIDO_VENCIDO: caducó pero el proceso vive.
        informe = nucleo.vitalidad(
            fila_de(raiz, "T-0901"),
            ahora=futuro,
            comprobar_proceso=lambda _pid: True,
            latido_maximo_s=60,
        )

        assert informe["vitalidad"] == nucleo.VITALIDAD_LATIDO_VENCIDO, informe
        assert informe["edad_latido_s"] >= 1800

        # HUERFANA: caducó y el proceso no está.
        informe = nucleo.vitalidad(
            fila_de(raiz, "T-0901"),
            ahora=futuro,
            comprobar_proceso=lambda _pid: False,
            latido_maximo_s=60,
        )

        assert informe["vitalidad"] == nucleo.VITALIDAD_HUERFANA, informe

        # ESPERA_HUMANA: sin ejecución, no se puede tomar y espera a
        # alguien. Rotularla FINALIZADA contradecía al motivo impreso al
        # lado («está esperando una decisión humana»).
        nucleo.devolver(raiz, "T-0901", trabajador_id="worker-A")
        nucleo.bloquear(raiz, "T-0901", "bloqueada para la prueba")

        informe = nucleo.vitalidad(fila_de(raiz, "T-0901"))

        assert informe["vitalidad"] == nucleo.VITALIDAD_ESPERA_HUMANA, informe
        assert informe["requiere_atencion"] is True

        # FINALIZADA: sin ejecución y cerrada de verdad.
        nucleo.rechazar(raiz, "T-0901", "cerrada para la prueba")

        informe = nucleo.vitalidad(fila_de(raiz, "T-0901"))

        assert informe["vitalidad"] == nucleo.VITALIDAD_FINALIZADA, informe
        assert informe["requiere_atencion"] is False

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_o_reanudar_es_idempotente():
    """
    Recuperar dos veces no duplica nada.

    Una recuperación se ejecuta justo después de un apagón, que es cuando
    más probable es que alguien la lance dos veces —o que un guion la
    reintente—. Si cada pasada añadiera una ejecución interrumpida y una
    transición, el historial acabaría contando una historia que no pasó.
    """
    print(" 15. reanudar dos veces no duplica nada:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A", pid=1)

        futuro = nucleo.ahora_datetime() + timedelta(hours=2)

        primera = nucleo.reanudar(
            raiz, ahora=futuro, comprobar_proceso=lambda _pid: False
        )
        METRICAS["OPERACIONES"] += 1

        assert len(primera["huerfanas"]) == 1

        estado_tras_una = fila_de(raiz, "T-0901")
        ficha_tras_una = fichas.leer(raiz, "T-0901")
        ejecuciones_una = len(estado_tras_una["ejecuciones"])

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            eventos_una = estado_global.contar_eventos(con)
        finally:
            con.close()

        # Segunda pasada, idéntica.
        segunda = nucleo.reanudar(
            raiz, ahora=futuro, comprobar_proceso=lambda _pid: False
        )
        METRICAS["OPERACIONES"] += 1

        assert not segunda["huerfanas"], (
            "La segunda pasada volvió a recuperar la misma tarea: "
            + repr(segunda["huerfanas"])
        )
        assert segunda["revisadas"] == 0, (
            "La tarea ya no estaba en ejecución: no había nada que revisar."
        )

        estado_tras_dos = fila_de(raiz, "T-0901")

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            eventos_dos = estado_global.contar_eventos(con)
        finally:
            con.close()

        assert len(estado_tras_dos["ejecuciones"]) == ejecuciones_una, (
            "Se duplicó la ejecución interrumpida: "
            + str(ejecuciones_una) + " -> "
            + str(len(estado_tras_dos["ejecuciones"]))
        )
        assert eventos_dos == eventos_una, (
            "Se duplicaron eventos: " + str(eventos_una) + " -> "
            + str(eventos_dos)
        )
        assert estado_tras_dos["estado"] == estado_tras_una["estado"]
        assert len(fichas.leer(raiz, "T-0901").historial) == len(
            ficha_tras_una.historial
        ), "Se duplicó el historial del espejo JSON."

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_p_tras_recuperar_b_toma_y_la_orden_tardia_de_a_cae():
    """
    Recuperada la tarea, B la toma y lo que A mande después no entra.

    Es el caso completo de un apagón: A muere de verdad, la recuperación
    invalida su propiedad, B empieza una ejecución nueva y una orden que A
    había dejado en vuelo llega tarde. Encadena lo de A3.2 con lo de A3.3,
    que es donde se ve si las garantías se sostienen juntas o sólo por
    separado.
    """
    print(" 16. tras recuperar, B toma y lo tardío de A cae:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        vieja = nucleo.tomar(
            raiz, "T-0901", trabajador_id="worker-A", pid=1
        )

        # A muere de verdad.
        futuro = nucleo.ahora_datetime() + timedelta(hours=2)

        nucleo.reanudar(
            raiz, ahora=futuro, comprobar_proceso=lambda _pid: False
        )
        METRICAS["OPERACIONES"] += 1

        fila = fila_de(raiz, "T-0901")

        assert fila["trabajador_id"] is None, "No se invalidó la propiedad."
        assert fila["estado"] == str(Estado.REABIERTO)

        # B empieza una ejecución NUEVA.
        nueva = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert nueva.generacion > vieja.generacion, (
            "La ejecución nueva no se distingue de la vieja."
        )

        testigo = fila_de(raiz, "T-0901")

        # Y lo que A había dejado en vuelo llega ahora.
        for nombre, emitir in (
            ("latido", lambda: nucleo.latido(
                raiz,
                "T-0901",
                trabajador_id="worker-A",
                generacion=vieja.generacion,
            )),
            ("devolver", lambda: nucleo.devolver(
                raiz,
                "T-0901",
                trabajador_id="worker-A",
                generacion=vieja.generacion,
            )),
        ):
            METRICAS["OPERACIONES"] += 1

            try:
                emitir()
            except nucleo.ErrorPropiedad:
                METRICAS["RECHAZADAS"] += 1
            else:
                METRICAS["ACEPTADAS"] += 1
                METRICAS["ROBOS_INDEBIDOS"] += 1
                raise AssertionError(
                    "La orden tardía '" + nombre + "' de A entró después de "
                    "que B tomara la tarea."
                )

        final = fila_de(raiz, "T-0901")

        assert final["trabajador_id"] == "worker-B"
        assert final["generacion"] == nueva.generacion
        assert final["ultimo_latido"] == testigo["ultimo_latido"]
        assert final["estado"] == str(Estado.EN_EJECUCION)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_s_el_worktree_que_desaparece_se_avisa_y_frena_la_retoma():
    """
    Si el árbol de la tarea ya no está, se avisa y no se concede a ciegas.

    Escenario real: la tarea se tomó en un worktree, el proceso murió y
    entre medias el árbol desapareció —lo borró el usuario, era una unidad
    extraíble, o el `git worktree` se deshizo—. La recuperación devuelve
    la tarea a REABIERTO, pero la fila sigue apuntando a una ruta muerta.

    Aquí se comprueban las dos mitades:

    1. `reanudar` lo dice. No borra el dato, porque el árbol puede volver
       y esa decisión es de una persona, pero no deja el problema callado.

    2. Una retoma que NO declara worktree hereda esa ruta, y se rechaza en
       la toma. Antes se concedía: el trabajador creía tener la tarea,
       hacía el trabajo y sólo al verificar descubría que su árbol no
       existía. Fallar al conceder cuesta un mensaje; fallar al verificar
       cuesta el trabajo entero.
    """
    print(" 17. un worktree que desaparece se avisa y no se hereda:",
          end=" ")

    principal, aparte, arboles = montar_tres_arboles()

    try:
        nucleo.crear(
            principal,
            "T-0903",
            titulo="Tarea del árbol A",
            ambito_archivos=["modulos/a/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_arbol.py"],
        )
        tomada = nucleo.tomar(
            principal,
            "T-0903",
            trabajador_id="worker-A",
            worktree=str(arboles["A"]),
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert tomada.worktree == str(arboles["A"].resolve())

        # Primero, con la ejecución todavía FRESCA: el árbol desaparece y
        # `reanudar` tiene que avisarlo sin tocar nada. Antes sólo se
        # miraba el árbol de las tareas que se liberaban, así que una
        # ejecución activa con el worktree borrado salía como «sigue
        # activa» y nada más.
        shutil.rmtree(arboles["A"])
        assert not arboles["A"].exists()

        fresco = nucleo.reanudar(principal, comprobar_proceso=lambda _pid: True)
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert [uno["id"] for uno in fresco["activas"]] == ["T-0903"], (
            "Una ejecución viva se tocó por perder el árbol: " + repr(fresco)
        )
        assert [uno["id"] for uno in fresco["worktree_ausente"]] == [
            "T-0903"
        ], (
            "La recuperación calló el worktree ausente de una ejecución "
            "activa: " + repr(fresco["worktree_ausente"])
        )
        assert fresco["worktree_ausente"][0]["clase"] == nucleo.CLASE_ACTIVA

        viva = fila_de(principal, "T-0903")

        assert viva["estado"] == str(Estado.EN_EJECUCION)
        assert viva["worktree"] == str(arboles["A"].resolve()), (
            "Avisar del árbol ausente no debía cambiar la fila."
        )

        # La ejecución queda demostrada muerta: latido antiguo.
        con = estado_global.abrir(estado_global.ruta_base(principal))

        try:
            with estado_global.transaccion(con):
                con.execute(
                    "UPDATE tareas SET ultimo_latido = ? WHERE id = ?",
                    ("2020-01-01T00:00:00+00:00", "T-0903"),
                )
        finally:
            con.close()

        informe = nucleo.reanudar(
            principal, comprobar_proceso=lambda _pid: False
        )

        assert len(informe["huerfanas"]) == 1, (
            "La recuperación no recuperó la tarea: " + repr(informe)
        )
        assert [uno["id"] for uno in informe["worktree_ausente"]] == [
            "T-0903"
        ], (
            "La recuperación no avisó del worktree ausente: "
            + repr(informe["worktree_ausente"])
        )

        fila = fila_de(principal, "T-0903")

        assert fila["estado"] == str(Estado.REABIERTO)

        # El árbol pertenece a la EJECUCIÓN, así que se suelta con ella. Si
        # se quedara pegado, el trabajador siguiente —que no puede saberlo—
        # acabaría verificando en el árbol del anterior: sus pruebas
        # correrían sobre trabajo ajeno y el resultado se grabaría como
        # suyo. Por eso la ruta muerta se informa y NO se hereda.
        assert fila["worktree"] is None, (
            "El árbol quedó pegado a la tarea tras recuperarla: "
            + repr(fila["worktree"])
        )

        # Declarar explícitamente la ruta muerta sí se rechaza, y en la
        # TOMA, no al verificar con el trabajo ya hecho.
        try:
            nucleo.tomar(
                principal,
                "T-0903",
                trabajador_id="worker-B",
                worktree=str(arboles["A"]),
            )
            raise AssertionError(
                "Se concedió la toma sobre un worktree que no existe."
            )
        except nucleo.ErrorWorktree as rechazo:
            assert "no existe" in str(rechazo), str(rechazo)

        METRICAS["OPERACIONES"] += 1
        METRICAS["RECHAZADAS"] += 1

        # Y la tarea sigue libre: un rechazo no la deja reclamada a medias.
        despues = fila_de(principal, "T-0903")

        assert despues["estado"] == str(Estado.REABIERTO)
        assert despues["trabajador_id"] is None

        # Declarando un árbol válido, la misma tarea se toma sin problema.
        rescatada = nucleo.tomar(
            principal,
            "T-0903",
            trabajador_id="worker-B",
            worktree=str(arboles["B"]),
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert rescatada.worktree == str(arboles["B"].resolve())

        comprobar_integridad(principal)
    finally:
        borrar(aparte)
        borrar(principal)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 6 — Seguridad de rutas
# ----------------------------------------------------------------------

def prueba_q_rutas_raras_pero_legitimas_se_aceptan():
    """
    Una ruta legítima no se rechaza por tener una forma incómoda.

    El otro lado de la moneda: si la validación fuera demasiado estricta,
    un worktree perfectamente válido dejaría de poder usarse por vivir en
    una carpeta con espacios o por llegar escrito con `..` en medio. Los
    dos errores cuestan; éste se nota menos y por eso conviene probarlo.
    """
    print(" 18. una ruta legítima con forma rara se acepta:", end=" ")

    base = Path(tempfile.mkdtemp(prefix="rutas con espacios "))

    try:
        principal = base / "repo del proyecto"
        principal.mkdir()

        inicio = _git(principal, "init", "-q", "-b", "main")
        assert inicio.returncode == 0, inicio.stderr

        _git(principal, "config", "user.name", "Prueba A3.3")
        _git(principal, "config", "user.email", "prueba@ingenieria.local")

        (principal / "semilla.txt").write_text("x", encoding="utf-8")
        _git(principal, "add", "-A")
        hecho = _git(principal, "commit", "-q", "-m", "semilla")
        assert hecho.returncode == 0, hecho.stderr

        enlazado = base / "arbol con espacios"
        creado = _git(
            principal, "worktree", "add", "-q", str(enlazado), "-b", "rama-x"
        )
        assert creado.returncode == 0, creado.stderr

        esperado = enlazado.resolve()

        formas = {
            "absoluta con espacios": str(enlazado),
            "relativa con espacios": "../arbol con espacios",
            "con puntos en medio": str(
                principal / ".." / "arbol con espacios"
            ),
            "con barra final": str(enlazado) + os.sep,
        }

        # En Windows la letra de unidad y el caso no distinguen rutas; en
        # Linux sí, así que esa variante sólo se prueba donde aplica.
        if os.name == "nt":
            formas["caso distinto"] = str(enlazado).upper()

        for caso, forma in formas.items():
            METRICAS["OPERACIONES"] += 1

            resuelta = nucleo.resolver_worktree(principal, forma)

            METRICAS["ACEPTADAS"] += 1

            assert resuelta == esperado, (
                "La forma '" + caso + "' no resolvió al mismo directorio: "
                + str(resuelta) + " en vez de " + str(esperado)
            )

        # Las dos formas de Windows que NO son absolutas y que al unirse a
        # la raíz la descartarían —`C:pruebas`, relativa a la unidad, y
        # `\pruebas`, con raíz y sin unidad— se rechazan con mensaje
        # propio. En POSIX son nombres relativos corrientes y el caso no se
        # puede ejercitar: se dice, no se calla.
        if os.name == "nt":
            for forma in ("C:pruebas", "\\pruebas"):
                METRICAS["OPERACIONES"] += 1

                try:
                    nucleo.resolver_worktree(principal, forma)
                except nucleo.ErrorWorktree as rechazo:
                    METRICAS["RECHAZADAS"] += 1
                    assert "lleva unidad o raíz" in str(rechazo), str(rechazo)
                else:
                    METRICAS["ACEPTADAS"] += 1
                    raise AssertionError(
                        "Se aceptó la forma de Windows '" + forma + "', que "
                        "ignora la raíz del repositorio."
                    )
        else:
            OMITIDAS.append(
                "rutas relativas a unidad de Windows (C:pruebas, \\pruebas): "
                "sólo se pueden ejercitar en Windows"
            )

        # Un enlace simbólico al worktree resuelve al mismo sitio, no a otro.
        if os.name != "nt":
            enlace = base / "atajo"

            try:
                enlace.symlink_to(enlazado, target_is_directory=True)
            except OSError as problema:
                # Si se omite, se DICE. Un caso que desaparece en silencio
                # y deja la comprobación imprimiendo «OK» es peor que no
                # tenerlo: nadie se entera de que dejó de probarse.
                enlace = None
                OMITIDAS.append(
                    "enlace simbólico al worktree (" + str(problema) + ")"
                )

            if enlace is not None:
                METRICAS["OPERACIONES"] += 1

                assert nucleo.resolver_worktree(principal, str(enlace)) == (
                    esperado
                ), "Un enlace simbólico no resolvió al worktree real."

                METRICAS["ACEPTADAS"] += 1
    finally:
        _git(base / "repo del proyecto", "worktree", "prune")
        borrar(base)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 7 — Estrés
# ----------------------------------------------------------------------

def _escritor_concurrente(ruta_raiz: str, identificador: str, papel: str,
                          credencial: dict, vueltas: int, barrera) -> dict:
    """
    Un proceso que emite su orden una y otra vez sobre campos propios.

    Cada papel escribe una columna distinta. Si las escrituras se pisaran,
    el valor de un papel aparecería revertido por el de otro.
    """
    import sys as _sys
    from pathlib import Path as _Path

    for sufijo in ("orquestacion", "nucleo"):
        destino = str(_Path(__file__).resolve().parents[2] / sufijo)
        if destino not in _sys.path:
            _sys.path.insert(0, destino)

    from ingenieria_supervisor import estado_global as _global
    from ingenieria_supervisor import supervisor as _nucleo

    raiz = _Path(ruta_raiz)

    recuento = {
        "papel": papel,
        "emitidas": 0,
        "aceptadas": 0,
        "rechazadas": 0,
        "errores_sqlite": 0,
        "inesperadas": 0,
        "ultimo": None,
        "retrocesos": 0,
        "detalles": [],
    }

    try:
        barrera.wait(timeout=ESPERA_BARRERA_S)
    except Exception as error:
        recuento["inesperadas"] += 1
        recuento["detalles"].append("barrera: " + str(error))
        return recuento

    for vuelta in range(vueltas):
        marca = papel + "-" + str(vuelta)
        recuento["emitidas"] += 1

        retroceso = False

        try:
            if papel.startswith("latido"):
                # Marca propia, distinta para cada proceso y cada vuelta.
                # Con un literal fijo para todos, un lost update entre dos
                # latidos era indetectable: la aserción final pasaba aunque
                # sólo hubiera entrado UNA escritura de dieciocho.
                #
                # Los procesos se intercalan como quieren, así que la guarda
                # de no regresión rechazará las marcas que lleguen «tarde»:
                # eso NO es una orden rechazada ni una pérdida, es la guarda
                # haciendo su trabajo, y se cuenta aparte. Lo que se mide
                # como aceptado es lo que el acompañante confirma
                # (`emitidos`), no lo que `emitir_uno` devuelve, que es
                # True también en un retroceso.
                indice = int("".join(c for c in papel if c.isdigit()) or "0")
                total = vuelta * 10 + indice
                marca_latido = "2030-01-01T%02d:%02d:%02d+00:00" % (
                    total // 3600, (total // 60) % 60, total % 60
                )
                acompanante = _nucleo.LatidoAutomatico(
                    raiz,
                    identificador,
                    credencial["trabajador_id"],
                    credencial["generacion"],
                    reloj=lambda valor=marca_latido: valor,
                )
                sigue = acompanante.emitir_uno()
                aceptada = acompanante.emitidos == 1
                retroceso = sigue and not aceptada and acompanante.retrocesos == 1

                if aceptada:
                    recuento.setdefault("latidos", []).append(marca_latido)
            else:
                con = _global.abrir(_global.ruta_base(raiz))

                try:
                    with _global.transaccion(con):
                        informe = _global.actualizar_si_propietario(
                            con,
                            identificador,
                            {"ultima_falla": _global._a_json({"marca": marca})},
                            generacion=credencial["generacion"],
                            momento="2030-01-01T00:00:00+00:00",
                            trabajador_id=credencial["trabajador_id"],
                        )
                finally:
                    con.close()

                aceptada = (
                    informe["resultado"] == _global.ESCRITURA_ACEPTADA
                )

            if aceptada:
                recuento["aceptadas"] += 1
                recuento["ultimo"] = marca
                recuento.setdefault("marcas", []).append(marca)
            elif retroceso:
                recuento["retrocesos"] += 1
            else:
                recuento["rechazadas"] += 1
        except sqlite3.Error as error:
            recuento["errores_sqlite"] += 1
            recuento["detalles"].append("sqlite: " + str(error))
        except Exception as error:
            recuento["inesperadas"] += 1
            recuento["detalles"].append(
                type(error).__name__ + ": " + str(error)
            )

    return recuento


def prueba_r_estres_de_escrituras_concurrentes(escritores: int, vueltas: int):
    """
    Muchos escritores válidos a la vez sobre campos distintos: nada se pierde.

    Es la versión concurrente de la comprobación 1. Allí se demostraba con
    dos órdenes secuenciales que el lost update existía; aquí se somete a
    procesos reales escribiendo sin parar, que es donde una condición mal
    puesta acaba apareciendo.
    """
    print(
        " 37. estrés: " + str(escritores) + " escritores x " + str(vueltas)
        + " vueltas:",
        end=" ",
    )

    contexto = multiprocessing.get_context("spawn")
    raiz = crear_repositorio("estres_")

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        credencial = {
            "trabajador_id": "worker-A",
            "generacion": tomada.generacion,
        }

        papeles = [
            "latido" if numero % 2 == 0 else "falla"
            for numero in range(escritores)
        ]

        with contexto.Manager() as gestor:
            barrera = gestor.Barrier(escritores)
            reserva = gestor.Pool(processes=escritores)

            try:
                pendientes = [
                    reserva.apply_async(
                        _escritor_concurrente,
                        (
                            str(raiz),
                            "T-0901",
                            papel + str(numero),
                            credencial,
                            vueltas,
                            barrera,
                        ),
                    )
                    for numero, papel in enumerate(papeles)
                ]

                recuentos = [
                    uno.get(timeout=ESPERA_PROCESO_S) for uno in pendientes
                ]
            finally:
                reserva.close()
                reserva.join()

        emitidas = sum(uno["emitidas"] for uno in recuentos)
        aceptadas = sum(uno["aceptadas"] for uno in recuentos)
        rechazadas = sum(uno["rechazadas"] for uno in recuentos)
        retrocesos = sum(uno["retrocesos"] for uno in recuentos)
        errores = sum(uno["errores_sqlite"] for uno in recuentos)
        raras = sum(uno["inesperadas"] for uno in recuentos)

        METRICAS["OPERACIONES"] += emitidas
        METRICAS["ACEPTADAS"] += aceptadas
        METRICAS["RECHAZADAS"] += rechazadas
        METRICAS["ERRORES_SQLITE"] += errores
        METRICAS["EXCEPCIONES"] += raras

        detalles = [t for uno in recuentos for t in uno["detalles"]][:5]

        assert not errores, "Errores de SQLite: " + "; ".join(detalles)
        assert not raras, "Excepciones: " + "; ".join(detalles)
        # Toda orden válida entra, salvo el latido que llega con una marca
        # más vieja que la grabada: ése lo frena la guarda de no regresión
        # a propósito, y se cuenta como retroceso, no como rechazo.
        assert rechazadas == 0 and aceptadas + retrocesos == emitidas, (
            "Se rechazaron órdenes válidas: " + str(rechazadas) + " de "
            + str(emitidas) + " (" + str(retrocesos) + " retrocesos)"
        )

        fila = fila_de(raiz, "T-0901")

        # Lo que de verdad se mide: la ejecución sigue entera. Ninguna
        # escritura de un papel borró lo de otro ni tocó la propiedad.
        if fila["trabajador_id"] != "worker-A":
            METRICAS["ROBOS_INDEBIDOS"] += 1

        assert fila["trabajador_id"] == "worker-A"
        assert fila["generacion"] == tomada.generacion
        assert fila["estado"] == str(Estado.EN_EJECUCION)
        assert fila["intentos"] == 0, (
            "Alguna escritura tocó los intentos: " + str(fila["intentos"])
        )

        # El latido y la falla conviven: ninguno dejó al otro en su valor
        # inicial, que es justo lo que pasaría si se pisaran.
        latidos = sorted(
            {marca for uno in recuentos for marca in uno.get("latidos", [])}
        )
        marcas = {marca for uno in recuentos for marca in uno.get("marcas", [])}

        assert latidos, "Ningún latido llegó a confirmarse."
        assert marcas, "Ninguna escritura de `ultima_falla` se confirmó."

        METRICAS["LATIDOS_AUTOMATICOS"] += len(latidos)

        # El latido que sobrevive debe ser el MAYOR de los confirmados. La
        # guarda de no retroceso lo garantiza; sin comprobarlo, un
        # retroceso pasaba desapercibido.
        if fila["ultimo_latido"] != latidos[-1]:
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert fila["ultimo_latido"] == latidos[-1], (
            "El latido grabado no es el último confirmado: "
            + repr(fila["ultimo_latido"]) + " en vez de " + repr(latidos[-1])
        )

        grabada = (fila["ultima_falla"] or {}).get("marca")

        # Y la falla grabada tiene que ser una que algún proceso emitió de
        # verdad. Antes bastaba con que existiera la clave: una mutación
        # que conservara la PRIMERA escritura y perdiera las diecisiete
        # siguientes pasaba la prueba.
        if grabada not in marcas:
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert grabada in marcas, (
            "La marca grabada no la emitió nadie: " + repr(grabada)
        )

        comprobar_integridad(raiz)

        print(
            "OK (" + str(emitidas) + " órdenes, " + str(aceptadas)
            + " aceptadas, " + str(len(latidos)) + " latidos confirmados, "
            + str(retrocesos) + " retrocesos frenados por la guarda, "
            "0 perdidas)"
        )
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# GRUPO 8 — Lo que la ronda de auditoría destapó
#
# Cada comprobación de aquí existe porque un mutante sobrevivió a la
# batería anterior: el motor hacía lo correcto y nada lo comprobaba.
# ----------------------------------------------------------------------

def prueba_t_verificar_rechaza_un_arbol_invalido():
    """
    `verificar` rechaza de verdad un árbol que ya no vale, y no graba nada.

    Éste era el gate crítico de A3.3 sin ninguna prueba de extremo a
    extremo: la comprobación 5 llama a `resolver_worktree` directamente y
    jamás invoca `verificar`. Bastaba con que `verificar` dejara de
    propagar `ErrorWorktree` —tragarlo y caer a la raíz— para que el
    agujero volviera con la batería entera en verde, y entonces las
    pruebas de main se grababan como resultado de la tarea.
    """
    print(" 19. verificar rechaza un árbol inválido y no graba:", end=" ")

    principal, aparte, arboles = montar_tres_arboles()

    try:
        nucleo.crear(
            principal,
            "T-0904",
            titulo="Tarea del árbol A",
            ambito_archivos=["modulos/a/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_arbol.py"],
        )
        tomada = nucleo.tomar(
            principal,
            "T-0904",
            trabajador_id="worker-A",
            worktree=str(arboles["A"]),
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        # El árbol desaparece con la tarea ya tomada.
        shutil.rmtree(arboles["A"])

        antes = fila_de(principal, "T-0904")

        METRICAS["OPERACIONES"] += 1

        try:
            nucleo.verificar(
                principal,
                "T-0904",
                trabajador_id="worker-A",
                generacion=tomada.generacion,
            )
            METRICAS["ACEPTADAS"] += 1
            METRICAS["VERIFICACIONES_EN_ARBOL_INCORRECTO"] += 1
            raise AssertionError(
                "verificar se ejecutó pese a que el árbol de la tarea no "
                "existe: el resultado sería el de otro árbol."
            )
        except nucleo.ErrorWorktree as rechazo:
            METRICAS["RECHAZADAS"] += 1
            assert "no existe" in str(rechazo), str(rechazo)

        despues = fila_de(principal, "T-0904")

        assert despues["ultima_verificacion"] == antes["ultima_verificacion"], (
            "Se grabó una verificación que no llegó a ejecutarse: "
            + repr(despues["ultima_verificacion"])
        )
        assert despues["estado"] == str(Estado.EN_EJECUCION), (
            "El rechazo cambió el estado de la tarea: " + despues["estado"]
        )
        assert despues["intentos"] == antes["intentos"], (
            "El rechazo consumió un intento."
        )

        # Segunda mitad: el árbol desaparece A MITAD de la corrida. El
        # corredor lanza cada prueba con `cwd` en el árbol, así que la
        # siguiente prueba fallaba con FileNotFoundError y eso salía como
        # traceback de Python con código 1, no como ErrorWorktree (6).
        # Windows no permite borrar el directorio de trabajo de un proceso
        # vivo: ahí el caso se omite, y se dice.
        if os.name == "nt":
            OMITIDAS.append(
                "árbol que desaparece a mitad de la corrida (Windows no "
                "permite borrar el directorio de trabajo de un proceso vivo)"
            )
        else:
            arbol_b = arboles["B"]
            (
                arbol_b / "pruebas" / "demostracion" / "prueba_0_lenta.py"
            ).write_text(
                "import time\ntime.sleep(1.5)\nprint('PRUEBA_LENTA=OK')\n",
                encoding="utf-8",
            )
            _git(arbol_b, "add", "-A")
            hecho = _git(arbol_b, "commit", "-q", "-m", "prueba lenta")
            assert hecho.returncode == 0, hecho.stderr

            nucleo.devolver(
                principal, "T-0904",
                trabajador_id="worker-A", generacion=tomada.generacion,
            )
            segunda = nucleo.tomar(
                principal, "T-0904",
                trabajador_id="worker-A", worktree=str(arbol_b),
            )
            METRICAS["OPERACIONES"] += 2
            METRICAS["ACEPTADAS"] += 2

            def borrar_a_mitad():
                time.sleep(0.6)
                shutil.rmtree(arbol_b, ignore_errors=True)

            hilo = threading.Thread(target=borrar_a_mitad)
            hilo.start()

            METRICAS["OPERACIONES"] += 1

            try:
                nucleo.verificar(
                    principal, "T-0904",
                    trabajador_id="worker-A", generacion=segunda.generacion,
                )
                METRICAS["ACEPTADAS"] += 1
                METRICAS["VERIFICACIONES_EN_ARBOL_INCORRECTO"] += 1
                raise AssertionError(
                    "verificar terminó como si nada sobre un árbol que "
                    "desapareció a mitad de la corrida."
                )
            except nucleo.ErrorWorktree as rechazo:
                METRICAS["RECHAZADAS"] += 1
                assert "desapareció" in str(rechazo), str(rechazo)
            finally:
                hilo.join()

            assert not arbol_b.exists(), (
                "El hilo no llegó a borrar el árbol: la prueba no probó nada."
            )

            fila = fila_de(principal, "T-0904")

            assert not fila["ultima_verificacion"], (
                "Se grabó la corrida de un árbol que desapareció: "
                + repr(fila["ultima_verificacion"])
            )
            assert fila["estado"] == str(Estado.EN_EJECUCION), fila["estado"]

        comprobar_integridad(principal)
    finally:
        borrar(aparte)
        borrar(principal)

    print("OK")


def prueba_u_un_arbol_que_se_mueve_invalida_la_corrida():
    """
    Si el árbol cambia MIENTRAS corren las pruebas, no se graba un verde.

    La rama y el commit se leían DESPUÉS de correr. Entre el arranque de
    la batería —hasta dos minutos por archivo— y esa lectura cabe
    cualquier `commit`, `rebase` o `checkout` del propio trabajador que
    sigue trabajando en ese árbol. No hace falta malicia. Quedaba grabado
    «commit X, todo en verde» cuando X nunca se ejecutó y encima estaba
    rojo.
    """
    print(" 20. un árbol que se mueve durante la corrida no vale:", end=" ")

    principal = crear_repositorio("movil_")

    try:
        carpeta = principal / "pruebas" / "demostracion"
        (carpeta / "prueba_lenta.py").write_text(
            "import time\ntime.sleep(1.5)\nprint('PRUEBA_LENTA=OK')\n",
            encoding="utf-8",
        )

        _git(principal, "add", "-A")
        hecho = _git(principal, "commit", "-q", "-m", "base")
        assert hecho.returncode == 0, hecho.stderr

        nucleo.crear(
            principal,
            "T-0905",
            titulo="Tarea con árbol móvil",
            ambito_archivos=["modulos/m/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_lenta.py"],
        )
        tomada = nucleo.tomar(principal, "T-0905", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        commit_inicial = _commit_de(principal)

        def commitear_a_mitad():
            time.sleep(0.6)
            (principal / "otro.txt").write_text("v2", encoding="utf-8")
            _git(principal, "add", "-A")
            _git(principal, "commit", "-q", "-m", "v2 a mitad de la corrida")

        hilo = threading.Thread(target=commitear_a_mitad)
        hilo.start()

        METRICAS["OPERACIONES"] += 1

        try:
            nucleo.verificar(
                principal,
                "T-0905",
                trabajador_id="worker-A",
                generacion=tomada.generacion,
            )
            METRICAS["ACEPTADAS"] += 1
            raise AssertionError(
                "Se dio por buena una corrida sobre un árbol que cambió a "
                "mitad: el verde no corresponde a ningún commit concreto."
            )
        except nucleo.ErrorWorktree as rechazo:
            METRICAS["RECHAZADAS"] += 1
            assert "cambió" in str(rechazo), str(rechazo)
        finally:
            hilo.join()

        assert _commit_de(principal) != commit_inicial, (
            "El hilo no llegó a commitear: la prueba no probó nada."
        )

        fila = fila_de(principal, "T-0905")

        assert not fila["ultima_verificacion"], (
            "Se grabó la corrida de un árbol que se movió: "
            + repr(fila["ultima_verificacion"])
        )

        # Y el otro lado: una prueba que deja rastro SIN versionar —un
        # informe de cobertura, una salida, `__pycache__`— no puede
        # invalidar la corrida. Si lo hiciera, ninguna verificación podría
        # aprobarse jamás en un proyecto real.
        nucleo.reabrir(principal, "T-0905", "Segundo intento.")
        segunda = nucleo.tomar(principal, "T-0905", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        (carpeta / "prueba_lenta.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "Path('informe_de_cobertura.txt').write_text('rastro')\n"
            "time.sleep(0.2)\n"
            "print('PRUEBA_LENTA=OK')\n",
            encoding="utf-8",
        )
        _git(principal, "add", "-A")
        _git(principal, "commit", "-q", "-m", "prueba que deja rastro")

        informe = nucleo.verificar(
            principal,
            "T-0905",
            trabajador_id="worker-A",
            generacion=segunda.generacion,
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert (principal / "informe_de_cobertura.txt").exists(), (
            "La prueba no llegó a dejar rastro: no se probó nada."
        )
        assert informe["arbol_estable"] is True, (
            "Un archivo sin versionar creado por las propias pruebas "
            "invalidó la corrida."
        )
        assert informe["estado"] == str(Estado.PROPUESTO), informe["estado"]
        assert informe["sin_confirmar"] is False, (
            "Un árbol limpio se grabó como sucio."
        )

        # Tercera dirección: un árbol que YA estaba sucio y cuyo contenido
        # sin confirmar cambia a mitad. Comparar sólo «¿hay cambios?» antes
        # y después daba True == True y pasaba por quieto.
        (carpeta / "prueba_lenta.py").write_text(
            "import time\ntime.sleep(1.5)\nprint('PRUEBA_LENTA=OK')\n",
            encoding="utf-8",
        )
        _git(principal, "add", "-A")
        hecho = _git(principal, "commit", "-q", "-m", "prueba lenta otra vez")
        assert hecho.returncode == 0, hecho.stderr

        nucleo.reabrir(principal, "T-0905", "Tercer intento.")
        tercera = nucleo.tomar(principal, "T-0905", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        # Sucio ANTES de empezar, sin confirmar.
        (principal / "otro.txt").write_text("v3 sin confirmar", encoding="utf-8")
        assert _git(principal, "status", "--porcelain").stdout.strip(), (
            "El árbol no quedó sucio: la prueba no probaría nada."
        )

        def editar_a_mitad():
            time.sleep(0.6)
            (principal / "otro.txt").write_text(
                "v4 editado a mitad", encoding="utf-8"
            )

        hilo = threading.Thread(target=editar_a_mitad)
        hilo.start()

        METRICAS["OPERACIONES"] += 1

        try:
            nucleo.verificar(
                principal, "T-0905",
                trabajador_id="worker-A", generacion=tercera.generacion,
            )
            METRICAS["ACEPTADAS"] += 1
            raise AssertionError(
                "Se dio por buena una corrida cuyo contenido sin confirmar "
                "cambió a mitad: sucio antes, sucio después, y distinto."
            )
        except nucleo.ErrorWorktree as rechazo:
            METRICAS["RECHAZADAS"] += 1
            assert "contenido sin confirmar cambió" in str(rechazo), (
                str(rechazo)
            )
        finally:
            hilo.join()

        assert (principal / "otro.txt").read_text(encoding="utf-8").startswith(
            "v4"
        ), "El hilo no llegó a editar: la prueba no probó nada."

        # Cuarta: el árbol sigue sucio pero quieto. La corrida vale, y la
        # evidencia DICE que el commit grabado no contiene lo ejecutado.
        METRICAS["OPERACIONES"] += 1
        informe = nucleo.verificar(
            principal, "T-0905",
            trabajador_id="worker-A", generacion=tercera.generacion,
        )
        METRICAS["ACEPTADAS"] += 1

        assert informe["estado"] == str(Estado.PROPUESTO), informe["estado"]
        assert informe["sin_confirmar"] is True, (
            "La evidencia no dice que había cambios sin confirmar."
        )

        fila = fila_de(principal, "T-0905")

        assert fila["ultima_verificacion"]["sin_confirmar"] is True, (
            "La marca de cambios sin confirmar no llegó a la base: "
            + repr(fila["ultima_verificacion"])
        )

        tarjeta = next(
            una for una in nucleo.tablero(principal)["tareas"]
            if una["id"] == "T-0905"
        )
        assert tarjeta["verificacion_sin_confirmar"] is True, (
            "El tablero no publica que el commit verificado no contiene lo "
            "ejecutado: " + repr(tarjeta["verificacion_sin_confirmar"])
        )

        # Quinta: el rastro del PROPIO Supervisor no cuenta. El espejo JSON
        # está versionado; `tomar` lo deja modificado y `decidir` lo
        # regenera. Con la huella del contenido, una decisión humana
        # resuelta MIENTRAS corría la batería —justo lo que `verificar`
        # maneja releyendo las decisiones— abortaba la corrida entera con
        # «el árbol cambió». Regresión de la revisión final, destapada por
        # su crítico de completitud antes de cerrar.
        nucleo.crear(
            principal, "T-0906",
            titulo="Decidida a mitad",
            ambito_archivos=["modulos/d/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_lenta.py"],
            decisiones=[{"clave": "D-1", "descripcion": "a mitad"}],
        )
        _git(principal, "add", "-A")
        hecho = _git(principal, "commit", "-q", "-m", "ficha versionada y árbol limpio")
        assert hecho.returncode == 0, hecho.stderr
        assert not _git(principal, "status", "--porcelain").stdout.strip()

        quinta = nucleo.tomar(principal, "T-0906", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        rastro = _git(principal, "status", "--porcelain").stdout
        assert "orquestacion/tareas/T-0906.json" in rastro, (
            "La toma no dejó el espejo modificado: la prueba no probaría nada."
        )

        def decidir_a_mitad():
            time.sleep(0.6)
            nucleo.decidir(principal, "T-0906", "D-1", "resuelta a mitad")

        hilo = threading.Thread(target=decidir_a_mitad)
        hilo.start()

        try:
            METRICAS["OPERACIONES"] += 1
            informe = nucleo.verificar(
                principal, "T-0906",
                trabajador_id="worker-A", generacion=quinta.generacion,
            )
        finally:
            hilo.join()

        METRICAS["ACEPTADAS"] += 1

        assert informe["estado"] == str(Estado.PROPUESTO), (
            "Una decisión resuelta a mitad tiró la corrida: "
            + informe["estado"] + " / " + informe["motivo"]
        )
        assert informe["sin_confirmar"] is False, (
            "El espejo del propio Supervisor se contó como cambios sin "
            "confirmar."
        )
        assert informe["arbol_estable"] is True

        comprobar_integridad(principal)
    finally:
        borrar(principal)

    print("OK")


def prueba_v_verificar_abandona_si_pierde_la_propiedad():
    """
    Perder la tarea a mitad de la corrida invalida el resultado.

    Son trece líneas de `verificar` con cobertura cero: se podían borrar
    enteras sin que la batería se inmutara. Y es la pieza que convierte la
    detección del latido en una decisión: el resultado de esa corrida no
    es de nadie.
    """
    print(" 21. verificar abandona si pierde la propiedad:", end=" ")

    raiz = crear_repositorio("perdida_")

    try:
        carpeta = raiz / "pruebas" / "demostracion"
        (carpeta / "prueba_lenta.py").write_text(
            "import time\ntime.sleep(1.5)\nprint('PRUEBA_LENTA=OK')\n",
            encoding="utf-8",
        )

        ficha_minima(
            raiz,
            "T-0901",
            pruebas_requeridas=["pruebas/demostracion/prueba_lenta.py"],
        )
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        def arrebatar():
            time.sleep(0.5)
            nucleo.devolver(raiz, "T-0901", trabajador_id="worker-A")
            nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")

        hilo = threading.Thread(target=arrebatar)
        hilo.start()

        METRICAS["OPERACIONES"] += 1

        try:
            nucleo.verificar(
                raiz,
                "T-0901",
                trabajador_id="worker-A",
                generacion=tomada.generacion,
                intervalo_latido_s=0.02,
            )
            METRICAS["ACEPTADAS"] += 1
            raise AssertionError(
                "Se grabó el resultado de una corrida cuya tarea cambió de "
                "manos mientras corría."
            )
        except nucleo.ErrorPropiedad as rechazo:
            METRICAS["RECHAZADAS"] += 1

            # El motivo tiene que ser el REAL, no uno fijo. Aquí la tarea
            # cambió de dueño y además de generación.
            assert rechazo.informe["motivo"] in (
                estado_global.MOTIVO_OTRO_PROPIETARIO,
                estado_global.MOTIVO_GENERACION_VENCIDA,
                estado_global.MOTIVO_SIN_PROPIETARIO,
            ), repr(rechazo.informe["motivo"])

            # Y lo detectó el ACOMPAÑANTE mientras corría, no la escritura
            # final. Sin esto, borrar el bloque de `verificar` que abandona
            # al perder la propiedad pasaba igual: `persistir` lanzaba el
            # mismo error, y el docstring de esta comprobación mentía.
            assert "MIENTRAS se verificaba" in str(rechazo.informe["detalle"]), (
                "El rechazo no lo produjo el acompañante: "
                + str(rechazo.informe["detalle"])
            )
        finally:
            hilo.join()

        fila = fila_de(raiz, "T-0901")

        assert fila["trabajador_id"] == "worker-B", (
            "El arrebato no llegó a ocurrir: la prueba no probó nada."
        )
        assert not fila["ultima_verificacion"], (
            "Se grabó la verificación de un dueño que ya no lo era."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_w_cada_precondicion_del_latido_frena_por_separado():
    """
    Las tres precondiciones del latido, aisladas una a una.

    Las pruebas 8 y 10 creían comprobar el estado y la identidad, pero
    `devolver` cambia las tres cosas a la vez: el rechazo lo producía la
    identidad y las otras dos condiciones se podían borrar del motor sin
    que nada fallara. Aquí cada fila se deja tocada A MANO para que sólo
    una condición pueda frenar la escritura.
    """
    print(" 22. cada precondición del latido frena por separado:", end=" ")

    for caso, cambios, esperado in (
        (
            "sólo el estado",
            {"estado": str(Estado.PROPUESTO)},
            "el estado ya no admite latido",
        ),
        (
            "sólo la identidad",
            {"trabajador_id": "worker-Z"},
            "el trabajador ya no es el mismo",
        ),
        (
            "sólo la generación",
            {"generacion": 99},
            "la generación ya no es la misma",
        ),
    ):
        raiz = crear_repositorio("precondicion_")

        try:
            ficha_minima(raiz, "T-0901")
            tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

            acompanante = nucleo.LatidoAutomatico(
                raiz, "T-0901", "worker-A", tomada.generacion
            )

            assert acompanante.emitir_uno() is True, (
                "El latido no funcionaba ni en su caso bueno (" + caso + ")."
            )

            testigo = "2001-02-03T04:05:06+00:00"
            con = estado_global.abrir(estado_global.ruta_base(raiz))

            try:
                with estado_global.transaccion(con):
                    columnas = dict(cambios)
                    columnas["ultimo_latido"] = testigo

                    for columna, valor in columnas.items():
                        con.execute(
                            "UPDATE tareas SET " + columna + " = ? "
                            "WHERE id = ?",
                            (valor, "T-0901"),
                        )
            finally:
                con.close()

            METRICAS["OPERACIONES"] += 1

            assert acompanante.emitir_uno() is False, (
                "El latido pasó aunque " + esperado + " (" + caso + ")."
            )
            assert acompanante.propiedad_perdida is True, caso
            METRICAS["RECHAZADAS"] += 1

            fila = fila_de(raiz, "T-0901")

            if fila["ultimo_latido"] != testigo:
                METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

            assert fila["ultimo_latido"] == testigo, (
                "El latido escribió pese al rechazo (" + caso + "): "
                + repr(fila["ultimo_latido"])
            )

            comprobar_integridad(raiz)
        finally:
            borrar(raiz)

    print("OK")


def prueba_x_un_trabajador_de_otra_maquina_no_se_juzga_por_el_pid_local():
    """
    El PID de otra máquina no significa nada aquí.

    Todas las pruebas usaban identidades planas (`worker-A`), para las que
    `equipo_de` devuelve None y la rama `ajeno` no se ejercitaba nunca. En
    producción los identificadores llevan equipo, así que se estaba
    probando el motor con identidades que no existen.

    Sin esta comprobación se podía poner `ajeno = False` y la batería
    entera seguía en verde: bastaba un PID ajeno que no exista aquí para
    arrebatarle la tarea a alguien que sigue trabajando en su máquina.
    """
    print(" 23. un trabajador de otra máquina no se juzga por el PID:",
          end=" ")

    ajeno = "OTRA-MAQUINA/4242/abcd"
    propio = socket.gethostname()

    assert nucleo.equipo_de(ajeno) == "OTRA-MAQUINA"
    assert nucleo.equipo_de(ajeno) != propio, (
        "El nombre de esta máquina coincide con el inventado: la prueba no "
        "probaría nada."
    )

    def fila(latido_hace_s):
        momento = nucleo.ahora_datetime() - timedelta(seconds=latido_hace_s)

        return {
            "id": "T-0901",
            "estado": str(Estado.EN_EJECUCION),
            "trabajador_id": ajeno,
            "pid": 999999,
            "iniciado_en": momento.isoformat(timespec="seconds"),
            "ultimo_latido": momento.isoformat(timespec="seconds"),
            "generacion": 1,
            "worktree": None,
        }

    # Latido reciente: viva, aunque el PID no exista en esta máquina.
    reciente = nucleo.vitalidad(fila(60), comprobar_proceso=lambda _p: False)

    assert reciente["vitalidad"] == nucleo.VITALIDAD_ACTIVA, (
        "Se juzgó a un trabajador remoto por un PID local: "
        + repr(reciente)
    )

    # Latido vencido: duda, NUNCA huérfana. No hay segunda señal posible.
    for edad in (1800, 7200, 200000):
        vencido = nucleo.vitalidad(
            fila(edad), comprobar_proceso=lambda _p: False
        )

        assert vencido["vitalidad"] == nucleo.VITALIDAD_LATIDO_VENCIDO, (
            "Se declaró huérfano a un trabajador de otra máquina con "
            + str(edad) + " s de latido: " + repr(vencido)
        )
        assert vencido["requiere_atencion"] is True

    print("OK")


def prueba_y_una_fila_incompleta_no_se_da_por_muerta():
    """
    Una fila rota es una duda, no una prueba de muerte.

    `CLASE_INCONSISTENTE` se informaba como HUÉRFANA —que afirma que la
    ejecución está demostrada perdida— y el mapa de vitalidad fallaba
    ABIERTO: ante algo desconocido devolvía ACTIVA, el valor más
    tranquilizador y el peor por omisión.
    """
    print(" 24. una fila incompleta no se da por muerta:", end=" ")

    base = {
        "id": "T-0901",
        "estado": str(Estado.EN_EJECUCION),
        "trabajador_id": "worker-A",
        "pid": 4242,
        "iniciado_en": "2026-01-01T00:00:00+00:00",
        "ultimo_latido": nucleo.ahora_utc(),
        "generacion": 1,
        "worktree": None,
    }

    for campo in ("pid", "iniciado_en", "ultimo_latido", "trabajador_id"):
        rota = dict(base)
        rota[campo] = None

        informe = nucleo.vitalidad(rota, comprobar_proceso=lambda _p: True)

        assert informe["vitalidad"] == nucleo.VITALIDAD_LATIDO_VENCIDO, (
            "Una fila sin '" + campo + "' se informó como "
            + str(informe["vitalidad"]) + ", que afirma más de lo que se "
            "sabe: " + repr(informe["motivo"])
        )
        assert informe["requiere_atencion"] is True

    # Latido ilegible y latido en el futuro: lo mismo.
    for valor, caso in (
        ("no-es-una-fecha", "latido ilegible"),
        (
            (nucleo.ahora_datetime() + timedelta(days=365)).isoformat(
                timespec="seconds"
            ),
            "latido en el futuro",
        ),
    ):
        rara = dict(base)
        rara["ultimo_latido"] = valor

        informe = nucleo.vitalidad(rara, comprobar_proceso=lambda _p: True)

        assert informe["vitalidad"] == nucleo.VITALIDAD_LATIDO_VENCIDO, (
            caso + " se informó como " + str(informe["vitalidad"])
            + ": " + repr(informe["motivo"])
        )

    print("OK")


def prueba_z_el_tablero_publica_la_vitalidad_y_el_arbol_reales():
    """
    Lo que el tablero enseña sale del motor, no de un valor inventado.

    Ninguna prueba del repositorio miraba `vitalidad`, `verificacion_raiz`
    ni `verificacion_commit`, así que `resumen_de_tarea` podía devolver
    cualquier cosa: tres mutantes que hacían mentir al tablero sobrevivían
    a la batería completa.
    """
    print(" 25. el tablero publica la vitalidad y el árbol reales:", end=" ")

    principal, aparte, arboles = montar_tres_arboles()

    try:
        nucleo.crear(
            principal,
            "T-0906",
            titulo="Tarea del árbol B",
            ambito_archivos=["modulos/b/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_arbol.py"],
        )
        tomada = nucleo.tomar(
            principal,
            "T-0906",
            trabajador_id="worker-A",
            worktree=str(arboles["B"]),
        )
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        commit_b = _commit_de(arboles["B"])

        # Viva y trabajando.
        viva = {
            una["id"]: una for una in nucleo.tablero(principal)["tareas"]
        }["T-0906"]

        assert viva["vitalidad"] == nucleo.VITALIDAD_ACTIVA, repr(viva)
        assert viva["requiere_atencion"] is False
        assert viva["worktree"] == str(arboles["B"].resolve())
        assert viva["generacion"] == tomada.generacion

        nucleo.verificar(
            principal,
            "T-0906",
            trabajador_id="worker-A",
            generacion=tomada.generacion,
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        verificada = {
            una["id"]: una for una in nucleo.tablero(principal)["tareas"]
        }["T-0906"]

        assert verificada["verificacion_raiz"] == str(arboles["B"].resolve()), (
            "El tablero no dice dónde se verificó: "
            + repr(verificada["verificacion_raiz"])
        )
        assert verificada["verificacion_commit"] == commit_b, (
            "El commit publicado no es el del árbol que se ejecutó: "
            + repr(verificada["verificacion_commit"]) + " != " + commit_b
        )
        assert verificada["verificacion_rama"] == "rama-b", (
            repr(verificada["verificacion_rama"])
        )
        assert verificada["verificacion_sin_confirmar"] is False, (
            "Un árbol limpio aparece en el tablero con cambios sin confirmar."
        )
        assert verificada["verificacion_vigente"] is True

        # Tras reabrir y volver a tomar, ese verde es de OTRA ejecución.
        nucleo.reabrir(principal, "T-0906", "Se reabre a mano.")
        nucleo.tomar(principal, "T-0906", trabajador_id="worker-B")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        retomada = {
            una["id"]: una for una in nucleo.tablero(principal)["tareas"]
        }["T-0906"]

        assert retomada["verificacion_vigente"] is False, (
            "El tablero presenta como actual el verde de una ejecución que "
            "ya no existe."
        )

        # Y muerta: latido antiguo y proceso inexistente.
        con = estado_global.abrir(estado_global.ruta_base(principal))

        try:
            with estado_global.transaccion(con):
                con.execute(
                    "UPDATE tareas SET ultimo_latido = ?, pid = ? "
                    "WHERE id = ?",
                    ("2020-01-01T00:00:00+00:00", 999999, "T-0906"),
                )
        finally:
            con.close()

        datos = nucleo.tablero(principal)
        muerta = {una["id"]: una for una in datos["tareas"]}["T-0906"]

        assert muerta["vitalidad"] == nucleo.VITALIDAD_HUERFANA, repr(muerta)
        assert muerta["requiere_atencion"] is True
        assert datos["resumen"]["agentes_activos"] == 0, (
            "Una ejecución muerta se sigue contando como agente activo."
        )
        assert "worker-B" in datos["resumen"]["agentes_sin_senal"]

        comprobar_integridad(principal)
    finally:
        borrar(aparte)
        borrar(principal)

    print("OK")


def prueba_aa_el_tablero_no_escribe_y_aguanta_una_fila_rota():
    """
    Una lectura no crea tareas, y una fila mala no borra el tablero.

    `tablero()` pedía `BEGIN IMMEDIATE` e insertaba filas desde un GET de
    la API web. Y una sola fila con un JSON operativo malformado lo tumbaba
    entero: desaparecían TODAS las tareas y el navegador decía «sin
    conexión con el motor local», que además es falso.
    """
    print(" 26. el tablero no escribe y aguanta una fila rota:", end=" ")

    raiz = crear_repositorio("tablero_")

    try:
        # Una ficha escrita a mano, que la base no conoce.
        ficha = fichas.Ficha(
            id="T-0907",
            titulo="Escrita a mano",
            ambito_archivos=["modulos/x/*.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
        )
        fichas.guardar(raiz, ficha)

        datos = nucleo.tablero(raiz)

        assert datos["resumen"]["totales"] == 0, (
            "Una lectura incorporó la tarea a la base."
        )
        assert datos["resumen"]["sin_importar"] == ["T-0907"], (
            repr(datos["resumen"]["sin_importar"])
        )
        METRICAS["OPERACIONES"] += 1

        # Se incorpora con la orden explícita.
        estado_global.sincronizar_definiciones(raiz)

        datos = nucleo.tablero(raiz)

        assert datos["resumen"]["totales"] == 1
        assert datos["resumen"]["sin_importar"] == []

        # Y ahora se rompe su JSON operativo en la base.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                con.execute(
                    "UPDATE tareas SET ultima_verificacion = ? WHERE id = ?",
                    ('"8 de 8"', "T-0907"),
                )
        finally:
            con.close()

        datos = nucleo.tablero(raiz)

        assert datos["base_global"]["estado"] == "ACTIVA", (
            "Una fila rota se presentó como base caída."
        )
        assert len(datos["tareas"]) == 1, (
            "Una fila rota hizo desaparecer el tablero entero."
        )
        assert datos["tareas"][0]["fila_legible"] is False
        assert any(
            "T-0907" in str(error.get("motivo", ""))
            or "T-0907" in str(error.get("archivo", ""))
            for error in datos["fichas_ilegibles"]
        ), repr(datos["fichas_ilegibles"])

        # La fila está rota; el JSON, no. La tarjeta lo dice así y el aviso
        # no señala la ruta del archivo: antes mandaba a arreglar una ficha
        # que estaba bien.
        assert datos["tareas"][0]["definicion_legible"] is True, (
            "Una fila rota culpó al JSON, que está bien."
        )
        assert not any(
            str(error.get("archivo", "")).endswith(".json")
            and "T-0907" in str(error.get("archivo", ""))
            for error in datos["fichas_ilegibles"]
        ), "El aviso de la fila rota señala la ruta del JSON: " + repr(
            datos["fichas_ilegibles"]
        )

        METRICAS["OPERACIONES"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_ab_la_consola_distingue_sus_rechazos():
    """
    Los códigos de salida son estables y distinguen cada rechazo.

    Ninguna prueba del repositorio ejercía la línea de órdenes, así que el
    día que alguien moviera un `except` por debajo del genérico, el código
    se convertía en 2 en silencio y un orquestador dejaba de poder
    distinguir «tu árbol desapareció» de «el Supervisor se rompió».
    """
    print(" 27. la consola distingue sus rechazos:", end=" ")

    raiz = crear_repositorio("consola_")

    try:
        _git(raiz, "add", "-A")
        _git(raiz, "commit", "-q", "-m", "base")

        def cli(*argumentos):
            entorno = dict(os.environ)
            entorno["PYTHONPATH"] = os.pathsep.join(
                [str(RAIZ), str(RAIZ / "nucleo"), str(RAIZ / "orquestacion")]
            )
            entorno["PYTHONDONTWRITEBYTECODE"] = "1"

            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "orquestacion.ingenieria_supervisor",
                    "--raiz",
                    str(raiz),
                    "--sin-git",
                    *argumentos,
                ],
                cwd=str(RAIZ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=entorno,
                timeout=ESPERA_PROCESO_S,
            )

        esperados = []

        primera = cli(
            "crear", "T-0908",
            "--titulo", "Tarea de consola",
            "--ambito", "modulos/c/*.py",
            "--prueba", "pruebas/demostracion/prueba_verde.py",
        )
        esperados.append(("crear por primera vez", primera, 0))

        esperados.append((
            "crear dos veces",
            cli("crear", "T-0908", "--titulo", "Repetida"),
            5,
        ))

        tomada = cli("tomar", "T-0908", "--trabajador", "W1")
        esperados.append(("tomar libre", tomada, 0))

        esperados.append((
            "tomar ya tomada",
            cli("tomar", "T-0908", "--trabajador", "W2"),
            3,
        ))

        esperados.append((
            "latido con generación vieja",
            cli("latido", "T-0908", "--trabajador", "W1", "--generacion", "0"),
            4,
        ))

        esperados.append((
            "devolver con generación vigente",
            cli(
                "devolver", "T-0908", "--trabajador", "W1", "--generacion", "1"
            ),
            0,
        ))

        esperados.append((
            "tomar con un árbol ajeno",
            cli(
                "tomar", "T-0908", "--trabajador", "W3",
                "--worktree", str(raiz / "pruebas"),
            ),
            6,
        ))

        esperados.append((
            "ver una tarea inexistente",
            cli("ver", "T-9999"),
            2,
        ))

        # Una fila de la base que no se puede interpretar: avería (2) con
        # mensaje en español, no un traceback con código 1, que la ayuda
        # define como «se completó pero el resultado no es el deseado».
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                con.execute(
                    "UPDATE tareas SET decisiones = ? WHERE id = ?",
                    ('"esto no es una lista"', "T-0908"),
                )
        finally:
            con.close()

        rota = cli("ver", "T-0908")
        esperados.append(("ver una fila rota", rota, 2))

        assert "AVERÍA DEL SUPERVISOR" in rota.stdout, rota.stdout[-400:]
        assert "Traceback" not in rota.stderr, rota.stderr[-400:]

        for caso, salida, codigo in esperados:
            METRICAS["OPERACIONES"] += 1

            if salida.returncode == 0:
                METRICAS["ACEPTADAS"] += 1
            else:
                METRICAS["RECHAZADAS"] += 1

            assert salida.returncode == codigo, (
                "«" + caso + "» devolvió " + str(salida.returncode)
                + " y se esperaba " + str(codigo) + ". Salida: "
                + (salida.stdout or salida.stderr)[-400:]
            )

        # Y la ayuda documenta los códigos, en español.
        ayuda = cli("--ayuda")

        assert ayuda.returncode == 0
        for fragmento in ("Códigos de salida", "Uso:", "Opciones:"):
            assert fragmento in ayuda.stdout, (
                "La ayuda no contiene '" + fragmento + "'."
            )

        assert "positional arguments" not in ayuda.stdout, (
            "La ayuda sigue teniendo rótulos en inglés."
        )
        assert "show this help message" not in ayuda.stdout

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")



def prueba_ac_un_latido_a_tiempo_evita_la_recuperacion():
    """
    Si el dueño da señal justo antes de la escritura, no se le quita nada.

    Un latido no mueve ni el estado ni la generación, que son las dos
    únicas columnas del WHERE de la recuperación. Así que la prueba de
    vida más reciente que existe —el dueño latiendo— llegaba a la base, se
    confirmaba, y acto seguido `reanudar` la pisaba y se llevaba la tarea.
    Y `reclamadas_mientras_tanto` quedaba vacío: el sistema ni se enteraba
    de que había robado.
    """
    print(" 28. un latido a tiempo evita la recuperación:", end=" ")

    raiz = crear_repositorio("latido_a_tiempo_")

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A",
                              pid=999999)
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        # Latido viejo: dentro de la ventana que exige dos señales.
        antiguo = (
            nucleo.ahora_datetime() - timedelta(seconds=1000)
        ).isoformat(timespec="seconds")

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                con.execute(
                    "UPDATE tareas SET ultimo_latido = ? WHERE id = ?",
                    (antiguo, "T-0901"),
                )
        finally:
            con.close()

        acompanante = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion
        )

        # El dueño late EXACTAMENTE entre la clasificación y la escritura.
        def clasificando(_pid):
            acompanante.emitir_uno()

            return False

        informe = nucleo.reanudar(raiz, comprobar_proceso=clasificando)
        METRICAS["OPERACIONES"] += 1

        assert acompanante.emitidos == 1, (
            "El latido no llegó a entrar: la prueba no probaría nada."
        )

        fila = fila_de(raiz, "T-0901")

        if fila["trabajador_id"] != "worker-A":
            METRICAS["ROBOS_INDEBIDOS"] += 1

        assert fila["trabajador_id"] == "worker-A", (
            "Se le quitó la tarea a un dueño que acababa de dar señal de "
            "vida."
        )
        assert fila["estado"] == str(Estado.EN_EJECUCION)
        assert informe["huerfanas"] == [], repr(informe["huerfanas"])
        assert [uno["id"] for uno in informe["reclamadas_mientras_tanto"]] == [
            "T-0901"
        ], (
            "La recuperación no informó de que la tarea daba señales: "
            + repr(informe)
        )
        METRICAS["RECHAZADAS"] += 1

        # Y el motivo dice lo que pasó. Antes caía en «estado incompatible»
        # con un detalle que se contradecía a sí mismo («está en
        # en_ejecucion, que no admite esta orden; la admiten: en_ejecucion»)
        # y eso era lo que la consola enseñaba bajo el rótulo de «reclamada».
        motivo = informe["reclamadas_mientras_tanto"][0]["motivo"]

        assert "señal de vida" in motivo, (
            "El motivo no dice que el dueño latió: " + motivo
        )
        assert "no admite esta orden" not in motivo, (
            "El motivo se contradice a sí mismo: " + motivo
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_ad_el_latido_aguanta_un_fallo_transitorio():
    """
    Un `database is locked` no puede apagar el latido para siempre.

    Es el error más probable justo en el escenario para el que existe el
    latido: varios procesos escribiendo a la vez. Rendirse al primero
    dejaba la operación sin señal el resto del tiempo, en silencio
    —`propiedad_perdida` seguía en falso—, y a partir de ahí bastaba que la
    verificación durase para que la recuperación le quitara la tarea al
    trabajador mientras trabajaba.
    """
    print(" 29. el latido aguanta un fallo transitorio:", end=" ")

    raiz = crear_repositorio("transitorio_")

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        acompanante = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion
        )

        abrir_real = estado_global.abrir

        # El bloqueo es REAL: otra conexión tiene el candado de escritura.
        # Antes se simulaba lanzando `sqlite3.OperationalError` desde
        # `abrir`, un error que en producción nunca llega así: `abrir` y
        # `transaccion` lo envuelven en ErrorEstadoGlobal, y esa envoltura
        # caía en «no es transitoria: se para». La prueba certificaba una
        # tolerancia que el bloqueo de verdad no tenía.
        espera_real = estado_global.BUSY_TIMEOUT_MS
        estado_global.BUSY_TIMEOUT_MS = 50

        candado = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            candado.execute("BEGIN IMMEDIATE")

            METRICAS["OPERACIONES"] += 1
            sigue = acompanante.emitir_uno()

            assert sigue is True, (
                "El latido se rindió al primer bloqueo; a partir de ahí la "
                "operación se queda sin señal en silencio."
            )
            assert acompanante.propiedad_perdida is False, (
                "Un bloqueo se confundió con perder la tarea."
            )
            assert "locked" in str(acompanante.error), acompanante.error
            assert acompanante.emitidos == 0
            assert acompanante.fallos_seguidos == 1

            # Con bloqueos suficientes SÍ se rinde, que también hace falta.
            resultados = [
                acompanante.emitir_uno()
                for _ in range(nucleo.FALLOS_LATIDO_SEGUIDOS - 1)
            ]
        finally:
            candado.execute("ROLLBACK")
            candado.close()
            estado_global.BUSY_TIMEOUT_MS = espera_real

        assert resultados[-1] is False, (
            "El latido no se rinde nunca: un fallo permanente lo dejaría "
            "girando para siempre."
        )
        METRICAS["RECHAZADAS"] += 1

        # Soltado el candado, otro acompañante late con normalidad.
        acompanante = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion
        )

        assert acompanante.emitir_uno() is True
        assert acompanante.emitidos == 1
        assert acompanante.fallos_seguidos == 0
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1
        METRICAS["LATIDOS_AUTOMATICOS"] += 1

        resultados = [True]

        # Una excepción que NO es de SQLite no es transitoria: se anota y
        # se para, sin confundirla con perder la tarea. Esa rama no la
        # ejercitaba nadie.
        otro = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion
        )

        def abrir_roto(*argumentos, **extras):
            raise RuntimeError("avería que no es de SQLite")

        estado_global.abrir = abrir_roto

        try:
            METRICAS["OPERACIONES"] += 1
            sigue = otro.emitir_uno()
        finally:
            estado_global.abrir = abrir_real

        assert sigue is False, (
            "Una avería que no es transitoria dejó el latido girando."
        )
        assert otro.propiedad_perdida is False
        assert "RuntimeError" in str(otro.error), otro.error
        assert otro.fallos_totales == 1, otro.fallos_totales
        METRICAS["RECHAZADAS"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_ae_el_latido_no_puede_retroceder_la_marca_de_vida():
    """
    El reloj de pared no es monótono, y la señal de vida no puede encoger.

    `Event.wait` usa reloj monótono, pero el valor ESCRITO es hora de
    pared. Un salto hacia atrás —NTP, cambio de zona, una máquina virtual
    restaurada— hacía que el propio latido REDUJERA la antigüedad
    registrada de la señal. La recuperación lee esa marca y declara
    huérfana una ejecución viva: el componente que existe para mantenerla
    viva se convierte en el que la mata.
    """
    print(" 30. el latido no puede retroceder la marca de vida:", end=" ")

    raiz = crear_repositorio("monotonia_")

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        bueno = "2030-06-01T12:00:00+00:00"
        atrasado = "2030-06-01T10:00:00+00:00"

        adelantado = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion,
            reloj=lambda: bueno,
        )

        assert adelantado.emitir_uno() is True
        assert fila_de(raiz, "T-0901")["ultimo_latido"] == bueno
        METRICAS["LATIDOS_AUTOMATICOS"] += 1

        # Ahora el reloj salta dos horas atrás.
        retrasado = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion,
            reloj=lambda: atrasado,
        )

        METRICAS["OPERACIONES"] += 1

        # No es un error: simplemente no encoge la marca. El latido sigue.
        assert retrasado.emitir_uno() is True, (
            "Un reloj atrasado se confundió con perder la tarea."
        )
        assert retrasado.propiedad_perdida is False
        assert retrasado.retrocesos == 1, (
            "El retroceso no quedó contado: " + str(retrasado.retrocesos)
        )

        fila = fila_de(raiz, "T-0901")

        if fila["ultimo_latido"] != bueno:
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert fila["ultimo_latido"] == bueno, (
            "La marca de vida retrocedió: " + repr(fila["ultimo_latido"])
        )

        # Y si la relectura que distingue «reloj atrasado» de «perdí la
        # tarea» falla por un error transitorio, eso NO es perder la tarea.
        # Antes se marcaba `propiedad_perdida` y `verificar` tiraba la
        # batería entera con un diagnóstico falso. La primera apertura es
        # la del UPDATE (rechazado por la guarda); la segunda, la relectura.
        abrir_real = estado_global.abrir
        aperturas = []

        def abrir_fragil(*argumentos, **claves):
            aperturas.append(1)

            if len(aperturas) == 2:
                raise sqlite3.OperationalError("database is locked")

            return abrir_real(*argumentos, **claves)

        estado_global.abrir = abrir_fragil

        try:
            METRICAS["OPERACIONES"] += 1
            sigue = retrasado.emitir_uno()
        finally:
            estado_global.abrir = abrir_real

        assert len(aperturas) == 2, (
            "La relectura no llegó a ejecutarse: " + str(len(aperturas))
        )
        assert sigue is True, (
            "Un error transitorio en la relectura se confundió con perder "
            "la tarea."
        )
        assert retrasado.propiedad_perdida is False
        assert retrasado.fallos_seguidos == 1, retrasado.fallos_seguidos
        assert "locked" in str(retrasado.error), retrasado.error
        METRICAS["RECHAZADAS"] += 1

        # La orden MANUAL `latido` lleva la misma guarda: desde una máquina
        # con el reloj atrasado podía reducir la antigüedad de la señal y
        # dejar la tarea huérfana en el acto.
        from datetime import datetime, timezone

        METRICAS["OPERACIONES"] += 1

        try:
            nucleo.latido(
                raiz, "T-0901",
                ahora=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
                trabajador_id="worker-A", generacion=tomada.generacion,
            )
            METRICAS["ACEPTADAS"] += 1
            raise AssertionError(
                "Un latido manual con la hora atrasada retrocedió la marca."
            )
        except nucleo.ErrorPropiedad as rechazo:
            METRICAS["RECHAZADAS"] += 1
            assert rechazo.informe["motivo"] == (
                estado_global.MOTIVO_MARCA_MAS_NUEVA
            ), repr(rechazo.informe)

        assert fila_de(raiz, "T-0901")["ultimo_latido"] == bueno

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_af_dos_decisiones_humanas_no_se_pisan():
    """
    Resolver una decisión no puede devolver otra a «pendiente».

    `decisiones` es UNA columna con el JSON de todas dentro. Resolver una
    leyendo la lista, cambiando un elemento en Python y reescribiendo la
    columna entera es el lost update de manual, y aquí no lo frenaba nada:
    dos `decidir` sobre claves DISTINTAS son órdenes válidas —misma
    generación, mismo estado, sin propietario que exigir—, así que las dos
    pasaban el WHERE y la segunda revertía a la primera. Sin error y sin
    rastro: al humano que decidió se le devolvía su decisión como resuelta.

    Aquí las dos resoluciones van una tras otra y se exige que las dos
    sobrevivan en la base Y en el espejo JSON; el orden BEGIN < lectura <
    escritura lo fija `resolver_decision` dentro de la transacción, y el
    solapamiento real entre procesos lo mide la 37.
    """
    print(" 31. dos decisiones humanas no se pisan:", end=" ")

    raiz = crear_repositorio("decisiones_")

    try:
        ficha_minima(
            raiz,
            "T-0901",
            decisiones=[
                {"clave": "D-1", "descripcion": "primera"},
                {"clave": "D-2", "descripcion": "segunda"},
            ],
        )

        nucleo.decidir(raiz, "T-0901", "D-1", "la resuelve el jefe")
        nucleo.decidir(raiz, "T-0901", "D-2", "la resuelve el calculista")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        registradas = {
            str(una["clave"]): una
            for una in (fila_de(raiz, "T-0901")["decisiones"] or [])
        }

        for clave in ("D-1", "D-2"):
            if not registradas.get(clave, {}).get("resuelta"):
                METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

            assert registradas.get(clave, {}).get("resuelta"), (
                "La resolución de " + clave + " se perdió: "
                + repr(registradas)
            )

        assert fila_de(raiz, "T-0901")["requiere_decision_humana"] in (0, False)

        # Y el espejo JSON —lo que ve el ingeniero— dice lo mismo que la
        # base. Un espejo que no fusionara la resolución con la definición
        # dejaba el archivo diciendo «pendiente» lo que la base decía
        # «resuelta», y ninguna prueba lo miraba.
        leida = fichas.leer(raiz, "T-0901")
        en_archivo = {
            str(una["clave"]): una for una in leida.requiere_decision_humana
        }

        for clave in ("D-1", "D-2"):
            assert en_archivo.get(clave, {}).get("resuelta"), (
                "El espejo JSON dice pendiente lo que la base dice resuelta: "
                + repr(en_archivo)
            )

        assert not leida.decisiones_pendientes()

        # Y una definición que no declara una clave NO borra su resolución.
        archivo = fichas.carpeta_tareas(raiz) / "T-0901.json"
        datos = json.loads(archivo.read_text(encoding="utf-8"))
        datos["requiere_decision_humana"] = [
            {"clave": "D-2", "descripcion": "segunda"}
        ]
        archivo.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        nucleo.cargar(raiz, "T-0901")
        METRICAS["OPERACIONES"] += 1

        despues = {
            str(una["clave"]): una
            for una in (fila_de(raiz, "T-0901")["decisiones"] or [])
        }

        if not despues.get("D-1", {}).get("resuelta"):
            METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

        assert despues.get("D-1", {}).get("resuelta"), (
            "Abrir la tarea desde una definición que no declara D-1 borró "
            "su resolución de la base: " + repr(despues)
        )

        # Resolver la última pendiente retira la falla ámbar. Sin esto la
        # fila decía a la vez «resueltas» y «hay decisiones sin resolver»
        # hasta la siguiente corrida.
        ficha_minima(
            raiz, "T-0903",
            decisiones=[{"clave": "D-1", "descripcion": "frena"}],
        )
        tercera = nucleo.tomar(raiz, "T-0903", trabajador_id="worker-A")
        informe = nucleo.verificar(
            raiz, "T-0903",
            trabajador_id="worker-A", generacion=tercera.generacion,
        )
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        assert informe["estado"] == str(Estado.REQUIERE_REVISION)
        assert fila_de(raiz, "T-0903")["ultima_falla"]["tipo"] == (
            nucleo.FALLA_DECISIONES_PENDIENTES
        )

        nucleo.decidir(raiz, "T-0903", "D-1", "ya no frena")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert fila_de(raiz, "T-0903")["ultima_falla"] is None, (
            "La falla ámbar quedó rancia tras resolver la última decisión: "
            + repr(fila_de(raiz, "T-0903")["ultima_falla"])
        )

        # Y `aprobar` exige al motor lo que comprobó en Python: una
        # decisión DECLARADA entre su lectura y su escritura —cualquier
        # `cargar` de otro proceso la sincroniza— no mueve ni el estado ni
        # la generación, y la tarea quedaba APROBADA con una pendiente.
        ficha_minima(raiz, "T-0904")
        cuarta = nucleo.tomar(raiz, "T-0904", trabajador_id="worker-A")
        nucleo.verificar(
            raiz, "T-0904",
            trabajador_id="worker-A", generacion=cuarta.generacion,
        )
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        assert fila_de(raiz, "T-0904")["estado"] == str(Estado.PROPUESTO)

        persistir_real = nucleo.persistir

        def persistir_con_decision_recien_declarada(*argumentos, **claves):
            archivo = fichas.carpeta_tareas(raiz) / "T-0904.json"
            datos = json.loads(archivo.read_text(encoding="utf-8"))
            datos["requiere_decision_humana"] = [
                {"clave": "D-NUEVA", "descripcion": "declarada entre medias"}
            ]
            archivo.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            nucleo.cargar(raiz, "T-0904")

            return persistir_real(*argumentos, **claves)

        nucleo.persistir = persistir_con_decision_recien_declarada

        try:
            METRICAS["OPERACIONES"] += 1
            nucleo.aprobar(raiz, "T-0904")
            METRICAS["ACEPTADAS"] += 1
            raise AssertionError(
                "Se aprobó una tarea con una decisión declarada entre la "
                "comprobación y la escritura."
            )
        except nucleo.ErrorPropiedad as rechazo:
            METRICAS["RECHAZADAS"] += 1
            assert rechazo.informe["motivo"] == (
                estado_global.MOTIVO_PRECONDICION_CAMBIADA
            ), repr(rechazo.informe)
        finally:
            nucleo.persistir = persistir_real

        assert fila_de(raiz, "T-0904")["estado"] == str(Estado.PROPUESTO)
        assert fila_de(raiz, "T-0904")["requiere_decision_humana"] in (1, True)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_ag_el_espejo_no_borra_lo_que_escribe_una_persona():
    """
    Lo que un ingeniero escribe en la ficha sobrevive a la operación.

    El espejo sólo debe reescribir los campos OPERATIVOS. Lo declarativo se
    quedaba en memoria tal como se leyó AL EMPEZAR la orden y se volcaba
    encima del archivo al terminar. Con `verificar` esa ventana es la
    batería entera: minutos. Y lo peor: una decisión humana recién
    declarada desaparecía, así que la tarea se iba a PROPUESTO saltándose
    justo la decisión que esa persona quería forzar.
    """
    print(" 32. el espejo no borra lo que escribe una persona:", end=" ")

    raiz = crear_repositorio("espejo_")

    try:
        carpeta = raiz / "pruebas" / "demostracion"
        (carpeta / "prueba_lenta.py").write_text(
            "import time\ntime.sleep(1.2)\nprint('PRUEBA_LENTA=OK')\n",
            encoding="utf-8",
        )

        ficha_minima(
            raiz,
            "T-0901",
            pruebas_requeridas=["pruebas/demostracion/prueba_lenta.py"],
        )
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        archivo = fichas.carpeta_tareas(raiz) / "T-0901.json"
        escrito = {"hecho": False}

        def persona():
            time.sleep(0.5)
            datos = json.loads(archivo.read_text(encoding="utf-8"))
            datos["objetivo"] = "OBJETIVO ESCRITO POR UNA PERSONA"
            datos["criterios_aceptacion"] = ["criterio nuevo"]
            datos["max_intentos"] = 9
            datos["requiere_decision_humana"] = [
                {"clave": "D-NUEVA", "descripcion": "hay que decidir esto"}
            ]
            archivo.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            escrito["hecho"] = True

        hilo = threading.Thread(target=persona)
        hilo.start()

        informe = nucleo.verificar(
            raiz,
            "T-0901",
            trabajador_id="worker-A",
            generacion=tomada.generacion,
        )
        hilo.join()

        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert escrito["hecho"], "La edición no llegó a ocurrir."

        final = json.loads(archivo.read_text(encoding="utf-8"))

        for campo, esperado in (
            ("objetivo", "OBJETIVO ESCRITO POR UNA PERSONA"),
            ("criterios_aceptacion", ["criterio nuevo"]),
            ("max_intentos", 9),
        ):
            if final[campo] != esperado:
                METRICAS["ACTUALIZACIONES_PERDIDAS"] += 1

            assert final[campo] == esperado, (
                "El espejo borró '" + campo + "': " + repr(final[campo])
            )

        claves = [
            str(una["clave"]) for una in final["requiere_decision_humana"]
        ]

        assert "D-NUEVA" in claves, (
            "El espejo borró la decisión que una persona acababa de "
            "declarar: " + repr(claves)
        )

        # Y esa decisión frena la propuesta, que es para lo que se declara.
        assert informe["estado"] == str(Estado.REQUIERE_REVISION), (
            "Las pruebas iban en verde y la tarea se propuso saltándose la "
            "decisión humana declarada durante la corrida: "
            + informe["estado"]
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# Corredor de este archivo
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# GRUPO 9 — Revisión final: lo que el README prometía y el código no hacía
# ----------------------------------------------------------------------

def prueba_ah_el_latido_toma_el_instante_con_el_candado_pedido():
    """
    El instante que se graba se lee DESPUÉS de pedir el candado, no antes.

    Bajo contención, `BEGIN IMMEDIATE` puede esperar hasta `busy_timeout`
    (cinco segundos). Tomar la hora antes de esa espera y grabarla después
    equivale a registrar una señal de vida ya rancia: la marca dice «vivo
    hace cinco segundos» en el instante mismo en que se confirma. El
    README y el comentario del código lo prometían; el código leía el
    reloj fuera de la transacción. Aquí se lee el orden REAL de lo que
    ocurre, con el trazador de SQLite: BEGIN IMMEDIATE, luego el reloj,
    luego el UPDATE.
    """
    print(" 33. el latido toma el instante con el candado ya pedido:",
          end=" ")

    raiz = crear_repositorio("instante_")

    try:
        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        sucesos = []
        conectar = sqlite3.connect

        def conectar_vigilado(*argumentos, **claves):
            con_nueva = conectar(*argumentos, **claves)
            con_nueva.set_trace_callback(
                lambda sentencia: sucesos.append(("sql", str(sentencia)))
            )

            return con_nueva

        def reloj():
            sucesos.append(("reloj", ""))

            return nucleo.ahora_utc()

        acompanante = nucleo.LatidoAutomatico(
            raiz, "T-0901", "worker-A", tomada.generacion, reloj=reloj
        )

        sqlite3.connect = conectar_vigilado

        try:
            METRICAS["OPERACIONES"] += 1
            assert acompanante.emitir_uno() is True
        finally:
            sqlite3.connect = conectar

        METRICAS["ACEPTADAS"] += 1
        METRICAS["LATIDOS_AUTOMATICOS"] += 1

        def exigir_orden(quien: str, marca_de_escritura: str) -> None:
            """BEGIN IMMEDIATE, después el reloj, después el UPDATE."""
            escritura = next(
                (
                    numero for numero, (clase, texto) in enumerate(sucesos)
                    if clase == "sql"
                    and texto.strip().upper().startswith("UPDATE TAREAS")
                    and marca_de_escritura in texto
                ),
                None,
            )
            assert escritura is not None, quien + " no escribió."

            candado = max(
                (
                    numero for numero, (clase, texto) in enumerate(sucesos)
                    if numero < escritura and clase == "sql"
                    and texto.strip().upper().startswith("BEGIN IMMEDIATE")
                ),
                default=None,
            )
            assert candado is not None, quien + " no abrió ninguna transacción."

            instante = next(
                (
                    numero for numero, (clase, _texto) in enumerate(sucesos)
                    if clase == "reloj"
                ),
                None,
            )
            assert instante is not None, quien + " no consultó el reloj."

            assert candado < instante < escritura, (
                "El instante de " + quien + " tiene que leerse con el candado "
                "ya pedido: BEGIN=" + str(candado) + " reloj="
                + str(instante) + " UPDATE=" + str(escritura)
            )

        exigir_orden("el latido", "ultimo_latido")

        # Lo mismo para la toma: su `momento` es el primer latido de la
        # ejecución, y también se tomaba antes de pedir el candado.
        ficha_minima(raiz, "T-0902")
        sucesos.clear()
        reloj_real = nucleo.ahora_datetime

        def reloj_de_toma():
            sucesos.append(("reloj", ""))

            return reloj_real()

        nucleo.ahora_datetime = reloj_de_toma
        sqlite3.connect = conectar_vigilado

        try:
            METRICAS["OPERACIONES"] += 1
            nucleo.tomar(raiz, "T-0902", trabajador_id="worker-A")
        finally:
            sqlite3.connect = conectar
            nucleo.ahora_datetime = reloj_real

        METRICAS["ACEPTADAS"] += 1

        exigir_orden("la toma", "generacion = generacion + 1")

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def _demostrar_muertas(raiz: Path) -> None:
    """Latido antiguo en todas las filas: con el proceso ausente, HUÉRFANA."""
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        with estado_global.transaccion(con):
            con.execute(
                "UPDATE tareas SET ultimo_latido = ?",
                ("2020-01-01T00:00:00+00:00",),
            )
    finally:
        con.close()


def prueba_ai_un_espejo_que_falla_no_tumba_la_recuperacion():
    """
    Si el espejo JSON de una tarea no se puede escribir, la recuperación
    lo anota y sigue con las demás.

    El README lo prometía (`espejo_no_regenerado`) y el código no lo
    cumplía: el `except` estaba alrededor de `_registrar_en_git`, que ni
    regenera el espejo ni lanza `ErrorSupervisor`. El espejo se regenera
    dentro de `persistir`, cuyo error subía sin capturar y abortaba la
    pasada entera: la primera tarea quedaba REABIERTA en la base, con el
    JSON diciendo EN_EJECUCION y sin figurar en el informe, y todas las
    que venían detrás, sin revisar. Reproducido antes de arreglarlo.
    """
    print(" 34. un espejo que falla no tumba la recuperación:", end=" ")

    raiz = crear_repositorio("espejo_falla_")

    try:
        for identificador in ("T-0901", "T-0902"):
            ficha_minima(raiz, identificador)
            nucleo.tomar(
                raiz, identificador,
                trabajador_id="worker-" + identificador, pid=999999,
            )
            METRICAS["OPERACIONES"] += 1
            METRICAS["ACEPTADAS"] += 1

        _demostrar_muertas(raiz)

        original = nucleo.guardar

        def guardar_roto(raiz_, ficha, **claves):
            if ficha.id == "T-0901":
                raise OSError("disco lleno (simulado)")

            return original(raiz_, ficha, **claves)

        nucleo.guardar = guardar_roto

        try:
            METRICAS["OPERACIONES"] += 1
            informe = nucleo.reanudar(
                raiz, comprobar_proceso=lambda _pid: False
            )
        finally:
            nucleo.guardar = original

        METRICAS["ACEPTADAS"] += 1

        assert sorted(uno["id"] for uno in informe["huerfanas"]) == [
            "T-0901", "T-0902"
        ], (
            "La recuperación no llegó a todas las tareas: "
            + repr(informe["huerfanas"])
        )
        assert [uno["id"] for uno in informe["espejo_no_regenerado"]] == [
            "T-0901"
        ], (
            "El fallo del espejo no quedó anotado: "
            + repr(informe["espejo_no_regenerado"])
        )
        assert "espejo" in informe["espejo_no_regenerado"][0]["motivo"]

        for identificador in ("T-0901", "T-0902"):
            assert fila_de(raiz, identificador)["estado"] == str(
                Estado.REABIERTO
            ), identificador + " no quedó recuperada en la base."

        # El espejo que sí se pudo escribir refleja la base; el otro quedó
        # viejo, y lo regenera la siguiente orden que confirme.
        assert fichas.leer(raiz, "T-0902").estado == Estado.REABIERTO
        assert fichas.leer(raiz, "T-0901").estado == Estado.EN_EJECUCION

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-C")
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        regenerada = fichas.leer(raiz, "T-0901")

        assert regenerada.estado == Estado.EN_EJECUCION
        assert regenerada.trabajador_id == "worker-C", (
            "La siguiente orden no regeneró el espejo: "
            + repr(regenerada.trabajador_id)
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_aj_la_consola_muestra_lo_que_reanudar_informa():
    """
    Lo que el motor informa llega a la persona que ejecuta `reanudar`.

    `reanudar` devolvía `inconsistentes_sin_tocar` y `espejo_no_regenerado`
    y la consola no los imprimía. El README decía que la salida para una
    fila incompleta es `reabrir`, una orden humana; pero el operador de
    consola nunca se enteraba de que existiera esa fila, así que la orden
    humana no llegaba a darse. Y «Inconsistentes recuperadas» salía siempre
    0, porque desde A3.3 ese camino no existe.

    Se ejecuta la consola real en este proceso (`principal`), con la
    salida capturada, para poder provocar el fallo del espejo.
    """
    print(" 35. la consola muestra lo que reanudar informa:", end=" ")

    import contextlib
    import io

    from ingenieria_supervisor import __main__ as consola

    raiz = crear_repositorio("consola_reanudar_")

    try:
        # T-0909: fila en ejecución sin identidad, propia de una escritura
        # interrumpida a medias. Se inyecta en la base, que es la autoridad.
        ficha_minima(raiz, "T-0909")

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                estado_global.actualizar_tarea(
                    con, "T-0909", {"estado": str(Estado.EN_EJECUCION)}
                )
        finally:
            con.close()

        # T-0910: huérfana de verdad, y con el espejo roto.
        ficha_minima(raiz, "T-0910")
        nucleo.tomar(raiz, "T-0910", trabajador_id="worker-A", pid=999999)
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        # T-0911: de OTRA máquina y con el latido vencido. No se libera sola
        # y la consola tiene que decir que hay que mirarla a mano.
        ficha_minima(raiz, "T-0911")
        nucleo.tomar(
            raiz, "T-0911", trabajador_id="otro-equipo/4321/abcd1234",
            pid=999999,
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        _demostrar_muertas(raiz)

        original = nucleo.guardar

        def guardar_roto(raiz_, ficha, **claves):
            if ficha.id == "T-0910":
                raise OSError("disco lleno (simulado)")

            return original(raiz_, ficha, **claves)

        # Con el PID 999999 muerto y el latido de 2020, no hace falta
        # inyectar `comprobar_proceso`: la consola usa el real.
        assert not nucleo.proceso_vivo(999999), (
            "El PID 999999 existe en esta máquina; la comprobación no puede "
            "distinguir un proceso muerto."
        )

        salida = io.StringIO()
        nucleo.guardar = guardar_roto

        try:
            METRICAS["OPERACIONES"] += 1

            with contextlib.redirect_stdout(salida):
                codigo = consola.principal(
                    ["--raiz", str(raiz), "--sin-git", "reanudar"]
                )
        finally:
            nucleo.guardar = original

        METRICAS["ACEPTADAS"] += 1

        texto = salida.getvalue()

        # Deja tareas que una persona debe mirar: código 1, no 0. Un guion
        # de arranque no podía distinguir «todo recuperado» de «hay algo
        # que mirar» sin leer el texto.
        assert codigo == 1, "reanudar devolvió " + str(codigo) + ":\n" + texto

        for fragmento in (
            "INCONSISTENTES (no recuperadas",
            "T-0909",
            "reabrir",
            "SIN ESPEJO JSON",
            "T-0910",
            "LATIDO VENCIDO",
            "T-0911",
        ):
            assert fragmento in texto, (
                "La consola no muestra '" + fragmento + "':\n" + texto
            )

        assert "INCONSISTENTES RECUPERADAS" not in texto, (
            "La consola sigue anunciando un camino que no existe."
        )

        resumen = [
            linea for linea in texto.splitlines()
            if "Inconsistentes (sin tocar)" in linea
        ]

        assert resumen and resumen[0].rstrip().endswith(" 1"), (
            "El resumen no cuenta la fila inconsistente: " + repr(resumen)
        )

        vencidas = [
            linea for linea in texto.splitlines()
            if "Latido vencido (sin tocar)" in linea
        ]

        assert vencidas and vencidas[0].rstrip().endswith(" 1"), (
            "El resumen no cuenta la ejecución con latido vencido: "
            + repr(vencidas)
        )
        assert fila_de(raiz, "T-0911")["estado"] == str(Estado.EN_EJECUCION)

        # Y lo que dice es verdad: la inconsistente no se tocó y la huérfana
        # sí se recuperó en la base.
        assert fila_de(raiz, "T-0909")["estado"] == str(Estado.EN_EJECUCION)
        assert fila_de(raiz, "T-0910")["estado"] == str(Estado.REABIERTO)

        # Una persona resuelve lo pendiente y la siguiente reanudación no
        # deja nada que mirar: entonces sí, 0.
        nucleo.reabrir(raiz, "T-0909", "Fila incompleta revisada a mano.")
        nucleo.reabrir(raiz, "T-0911", "Trabajador remoto dado por perdido.")
        METRICAS["OPERACIONES"] += 2
        METRICAS["ACEPTADAS"] += 2

        limpia = io.StringIO()

        with contextlib.redirect_stdout(limpia):
            codigo = consola.principal(
                ["--raiz", str(raiz), "--sin-git", "reanudar"]
            )

        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1

        assert codigo == 0, (
            "reanudar sin nada pendiente devolvió " + str(codigo) + ":\n"
            + limpia.getvalue()
        )
        assert "INCONSISTENTES" not in limpia.getvalue()
        assert "LATIDO VENCIDO" not in limpia.getvalue()

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


COMPROBACIONES = (
    prueba_a_dos_ordenes_validas_no_se_pisan,
    prueba_b_una_orden_no_escribe_columnas_ajenas,
    prueba_c_los_intentos_los_cuenta_el_motor,
    prueba_d_verificar_ejecuta_en_el_worktree_de_la_tarea,
    prueba_e_una_ruta_ajena_no_se_ejecuta,
    prueba_f_el_worktree_se_valida_al_tomar,
    prueba_h_el_latido_automatico_mantiene_viva_la_ejecucion,
    prueba_i_el_latido_se_para_al_perder_la_propiedad,
    prueba_i2_el_latido_viejo_no_vale_para_la_ejecucion_nueva,
    prueba_j_el_latido_no_resucita_una_ejecucion_terminada,
    prueba_k_el_acompanante_se_para_aunque_el_trabajo_falle,
    prueba_l_verificar_late_mientras_corre,
    prueba_m_un_latido_vencido_no_basta_para_declarar_abandono,
    prueba_n_la_vitalidad_distingue_los_cinco_estados,
    prueba_o_reanudar_es_idempotente,
    prueba_p_tras_recuperar_b_toma_y_la_orden_tardia_de_a_cae,
    prueba_s_el_worktree_que_desaparece_se_avisa_y_frena_la_retoma,
    prueba_q_rutas_raras_pero_legitimas_se_aceptan,
    prueba_t_verificar_rechaza_un_arbol_invalido,
    prueba_u_un_arbol_que_se_mueve_invalida_la_corrida,
    prueba_v_verificar_abandona_si_pierde_la_propiedad,
    prueba_w_cada_precondicion_del_latido_frena_por_separado,
    prueba_x_un_trabajador_de_otra_maquina_no_se_juzga_por_el_pid_local,
    prueba_y_una_fila_incompleta_no_se_da_por_muerta,
    prueba_z_el_tablero_publica_la_vitalidad_y_el_arbol_reales,
    prueba_aa_el_tablero_no_escribe_y_aguanta_una_fila_rota,
    prueba_ab_la_consola_distingue_sus_rechazos,
    prueba_ac_un_latido_a_tiempo_evita_la_recuperacion,
    prueba_ad_el_latido_aguanta_un_fallo_transitorio,
    prueba_ae_el_latido_no_puede_retroceder_la_marca_de_vida,
    prueba_af_dos_decisiones_humanas_no_se_pisan,
    prueba_ag_el_espejo_no_borra_lo_que_escribe_una_persona,
    prueba_ah_el_latido_toma_el_instante_con_el_candado_pedido,
    prueba_ai_un_espejo_que_falla_no_tumba_la_recuperacion,
    prueba_aj_la_consola_muestra_lo_que_reanudar_informa,
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

    prueba_g_crear_concurrente_tiene_un_solo_ganador(CREADORES_POR_OMISION)
    prueba_r_estres_de_escrituras_concurrentes(
        ESCRITORES_POR_OMISION, rondas
    )

    duracion = time.monotonic() - inicio

    imprimir_metricas()

    if OMITIDAS:
        print("")
        print("  Casos OMITIDOS en esta corrida (el entorno no los admite)")
        print("  --------------------------------------------------------")
        for omitida in OMITIDAS:
            print("      · " + omitida)

    # El veredicto no se declara: se comprueba contra lo medido.
    #
    # Los contadores POSITIVOS llevan cota mínima. Sin ella, el bloque de
    # métricas era decorativo: los contadores de fallo se incrementan justo
    # antes de un assert que ya aborta la corrida, así que ninguno puede
    # llegar vivo hasta aquí y las seis comprobaciones eran inalcanzables.
    # Una corrida que no hubiera hecho nada habría salido igual de verde.
    minimos = {
        "OPERACIONES": 90,
        "ACEPTADAS": 55,
        "RECHAZADAS": 30,
        "LATIDOS_AUTOMATICOS": 20,
        "COMPROBACIONES_INTEGRIDAD": 20,
    }

    for clave, minimo in minimos.items():
        assert METRICAS[clave] >= minimo, (
            "La corrida hizo menos trabajo del que esta batería debería "
            "hacer: " + clave + " = " + str(METRICAS[clave]) + ", se "
            "esperaban al menos " + str(minimo) + ". O falta una "
            "comprobación, o alguna se está saltando en silencio."
        )

    for clave in (
        "ACTUALIZACIONES_PERDIDAS",
        "ROBOS_INDEBIDOS",
        "VERIFICACIONES_EN_ARBOL_INCORRECTO",
        "ERRORES_SQLITE",
        "EXCEPCIONES",
        "FALLOS_INTEGRIDAD",
    ):
        assert METRICAS[clave] == 0, (
            clave + " = " + str(METRICAS[clave]) + ", y tiene que ser 0."
        )

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
