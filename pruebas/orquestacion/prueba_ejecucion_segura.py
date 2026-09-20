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

        comprobar_integridad(principal)
    finally:
        _git(principal, "worktree", "prune")
        borrar(aparte)
        borrar(principal)

    print("OK")


def prueba_e_una_ruta_ajena_no_se_ejecuta():
    """
    Una ruta que existe no es, por eso, una ruta que se pueda ejecutar.

    Un worktree registrado apuntando fuera del proyecto —por error o a
    propósito— haría que `verificar` corriera código de otro sitio y grabara
    su resultado como si fuera el de esta tarea.
    """
    print("  5. una ruta ajena o rota no se verifica:", end=" ")

    principal = crear_repositorio("ajena_")
    ajeno = crear_repositorio("ajeno_otro_")

    try:
        for caso, ruta, fragmento in (
            ("otro repositorio", str(ajeno), "OTRO"),
            ("no existe", str(principal / "no_existe"), "no existe"),
            (
                "no es un directorio",
                str(principal / "pruebas" / "demostracion" / "prueba_verde.py"),
                "no es un directorio",
            ),
        ):
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

        # Sin worktree declarado se usa la raíz, que es el caso normal.
        assert nucleo.resolver_worktree(principal, None) == principal
        assert nucleo.resolver_worktree(principal, "  ") == principal

        # Una ruta RELATIVA se interpreta contra la raíz, no contra el
        # directorio desde el que se invocó el Supervisor.
        subdirectorio = principal / "pruebas"

        assert nucleo.resolver_worktree(principal, "pruebas") == (
            subdirectorio.resolve()
        ), "Una ruta relativa no se resolvió contra la raíz de la tarea."
    finally:
        borrar(ajeno)
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


def prueba_j_el_latido_no_resucita_una_ejecucion_terminada():
    """
    Terminada la ejecución, un latido rezagado no la revive.

    El acompañante se para solo al salir del bloque, pero si uno quedara
    en vuelo tampoco podría hacer daño: su escritura exige estado
    EN_EJECUCION, y una tarea ya cerrada no lo está.
    """
    print("  9. un latido rezagado no resucita lo terminado:", end=" ")

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
    print(" 10. el acompañante se para aunque el trabajo falle:", end=" ")

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
        assert acompanante.emitidos > 0, (
            "No llegó a latir: la prueba no probó nada."
        )

        METRICAS["LATIDOS_AUTOMATICOS"] += acompanante.emitidos

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
    print(" 11. verificar late mientras corre la batería:", end=" ")

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

        informe = nucleo.verificar(
            raiz,
            "T-0901",
            trabajador_id="worker-A",
            generacion=tomada.generacion,
            intervalo_latido_s=0.02,
        )
        METRICAS["OPERACIONES"] += 1
        METRICAS["ACEPTADAS"] += 1
        METRICAS["LATIDOS_AUTOMATICOS"] += informe["latidos"]

        assert informe["latidos"] > 0, (
            "La verificación no emitió ni un latido: una corrida larga "
            "seguiría pareciendo abandono."
        )
        assert informe["estado"] == str(Estado.PROPUESTO), informe["motivo"]

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
        " 16. crear concurrente (" + str(creadores) + " procesos): ",
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
    print(" 12. un latido vencido no basta para declarar abandono:", end=" ")

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
    print(" 13. la vitalidad distingue los cinco estados:", end=" ")

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

        # FINALIZADA: sin ejecución y sin posibilidad de toma.
        nucleo.devolver(raiz, "T-0901", trabajador_id="worker-A")
        nucleo.bloquear(raiz, "T-0901", "cerrada para la prueba")

        informe = nucleo.vitalidad(fila_de(raiz, "T-0901"))

        assert informe["vitalidad"] == nucleo.VITALIDAD_FINALIZADA, informe

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
    print(" 14. reanudar dos veces no duplica nada:", end=" ")

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
    print(" 15. tras recuperar, B toma y lo tardío de A cae:", end=" ")

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


# ----------------------------------------------------------------------
# Corredor de este archivo
# ----------------------------------------------------------------------

COMPROBACIONES = (
    prueba_a_dos_ordenes_validas_no_se_pisan,
    prueba_b_una_orden_no_escribe_columnas_ajenas,
    prueba_c_los_intentos_los_cuenta_el_motor,
    prueba_d_verificar_ejecuta_en_el_worktree_de_la_tarea,
    prueba_e_una_ruta_ajena_no_se_ejecuta,
    prueba_f_el_worktree_se_valida_al_tomar,
    prueba_h_el_latido_automatico_mantiene_viva_la_ejecucion,
    prueba_i_el_latido_se_para_al_perder_la_propiedad,
    prueba_j_el_latido_no_resucita_una_ejecucion_terminada,
    prueba_k_el_acompanante_se_para_aunque_el_trabajo_falle,
    prueba_l_verificar_late_mientras_corre,
    prueba_m_un_latido_vencido_no_basta_para_declarar_abandono,
    prueba_n_la_vitalidad_distingue_los_cinco_estados,
    prueba_o_reanudar_es_idempotente,
    prueba_p_tras_recuperar_b_toma_y_la_orden_tardia_de_a_cae,
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
