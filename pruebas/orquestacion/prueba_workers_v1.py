"""
Pruebas de T-0003: TRABAJADORES V1 (cola persistente, despacho, worktrees
automáticos, proceso trabajador, limpieza y recuperación).

Lo que se demuestra, con procesos y árboles reales:

 1. la cola es persistente y su orden es determinista (prioridad, llegada),
    también leído desde otro proceso;
 2. dos procesos despachando la misma tarea -> un solo ganador;
 3. dos tareas con ámbitos solapados -> sólo una con escritor;
 4. dos tareas sin solapamiento -> ambas progresan, y ningún trabajador
    escribe fuera de su ámbito;
 5. un doble lanzamiento accidental -> rechazado (el despacho y el proceso);
 6. un trabajador que termina bien -> estado consistente y trazable;
 7. un trabajador que falla -> recuperación consistente (código, lanzamiento
    imposible, tiempo agotado, escritura fuera del ámbito);
 8. un trabajador que parece muerto pero hay duda -> NO se libera;
 9. un worktree registrado fuera de la zona controlada -> rechazado;
10. borrar un worktree con cambios ajenos -> rechazado;
11. apagón y reinicio simulados -> cola y propiedad recuperables;
12. los argumentos del trabajador viajan como lista, nunca `shell=True`;
13. la consola expone las órdenes con códigos de salida propios;
14. estrés: varios procesos despachando a la vez, varias rondas.

Todo es hermético: repositorios Git temporales con su propia base SQLite.
Jamás se toca el repositorio real ni su base. Los trabajadores son
procesos reales lanzados por el despacho, con el Supervisor de ESTE árbol.

Ejecución por omisión: pensada para terminar muy por debajo del tiempo
límite del corredor único (120 s). Para más rondas de estrés:

    python pruebas/orquestacion/prueba_workers_v1.py --rondas 10
"""

import argparse
import json
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(RAIZ / "orquestacion"))
sys.path.insert(0, str(RAIZ / "nucleo"))


from ingenieria_nucleo.estados import Estado

from ingenieria_supervisor import estado_global
from ingenieria_supervisor import supervisor as nucleo
from ingenieria_supervisor import tarea as fichas
from ingenieria_supervisor import trabajadores


PRUEBA_VERDE = "print('PRUEBA_VERDE=OK')\n"

# El trabajo de demostración: escribe el archivo de la tarea dentro de su
# ámbito, opcionalmente uno FUERA, opcionalmente falla o duerme, y confirma.
TRABAJO_DEMO = '''\
import argparse, json, pathlib, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument("tarea")
p.add_argument("--fuera")
p.add_argument("--fallar", type=int, default=0)
p.add_argument("--dormir", type=float, default=0.0)
p.add_argument("--sin-commit", action="store_true")
p.add_argument("extras", nargs="*")
a = p.parse_intermixed_args()
if a.dormir:
    time.sleep(a.dormir)
d = pathlib.Path("modulos/demostracion")
d.mkdir(parents=True, exist_ok=True)
(d / (a.tarea + ".py")).write_text(
    "# " + a.tarea + "\\nARGV = " + json.dumps(a.extras) + "\\n", encoding="utf-8"
)
if a.fuera:
    f = pathlib.Path(a.fuera)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("escrito fuera del ambito\\n", encoding="utf-8")
if a.fallar:
    sys.exit(a.fallar)
if not a.sin_commit:
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=W", "-c", "user.email=w@x", "commit", "-q",
         "-m", "trabajo " + a.tarea],
        check=True,
    )
print("trabajo hecho")
'''

# Eco: deja en el ámbito exactamente el argv que recibió.
ECO = '''\
import json, pathlib, sys
d = pathlib.Path("modulos/demostracion")
d.mkdir(parents=True, exist_ok=True)
(d / "eco.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
'''

RONDAS_POR_OMISION = 3
DESPACHADORES_POR_OMISION = 6

ESPERA_BARRERA_S = 20
ESPERA_PROCESO_S = 60
ESPERA_TRABAJADOR_S = 60
ESPERA_GIT_S = 30

# Latido rápido para que los trabajadores de prueba dejen señal aunque duren
# décimas de segundo.
INTERVALO_LATIDO_S = 0.2

OMITIDAS: list[str] = []

METRICAS = {
    "TAREAS_ENCOLADAS": 0,
    "DESPACHOS_ACEPTADOS": 0,
    "DESPACHOS_RECHAZADOS": 0,
    "DOBLES_DESPACHOS": 0,
    "CONFLICTOS_DE_AMBITO_DETECTADOS": 0,
    "WORKTREES_CREADOS": 0,
    "WORKTREES_LIMPIADOS": 0,
    "WORKTREES_RECHAZADOS": 0,
    "RECUPERACIONES": 0,
    "CASOS_DUDOSOS_ESCALADOS": 0,
    "ERRORES_SQLITE": 0,
    "EXCEPCIONES_INESPERADAS": 0,
    "FALLOS_INTEGRIDAD": 0,
    "COMPROBACIONES_INTEGRIDAD": 0,
}


# ----------------------------------------------------------------------
# Utilidades
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


def crear_repositorio(prefijo="trabajadores_") -> Path:
    """Repositorio Git temporal, con su base SQLite, su prueba verde y el
    trabajo de demostración ya confirmados."""
    raiz = Path(tempfile.mkdtemp(prefix=prefijo))

    inicio = _git(raiz, "init", "-q", "-b", "main")
    assert inicio.returncode == 0, inicio.stderr

    _git(raiz, "config", "user.name", "Prueba T-0003")
    _git(raiz, "config", "user.email", "prueba@ingenieria.local")
    _git(raiz, "config", "commit.gpgsign", "false")

    carpeta = raiz / "pruebas" / "demostracion"
    carpeta.mkdir(parents=True)
    (carpeta / "prueba_verde.py").write_text(PRUEBA_VERDE, encoding="utf-8")

    herramientas = raiz / "herramientas"
    herramientas.mkdir()
    (herramientas / "trabajo.py").write_text(TRABAJO_DEMO, encoding="utf-8")
    (herramientas / "eco.py").write_text(ECO, encoding="utf-8")

    fichas.carpeta_tareas(raiz).mkdir(parents=True)

    _git(raiz, "add", "-A")
    confirmado = _git(raiz, "commit", "-q", "-m", "base")
    assert confirmado.returncode == 0, confirmado.stderr

    return raiz


def _quitar_solo_lectura(funcion, ruta, _excepcion):
    os.chmod(ruta, 0o700)
    funcion(ruta)


def borrar(raiz: Path) -> None:
    """Borra el temporal sin lanzar NUNCA (se llama desde `finally`)."""
    import shutil

    clave = "onexc" if sys.version_info >= (3, 12) else "onerror"

    for intento in range(3):
        try:
            shutil.rmtree(raiz, ignore_errors=False, **{clave: _quitar_solo_lectura})
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


def ficha_minima(raiz: Path, identificador, ambito=None, **extras):
    parametros = {
        "titulo": "Tarea " + identificador,
        "objetivo": "Comprobar los trabajadores.",
        "criterios_aceptacion": ["La prueba verde pasa."],
        "ambito_archivos": ambito or ["modulos/demostracion/" + identificador + ".py"],
        "pruebas_requeridas": ["pruebas/demostracion/prueba_verde.py"],
    }
    parametros.update(extras)

    return nucleo.crear(raiz, identificador, **parametros)


def fila_de(raiz: Path, identificador: str) -> dict:
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        return estado_global.obtener_tarea(con, identificador)
    finally:
        con.close()


def entradas_de(raiz: Path, identificador: str) -> list[dict]:
    """Todas las entradas de la tarea, la más reciente primero."""
    return sorted(
        (una for una in trabajadores.listar_cola(raiz)
         if una["tarea_id"] == identificador),
        key=lambda una: -una["secuencia"],
    )


def entrada_de(raiz: Path, identificador: str) -> dict:
    lista = entradas_de(raiz, identificador)
    assert lista, "La tarea " + identificador + " no tiene entradas."
    return lista[0]


def eventos_de(raiz: Path, identificador: str) -> list[dict]:
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        return estado_global.listar_eventos(con, identificador)
    finally:
        con.close()


def escribir_sql(raiz: Path, sentencia: str, parametros=()) -> None:
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        with estado_global.transaccion(con):
            con.execute(sentencia, parametros)
    finally:
        con.close()


def comprobar_integridad(raiz: Path) -> str:
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        veredicto = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        claves = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()

    METRICAS["COMPROBACIONES_INTEGRIDAD"] += 1

    if veredicto.lower() != "ok" or claves:
        METRICAS["FALLOS_INTEGRIDAD"] += 1

    assert not claves, "Claves foráneas rotas: " + repr(claves)
    assert veredicto.lower() == "ok", "integrity_check: " + veredicto

    return veredicto


def zona(raiz: Path) -> Path:
    return trabajadores.zona_de_arboles(raiz)


def arboles_en_zona(raiz: Path) -> list[str]:
    if not zona(raiz).is_dir():
        return []

    return sorted(
        ruta.name for ruta in zona(raiz).iterdir()
        if ruta.is_dir() and ruta.name != trabajadores.CARPETA_REGISTROS
    )


def trabajo_demo(identificador: str, *extras: str) -> list[str]:
    return [sys.executable, "herramientas/trabajo.py", identificador, *extras]


def despachar_y_esperar(raiz: Path, identificador=None, espera=ESPERA_TRABAJADOR_S):
    """Despacha (con trabajador real) y espera a que el proceso termine."""
    informe = trabajadores.despachar(
        raiz, identificador, intervalo_latido_s=INTERVALO_LATIDO_S
    )

    METRICAS["DESPACHOS_ACEPTADOS"] += 1

    if informe["arbol_creado"]:
        METRICAS["WORKTREES_CREADOS"] += 1

    proceso = informe["proceso"]
    assert proceso is not None, "El despacho no lanzó ningún proceso."

    try:
        codigo = proceso.wait(timeout=espera)
    except subprocess.TimeoutExpired:
        matar(proceso)
        raise AssertionError(
            "El trabajador de " + informe["tarea"] + " no terminó en "
            + str(espera) + " s."
        )

    registro = Path(informe["registro"]).read_text(encoding="utf-8", errors="replace")

    return informe, codigo, registro


def informe_del_registro(registro: str) -> dict:
    for linea in registro.splitlines():
        if linea.startswith("INFORME_JSON="):
            return json.loads(linea[len("INFORME_JSON="):])

    raise AssertionError("El registro del trabajador no trae INFORME_JSON:\n" + registro[-800:])


def matar(proceso) -> None:
    """Mata al trabajador Y a su trabajo (misma sesión en POSIX)."""
    if proceso.poll() is not None:
        return

    try:
        if os.name != "nt":
            os.killpg(proceso.pid, signal.SIGKILL)
        else:
            proceso.kill()
    except OSError:
        proceso.kill()

    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def esperar_a(condicion, espera_s=20.0, paso_s=0.05, descripcion="condición"):
    limite = time.monotonic() + espera_s

    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(paso_s)

    raise AssertionError("No se cumplió a tiempo: " + descripcion)


def cli(raiz: Path, *argumentos: str) -> subprocess.CompletedProcess:
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = os.pathsep.join(
        [str(RAIZ), str(RAIZ / "nucleo"), str(RAIZ / "orquestacion")]
    )
    entorno["PYTHONDONTWRITEBYTECODE"] = "1"

    return subprocess.run(
        [
            sys.executable, "-m", "orquestacion.ingenieria_supervisor",
            "--raiz", str(raiz), "--sin-git", *argumentos,
        ],
        cwd=str(RAIZ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=entorno,
        timeout=ESPERA_PROCESO_S,
    )


def contar_rechazo(error) -> None:
    METRICAS["DESPACHOS_RECHAZADOS"] += 1

    if getattr(error, "motivo", None) == trabajadores.RECHAZO_AMBITO:
        METRICAS["CONFLICTOS_DE_AMBITO_DETECTADOS"] += 1


# ----------------------------------------------------------------------
# 1. Cola persistente y orden determinista
# ----------------------------------------------------------------------

def prueba_01_cola_persistente_y_orden_determinista():
    print("  1. la cola persiste y su orden es determinista:", end=" ")

    raiz = crear_repositorio("cola_")

    try:
        prioridades = {
            "T-0901": 0, "T-0902": 5, "T-0903": 0, "T-0904": 5, "T-0905": -1,
        }

        for identificador, prioridad in prioridades.items():
            ficha_minima(raiz, identificador)
            entrada = trabajadores.encolar(raiz, identificador, prioridad=prioridad)
            METRICAS["TAREAS_ENCOLADAS"] += 1
            assert entrada["estado_cola"] == estado_global.COLA_PENDIENTE
            assert entrada["trabajo"] == []

        esperado = ["T-0902", "T-0904", "T-0901", "T-0903", "T-0905"]

        orden = [una["tarea_id"] for una in trabajadores.listar_cola(raiz)]
        assert orden == esperado, orden

        # Todas despachables ahora mismo, y sin motivo en contra.
        for una in trabajadores.listar_cola(raiz):
            assert una["despachable"] is True, una
            assert una["por_que_no"] is None, una

        # El mismo orden leído desde OTRO proceso: la persistencia es la
        # base, no la memoria de quien encoló.
        otro = subprocess.run(
            [
                sys.executable, "-c",
                "import sys, json; sys.path[:0] = [%r, %r]\n"
                "from ingenieria_supervisor import trabajadores\n"
                "print(json.dumps([u['tarea_id'] for u in trabajadores.listar_cola(%r)]))"
                % (str(RAIZ / "orquestacion"), str(RAIZ / "nucleo"), str(raiz)),
            ],
            capture_output=True, text=True, timeout=ESPERA_PROCESO_S,
        )
        assert otro.returncode == 0, otro.stderr
        assert json.loads(otro.stdout.strip()) == esperado, otro.stdout

        # Encolar dos veces: rechazado, y la cola no cambia.
        try:
            trabajadores.encolar(raiz, "T-0901")
            raise AssertionError("Se encoló dos veces la misma tarea.")
        except trabajadores.ErrorCola as choque:
            assert "ya está en la cola" in str(choque), str(choque)

        # Y el índice único de la base lo garantiza aunque se salte la
        # comprobación amable: un INSERT directo de otra viva choca.
        try:
            escribir_sql(
                raiz,
                "INSERT INTO cola (tarea_id, prioridad, estado_cola, trabajo, "
                "tiempo_limite_s, encolado_en, actualizado_en) "
                "VALUES ('T-0901', 0, 'pendiente', '[]', 60, 'x', 'x')",
            )
            raise AssertionError("La base admitió dos entradas vivas de una tarea.")
        except estado_global.ErrorEstadoGlobal as choque:
            assert "UNIQUE" in str(choque).upper(), str(choque)

        # El trabajo es una lista o nada: una cadena se rechaza.
        for malo in ("python herramientas/trabajo.py T-0901", [1, 2], ["a\0b"]):
            try:
                trabajadores.encolar(raiz, "T-0906", trabajo=malo)
                raise AssertionError("Se aceptó un trabajo mal formado: " + repr(malo))
            except trabajadores.ErrorCola:
                pass

        # Retirar y volver a encolar: entrada nueva, secuencia nueva, orden
        # de llegada respetado.
        retirada = trabajadores.desencolar(raiz, "T-0905", "ya no hace falta")
        assert retirada["estado_cola"] == estado_global.COLA_RETIRADA
        assert [u["tarea_id"] for u in trabajadores.listar_cola(raiz)
                if u["estado_cola"] in estado_global.COLA_ESTADOS_VIVOS] == esperado[:-1]

        # Un despacho que eligió la entrada ANTES de que se retirara (una
        # lectura rancia) llega al gancho con la entrada ya cerrada: el
        # UPDATE de la cola no casa, el gancho lanza y el ROLLBACK deshace
        # también la toma. Ni la entrada se marca ni la tarea se toma: o
        # las dos cosas o ninguna.
        try:
            trabajadores._despachar_entrada(
                raiz, retirada, False, None, "rezagado", None, None,
            )
            raise AssertionError("Se despachó una entrada ya retirada.")
        except trabajadores.ErrorDespacho as rechazo:
            METRICAS["DESPACHOS_RECHAZADOS"] += 1
            METRICAS["WORKTREES_CREADOS"] += 1
            assert rechazo.motivo == trabajadores.RECHAZO_NO_PENDIENTE, rechazo.motivo

        fila = fila_de(raiz, "T-0905")
        assert fila["estado"] == str(Estado.NUEVO), fila["estado"]
        assert fila["trabajador_id"] is None and int(fila["generacion"]) == 0
        assert entrada_de(raiz, "T-0905")["estado_cola"] == estado_global.COLA_RETIRADA
        assert not any(
            e["tipo"] == estado_global.EVENTO_TRANSICION for e in eventos_de(raiz, "T-0905")
        ), "La toma rezagada dejó rastro."

        de_nuevo = trabajadores.encolar(raiz, "T-0905", prioridad=5)
        METRICAS["TAREAS_ENCOLADAS"] += 1
        assert de_nuevo["secuencia"] > retirada["secuencia"]

        orden = [u["tarea_id"] for u in trabajadores.listar_cola(raiz)
                 if u["estado_cola"] in estado_global.COLA_ESTADOS_VIVOS]
        assert orden == ["T-0902", "T-0904", "T-0905", "T-0901", "T-0903"], orden

        try:
            trabajadores.desencolar(raiz, "T-0999")
            raise AssertionError("Se retiró una tarea que no estaba en la cola.")
        except trabajadores.ErrorCola:
            pass

        # Cada movimiento deja un evento de la tarea.
        tipos = [e["tipo"] for e in eventos_de(raiz, "T-0905")]
        assert tipos.count(estado_global.EVENTO_COLA) == 3, tipos

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 2. Dos procesos despachando la misma tarea
# ----------------------------------------------------------------------

def _despachador(ruta_raiz: str, identificador, marca: str, barrera, lanzar: bool) -> dict:
    import sys as _sys
    from pathlib import Path as _Path

    for sufijo in ("orquestacion", "nucleo"):
        destino = str(_Path(__file__).resolve().parents[2] / sufijo)
        if destino not in _sys.path:
            _sys.path.insert(0, destino)

    from ingenieria_supervisor import trabajadores as _trabajadores

    try:
        barrera.wait(timeout=ESPERA_BARRERA_S)
    except Exception as error:
        return {"clase": "barrera", "marca": marca, "detalle": str(error)}

    try:
        informe = _trabajadores.despachar(
            _Path(ruta_raiz), identificador, lanzar=lanzar, trabajador_id=marca,
            intervalo_latido_s=INTERVALO_LATIDO_S,
        )

        return {
            "clase": "despachada", "marca": marca,
            "tarea": informe["tarea"], "secuencia": informe["secuencia"],
            "generacion": informe["generacion"],
            "trabajador_id": informe["trabajador_id"],
            "worktree": informe["worktree"], "creado": informe["arbol_creado"],
        }
    except _trabajadores.ErrorDespacho as rechazo:
        return {
            "clase": "rechazada", "marca": marca, "motivo": rechazo.motivo,
            "detalle": str(rechazo),
            "motivos": [uno.get("motivo") for uno in rechazo.rechazos],
        }
    except sqlite3.Error as error:
        return {"clase": "sqlite", "marca": marca, "detalle": str(error)}
    except Exception as error:
        return {
            "clase": "inesperada", "marca": marca,
            "detalle": type(error).__name__ + ": " + str(error),
        }


def _carrera(raiz: Path, identificador, contendientes: int, lanzar=False) -> list:
    contexto = multiprocessing.get_context("spawn")

    with contexto.Manager() as gestor:
        barrera = gestor.Barrier(contendientes)
        grupo = gestor.Pool(processes=contendientes)

        try:
            pendientes = [
                grupo.apply_async(
                    _despachador,
                    (str(raiz), identificador, "D" + str(numero), barrera, lanzar),
                )
                for numero in range(contendientes)
            ]

            respuestas = [uno.get(timeout=ESPERA_PROCESO_S) for uno in pendientes]
        finally:
            grupo.close()
            grupo.join()

    for respuesta in respuestas:
        if respuesta["clase"] == "sqlite":
            METRICAS["ERRORES_SQLITE"] += 1
        elif respuesta["clase"] in ("inesperada", "barrera"):
            METRICAS["EXCEPCIONES_INESPERADAS"] += 1
        elif respuesta["clase"] == "despachada":
            METRICAS["DESPACHOS_ACEPTADOS"] += 1
            if respuesta["creado"]:
                METRICAS["WORKTREES_CREADOS"] += 1
        else:
            METRICAS["DESPACHOS_RECHAZADOS"] += 1
            if respuesta["motivo"] == trabajadores.RECHAZO_AMBITO or (
                trabajadores.RECHAZO_AMBITO in (respuesta.get("motivos") or [])
            ):
                METRICAS["CONFLICTOS_DE_AMBITO_DETECTADOS"] += 1

    raras = [r for r in respuestas if r["clase"] in ("sqlite", "inesperada", "barrera")]
    assert not raras, "Respuestas anómalas en la carrera: " + repr(raras)

    return respuestas


def prueba_02_dos_procesos_despachan_la_misma_tarea(contendientes: int, rondas: int):
    print("  2. dos procesos despachan la misma tarea -> un solo ganador:", end=" ")

    raiz = crear_repositorio("misma_")

    try:
        ficha_minima(raiz, "T-0911")
        trabajadores.encolar(raiz, "T-0911")
        METRICAS["TAREAS_ENCOLADAS"] += 1

        secuencia = entrada_de(raiz, "T-0911")["secuencia"]

        for ronda in range(1, rondas + 1):
            respuestas = _carrera(raiz, "T-0911", contendientes)

            ganadoras = [r for r in respuestas if r["clase"] == "despachada"]

            if len(ganadoras) > 1:
                METRICAS["DOBLES_DESPACHOS"] += len(ganadoras) - 1

            assert len(ganadoras) == 1, (
                "Ronda " + str(ronda) + ": " + str(len(ganadoras))
                + " ganadores. " + repr(respuestas)
            )

            ganadora = ganadoras[0]
            fila = fila_de(raiz, "T-0911")
            entrada = entrada_de(raiz, "T-0911")

            assert fila["estado"] == str(Estado.EN_EJECUCION)
            assert fila["trabajador_id"] == ganadora["marca"] == entrada["trabajador_id"]
            assert int(fila["generacion"]) == ganadora["generacion"] == int(entrada["generacion"]) == ronda
            assert entrada["estado_cola"] == estado_global.COLA_DESPACHADA
            assert entrada["secuencia"] == secuencia, "La entrada cambió de secuencia."
            assert Path(fila["worktree"]) == Path(entrada["worktree"]) == trabajadores.ruta_de_arbol(raiz, "T-0911")
            assert arboles_en_zona(raiz) == ["T-0911"], arboles_en_zona(raiz)

            for perdedora in respuestas:
                if perdedora["clase"] == "rechazada":
                    assert perdedora["motivo"] in (
                        trabajadores.RECHAZO_NO_PENDIENTE,
                        trabajadores.RECHAZO_TOMA,
                        trabajadores.RECHAZO_ESTADO,
                    ), perdedora

            # El ganador suelta la tarea; la entrada vuelve a la cola con la
            # MISMA secuencia en la siguiente reconciliación.
            nucleo.devolver(
                raiz, "T-0911", "fin de ronda",
                trabajador_id=ganadora["marca"], generacion=ganadora["generacion"],
            )

            reconciliado = trabajadores.reconciliar_cola(raiz)
            assert [u["secuencia"] for u in reconciliado["reencoladas"]] == [secuencia]
            assert entrada_de(raiz, "T-0911")["estado_cola"] == estado_global.COLA_PENDIENTE
            METRICAS["RECUPERACIONES"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 3. Ámbitos solapados: sólo una con escritor
# ----------------------------------------------------------------------

def prueba_03_ambitos_solapados_solo_una_con_escritor(contendientes: int):
    print("  3. dos tareas con ámbitos solapados -> sólo una con escritor:", end=" ")

    raiz = crear_repositorio("solapadas_")

    try:
        ficha_minima(raiz, "T-0921", ambito=["modulos/comun/*.py"])
        ficha_minima(raiz, "T-0922", ambito=["modulos/comun/util.py"])
        trabajadores.encolar(raiz, "T-0921")
        trabajadores.encolar(raiz, "T-0922")
        METRICAS["TAREAS_ENCOLADAS"] += 2

        # a) Carrera real: los dos procesos intentan despachar cada uno una
        #    tarea distinta que choca con la otra. Como mucho, uno gana.
        respuestas = _carrera(raiz, None, contendientes)
        ganadoras = [r for r in respuestas if r["clase"] == "despachada"]
        tareas = {r["tarea"] for r in ganadoras}

        if len(tareas) > 1:
            METRICAS["DOBLES_DESPACHOS"] += len(tareas) - 1

        assert len(ganadoras) == 1, repr(respuestas)
        assert len(tareas) == 1, "Dos tareas solapadas quedaron en ejecución: " + repr(tareas)

        con_escritor = [
            f for f in (fila_de(raiz, "T-0921"), fila_de(raiz, "T-0922"))
            if f["estado"] == str(Estado.EN_EJECUCION)
        ]
        assert len(con_escritor) == 1, [f["estado"] for f in con_escritor]

        ganadora = ganadoras[0]
        otra = "T-0922" if ganadora["tarea"] == "T-0921" else "T-0921"

        # b) Secuencial: la otra queda frenada con motivo, sin perder su puesto.
        try:
            trabajadores.despachar(raiz, otra, lanzar=False)
            raise AssertionError("Se despachó una tarea con el ámbito ocupado.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_AMBITO, rechazo.motivo

        listado = {u["tarea_id"]: u for u in trabajadores.listar_cola(raiz)}
        assert listado[otra]["estado_cola"] == estado_global.COLA_PENDIENTE
        assert listado[otra]["despachable"] is False
        assert "choca" in listado[otra]["por_que_no"], listado[otra]
        assert "ambito" in str(listado[otra]["ultimo_rechazo"]), listado[otra]["ultimo_rechazo"]

        # c) La guarda que decide es la de la TRANSACCIÓN, no la lectura
        #    previa: con la lectura previa cegada, la toma sigue negando.
        original = trabajadores._por_que_no_se_despacha
        trabajadores._por_que_no_se_despacha = lambda fila, tareas: None

        try:
            trabajadores.despachar(raiz, otra, lanzar=False)
            raise AssertionError("La transacción concedió dos escritores sobre el mismo ámbito.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_AMBITO, rechazo.motivo
            assert "Un solo escritor" in str(rechazo), str(rechazo)
        finally:
            trabajadores._por_que_no_se_despacha = original

        assert fila_de(raiz, otra)["estado"] == str(Estado.NUEVO)
        assert entrada_de(raiz, otra)["estado_cola"] == estado_global.COLA_PENDIENTE
        # El árbol que se preparó para intentarlo se queda (limpio, en su
        # rama): es de la tarea, no del despacho que perdió, y se reutiliza.
        assert arboles_en_zona(raiz) == ["T-0921", "T-0922"], arboles_en_zona(raiz)
        assert nucleo.Git(trabajadores.ruta_de_arbol(raiz, otra)).cambios_del_arbol() == []

        # d) Al liberar el ámbito, la frenada sale.
        nucleo.devolver(
            raiz, ganadora["tarea"], "hecho",
            trabajador_id=ganadora["marca"], generacion=ganadora["generacion"],
        )

        informe = trabajadores.despachar(raiz, otra, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert informe["tarea"] == otra
        assert informe["arbol_creado"] is False, "No reutilizó el árbol ya preparado."
        assert fila_de(raiz, otra)["estado"] == str(Estado.EN_EJECUCION)
        assert fila_de(raiz, ganadora["tarea"])["estado"] == str(Estado.REABIERTO)

        nucleo.devolver(
            raiz, otra, "hecho",
            trabajador_id=informe["trabajador_id"], generacion=informe["generacion"],
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 4. Sin solapamiento: ambas progresan, cada una en su ámbito
# ----------------------------------------------------------------------

def prueba_04_sin_solapamiento_ambas_progresan():
    print("  4. dos tareas sin solapamiento -> ambas progresan, cada una en su ámbito:", end=" ")

    raiz = crear_repositorio("paralelas_")
    procesos = []

    try:
        base = _git(raiz, "rev-parse", "HEAD").stdout.strip()
        tareas = ["T-0931", "T-0932", "T-0933"]

        for identificador in tareas:
            ficha_minima(raiz, identificador)
            trabajadores.encolar(
                raiz, identificador,
                trabajo=trabajo_demo(identificador, "--dormir", "3", "extra " + identificador),
            )
            METRICAS["TAREAS_ENCOLADAS"] += 1

        informes = []

        for _ in tareas:
            informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
            METRICAS["DESPACHOS_ACEPTADOS"] += 1
            METRICAS["WORKTREES_CREADOS"] += 1
            informes.append(informe)
            procesos.append(informe["proceso"])

        assert sorted(i["tarea"] for i in informes) == tareas
        assert len({i["worktree"] for i in informes}) == 3
        assert all(trabajadores.dentro_de_zona(raiz, i["worktree"]) for i in informes)

        # Las tres a la vez, con su trabajador vivo y su PID adoptado.
        for informe in informes:
            esperar_a(
                lambda i=informe: fila_de(raiz, i["tarea"])["pid"] == i["pid_trabajador"],
                descripcion="adopción del PID por el trabajador de " + informe["tarea"],
            )

        vivas = [f for f in (fila_de(raiz, t) for t in tareas)
                 if f["estado"] == str(Estado.EN_EJECUCION)]
        assert len(vivas) == 3, [f["estado"] for f in vivas]
        assert all(nucleo.proceso_vivo(f["pid"]) for f in vivas)

        for informe in informes:
            codigo = informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S)
            registro = Path(informe["registro"]).read_text(encoding="utf-8")
            assert codigo == 0, registro[-800:]

            detalle = informe_del_registro(registro)
            assert detalle["resultado"] == trabajadores.RESULTADO_VERIFICADA
            assert detalle["estado_final"] == str(Estado.PROPUESTO)
            assert detalle["latidos"] >= 1
            assert detalle["fuera_de_ambito"] == []

            fila = fila_de(raiz, informe["tarea"])
            assert fila["estado"] == str(Estado.PROPUESTO)
            assert fila["trabajador_id"] is None and fila["pid"] is None
            assert fila["worktree"] is None

            entrada = entrada_de(raiz, informe["tarea"])
            assert entrada["estado_cola"] == estado_global.COLA_TERMINADA
            assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_VERIFICADA
            assert entrada["resultado"]["estado"] == str(Estado.PROPUESTO)

            # Cada rama sólo contiene el archivo de su tarea: ningún
            # trabajador escribió fuera de su ámbito.
            cambios = _git(
                raiz, "diff", "--name-only", base, "tarea/" + informe["tarea"]
            ).stdout.split()
            assert cambios == ["modulos/demostracion/" + informe["tarea"] + ".py"], cambios

            contenido = (Path(informe["worktree"]) / cambios[0]).read_text(encoding="utf-8")
            assert "extra " + informe["tarea"] in contenido

        # La raíz no la tocó nadie (salvo el espejo JSON del Supervisor), y
        # la zona no aparece como «sin versionar»: el despacho la anota en
        # `.git/info/exclude`, que es local, sin tocar el `.gitignore`
        # versionado del proyecto.
        sucio = _git(
            raiz, "status", "--porcelain", "--", ".", ":(exclude)orquestacion/tareas/",
        ).stdout.strip()
        assert sucio == "", sucio
        exclusion = (raiz / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert "/" + trabajadores.ZONA_ARBOLES + "/" in exclusion.splitlines(), exclusion
        assert not (raiz / ".gitignore").exists()

        # Limpieza: los tres árboles están limpios, sin ejecución, y se van.
        limpieza = trabajadores.limpiar_arboles(raiz)
        assert sorted(u["tarea"] for u in limpieza["limpiados"]) == tareas, limpieza
        assert limpieza["rechazados"] == []
        METRICAS["WORKTREES_LIMPIADOS"] += 3
        assert arboles_en_zona(raiz) == []

        listado = _git(raiz, "worktree", "list", "--porcelain").stdout
        assert ".arboles" not in listado, listado

        # Las ramas siguen con su trabajo.
        for identificador in tareas:
            assert _git(raiz, "rev-parse", "--verify", "tarea/" + identificador).returncode == 0

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 5. Doble lanzamiento accidental
# ----------------------------------------------------------------------

def prueba_05_doble_lanzamiento_rechazado():
    print("  5. un doble lanzamiento accidental -> rechazado:", end=" ")

    raiz = crear_repositorio("doble_")
    procesos = []

    try:
        ficha_minima(raiz, "T-0941")
        trabajadores.encolar(raiz, "T-0941", trabajo=trabajo_demo("T-0941", "--dormir", "3"))
        METRICAS["TAREAS_ENCOLADAS"] += 1

        informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        procesos.append(informe["proceso"])

        esperar_a(
            lambda: fila_de(raiz, "T-0941")["pid"] == informe["pid_trabajador"],
            descripcion="adopción del PID",
        )

        # a) Despachar otra vez, por la API y por la consola: rechazado.
        try:
            trabajadores.despachar(raiz, "T-0941")
            raise AssertionError("Se despachó dos veces la misma entrada.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_NO_PENDIENTE, rechazo.motivo

        try:
            trabajadores.despachar(raiz)
            raise AssertionError("Se despachó algo con la cola sin pendientes.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)

        salida = cli(raiz, "despachar", "T-0941")
        assert salida.returncode == 7, salida.stdout + salida.stderr
        assert "DESPACHO RECHAZADO" in salida.stdout
        METRICAS["DESPACHOS_RECHAZADOS"] += 1

        # b) Encolar otra vez mientras está despachada: rechazado.
        try:
            trabajadores.encolar(raiz, "T-0941")
            raise AssertionError("Se encoló una tarea ya despachada.")
        except trabajadores.ErrorCola:
            pass

        # c) Tomarla a mano: rechazado por la toma atómica.
        try:
            nucleo.tomar(raiz, "T-0941", trabajador_id="intruso")
            raise AssertionError("Se tomó a mano una tarea en ejecución.")
        except nucleo.ErrorToma:
            pass

        # d) Lanzar el MISMO proceso trabajador otra vez, con el mismo argv
        #    (un guion que repite el lanzamiento): no adopta, no trabaja.
        segundo = trabajadores.lanzar_trabajador(
            informe["argv"], raiz, Path(informe["registro"]).with_suffix(".2.log")
        )
        procesos.append(segundo)
        codigo_segundo = segundo.wait(timeout=ESPERA_TRABAJADOR_S)
        registro_segundo = Path(informe["registro"]).with_suffix(".2.log").read_text(encoding="utf-8")
        assert codigo_segundo == 4, registro_segundo[-800:]
        detalle = informe_del_registro(registro_segundo)
        assert detalle["adoptada"] is False
        assert detalle["resultado"] == "no_adoptada"
        assert detalle["trabajo"] is None, "El segundo proceso ejecutó el trabajo."

        # El primero sigue siendo el dueño, con su PID.
        fila = fila_de(raiz, "T-0941")
        assert fila["estado"] == str(Estado.EN_EJECUCION)
        assert fila["pid"] == informe["pid_trabajador"]
        assert fila["trabajador_id"] == informe["trabajador_id"]

        codigo = informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S)
        assert codigo == 0, Path(informe["registro"]).read_text(encoding="utf-8")[-800:]
        assert fila_de(raiz, "T-0941")["estado"] == str(Estado.PROPUESTO)

        # Sólo un commit de trabajo en la rama: el segundo no hizo nada.
        commits = _git(raiz, "rev-list", "--count", "main..tarea/T-0941").stdout.strip()
        assert commits == "1", commits

        # e) Y después de terminar, un lanzamiento rezagado tampoco resucita nada.
        tercero = trabajadores.lanzar_trabajador(
            informe["argv"], raiz, Path(informe["registro"]).with_suffix(".3.log")
        )
        procesos.append(tercero)
        assert tercero.wait(timeout=ESPERA_TRABAJADOR_S) == 4
        assert fila_de(raiz, "T-0941")["estado"] == str(Estado.PROPUESTO)
        assert entrada_de(raiz, "T-0941")["estado_cola"] == estado_global.COLA_TERMINADA

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 6. Trabajador que termina bien
# ----------------------------------------------------------------------

def prueba_06_trabajador_termina_bien_estado_consistente():
    print("  6. un trabajador que termina bien -> estado consistente y trazable:", end=" ")

    raiz = crear_repositorio("bien_")

    try:
        ficha_minima(raiz, "T-0951")
        trabajadores.encolar(raiz, "T-0951", trabajo=trabajo_demo("T-0951"))
        METRICAS["TAREAS_ENCOLADAS"] += 1

        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-800:]

        detalle = informe_del_registro(registro)
        assert detalle["adoptada"] is True
        assert detalle["trabajo"]["codigo"] == 0
        assert "trabajo hecho" in detalle["trabajo"]["salida"]
        assert detalle["entrada_cerrada"] is True
        assert detalle["verificacion"]["raiz"] == informe["worktree"]
        assert detalle["verificacion"]["rama"] == "tarea/T-0951"
        assert detalle["verificacion"]["sin_confirmar"] is False
        assert detalle["verificacion"]["ok"] == detalle["verificacion"]["total"] == 1

        fila = fila_de(raiz, "T-0951")
        assert fila["estado"] == str(Estado.PROPUESTO)
        for columna in ("trabajador_id", "pid", "iniciado_en", "ultimo_latido", "worktree"):
            assert fila[columna] is None, (columna, fila[columna])
        assert fila["intentos"] == 0
        assert int(fila["generacion"]) == 1

        ultima = fila["ejecuciones"][-1]
        assert ultima["resultado"] == "APROBADO"
        assert Path(ultima["raiz"]) == Path(informe["worktree"])
        assert ultima["es_worktree"] is True
        assert ultima["rama"] == "tarea/T-0951"
        commit_arbol = _git(raiz, "rev-parse", "--short", "tarea/T-0951").stdout.strip()
        assert ultima["commit"] == commit_arbol, (ultima["commit"], commit_arbol)
        assert ultima["trabajador_id"] == informe["trabajador_id"]
        assert fila["commit_inicial"] == informe["commit_inicial"]
        assert fila["commit_inicial"] != commit_arbol, "El trabajo no dejó commit."

        entrada = entrada_de(raiz, "T-0951")
        assert entrada["estado_cola"] == estado_global.COLA_TERMINADA
        assert entrada["terminado_en"]
        assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_VERIFICADA
        assert entrada["resultado"]["estado"] == str(Estado.PROPUESTO)
        assert entrada["resultado"]["verificacion"]["commit"] == commit_arbol
        assert entrada["trabajador_id"] == informe["trabajador_id"]

        # El historial cuenta la historia entera, en orden.
        tipos = [e["tipo"] for e in reversed(eventos_de(raiz, "T-0951"))]
        assert tipos == [
            estado_global.EVENTO_CREACION,
            estado_global.EVENTO_COLA,         # encolada
            estado_global.EVENTO_COLA,         # despachada (mismo COMMIT que la toma)
            estado_global.EVENTO_TRANSICION,   # tomada
            estado_global.EVENTO_VERIFICACION,
            estado_global.EVENTO_TRANSICION,   # propuesta
            estado_global.EVENTO_COLA,         # cerrada
        ], tipos

        # El espejo JSON refleja lo mismo que la base.
        espejo = json.loads(fichas.ruta_ficha(raiz, "T-0951").read_text(encoding="utf-8"))
        assert espejo["estado"] == str(Estado.PROPUESTO)
        assert espejo["trabajador_id"] is None

        # El registro del trabajador quedó en la zona, aparte de los árboles.
        assert Path(informe["registro"]).parent == zona(raiz) / trabajadores.CARPETA_REGISTROS
        assert arboles_en_zona(raiz) == ["T-0951"]

        # Sin ejecución y limpio: el árbol se puede retirar.
        limpieza = trabajadores.limpiar_arbol(raiz, "T-0951")
        assert limpieza["limpiado"] is True
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        assert arboles_en_zona(raiz) == []

        # Segunda vez: no hay nada, y no es error.
        assert trabajadores.limpiar_arbol(raiz, "T-0951")["limpiado"] is False

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 7. Trabajador que falla
# ----------------------------------------------------------------------

def prueba_07_trabajador_falla_recuperacion_consistente():
    print("  7. un trabajador que falla -> recuperación consistente:", end=" ")

    raiz = crear_repositorio("falla_")

    try:
        # a) El trabajo termina con código 3: la tarea se devuelve.
        ficha_minima(raiz, "T-0961")
        trabajadores.encolar(raiz, "T-0961", trabajo=trabajo_demo("T-0961", "--fallar", "3"))
        METRICAS["TAREAS_ENCOLADAS"] += 1

        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 1, registro[-800:]
        detalle = informe_del_registro(registro)
        assert detalle["resultado"] == trabajadores.RESULTADO_TRABAJO_FALLIDO
        assert detalle["trabajo"]["codigo"] == 3
        assert detalle["verificacion"] is None, "Se verificó un trabajo fallido."

        fila = fila_de(raiz, "T-0961")
        assert fila["estado"] == str(Estado.REABIERTO), fila["estado"]
        assert fila["trabajador_id"] is None and fila["pid"] is None
        assert fila["intentos"] == 0

        entrada = entrada_de(raiz, "T-0961")
        assert entrada["estado_cola"] == estado_global.COLA_FALLIDA
        assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_TRABAJO_FALLIDO
        assert "código 3" in entrada["resultado"]["motivo"]

        ultimo = eventos_de(raiz, "T-0961")[1]
        assert ultimo["tipo"] == estado_global.EVENTO_TRANSICION
        assert "código 3" in ultimo["motivo"], ultimo["motivo"]

        # El árbol se conserva con lo que el trabajo dejó (evidencia), así
        # que NO se puede limpiar: tiene cambios sin confirmar.
        assert (Path(informe["worktree"]) / "modulos/demostracion/T-0961.py").is_file()

        try:
            trabajadores.limpiar_arbol(raiz, "T-0961")
            raise AssertionError("Se borró un árbol con trabajo sin confirmar.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "sin confirmar" in str(rechazo)

        # Y la tarea puede volver a encolarse y despacharse (mismo árbol,
        # limpiado por el propio trabajo al confirmar).
        trabajadores.encolar(raiz, "T-0961", trabajo=trabajo_demo("T-0961"))
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe2, codigo2, registro2 = despachar_y_esperar(raiz)
        assert codigo2 == 0, registro2[-800:]
        assert informe2["arbol_creado"] is False
        assert informe2["generacion"] == 2
        assert fila_de(raiz, "T-0961")["estado"] == str(Estado.PROPUESTO)
        assert len(entradas_de(raiz, "T-0961")) == 2

        # b) El trabajo escribe FUERA del ámbito y además lo confirma: se
        #    bloquea (decide una persona), y el commit no lo esconde.
        ficha_minima(raiz, "T-0962")
        trabajadores.encolar(
            raiz, "T-0962",
            trabajo=trabajo_demo("T-0962", "--fuera", "otros/ajeno.py"),
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1

        informe3, codigo3, registro3 = despachar_y_esperar(raiz)
        assert codigo3 == 1, registro3[-800:]
        detalle3 = informe_del_registro(registro3)
        assert detalle3["resultado"] == trabajadores.RESULTADO_FUERA_DE_AMBITO
        assert detalle3["fuera_de_ambito"] == ["otros/ajeno.py"], detalle3["fuera_de_ambito"]
        assert detalle3["verificacion"] is None

        fila3 = fila_de(raiz, "T-0962")
        assert fila3["estado"] == str(Estado.BLOQUEADO), fila3["estado"]
        assert fila3["trabajador_id"] is None
        entrada3 = entrada_de(raiz, "T-0962")
        assert entrada3["estado_cola"] == estado_global.COLA_FALLIDA
        assert entrada3["resultado"]["rutas"] == ["otros/ajeno.py"]
        assert "otros/ajeno.py" in eventos_de(raiz, "T-0962")[1]["motivo"]

        # c) Un trabajo sin confirmar fuera del ámbito también se ve.
        ficha_minima(raiz, "T-0963")
        trabajadores.encolar(
            raiz, "T-0963",
            trabajo=trabajo_demo("T-0963", "--sin-commit", "--fuera", "pruebas/demostracion/prueba_ajena.py"),
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe4, codigo4, registro4 = despachar_y_esperar(raiz)
        assert codigo4 == 1
        detalle4 = informe_del_registro(registro4)
        assert detalle4["resultado"] == trabajadores.RESULTADO_FUERA_DE_AMBITO
        assert detalle4["fuera_de_ambito"] == ["pruebas/demostracion/prueba_ajena.py"]
        assert fila_de(raiz, "T-0963")["estado"] == str(Estado.BLOQUEADO)

        # d) El ejecutable no existe: trabajo fallido, no avería.
        ficha_minima(raiz, "T-0964")
        trabajadores.encolar(raiz, "T-0964", trabajo=["/no/existe/este/programa", "x"])
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe5, codigo5, registro5 = despachar_y_esperar(raiz)
        assert codigo5 == 1
        detalle5 = informe_del_registro(registro5)
        assert detalle5["resultado"] == trabajadores.RESULTADO_TRABAJO_FALLIDO
        assert "No se pudo lanzar" in detalle5["trabajo"]["detalle"], detalle5["trabajo"]
        assert fila_de(raiz, "T-0964")["estado"] == str(Estado.REABIERTO)
        assert entrada_de(raiz, "T-0964")["estado_cola"] == estado_global.COLA_FALLIDA

        # e) El trabajo se pasa de tiempo: se interrumpe y se devuelve.
        ficha_minima(raiz, "T-0965")
        trabajadores.encolar(
            raiz, "T-0965", trabajo=trabajo_demo("T-0965", "--dormir", "20"),
            tiempo_limite_s=1,
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        inicio = time.monotonic()
        informe6, codigo6, registro6 = despachar_y_esperar(raiz)
        duracion = time.monotonic() - inicio
        assert codigo6 == 1
        detalle6 = informe_del_registro(registro6)
        assert detalle6["trabajo"]["agotado"] is True, detalle6["trabajo"]
        assert duracion < 15, duracion
        assert fila_de(raiz, "T-0965")["estado"] == str(Estado.REABIERTO)
        assert "tiempo límite" in entrada_de(raiz, "T-0965")["resultado"]["motivo"]

        # En ningún caso quedó una ejecución colgada ni un PID fantasma.
        for identificador in ("T-0961", "T-0962", "T-0963", "T-0964", "T-0965"):
            fila = fila_de(raiz, identificador)
            assert fila["estado"] != str(Estado.EN_EJECUCION), identificador
            assert fila["pid"] is None and fila["trabajador_id"] is None, identificador

        assert trabajadores.reconciliar_cola(raiz)["revisadas"] == 0

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 8. Parece muerto pero hay duda: no se libera
# ----------------------------------------------------------------------

def prueba_08_duda_no_se_libera():
    print("  8. un trabajador que parece muerto pero hay duda -> NO se libera:", end=" ")

    raiz = crear_repositorio("duda_")

    try:
        ficha_minima(raiz, "T-0971")
        trabajadores.encolar(raiz, "T-0971")
        METRICAS["TAREAS_ENCOLADAS"] += 1

        # Se despacha sin lanzar: la fila lleva el PID de ESTE proceso, que
        # está vivo, y un latido que se envejece a mano.
        informe = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        secuencia = informe["secuencia"]

        viejo = (nucleo.ahora_datetime() - timedelta(seconds=2000)).isoformat(timespec="seconds")
        escribir_sql(raiz, "UPDATE tareas SET ultimo_latido = ? WHERE id = 'T-0971'", (viejo,))

        def sigue_sin_tocar(recuperacion, grupo):
            ids = [u["id"] for u in recuperacion[grupo]]
            assert ids == ["T-0971"], (grupo, recuperacion)
            assert recuperacion["huerfanas"] == [], recuperacion["huerfanas"]
            fila = fila_de(raiz, "T-0971")
            assert fila["estado"] == str(Estado.EN_EJECUCION)
            assert fila["trabajador_id"] == informe["trabajador_id"]
            cola = trabajadores.reconciliar_cola(raiz)
            assert [u["secuencia"] for u in cola["sin_tocar"]] == [secuencia], cola
            assert cola["reencoladas"] == []
            assert entrada_de(raiz, "T-0971")["estado_cola"] == estado_global.COLA_DESPACHADA
            METRICAS["CASOS_DUDOSOS_ESCALADOS"] += 1

        # a) Latido vencido, proceso vivo: duda.
        sigue_sin_tocar(nucleo.reanudar(raiz), "latido_vencido")

        # b) Trabajador de otra máquina: aquí no hay segunda señal posible.
        escribir_sql(
            raiz, "UPDATE tareas SET trabajador_id = ? WHERE id = 'T-0971'",
            ("otra-maquina/1/abc",),
        )
        escribir_sql(
            raiz, "UPDATE cola SET trabajador_id = ? WHERE secuencia = ?",
            ("otra-maquina/1/abc", secuencia),
        )
        informe["trabajador_id"] = "otra-maquina/1/abc"
        sigue_sin_tocar(
            nucleo.reanudar(raiz, comprobar_proceso=lambda pid: False), "latido_vencido"
        )

        # c) Fila incompleta: tampoco demuestra nada.
        escribir_sql(raiz, "UPDATE tareas SET pid = NULL WHERE id = 'T-0971'")
        sigue_sin_tocar(
            nucleo.reanudar(raiz, comprobar_proceso=lambda pid: False),
            "inconsistentes_sin_tocar",
        )

        # La consola lo escala igual y devuelve 1.
        salida = cli(raiz, "reanudar")
        assert salida.returncode == 1, salida.stdout
        assert "DESPACHADAS QUE SIGUEN VIVAS" in salida.stdout, salida.stdout
        assert "INCONSISTENTES" in salida.stdout

        # d) Y cuando SÍ hay dos señales (latido vencido Y proceso
        #    demostrado muerto), se recupera, y la cola lo sigue.
        escribir_sql(
            raiz, "UPDATE tareas SET pid = 4242, trabajador_id = ? WHERE id = 'T-0971'",
            (informe["trabajador_id"].replace("otra-maquina", "vm-local"),),
        )
        escribir_sql(
            raiz, "UPDATE cola SET trabajador_id = ? WHERE secuencia = ?",
            (informe["trabajador_id"].replace("otra-maquina", "vm-local"), secuencia),
        )
        # `equipo_de` compara con el nombre de esta máquina: se pone el real.
        import socket
        propio = socket.gethostname() + "/1/abc"
        escribir_sql(raiz, "UPDATE tareas SET trabajador_id = ? WHERE id = 'T-0971'", (propio,))
        escribir_sql(raiz, "UPDATE cola SET trabajador_id = ? WHERE secuencia = ?", (propio, secuencia))

        recuperacion = nucleo.reanudar(raiz, comprobar_proceso=lambda pid: False)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0971"], recuperacion
        assert fila_de(raiz, "T-0971")["estado"] == str(Estado.REABIERTO)

        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [secuencia]
        entrada = entrada_de(raiz, "T-0971")
        assert entrada["estado_cola"] == estado_global.COLA_PENDIENTE
        assert entrada["trabajador_id"] is None and entrada["generacion"] is None
        assert "reencolada" in str(entrada["ultimo_rechazo"])
        METRICAS["RECUPERACIONES"] += 1

        # Idempotente: una segunda pasada no cambia nada.
        assert trabajadores.reconciliar_cola(raiz)["reencoladas"] == []
        assert nucleo.reanudar(raiz)["revisadas"] == 0

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 9. Worktree fuera de la zona controlada
# ----------------------------------------------------------------------

def prueba_09_worktree_fuera_de_zona_rechazado():
    print("  9. un worktree registrado fuera de la zona controlada -> rechazado:", end=" ")

    raiz = crear_repositorio("fuera_")
    ajeno = Path(tempfile.mkdtemp(prefix="fuera_zona_")) / "arbol"

    try:
        ficha_minima(raiz, "T-0981")

        # Un worktree REAL de este repositorio, pero fuera de la zona,
        # registrado a mano por una toma legítima.
        creado = _git(raiz, "worktree", "add", str(ajeno), "-b", "tarea/T-0981")
        assert creado.returncode == 0, creado.stderr

        tomada = nucleo.tomar(raiz, "T-0981", trabajador_id="manual", worktree=str(ajeno))
        assert Path(tomada.worktree) == ajeno.resolve()

        for intento in ("en ejecución", "devuelta"):
            try:
                trabajadores.limpiar_arbol(raiz, "T-0981")
                raise AssertionError("Se aceptó limpiar un árbol fuera de la zona (" + intento + ").")
            except trabajadores.ErrorLimpieza as rechazo:
                METRICAS["WORKTREES_RECHAZADOS"] += 1
                assert "FUERA de la zona" in str(rechazo), str(rechazo)

            assert ajeno.is_dir(), "El árbol ajeno desapareció."

            if intento == "en ejecución":
                # También con la ruta en una entrada despachada de la cola.
                trabajadores.encolar(raiz, "T-0982") if False else None
                nucleo.devolver(raiz, "T-0981", "hecho", trabajador_id="manual", generacion=tomada.generacion)
                # La fila ya no lleva árbol; se simula una entrada despachada
                # que sí lo lleve (un despacho manipulado).
                trabajadores.encolar(raiz, "T-0981")
                METRICAS["TAREAS_ENCOLADAS"] += 1
                secuencia = entrada_de(raiz, "T-0981")["secuencia"]
                escribir_sql(
                    raiz,
                    "UPDATE cola SET estado_cola = 'despachada', worktree = ?, "
                    "trabajador_id = 'x', generacion = 1 WHERE secuencia = ?",
                    (str(ajeno), secuencia),
                )

        # Sin ninguna referencia a la ruta ajena, la tarea simplemente no
        # tiene árbol automático: nada que borrar, y el ajeno sigue.
        escribir_sql(raiz, "UPDATE cola SET estado_cola = 'retirada', worktree = NULL WHERE secuencia = ?", (secuencia,))
        assert trabajadores.limpiar_arbol(raiz, "T-0981")["limpiado"] is False
        assert ajeno.is_dir()

        # El despacho SIEMPRE crea dentro de la zona.
        ficha_minima(raiz, "T-0983")
        trabajadores.encolar(raiz, "T-0983")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        assert trabajadores.dentro_de_zona(raiz, informe["worktree"])
        assert Path(informe["worktree"]).parent == zona(raiz)
        nucleo.devolver(raiz, "T-0983", "hecho", trabajador_id=informe["trabajador_id"], generacion=informe["generacion"])

        # Mientras la entrada siga DESPACHADA (nadie reconcilió aún), el
        # árbol no se toca; reconciliada, vuelve a pendiente y se retira.
        try:
            trabajadores.limpiar_arbol(raiz, "T-0983")
            raise AssertionError("Se borró el árbol de una entrada despachada.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "despachada" in str(rechazo), str(rechazo)

        assert [u["tarea"] for u in trabajadores.reconciliar_cola(raiz)["reencoladas"]] == ["T-0983"]
        METRICAS["RECUPERACIONES"] += 1
        trabajadores.desencolar(raiz, "T-0983")

        # Una carpeta corriente dentro de la zona con nombre de tarea: Git
        # no la reconoce, así que no se borra (no se verifica -> no se toca).
        falsa = zona(raiz) / "T-0984"
        falsa.mkdir()
        (falsa / "algo.txt").write_text("no soy un worktree\n", encoding="utf-8")
        ficha_minima(raiz, "T-0984")

        try:
            trabajadores.limpiar_arbol(raiz, "T-0984")
            raise AssertionError("Se borró una carpeta que Git no reconoce.")
        except nucleo.ErrorWorktree as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "no es un worktree registrado" in str(rechazo), str(rechazo)

        assert (falsa / "algo.txt").is_file()

        # Y algo con un nombre que no es de tarea, dentro de la zona: se
        # informa, no se toca.
        rara = zona(raiz) / "no-es-tarea"
        rara.mkdir()
        limpieza = trabajadores.limpiar_arboles(raiz)
        rechazadas = {u["tarea"] for u in limpieza["rechazados"]}
        assert {"T-0984", "no-es-tarea"} <= rechazadas, limpieza
        assert [u["tarea"] for u in limpieza["limpiados"]] == ["T-0983"]
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        METRICAS["WORKTREES_RECHAZADOS"] += 1
        assert rara.is_dir() and falsa.is_dir()

        # Hijo directo de la zona, no descendiente: un trozo de árbol no cuenta.
        assert not trabajadores.dentro_de_zona(raiz, zona(raiz) / "T-0983" / "pruebas")
        assert not trabajadores.dentro_de_zona(raiz, zona(raiz))
        assert not trabajadores.dentro_de_zona(raiz, raiz)

        comprobar_integridad(raiz)
    finally:
        _git(raiz, "worktree", "remove", "--force", str(ajeno))
        borrar(ajeno.parent)
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 10. Borrar un worktree con cambios ajenos
# ----------------------------------------------------------------------

def prueba_10_borrar_worktree_con_cambios_ajenos_rechazado():
    print(" 10. borrar un worktree con cambios ajenos -> rechazado:", end=" ")

    raiz = crear_repositorio("ajenos_")

    try:
        ficha_minima(raiz, "T-0991")
        trabajadores.encolar(raiz, "T-0991", trabajo=trabajo_demo("T-0991"))
        METRICAS["TAREAS_ENCOLADAS"] += 1

        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-800:]

        arbol = Path(informe["worktree"])

        def se_rechaza(descripcion, fragmento):
            try:
                trabajadores.limpiar_arbol(raiz, "T-0991")
                raise AssertionError("Se borró el árbol " + descripcion + ".")
            except trabajadores.ErrorLimpieza as rechazo:
                METRICAS["WORKTREES_RECHAZADOS"] += 1
                assert fragmento in str(rechazo), (descripcion, str(rechazo))

            assert arbol.is_dir(), "El árbol desapareció (" + descripcion + ")."

        # a) Un archivo nuevo sin versionar, de alguien.
        nuevo = arbol / "notas_de_otro.txt"
        nuevo.write_text("trabajo ajeno sin confirmar\n", encoding="utf-8")
        se_rechaza("con un archivo sin versionar", "notas_de_otro.txt")
        assert nuevo.is_file()

        # b) Preparado en el índice, sin commit.
        _git(arbol, "add", "notas_de_otro.txt")
        se_rechaza("con un archivo preparado", "notas_de_otro.txt")
        _git(arbol, "reset", "-q")
        nuevo.unlink()

        # c) Un archivo versionado modificado.
        versionado = arbol / "pruebas" / "demostracion" / "prueba_verde.py"
        versionado.write_text(PRUEBA_VERDE + "# cambio ajeno\n", encoding="utf-8")
        se_rechaza("con un archivo versionado modificado", "prueba_verde.py")
        _git(arbol, "checkout", "--", "pruebas/demostracion/prueba_verde.py")

        # d) Un nombre con espacios y acento: también se ve entero.
        raro = arbol / "informe con espacios y ñ.txt"
        raro.write_text("x\n", encoding="utf-8")
        se_rechaza("con un archivo de nombre raro", "informe con espacios y ñ.txt")
        raro.unlink()

        # e) Una ejecución viva sobre el árbol.
        retomada = nucleo.tomar(raiz, "T-0991", trabajador_id="humano", worktree=str(arbol)) if False else None
        # (PROPUESTO no es tomable: se simula con la fila.)
        escribir_sql(
            raiz,
            "UPDATE tareas SET estado = 'en_ejecucion', trabajador_id = 'humano', "
            "pid = 1, worktree = ? WHERE id = 'T-0991'", (str(arbol),),
        )
        se_rechaza("de una ejecución viva", "EN EJECUCIÓN")
        escribir_sql(
            raiz,
            "UPDATE tareas SET estado = 'propuesto', trabajador_id = NULL, "
            "pid = NULL, worktree = NULL WHERE id = 'T-0991'",
        )

        # f) Una entrada despachada de la cola.
        escribir_sql(
            raiz,
            "UPDATE cola SET estado_cola = 'despachada' WHERE tarea_id = 'T-0991'",
        )
        se_rechaza("con una entrada despachada", "despachada")
        escribir_sql(
            raiz,
            "UPDATE cola SET estado_cola = 'terminada' WHERE tarea_id = 'T-0991'",
        )

        # g) Limpio de verdad: se retira, sin --force, y la rama conserva el
        #    commit del trabajo.
        commit = _git(raiz, "rev-parse", "tarea/T-0991").stdout.strip()
        limpieza = trabajadores.limpiar_arbol(raiz, "T-0991")
        assert limpieza["limpiado"] is True
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        assert not arbol.exists()
        assert _git(raiz, "rev-parse", "tarea/T-0991").stdout.strip() == commit
        assert str(arbol) not in _git(raiz, "worktree", "list", "--porcelain").stdout

        # h) Y la limpieza nunca pasa por `--force`: el código no lo contiene.
        fuente = (RAIZ / "orquestacion" / "ingenieria_supervisor" / "trabajadores.py").read_text(encoding="utf-8")
        assert '"--force"' not in fuente and "'--force'" not in fuente, "La limpieza usa --force."

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 11. Apagón y reinicio
# ----------------------------------------------------------------------

def prueba_11_apagon_y_reinicio_cola_y_propiedad_recuperables():
    print(" 11. apagón/reinicio simulado -> cola y propiedad recuperables:", end=" ")

    raiz = crear_repositorio("apagon_")
    procesos = []

    try:
        for identificador, prioridad in (("T-0901", 0), ("T-0902", 9), ("T-0903", 0)):
            ficha_minima(raiz, identificador)
            trabajadores.encolar(
                raiz, identificador, prioridad=prioridad,
                trabajo=trabajo_demo(identificador, "--dormir", "30"),
                tiempo_limite_s=120,
            )
            METRICAS["TAREAS_ENCOLADAS"] += 1

        # a) El trabajador de la cabeza (T-0902) se lanza y muere de golpe
        #    con su trabajo a medias: apagón.
        informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        procesos.append(informe["proceso"])
        assert informe["tarea"] == "T-0902"

        esperar_a(
            lambda: fila_de(raiz, "T-0902")["pid"] == informe["pid_trabajador"],
            descripcion="adopción del PID",
        )
        matar(informe["proceso"])
        assert not nucleo.proceso_vivo(informe["pid_trabajador"])

        # b) «Reinicio»: otro proceso lee la cola. Mismo orden, mismas
        #    entradas, la muerta sigue despachada hasta que se recupere.
        salida = cli(raiz, "cola", "--json")
        assert salida.returncode == 0, salida.stderr
        vivas = [u for u in json.loads(salida.stdout) if u["estado_cola"] in ("pendiente", "despachada")]
        assert [u["tarea_id"] for u in vivas] == ["T-0902", "T-0901", "T-0903"], vivas
        assert vivas[0]["estado_cola"] == "despachada"

        # c) Recuperación: el proceso está muerto y el latido ya no es
        #    reciente (se juzga 200 s después): huérfana, liberada, y la
        #    entrada vuelve a la cola con su secuencia.
        despues = nucleo.ahora_datetime() + timedelta(seconds=200)
        salida = cli(raiz, "reanudar")
        # Con el reloj real el latido es fresco: ACTIVA dentro del margen de
        # cortesía. No se libera nada todavía (y la consola lo dice).
        assert any(
            linea.strip().startswith("Siguen activas") and linea.strip().endswith(" 1")
            for linea in salida.stdout.splitlines()
        ), salida.stdout
        assert "DESPACHADAS QUE SIGUEN VIVAS" in salida.stdout, salida.stdout
        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.EN_EJECUCION)

        recuperacion = nucleo.reanudar(raiz, ahora=despues)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0902"], recuperacion
        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.REABIERTO)
        METRICAS["RECUPERACIONES"] += 1

        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["tarea"] for u in cola["reencoladas"]] == ["T-0902"]
        entrada = entrada_de(raiz, "T-0902")
        assert entrada["estado_cola"] == estado_global.COLA_PENDIENTE
        assert entrada["secuencia"] == informe["secuencia"]

        # El orden no cambió por el reinicio ni por la recuperación.
        assert [u["tarea_id"] for u in trabajadores.listar_cola(raiz)
                if u["estado_cola"] in estado_global.COLA_ESTADOS_VIVOS] == ["T-0902", "T-0901", "T-0903"]

        # d) Se vuelve a despachar: misma entrada, generación nueva, mismo
        #    árbol (limpio: el trabajo murió durmiendo), y esta vez termina.
        escribir_sql(
            raiz, "UPDATE cola SET trabajo = ? WHERE secuencia = ?",
            (json.dumps(trabajo_demo("T-0902")), informe["secuencia"]),
        )
        informe2, codigo2, registro2 = despachar_y_esperar(raiz)
        procesos.append(informe2["proceso"])
        assert informe2["tarea"] == "T-0902"
        assert informe2["secuencia"] == informe["secuencia"]
        assert informe2["generacion"] == 2
        assert informe2["arbol_creado"] is False
        assert codigo2 == 0, registro2[-800:]
        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.PROPUESTO)
        assert entrada_de(raiz, "T-0902")["estado_cola"] == estado_global.COLA_TERMINADA

        # e) El despacho que muere DESPUÉS de confirmar y ANTES de lanzar:
        #    la fila lleva el PID del despacho. Se recupera igual.
        informe3 = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        assert informe3["tarea"] == "T-0901"
        assert fila_de(raiz, "T-0901")["pid"] == os.getpid()

        recuperacion = nucleo.reanudar(raiz, ahora=despues, comprobar_proceso=lambda pid: False)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0901"]
        METRICAS["RECUPERACIONES"] += 1
        assert [u["tarea"] for u in trabajadores.reconciliar_cola(raiz)["reencoladas"]] == ["T-0901"]

        # f) Lo que quedó despachado a una ejecución que ya terminó por otra
        #    vía (una persona la aprobó, por ejemplo) se cierra.
        escribir_sql(
            raiz, "UPDATE cola SET estado_cola = 'despachada', trabajador_id = 'z', "
            "generacion = 7 WHERE tarea_id = 'T-0903'",
        )
        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["tarea"] for u in cola["reencoladas"]] == ["T-0903"]

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 12. Argumentos como lista, nunca shell=True
# ----------------------------------------------------------------------

def prueba_12_argv_estructurado_nunca_shell():
    print(" 12. los argumentos del trabajador viajan como lista, nunca shell=True:", end=" ")

    raiz = crear_repositorio("argv_")

    try:
        peligrosos = [
            "a b; echo PWNED > pwned.txt",
            "$(touch pwned2.txt)",
            "%TEMP%\\pwned3.txt",
            "'entre comillas'",
            '"dobles"',
            "&& rm -rf x",
            "|tuberia",
            "acento ñ y espacio ",
        ]

        # a) En este mismo proceso: cada argumento llega tal cual.
        resultado = trabajadores.correr_trabajo(
            [sys.executable, "herramientas/eco.py", *peligrosos], raiz, 30
        )
        assert resultado["codigo"] == 0, resultado
        eco = json.loads((raiz / "modulos" / "demostracion" / "eco.json").read_text(encoding="utf-8"))
        assert eco == peligrosos, eco
        for rastro in ("pwned.txt", "pwned2.txt", "pwned3.txt"):
            assert not (raiz / rastro).exists(), rastro + " apareció: algo interpretó la orden."
        (raiz / "modulos" / "demostracion" / "eco.json").unlink()

        # b) Una cadena no es un trabajo.
        for malo in ("python herramientas/eco.py", b"x"):
            try:
                trabajadores.correr_trabajo(malo, raiz, 5)
                raise AssertionError("Se ejecutó un trabajo dado como cadena.")
            except trabajadores.ErrorCola:
                pass

        try:
            trabajadores.lanzar_trabajador("python -m algo", raiz, zona(raiz) / "x.log")
            raise AssertionError("Se lanzó un trabajador con una cadena.")
        except nucleo.ErrorSupervisor:
            pass

        # c) El argv del trabajador es una lista y el trabajo va detrás de
        #    `--`, sin tocar.
        ficha_minima(raiz, "T-0901", ambito=["modulos/demostracion/*"])
        entrada = trabajadores.encolar(
            raiz, "T-0901", trabajo=[sys.executable, "herramientas/eco.py", *peligrosos],
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        assert entrada["trabajo"][2:] == peligrosos

        clase = nucleo.Ficha(id="T-0901", titulo="x")
        clase.trabajador_id = "W"
        clase.generacion = 1
        clase.worktree = "/x"
        clase.pid = 1
        argv = trabajadores.argumentos_del_trabajador(raiz, clase, entrada)
        assert isinstance(argv, list) and all(isinstance(uno, str) for uno in argv)
        assert argv[argv.index("--") + 1:] == [sys.executable, "herramientas/eco.py", *peligrosos]
        assert "-m" in argv and "orquestacion.ingenieria_supervisor.trabajador" in argv

        # d) De punta a punta, con el trabajador real: el eco es exacto.
        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-600:]
        detalle = informe_del_registro(registro)
        assert detalle["resultado"] == trabajadores.RESULTADO_VERIFICADA, detalle
        assert detalle["estado_final"] == str(Estado.PROPUESTO)
        eco = json.loads((Path(informe["worktree"]) / "modulos/demostracion/eco.json").read_text(encoding="utf-8"))
        assert eco == peligrosos, eco
        for rastro in ("pwned.txt", "pwned2.txt", "pwned3.txt"):
            assert not (Path(informe["worktree"]) / rastro).exists()
            assert not (raiz / rastro).exists()

        # e) Y el código fuente no abre esa puerta: ninguna LLAMADA lleva
        #    `shell=True`, ni pasa por `os.system`, `shlex.split` o similares.
        #    Se mira el árbol sintáctico, no el texto: la documentación del
        #    módulo nombra esas cosas justamente para prohibirlas.
        import ast

        for nombre in ("trabajadores.py", "trabajador.py"):
            fuente = (RAIZ / "orquestacion" / "ingenieria_supervisor" / nombre).read_text(encoding="utf-8")

            for nodo in ast.walk(ast.parse(fuente)):
                if not isinstance(nodo, ast.Call):
                    continue

                for clave in nodo.keywords:
                    if clave.arg == "shell":
                        assert isinstance(clave.value, ast.Constant) and clave.value.value is False, (
                            nombre + " llama con shell=" + ast.dump(clave.value)
                        )

                objetivo = nodo.func
                if isinstance(objetivo, ast.Attribute):
                    assert objetivo.attr not in (
                        "system", "getoutput", "getstatusoutput", "popen", "split",
                    ) or not (
                        isinstance(objetivo.value, ast.Name)
                        and objetivo.value.id in ("os", "subprocess", "shlex", "commands")
                    ), nombre + " usa " + ast.dump(objetivo)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 13. Consola
# ----------------------------------------------------------------------

def prueba_13_la_consola_expone_las_ordenes():
    print(" 13. la consola expone las órdenes con códigos propios:", end=" ")

    raiz = crear_repositorio("consola_")

    try:
        esperados = []

        creada = cli(
            raiz, "crear", "T-0901", "--titulo", "Tarea de consola",
            "--ambito", "modulos/demostracion/T-0901.py",
            "--prueba", "pruebas/demostracion/prueba_verde.py",
        )
        esperados.append(("crear", creada, 0))

        vacia = cli(raiz, "despachar")
        esperados.append(("despachar con la cola vacía", vacia, 7))
        assert "No hay ninguna entrada pendiente" in vacia.stdout, vacia.stdout

        encolada = cli(
            raiz, "encolar", "T-0901", "--prioridad", "3", "--trabajo",
            sys.executable, "herramientas/trabajo.py", "T-0901", "--dormir", "4",
            "con espacio; y punto y coma",
        )
        esperados.append(("encolar con trabajo tras --trabajo", encolada, 0))
        assert "Encolada: T-0901 (entrada 1, prioridad 3)" in encolada.stdout, encolada.stdout
        assert "con espacio; y punto y coma" in encolada.stdout
        METRICAS["TAREAS_ENCOLADAS"] += 1

        esperados.append(("encolar dos veces", cli(raiz, "encolar", "T-0901"), 2))
        esperados.append(("encolar una tarea inexistente", cli(raiz, "encolar", "T-0999"), 2))

        listado = cli(raiz, "cola")
        esperados.append(("cola", listado, 0))
        assert "ORDEN DE DESPACHO" in listado.stdout and "T-0901  prioridad 3  PENDIENTE" in listado.stdout, listado.stdout

        despachada = cli(raiz, "despachar")
        esperados.append(("despachar", despachada, 0))
        assert "Despachada: T-0901 (entrada 1)" in despachada.stdout, despachada.stdout
        assert "Argumentos (lista, sin intérprete)" in despachada.stdout
        assert str(zona(raiz) / "T-0901") in despachada.stdout
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1

        pid = int(despachada.stdout.split("Proceso trabajador: ")[1].splitlines()[0])

        # Con el trabajador todavía trabajando (el trabajo duerme 4 s):
        assert nucleo.proceso_vivo(pid), "El trabajador terminó antes de tiempo."
        esperados.append(("despachar de nuevo", cli(raiz, "despachar", "T-0901"), 7))
        METRICAS["DESPACHOS_RECHAZADOS"] += 1
        esperados.append(("desencolar una despachada", cli(raiz, "desencolar", "T-0901"), 2))
        viva = cli(raiz, "limpiar-arboles", "T-0901")
        esperados.append(("limpiar el árbol de una ejecución viva", viva, 6))
        assert "EN EJECUCIÓN" in viva.stdout or "despachada" in viva.stdout, viva.stdout
        METRICAS["WORKTREES_RECHAZADOS"] += 1
        assert nucleo.proceso_vivo(pid), "El trabajador terminó antes de las tres órdenes."

        esperar_a(
            lambda: not nucleo.proceso_vivo(pid)
            and entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_TERMINADA,
            espera_s=ESPERA_TRABAJADOR_S, descripcion="fin del trabajador lanzado por la consola",
        )
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.PROPUESTO)

        ver = cli(raiz, "ver", "T-0901")
        esperados.append(("ver", ver, 0))
        assert "PROPUESTO" in ver.stdout

        listado = cli(raiz, "cola", "--json")
        esperados.append(("cola --json", listado, 0))
        datos = json.loads(listado.stdout)
        assert datos[0]["estado_cola"] == "terminada" and datos[0]["resultado"]["tipo"] == "verificada"

        limpieza = cli(raiz, "limpiar-arboles")
        esperados.append(("limpiar-arboles", limpieza, 0))
        assert "retirado" in limpieza.stdout, limpieza.stdout
        METRICAS["WORKTREES_LIMPIADOS"] += 1

        reanudada = cli(raiz, "reanudar")
        esperados.append(("reanudar", reanudada, 0))
        assert "Entradas de la cola revisadas" in reanudada.stdout, reanudada.stdout

        ayuda = cli(raiz, "--ayuda")
        esperados.append(("ayuda", ayuda, 0))
        for fragmento in ("encolar", "despachar", "limpiar-arboles", "  7   despacho rechazado"):
            assert fragmento in ayuda.stdout, fragmento

        ayuda_trabajador = subprocess.run(
            [sys.executable, "-m", "orquestacion.ingenieria_supervisor.trabajador", "--ayuda"],
            cwd=str(RAIZ), capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=ESPERA_PROCESO_S,
        )
        assert ayuda_trabajador.returncode == 0 and "--pid-despacho" in ayuda_trabajador.stdout

        for caso, salida, codigo in esperados:
            assert salida.returncode == codigo, (
                "«" + caso + "» devolvió " + str(salida.returncode) + " y se esperaba "
                + str(codigo) + ". Salida: " + (salida.stdout + salida.stderr)[-600:]
            )
            assert "Traceback" not in salida.stderr, caso + ": " + salida.stderr[-600:]

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 14. Estrés entre procesos
# ----------------------------------------------------------------------

def prueba_14_estres_entre_procesos(despachadores: int, rondas: int):
    print(" 14. estrés: " + str(despachadores) + " procesos despachando a la vez, "
          + str(rondas) + " rondas:", end=" ")

    raiz = crear_repositorio("estres_")

    try:
        tareas = ["T-0901", "T-0902", "T-0903"]

        for identificador in tareas:
            ficha_minima(raiz, identificador)
            trabajadores.encolar(raiz, identificador)
            METRICAS["TAREAS_ENCOLADAS"] += 1

        secuencias = {t: entrada_de(raiz, t)["secuencia"] for t in tareas}

        for ronda in range(1, rondas + 1):
            respuestas = _carrera(raiz, None, despachadores)

            ganadoras = [r for r in respuestas if r["clase"] == "despachada"]
            por_tarea = {}

            for una in ganadoras:
                por_tarea.setdefault(una["tarea"], []).append(una)

            for identificador, lista in por_tarea.items():
                if len(lista) > 1:
                    METRICAS["DOBLES_DESPACHOS"] += len(lista) - 1

                assert len(lista) == 1, (identificador, lista)

                fila = fila_de(raiz, identificador)
                entrada = entrada_de(raiz, identificador)
                assert fila["estado"] == str(Estado.EN_EJECUCION)
                assert fila["trabajador_id"] == lista[0]["marca"] == entrada["trabajador_id"]
                assert int(fila["generacion"]) == lista[0]["generacion"] == int(entrada["generacion"])
                assert entrada["secuencia"] == secuencias[identificador]

            # Como mucho tantas ganadoras como tareas; ninguna tarea sin
            # dueño si alguien la ganó; ninguna con dos dueños.
            assert len(ganadoras) <= len(tareas)
            assert len(ganadoras) >= 1, repr(respuestas)

            en_ejecucion = [t for t in tareas if fila_de(raiz, t)["estado"] == str(Estado.EN_EJECUCION)]
            assert sorted(en_ejecucion) == sorted(por_tarea), (en_ejecucion, sorted(por_tarea))

            for identificador, lista in por_tarea.items():
                nucleo.devolver(
                    raiz, identificador, "fin de ronda " + str(ronda),
                    trabajador_id=lista[0]["marca"], generacion=lista[0]["generacion"],
                )

            reencoladas = trabajadores.reconciliar_cola(raiz)["reencoladas"]
            assert sorted(u["tarea"] for u in reencoladas) == sorted(por_tarea)
            METRICAS["RECUPERACIONES"] += len(reencoladas)

            comprobar_integridad(raiz)

        assert arboles_en_zona(raiz) == tareas
        limpieza = trabajadores.limpiar_arboles(raiz)
        assert len(limpieza["limpiados"]) == 3 and not limpieza["rechazados"], limpieza
        METRICAS["WORKTREES_LIMPIADOS"] += 3
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# Corrida
# ----------------------------------------------------------------------

def imprimir_metricas() -> None:
    print("")
    print("  Métricas reales de esta corrida")
    print("  -------------------------------")

    for clave in (
        "TAREAS_ENCOLADAS",
        "DESPACHOS_ACEPTADOS",
        "DESPACHOS_RECHAZADOS",
        "DOBLES_DESPACHOS",
        "CONFLICTOS_DE_AMBITO_DETECTADOS",
        "WORKTREES_CREADOS",
        "WORKTREES_LIMPIADOS",
        "WORKTREES_RECHAZADOS",
        "RECUPERACIONES",
        "CASOS_DUDOSOS_ESCALADOS",
        "ERRORES_SQLITE",
        "EXCEPCIONES_INESPERADAS",
        "FALLOS_INTEGRIDAD",
        "COMPROBACIONES_INTEGRIDAD",
    ):
        print("  " + clave.ljust(36) + " = " + str(METRICAS[clave]))

    print("")


def prueba_workers_v1(despachadores: int, rondas: int) -> None:
    print("")
    print("PRUEBA: trabajadores V1 (T-0003)")
    print("")

    inicio = time.monotonic()

    prueba_01_cola_persistente_y_orden_determinista()
    prueba_02_dos_procesos_despachan_la_misma_tarea(despachadores, rondas)
    prueba_03_ambitos_solapados_solo_una_con_escritor(despachadores)
    prueba_04_sin_solapamiento_ambas_progresan()
    prueba_05_doble_lanzamiento_rechazado()
    prueba_06_trabajador_termina_bien_estado_consistente()
    prueba_07_trabajador_falla_recuperacion_consistente()
    prueba_08_duda_no_se_libera()
    prueba_09_worktree_fuera_de_zona_rechazado()
    prueba_10_borrar_worktree_con_cambios_ajenos_rechazado()
    prueba_11_apagon_y_reinicio_cola_y_propiedad_recuperables()
    prueba_12_argv_estructurado_nunca_shell()
    prueba_13_la_consola_expone_las_ordenes()
    prueba_14_estres_entre_procesos(despachadores, rondas)

    duracion = time.monotonic() - inicio

    imprimir_metricas()

    if OMITIDAS:
        print("")
        print("  Casos OMITIDOS en esta corrida (el entorno no los admite)")
        print("  --------------------------------------------------------")
        for omitida in OMITIDAS:
            print("      · " + omitida)

    # Cotas mínimas: una corrida que no hiciera nada no puede salir verde.
    minimos = {
        "TAREAS_ENCOLADAS": 25,
        "DESPACHOS_ACEPTADOS": 20,
        "DESPACHOS_RECHAZADOS": 15,
        "CONFLICTOS_DE_AMBITO_DETECTADOS": 2,
        "WORKTREES_CREADOS": 12,
        "WORKTREES_LIMPIADOS": 8,
        "WORKTREES_RECHAZADOS": 8,
        "RECUPERACIONES": 5,
        "CASOS_DUDOSOS_ESCALADOS": 3,
        "COMPROBACIONES_INTEGRIDAD": 12,
    }

    for clave, minimo in minimos.items():
        assert METRICAS[clave] >= minimo, (
            "La corrida hizo menos trabajo del que debería: " + clave + " = "
            + str(METRICAS[clave]) + ", se esperaban al menos " + str(minimo) + "."
        )

    for clave in (
        "DOBLES_DESPACHOS",
        "ERRORES_SQLITE",
        "EXCEPCIONES_INESPERADAS",
        "FALLOS_INTEGRIDAD",
    ):
        assert METRICAS[clave] == 0, clave + " = " + str(METRICAS[clave]) + ", y tiene que ser 0."

    print("  Tiempo: " + str(round(duracion, 2)) + " s")
    print("")
    print("PRUEBA_WORKERS_V1=OK")


def principal() -> int:
    analizador = argparse.ArgumentParser(description="Pruebas de Workers V1 (T-0003).")
    analizador.add_argument("--rondas", type=int, default=RONDAS_POR_OMISION)
    analizador.add_argument("--despachadores", type=int, default=DESPACHADORES_POR_OMISION)

    argumentos = analizador.parse_args()

    prueba_workers_v1(argumentos.despachadores, argumentos.rondas)

    return 0


if __name__ == "__main__":
    sys.exit(principal())
