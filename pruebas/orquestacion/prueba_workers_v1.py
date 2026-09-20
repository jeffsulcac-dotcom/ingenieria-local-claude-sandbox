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
14. estrés: varios procesos despachando a la vez, varias rondas;
15-27. lo que dejó la auditoría R1: transición y cierre en una sola
    transacción, adopción exclusiva con reintento, lanzamiento fallido,
    ficha ilegible que no para la cola, reconciliación con otro dueño o
    con el proceso vivo, árbol que desaparece, enlaces en la zona, lo que
    Git podría esconder, nietos y señales, entorno y argv, árbol roto,
    orden entre vivas y restos, base de la rama y commit inicial;
28-36. lo que dejó la auditoría R2: un trabajo que sobrevive a su
    trabajador, el respaldo del trabajador ante un candado, el espejo
    JSON que falla tras el COMMIT, la duda que no reencola ni cierra y
    `desencolar` como salida, la limpieza compitiendo de verdad con el
    despacho, el gancho de la toma, la huella de la raíz, las señales
    en las ventanas que quedaban, la poda dirigida, la zona enlazada, el
    candado traducido y el ejecutable resuelto.

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
import shutil
import signal
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
from ingenieria_supervisor import supervisor as nucleo
from ingenieria_supervisor import tarea as fichas
from ingenieria_supervisor import trabajador
from ingenieria_supervisor import trabajadores


PRUEBA_VERDE = "print('PRUEBA_VERDE=OK')\n"

# El trabajo de demostración: escribe el archivo de la tarea dentro de su
# ámbito, opcionalmente uno FUERA, opcionalmente falla o duerme, y confirma.
TRABAJO_DEMO = '''\
import argparse, json, os, pathlib, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument("tarea")
p.add_argument("--fuera")
p.add_argument("--fallar", type=int, default=0)
p.add_argument("--dormir", type=float, default=0.0)
p.add_argument("--sin-commit", action="store_true")
p.add_argument("--esperar", help="archivo cuya aparicion se espera (tope 60 s)")
p.add_argument("--nieto", action="store_true", help="lanza un nieto que duerme 60 s")
p.add_argument("--senales", help="carpeta FUERA del arbol donde dejar los PID")
p.add_argument("extras", nargs="*")
a = p.parse_intermixed_args()
if a.nieto:
    nieto = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if a.senales:
        pathlib.Path(a.senales, "nieto.pid").write_text(str(nieto.pid), encoding="utf-8")
if a.senales:
    pathlib.Path(a.senales, "trabajo.pid").write_text(str(os.getpid()), encoding="utf-8")
if a.esperar:
    limite = time.monotonic() + 60
    while not pathlib.Path(a.esperar).exists() and time.monotonic() < limite:
        time.sleep(0.05)
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
        "ambito_archivos": (
            ambito if ambito is not None
            else ["modulos/demostracion/" + identificador + ".py"]
        ),
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


def matar_trabajo_de(raiz: Path, tarea: str) -> None:
    """El trabajo corre en OTRA sesión que su trabajador: se busca su PID en
    la entrada de la cola y se mata su grupo."""
    try:
        pid = entrada_de(raiz, tarea).get("pid_trabajo")
    except Exception:
        return

    if not pid:
        return

    try:
        if os.name != "nt":
            os.killpg(int(pid), signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    except OSError:
        pass


def ejecutar_en_proceso(raiz: Path, despacho: dict, trabajo=None, **extras) -> dict:
    """El cuerpo del trabajador en ESTE proceso, con la credencial del
    despacho (que fue `lanzar=False`, así que el PID de la fila es el nuestro)."""
    if trabajo is None:
        trabajo = list(entrada_de(raiz, despacho["tarea"])["trabajo"] or [])

    parametros = dict(
        tiempo_limite_s=30, intervalo_latido_s=INTERVALO_LATIDO_S, pid_despacho=os.getpid(),
    )
    parametros.update(extras)

    return trabajadores.ejecutar_trabajador(
        raiz, despacho["tarea"], despacho["trabajador_id"], despacho["generacion"],
        despacho["secuencia"], despacho["worktree"], trabajo, **parametros,
    )


def matar(proceso, raiz: Path | None = None, tarea: str | None = None) -> None:
    """Mata al trabajador y, si se dice de qué tarea es, a su trabajo (que
    corre en otra sesión)."""
    if raiz is not None and tarea is not None:
        matar_trabajo_de(raiz, tarea)

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
                        trabajadores.RECHAZO_ARBOL,
                    ), perdedora

            # El ganador suelta la tarea; la entrada vuelve a la cola con la
            # MISMA secuencia en la siguiente reconciliación.
            nucleo.devolver(
                raiz, "T-0911", "fin de ronda",
                trabajador_id=ganadora["marca"], generacion=ganadora["generacion"],
            )

            reconciliado = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
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
        trabajadores._por_que_no_se_despacha = lambda *a, **k: None

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
    senales = Path(tempfile.mkdtemp(prefix="senales_"))
    procesos = []

    try:
        base = _git(raiz, "rev-parse", "HEAD").stdout.strip()
        tareas = ["T-0931", "T-0932", "T-0933"]

        for identificador in tareas:
            ficha_minima(raiz, identificador)
            trabajadores.encolar(
                raiz, identificador,
                trabajo=trabajo_demo(
                    identificador, "--esperar", str(senales / (identificador + ".continuar")),
                    "extra " + identificador,
                ),
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

        # La cola lleva el PID del trabajador que adoptó, no el del
        # despacho, y dice cuándo adoptó.
        for informe in informes:
            entrada = entrada_de(raiz, informe["tarea"])
            assert entrada["pid"] == informe["pid_trabajador"], entrada
            assert entrada["adoptada"] is True and entrada["adoptado_en"]

        # Con los tres esperando, hay tiempo de sobra para varios latidos.
        time.sleep(0.6)

        for informe in informes:
            (senales / (informe["tarea"] + ".continuar")).write_text("", encoding="utf-8")

        for informe in informes:
            codigo = informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S)
            registro = Path(informe["registro"]).read_text(encoding="utf-8")
            assert codigo == 0, registro[-800:]

            detalle = informe_del_registro(registro)
            assert detalle["resultado"] == trabajadores.RESULTADO_VERIFICADA
            assert detalle["estado_final"] == str(Estado.PROPUESTO)
            assert detalle["latidos"] >= 2, detalle["latidos"]
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
            # El trabajo corrió con el entorno del ÁRBOL, no del Supervisor.
            assert informe["rama_creada_desde"] == "main", informe["rama_creada_desde"]

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
        borrar(senales)

    print("OK")


# ----------------------------------------------------------------------
# 5. Doble lanzamiento accidental
# ----------------------------------------------------------------------

def prueba_05_doble_lanzamiento_rechazado():
    print("  5. un doble lanzamiento accidental -> rechazado:", end=" ")

    raiz = crear_repositorio("doble_")
    senales = Path(tempfile.mkdtemp(prefix="senales_"))
    procesos = []

    try:
        ficha_minima(raiz, "T-0941")
        trabajadores.encolar(
            raiz, "T-0941",
            trabajo=trabajo_demo("T-0941", "--esperar", str(senales / "continuar")),
        )
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

        (senales / "continuar").write_text("", encoding="utf-8")
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
        borrar(senales)

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
        eventos = list(reversed(eventos_de(raiz, "T-0951")))
        tipos = [e["tipo"] for e in eventos]
        assert tipos == [
            estado_global.EVENTO_CREACION,
            estado_global.EVENTO_COLA,         # encolada
            estado_global.EVENTO_TRANSICION,   # tomada
            estado_global.EVENTO_COLA,         # despachada (mismo COMMIT que la toma)
            estado_global.EVENTO_VERIFICACION,
            estado_global.EVENTO_TRANSICION,   # propuesta
            estado_global.EVENTO_COLA,         # cerrada (mismo COMMIT que la propuesta)
        ], tipos
        assert all(e["estado_nuevo"] for e in eventos if e["tipo"] == estado_global.EVENTO_COLA), eventos

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

        assert trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)["revisadas"] == 0

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
            cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
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
        propio = socket.gethostname() + "/1/abc"
        escribir_sql(raiz, "UPDATE tareas SET trabajador_id = ? WHERE id = 'T-0971'", (propio,))
        escribir_sql(raiz, "UPDATE cola SET trabajador_id = ? WHERE secuencia = ?", (propio, secuencia))

        recuperacion = nucleo.reanudar(raiz, comprobar_proceso=lambda pid: False)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0971"], recuperacion
        assert fila_de(raiz, "T-0971")["estado"] == str(Estado.REABIERTO)

        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [secuencia]
        entrada = entrada_de(raiz, "T-0971")
        assert entrada["estado_cola"] == estado_global.COLA_PENDIENTE
        assert entrada["trabajador_id"] is None and entrada["generacion"] is None
        assert "reencolada" in str(entrada["ultimo_rechazo"])
        METRICAS["RECUPERACIONES"] += 1

        # Idempotente: una segunda pasada no cambia nada.
        assert trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)["reencoladas"] == []
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

        assert [u["tarea"] for u in trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)["reencoladas"]] == ["T-0983"]
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

        # e) Una ejecución viva sobre el árbol (PROPUESTO no es tomable:
        #    se simula con la fila).
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
        esperar_a(
            lambda: bool(entrada_de(raiz, "T-0902").get("pid_trabajo")),
            descripcion="PID del trabajo en la entrada",
        )
        # Apagón: mueren el trabajador y su trabajo (que corre en otra sesión).
        matar(informe["proceso"], raiz, "T-0902")
        assert not nucleo.proceso_vivo(informe["pid_trabajador"])
        esperar_a(
            lambda: not nucleo.proceso_vivo(entrada_de(raiz, "T-0902")["pid_trabajo"]),
            espera_s=10, descripcion="muerte del trabajo en el apagón",
        )

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

        # Con la comprobación REAL: el trabajador está muerto de verdad.
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
        assert informe2["registro"] != informe["registro"], "Dos lanzamientos comparten registro."
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
        assert [u["tarea"] for u in trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)["reencoladas"]] == ["T-0901"]

        # f) Lo que quedó despachado a una ejecución que ya terminó por otra
        #    vía (una persona la aprobó, por ejemplo) se cierra.
        escribir_sql(
            raiz, "UPDATE cola SET estado_cola = 'despachada', trabajador_id = 'z', "
            "generacion = 7 WHERE tarea_id = 'T-0903'",
        )
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
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
        assert all("=" in uno for uno in argv[3:argv.index("--")]), argv

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
    senales = Path(tempfile.mkdtemp(prefix="senales_"))

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
            sys.executable, "herramientas/trabajo.py", "T-0901",
            "--esperar", str(senales / "continuar"), "con espacio; y punto y coma",
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

        # Con el trabajador todavía trabajando (el trabajo espera el semáforo):
        assert nucleo.proceso_vivo(pid), "El trabajador terminó antes de tiempo."
        esperados.append(("despachar de nuevo", cli(raiz, "despachar", "T-0901"), 7))
        METRICAS["DESPACHOS_RECHAZADOS"] += 1
        esperados.append(("desencolar una despachada", cli(raiz, "desencolar", "T-0901"), 2))
        viva = cli(raiz, "limpiar-arboles", "T-0901")
        esperados.append(("limpiar el árbol de una ejecución viva", viva, 6))
        assert "EN EJECUCIÓN" in viva.stdout or "despachada" in viva.stdout, viva.stdout
        METRICAS["WORKTREES_RECHAZADOS"] += 1
        assert nucleo.proceso_vivo(pid), "El trabajador terminó antes de las tres órdenes."
        (senales / "continuar").write_text("", encoding="utf-8")

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
        borrar(senales)

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

            reencoladas = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)["reencoladas"]
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
# 15. Transición y cierre de la entrada en UNA transacción (R1)
# ----------------------------------------------------------------------

def _con_rastro_sql(funcion):
    """Ejecuta `funcion()` registrando cada sentencia SQL de este proceso."""
    sentencias = []
    conectar = sqlite3.connect

    def vigilado(*argumentos, **claves):
        con = conectar(*argumentos, **claves)
        con.set_trace_callback(lambda s: sentencias.append(str(s)))
        return con

    sqlite3.connect = vigilado

    try:
        resultado = funcion()
    finally:
        sqlite3.connect = conectar

    return resultado, sentencias


def _misma_transaccion(sentencias, primera, segunda) -> bool:
    """Si la primera sentencia que contiene `segunda` cae entre el mismo
    BEGIN y COMMIT que alguna que contiene `primera`."""
    indice = next(
        (i for i, s in enumerate(sentencias) if segunda in s.upper()), None
    )
    assert indice is not None, "No se ejecutó: " + segunda

    inicio = max(i for i, s in enumerate(sentencias[:indice]) if s.strip().upper().startswith("BEGIN"))
    fin = next(
        i for i, s in enumerate(sentencias) if i > indice and s.strip().upper() in ("COMMIT", "ROLLBACK")
    )

    return any(primera in s.upper() for s in sentencias[inicio:fin])


def prueba_15_la_transicion_y_el_cierre_van_juntos():
    print(" 15. la transición de la tarea y el cierre de su entrada van en una transacción:", end=" ")

    raiz = crear_repositorio("atomico_")
    procesos = []

    try:
        # a) Determinista, en este proceso: el UPDATE de `tareas` (devolver)
        #    y el de `cola` (cierre) comparten BEGIN y COMMIT.
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901", trabajo=trabajo_demo("T-0901", "--fallar", "3"))
        METRICAS["TAREAS_ENCOLADAS"] += 1

        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])

        def correr():
            return trabajadores.ejecutar_trabajador(
                raiz, "T-0901", despacho["trabajador_id"], despacho["generacion"],
                despacho["secuencia"], despacho["worktree"],
                trabajo_demo("T-0901", "--fallar", "3"), tiempo_limite_s=30,
                intervalo_latido_s=INTERVALO_LATIDO_S, pid_despacho=os.getpid(),
            )

        informe, sentencias = _con_rastro_sql(correr)

        assert informe["resultado"] == trabajadores.RESULTADO_TRABAJO_FALLIDO, informe
        assert informe["entrada_cerrada"] is True
        assert _misma_transaccion(sentencias, "UPDATE TAREAS SET", "TERMINADO_EN ="), (
            "El cierre de la entrada no va en la transacción de la transición."
        )
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_FALLIDA

        # También al verificar: el UPDATE de la propuesta y el cierre juntos.
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(raiz, "T-0902", trabajo=trabajo_demo("T-0902"))
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho2 = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho2["arbol_creado"])

        informe2, sentencias2 = _con_rastro_sql(lambda: trabajadores.ejecutar_trabajador(
            raiz, "T-0902", despacho2["trabajador_id"], despacho2["generacion"],
            despacho2["secuencia"], despacho2["worktree"], trabajo_demo("T-0902"),
            tiempo_limite_s=30, intervalo_latido_s=INTERVALO_LATIDO_S, pid_despacho=os.getpid(),
        ))
        assert informe2["resultado"] == trabajadores.RESULTADO_VERIFICADA, informe2
        assert informe2["entrada_cerrada"] is True
        assert _misma_transaccion(sentencias2, "UPDATE TAREAS SET", "TERMINADO_EN =")
        entrada2 = entrada_de(raiz, "T-0902")
        assert entrada2["estado_cola"] == estado_global.COLA_TERMINADA
        assert entrada2["resultado"]["estado"] == str(Estado.PROPUESTO)
        assert entrada2["resultado"]["verificacion"]["resultado"] == "APROBADO", entrada2["resultado"]

        # b) Con procesos reales y una reconciliación martilleando: NINGÚN
        #    trabajo fallido vuelve a la cola ni pierde su resultado.
        tareas = ["T-0911", "T-0912", "T-0913", "T-0914"]

        for identificador in tareas:
            ficha_minima(raiz, identificador)
            trabajadores.encolar(raiz, identificador, trabajo=trabajo_demo(identificador, "--fallar", "3"))
            METRICAS["TAREAS_ENCOLADAS"] += 1

        parar = threading.Event()
        vueltas = [0]

        def martillo():
            while not parar.is_set():
                try:
                    trabajadores.reconciliar_cola(raiz)
                    vueltas[0] += 1
                except (trabajadores.ErrorCola, estado_global.ErrorEstadoGlobal):
                    pass

        hilo = threading.Thread(target=martillo, daemon=True)
        hilo.start()

        try:
            informes = []

            for _ in tareas:
                informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
                METRICAS["DESPACHOS_ACEPTADOS"] += 1
                METRICAS["WORKTREES_CREADOS"] += int(informe["arbol_creado"])
                informes.append(informe)
                procesos.append(informe["proceso"])

            for informe in informes:
                assert informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S) == 1
        finally:
            parar.set()
            hilo.join(timeout=10)

        assert vueltas[0] >= 5, "La reconciliación apenas corrió: " + str(vueltas[0])

        for identificador in tareas:
            entrada = entrada_de(raiz, identificador)
            assert entrada["estado_cola"] == estado_global.COLA_FALLIDA, (identificador, entrada["estado_cola"])
            assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_TRABAJO_FALLIDO
            assert entrada["resultado"]["trabajo"]["codigo"] == 3
            assert fila_de(raiz, identificador)["estado"] == str(Estado.REABIERTO)
            assert len(entradas_de(raiz, identificador)) == 1, "Se relanzó un trabajo fallido."

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 16. Adopción: exclusiva, con reintento ante la base bloqueada
# ----------------------------------------------------------------------

def prueba_16_la_adopcion_es_exclusiva_y_aguanta_un_candado():
    print(" 16. la adopción es exclusiva y aguanta la base bloqueada:", end=" ")

    raiz = crear_repositorio("adopcion_")

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])

        credencial = (despacho["trabajador_id"], despacho["generacion"])

        # a) Sin el PID del despacho no se adopta (ni por la API ni por el
        #    cuerpo del trabajador): la exclusividad no es opcional.
        for malo in (None, 0, -1, True):
            try:
                nucleo.adoptar(raiz, "T-0901", *credencial, pid_anterior=malo)
                raise AssertionError("Se adoptó sin PID del despacho: " + repr(malo))
            except nucleo.ErrorSupervisor as rechazo:
                # De la GUARDA, no de otra precondición que casualmente no case.
                assert "pid_anterior" in str(rechazo), (malo, str(rechazo))

        try:
            nucleo.adoptar(raiz, "T-0901", None, None, pid_anterior=os.getpid())
            raise AssertionError("Se adoptó con credencial implícita.")
        except nucleo.ErrorSupervisor:
            pass

        informe = trabajadores.ejecutar_trabajador(
            raiz, "T-0901", *credencial, despacho["secuencia"], despacho["worktree"], [],
            tiempo_limite_s=5, intervalo_latido_s=INTERVALO_LATIDO_S, pid_despacho=None,
        )
        assert informe["adoptada"] is False and "pid-despacho" in informe["detalle"], informe
        assert fila_de(raiz, "T-0901")["pid"] == os.getpid()

        # b) Con un PID del despacho que no es el de la fila: rechazada.
        try:
            nucleo.adoptar(raiz, "T-0901", *credencial, pid_anterior=os.getpid() + 100000)
            raise AssertionError("Se adoptó con otro PID de despacho.")
        except nucleo.ErrorPropiedad as rechazo:
            assert rechazo.motivo == estado_global.MOTIVO_PRECONDICION_CAMBIADA, rechazo.motivo

        # c) La base bloqueada más tiempo que `busy_timeout`: la adopción
        #    reintenta y entra; no dice «no es mía».
        # El candado se toma y se suelta desde el MISMO hilo (SQLite lo
        # exige), y se avisa cuando ya está tomado.
        tomado = threading.Event()

        def retener():
            candado = estado_global.abrir(estado_global.ruta_base(raiz))
            try:
                candado.execute("BEGIN IMMEDIATE")
                tomado.set()
                time.sleep(estado_global.BUSY_TIMEOUT_MS / 1000 + 1.5)
                candado.execute("ROLLBACK")
            finally:
                candado.close()

        hilo = threading.Thread(target=retener, daemon=True)
        hilo.start()
        assert tomado.wait(timeout=10), "El candado no se llegó a tomar."

        inicio = time.monotonic()
        try:
            informe = trabajadores.ejecutar_trabajador(
                raiz, "T-0901", *credencial, despacho["secuencia"], despacho["worktree"], [],
                tiempo_limite_s=30, intervalo_latido_s=INTERVALO_LATIDO_S,
                pid_despacho=os.getpid(),
            )
        finally:
            hilo.join(timeout=20)

        assert time.monotonic() - inicio >= estado_global.BUSY_TIMEOUT_MS / 1000, "No hubo espera."
        assert informe["adoptada"] is True, informe
        assert informe["resultado"] == trabajadores.RESULTADO_VERIFICADA
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.PROPUESTO)
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_TERMINADA

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 17. Lo que falla después de la toma no deja una ejecución sin nadie
# ----------------------------------------------------------------------

def prueba_17_un_lanzamiento_fallido_devuelve_la_tarea():
    print(" 17. un lanzamiento fallido tras la toma devuelve la tarea y cierra la entrada:", end=" ")

    raiz = crear_repositorio("lanzamiento_")

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1

        try:
            trabajadores.despachar(raiz, ejecutable="/no/existe/este/python")
            raise AssertionError("Se despachó con un intérprete inexistente.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_LANZAMIENTO, rechazo.motivo
            assert [u["motivo"] for u in rechazo.rechazos] == [trabajadores.RECHAZO_LANZAMIENTO]

        METRICAS["WORKTREES_CREADOS"] += 1

        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.REABIERTO), fila["estado"]
        assert fila["trabajador_id"] is None and fila["pid"] is None
        entrada = entrada_de(raiz, "T-0901")
        assert entrada["estado_cola"] == estado_global.COLA_FALLIDA
        assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_NO_LANZADO
        assert "no se pudo lanzar" in entrada["resultado"]["motivo"].lower()
        assert nucleo.reanudar(raiz)["revisadas"] == 0

        # Y con el intérprete de verdad, la misma tarea sale.
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-600:]
        assert informe["arbol_creado"] is False

        # Con DOS entradas y el lanzamiento fallando sólo en la primera, el
        # despacho devuelve la primera y sigue con la segunda.
        nucleo.reabrir(raiz, "T-0901")
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0902")
        METRICAS["TAREAS_ENCOLADAS"] += 2
        original = trabajadores.lanzar_trabajador
        lanzamientos = []

        def falla_el_primero(argv, raiz_, registro_):
            lanzamientos.append(argv)

            if len(lanzamientos) == 1:
                raise OSError("intérprete inexistente (simulado)")

            return original(argv, raiz_, registro_)

        trabajadores.lanzar_trabajador = falla_el_primero

        try:
            informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
        finally:
            trabajadores.lanzar_trabajador = original

        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["DESPACHOS_RECHAZADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(informe["arbol_creado"])
        assert informe["tarea"] == "T-0902", informe
        assert [(u["tarea"], u["motivo"]) for u in informe["rechazos"]] == [("T-0901", trabajadores.RECHAZO_LANZAMIENTO)], informe["rechazos"]
        assert len(lanzamientos) == 2
        informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S)
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_FALLIDA
        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.PROPUESTO)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 18. Una cabeza envenenada no para la cola
# ----------------------------------------------------------------------

def prueba_18_una_ficha_invalida_no_para_la_cola():
    print(" 18. una entrada con ficha ilegible o sin ámbito no para la cola:", end=" ")

    raiz = crear_repositorio("envenenada_")

    try:
        ficha_minima(raiz, "T-0901")
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(raiz, "T-0901", prioridad=9)
        trabajadores.encolar(raiz, "T-0902")
        METRICAS["TAREAS_ENCOLADAS"] += 2

        # a) La ficha de la cabeza se corrompe después de encolarla.
        fichas.ruta_ficha(raiz, "T-0901").write_text("{{ esto no es json", encoding="utf-8")

        listado = {u["tarea_id"]: u for u in trabajadores.listar_cola(raiz)}
        assert listado["T-0901"]["despachable"] is False
        assert "no se puede leer" in listado["T-0901"]["por_que_no"], listado["T-0901"]

        informe = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(informe["arbol_creado"])
        assert informe["tarea"] == "T-0902", informe["tarea"]
        assert [u["motivo"] for u in informe["rechazos"]] == [trabajadores.RECHAZO_FICHA], informe["rechazos"]
        METRICAS["DESPACHOS_RECHAZADOS"] += 1

        cabeza = entrada_de(raiz, "T-0901")
        assert cabeza["estado_cola"] == estado_global.COLA_PENDIENTE
        assert cabeza["ultimo_rechazo"]["motivo"] == trabajadores.RECHAZO_FICHA, cabeza["ultimo_rechazo"]
        assert arboles_en_zona(raiz) == ["T-0902"], "Se creó un árbol para una ficha ilegible."

        salida = cli(raiz, "despachar")
        assert salida.returncode == 7 and "ficha" in salida.stdout.lower(), salida.stdout
        METRICAS["DESPACHOS_RECHAZADOS"] += 1

        nucleo.devolver(raiz, "T-0902", "hecho", trabajador_id=informe["trabajador_id"], generacion=informe["generacion"])

        # b) Sin ámbito, aprobada o con prioridad imposible: no se encola.
        ficha_minima(raiz, "T-0903", ambito=[])
        try:
            trabajadores.encolar(raiz, "T-0903")
            raise AssertionError("Se encoló una ficha sin ámbito.")
        except trabajadores.ErrorCola as choque:
            assert "ámbito" in str(choque)

        try:
            trabajadores.encolar(raiz, "T-0902", prioridad=2 ** 63)
            raise AssertionError("Se encoló con una prioridad de más de 64 bits.")
        except trabajadores.ErrorCola:
            pass

        ficha_minima(raiz, "T-0905")
        escribir_sql(raiz, "UPDATE tareas SET estado = 'aprobado' WHERE id = 'T-0905'")
        try:
            trabajadores.encolar(raiz, "T-0905")
            raise AssertionError("Se encoló una tarea aprobada.")
        except trabajadores.ErrorCola as choque:
            assert "aprobada" in str(choque), str(choque)

        # c) Un ámbito no relativo declarado después de encolar: rechazo
        #    propio, sin árbol y sin parar la cola.
        ficha_minima(raiz, "T-0904")
        trabajadores.encolar(raiz, "T-0904")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        datos = json.loads(fichas.ruta_ficha(raiz, "T-0904").read_text(encoding="utf-8"))
        datos["ambito_archivos"] = ["/etc/absoluto.py"]
        fichas.ruta_ficha(raiz, "T-0904").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")

        try:
            trabajadores.despachar(raiz, "T-0904", lanzar=False)
            raise AssertionError("Se despachó una ficha con ámbito absoluto.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_FICHA, rechazo.motivo
            assert "relativos" in str(rechazo), str(rechazo)

        assert "T-0904" not in arboles_en_zona(raiz)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 19. Reconciliación: otro propietario reencola; un proceso vivo frena
# ----------------------------------------------------------------------

def prueba_19_la_reconciliacion_no_pierde_trabajos_ni_reencola_con_el_proceso_vivo():
    print(" 19. la reconciliación reencola si otro tomó la tarea y NO si el proceso sigue vivo:", end=" ")

    raiz = crear_repositorio("reconciliar_")

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        secuencia = despacho["secuencia"]

        # a) La recuperación libera la ejecución (despacho muerto) y una
        #    persona toma la tarea a mano ANTES de reconciliar: la entrada
        #    vuelve a la cola (el trabajo encolado no se hizo), no se cierra.
        despues = nucleo.ahora_datetime() + timedelta(seconds=200)
        recuperacion = nucleo.reanudar(raiz, ahora=despues, comprobar_proceso=lambda pid: False)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0901"]
        METRICAS["RECUPERACIONES"] += 1

        manual = nucleo.tomar(raiz, "T-0901", trabajador_id="humano-manual")
        assert manual.generacion == 2

        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [secuencia], cola
        assert cola["cerradas"] == []
        entrada = entrada_de(raiz, "T-0901")
        assert entrada["estado_cola"] == estado_global.COLA_PENDIENTE
        assert entrada["trabajador_id"] is None

        # Y se saltará mientras dure la ejecución manual.
        listado = {u["tarea_id"]: u for u in trabajadores.listar_cola(raiz)}
        assert listado["T-0901"]["despachable"] is False
        assert "en_ejecucion" in listado["T-0901"]["por_que_no"]

        nucleo.devolver(raiz, "T-0901", "hecho", trabajador_id="humano-manual", generacion=2)

        # b) Entrada despachada cuya tarea ya no es suya, pero cuyo proceso
        #    trabajador (el PID de la entrada) sigue vivo AQUÍ: no se
        #    reencola, se escala.
        escribir_sql(
            raiz,
            "UPDATE cola SET estado_cola = 'despachada', trabajador_id = ?, generacion = 7, "
            "pid = ?, adoptado_en = 'x' WHERE secuencia = ?",
            (socket.gethostname() + "/1/vivo", os.getpid(), secuencia),
        )

        # Con la comprobación REAL: el PID es el de este proceso, vivo.
        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["secuencia"] for u in cola["vivas_sin_tarea"]] == [secuencia], cola
        assert cola["reencoladas"] == []
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_DESPACHADA
        METRICAS["CASOS_DUDOSOS_ESCALADOS"] += 1

        salida = cli(raiz, "reanudar")
        assert salida.returncode == 1, salida.stdout
        assert "CON DUDA" in salida.stdout and "(trabajador) sigue vivo" in salida.stdout, salida.stdout

        # Con el proceso demostrado muerto, sí vuelve a la cola.
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [secuencia]
        METRICAS["RECUPERACIONES"] += 1

        # c) Un despacho que muere después del COMMIT deja la entrada sin
        #    adoptar, y `cola` lo dice.
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        listado = {u["tarea_id"]: u for u in trabajadores.listar_cola(raiz)}
        assert listado["T-0901"]["adoptada"] is False
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 20. El árbol que desaparece entre validar y tomar; limpieza con candado
# ----------------------------------------------------------------------

def prueba_20_la_toma_no_se_concede_sobre_un_arbol_que_desaparecio():
    print(" 20. un árbol que desaparece antes de la toma se rechaza, y la limpieza decide con el candado:", end=" ")

    raiz = crear_repositorio("desaparece_")

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1

        # `tomar` valida el árbol ANTES de pedir el candado; entre esa
        # validación y la transacción una limpieza lo retira. Se simula
        # retirándolo justo después de la validación de `tomar`.
        original = nucleo.resolver_worktree
        estado = {"borrado": False}

        def validar_y_borrar(raiz_, declarado):
            arbol = original(raiz_, declarado)
            if not estado["borrado"] and trabajadores.dentro_de_zona(raiz_, arbol):
                estado["borrado"] = True
                _git(raiz_, "worktree", "remove", str(arbol))
            return arbol

        nucleo.resolver_worktree = validar_y_borrar

        try:
            trabajadores.despachar(raiz, "T-0901", lanzar=False)
            raise AssertionError("Se tomó la tarea sobre un árbol borrado.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_ARBOL, rechazo.motivo
            assert "desapareció" in str(rechazo), str(rechazo)
        finally:
            nucleo.resolver_worktree = original

        assert estado["borrado"], "La simulación no llegó a la validación de la toma."

        METRICAS["WORKTREES_CREADOS"] += 1
        METRICAS["WORKTREES_RECHAZADOS"] += 1

        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.NUEVO) and int(fila["generacion"]) == 0
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_PENDIENTE

        # Sin la manipulación, el siguiente despacho recrea el árbol.
        despacho = trabajadores.despachar(raiz, "T-0901", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert despacho["arbol_creado"] is True
        METRICAS["WORKTREES_CREADOS"] += 1
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        trabajadores.desencolar(raiz, "T-0901")

        # La limpieza decide y borra con el candado de escritura tomado:
        # `git worktree remove` corre entre BEGIN IMMEDIATE y COMMIT.
        git_original = trabajadores._git
        marcas = []

        def git_vigilado(raiz_, *argumentos, **claves):
            if argumentos[:2] == ("worktree", "remove"):
                marcas.append("<<remove>>")
            return git_original(raiz_, *argumentos, **claves)

        trabajadores._git = git_vigilado
        sentencias = []
        conectar = sqlite3.connect

        def vigilado(*a, **k):
            con = conectar(*a, **k)
            con.set_trace_callback(lambda s: (sentencias.append(str(s)), marcas.append(str(s))))
            return con

        sqlite3.connect = vigilado

        try:
            resultado = trabajadores.limpiar_arbol(raiz, "T-0901")
        finally:
            sqlite3.connect = conectar
            trabajadores._git = git_original

        assert resultado["limpiado"] is True
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        posicion = marcas.index("<<remove>>")
        abiertas = [m for m in marcas[:posicion] if m.strip().upper().startswith("BEGIN")]
        cerradas = [m for m in marcas[:posicion] if m.strip().upper() in ("COMMIT", "ROLLBACK")]
        assert len(abiertas) > len(cerradas), "La limpieza borró sin el candado tomado."

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 21. Enlaces simbólicos en la zona
# ----------------------------------------------------------------------

def prueba_21_un_enlace_en_la_zona_no_se_sigue():
    print(" 21. un enlace simbólico en la zona no se sigue ni se borra:", end=" ")

    raiz = crear_repositorio("vinculos_")

    try:
        ficha_minima(raiz, "T-0901")
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(raiz, "T-0901", trabajo=trabajo_demo("T-0901"))
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-400:]

        enlace = zona(raiz) / "T-0902"

        try:
            os.symlink(zona(raiz) / "T-0901", enlace)
        except (OSError, NotImplementedError):
            OMITIDAS.append("21: el sistema no permite enlaces simbólicos.")
            print("OMITIDA")
            return

        # La fila de T-0902 está NUEVA: por la fila, el árbol se borraría.
        for nombre in ("T-0902",):
            try:
                trabajadores.limpiar_arbol(raiz, nombre)
                raise AssertionError("Se limpió a través de un enlace.")
            except trabajadores.ErrorLimpieza as rechazo:
                METRICAS["WORKTREES_RECHAZADOS"] += 1
                assert "enlace simbólico" in str(rechazo), str(rechazo)

        assert (zona(raiz) / "T-0901" / ".git").exists(), "El árbol enlazado desapareció."

        limpieza = trabajadores.limpiar_arboles(raiz)
        assert [u["tarea"] for u in limpieza["limpiados"]] == ["T-0901"]
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        assert any(u["tarea"] == "T-0902" and "enlace simbólico" in u["motivo"] for u in limpieza["rechazados"]), limpieza
        METRICAS["WORKTREES_RECHAZADOS"] += 1
        assert enlace.is_symlink(), "Se borró el enlace."

        # Y el despacho tampoco entra por un enlace.
        trabajadores.encolar(raiz, "T-0902")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        try:
            trabajadores.despachar(raiz, "T-0902", lanzar=False)
            raise AssertionError("Se despachó sobre un enlace.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_ARBOL and "enlace simbólico" in str(rechazo), str(rechazo)

        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.NUEVO)

        enlace.unlink()

        # La zona entera como enlace: nada se crea a través de ella.
        zona_real = raiz / "zona_real"
        zona_real.mkdir()
        shutil.rmtree(zona(raiz))
        os.symlink(zona_real, zona(raiz))
        try:
            trabajadores.despachar(raiz, "T-0902", lanzar=False)
            raise AssertionError("Se despachó con la zona enlazada.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert "enlace simbólico" in str(rechazo), str(rechazo)
        assert not any(zona_real.iterdir()), "Se creó algo a través de la zona enlazada."

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 22. Lo que Git podría esconder: rama movida, renombrados, ocultos, ignorados
# ----------------------------------------------------------------------

def prueba_22_git_no_esconde_escrituras_fuera_del_ambito():
    print(" 22. ni un checkout, ni un renombrado, ni skip-worktree esconden una escritura fuera del ámbito:", end=" ")

    raiz = crear_repositorio("esconder_")

    try:
        # a) Confirmar fuera del ámbito y volver con `checkout --detach` al
        #    commit inicial: la rama queda contaminada -> BLOQUEADA.
        (raiz / "herramientas" / "esconde.py").write_text(
            "import subprocess, pathlib\n"
            "pathlib.Path('fuera').mkdir(exist_ok=True)\n"
            "pathlib.Path('fuera/x.txt').write_text('fuera\\n')\n"
            "subprocess.run(['git', 'add', '-A'], check=True)\n"
            "subprocess.run(['git', '-c', 'user.name=W', '-c', 'user.email=w@x', 'commit', '-q', '-m', 'fuera'], check=True)\n"
            "subprocess.run(['git', 'checkout', '-q', '--detach', 'HEAD~1'], check=True)\n",
            encoding="utf-8",
        )
        (raiz / "herramientas" / "renombra.py").write_text(
            "import subprocess, pathlib\n"
            "pathlib.Path('modulos/demostracion').mkdir(parents=True, exist_ok=True)\n"
            "subprocess.run(['git', 'mv', 'herramientas/eco.py', 'modulos/demostracion/T-0902.py'], check=True)\n",
            encoding="utf-8",
        )
        (raiz / "herramientas" / "oculta.py").write_text(
            "import subprocess, pathlib\n"
            "p = pathlib.Path('pruebas/demostracion/prueba_verde.py')\n"
            "p.write_text(p.read_text() + '# tocado\\n')\n"
            "subprocess.run(['git', 'update-index', '--skip-worktree', str(p)], check=True)\n"
            "pathlib.Path('modulos/demostracion').mkdir(parents=True, exist_ok=True)\n"
            "pathlib.Path('modulos/demostracion/T-0903.py').write_text('# ok\\n')\n",
            encoding="utf-8",
        )
        _git(raiz, "add", "-A")
        _git(raiz, "commit", "-q", "-m", "guiones")

        casos = (
            ("T-0901", "esconde.py", ["fuera/x.txt", "(el árbol quedó en 'HEAD'"]),
            ("T-0902", "renombra.py", ["herramientas/eco.py"]),
            ("T-0903", "oculta.py", ["pruebas/demostracion/prueba_verde.py"]),
        )

        for identificador, guion, esperadas in casos:
            ficha_minima(raiz, identificador)
            trabajadores.encolar(raiz, identificador, trabajo=[sys.executable, "herramientas/" + guion])
            METRICAS["TAREAS_ENCOLADAS"] += 1
            informe, codigo, registro = despachar_y_esperar(raiz)
            detalle = informe_del_registro(registro)
            assert codigo == 1, (identificador, registro[-600:])
            assert detalle["resultado"] == trabajadores.RESULTADO_FUERA_DE_AMBITO, (identificador, detalle)
            for esperada in esperadas:
                assert any(esperada in ruta for ruta in detalle["fuera_de_ambito"]), (identificador, esperada, detalle["fuera_de_ambito"])
            assert fila_de(raiz, identificador)["estado"] == str(Estado.BLOQUEADO)

        # b) Los comodines respetan los directorios (y `**` los cruza).
        assert trabajadores.ruta_en_ambito("x.py", ["**/*.py"])
        assert trabajadores.ruta_en_ambito("a/c.py", ["a/**/c.py"])
        assert trabajadores.ruta_en_ambito("a/b/c/d.py", ["a/**"])
        assert not trabajadores.ruta_en_ambito("a/b/x.py", ["a/*.py"])
        # Un patrón escrito con barras invertidas (en Windows) se normaliza;
        # una RUTA con barra invertida en POSIX es un nombre con ese
        # carácter, no un directorio.
        assert trabajadores.ruta_en_ambito("modulos/demostracion/x.py", ["modulos\\demostracion\\x.py"])
        assert not trabajadores.ruta_en_ambito("modulos/demostracion/otro.py", ["modulos\\demostracion\\x.py"])
        if os.name != "nt":
            assert not trabajadores.ruta_en_ambito("modulos\\demostracion\\x.py", ["modulos/demostracion/x.py"])

        # c) La limpieza no borra lo IGNORADO ni un árbol con HEAD separada.
        ficha_minima(raiz, "T-0904")
        trabajadores.encolar(raiz, "T-0904", trabajo=trabajo_demo("T-0904"))
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-400:]
        arbol = Path(informe["worktree"])

        exclusion = raiz / ".git" / "info" / "exclude"
        exclusion.write_text(
            exclusion.read_text(encoding="utf-8") + "salidas/\n__pycache__/\n*.pyc\n",
            encoding="utf-8",
        )
        (arbol / "salidas").mkdir()
        (arbol / "salidas" / "modelo.etabs").write_text("modelo", encoding="utf-8")
        (arbol / "__pycache__").mkdir()
        (arbol / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")

        try:
            trabajadores.limpiar_arbol(raiz, "T-0904")
            raise AssertionError("Se borró un árbol con archivos ignorados.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "ignorados" in str(rechazo) and "salidas/" in str(rechazo), str(rechazo)
            assert "__pycache__" not in str(rechazo)

        assert (arbol / "salidas" / "modelo.etabs").is_file()
        shutil.rmtree(arbol / "salidas")

        _git(arbol, "checkout", "-q", "--detach")
        try:
            trabajadores.limpiar_arbol(raiz, "T-0904")
            raise AssertionError("Se borró un árbol con HEAD separada.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "rama de la tarea" in str(rechazo), str(rechazo)

        _git(arbol, "checkout", "-q", "tarea/T-0904")
        assert trabajadores.limpiar_arbol(raiz, "T-0904")["limpiado"] is True
        METRICAS["WORKTREES_LIMPIADOS"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 23. El trabajo no sobrevive a su trabajador
# ----------------------------------------------------------------------

def prueba_23_el_trabajo_y_sus_nietos_mueren_con_el_trabajador():
    print(" 23. el tiempo límite y una señal matan al trabajo y a sus nietos:", end=" ")

    if os.name == "nt":
        OMITIDAS.append("23: en Windows sólo se mata al hijo directo (sin Job Object, V2).")
        print("OMITIDA")
        return

    raiz = crear_repositorio("nietos_")
    senales = Path(tempfile.mkdtemp(prefix="senales_"))
    procesos = []

    try:
        # a) Tiempo agotado: el trabajo lanzó un nieto; los dos mueren.
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(
            raiz, "T-0901",
            trabajo=trabajo_demo("T-0901", "--nieto", "--dormir", "30", "--senales", str(senales)),
            tiempo_limite_s=1,
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 1, registro[-400:]
        detalle = informe_del_registro(registro)
        assert detalle["trabajo"]["agotado"] is True

        nieto = int((senales / "nieto.pid").read_text())
        trabajo = int((senales / "trabajo.pid").read_text())
        esperar_a(lambda: not nucleo.proceso_vivo(nieto), espera_s=5, descripcion="muerte del nieto")
        assert not nucleo.proceso_vivo(trabajo)
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)

        # b) SIGTERM al trabajador: devuelve la tarea, cierra la entrada y
        #    mata al trabajo y al nieto.
        ficha_minima(raiz, "T-0902")
        (senales / "nieto.pid").unlink()
        (senales / "trabajo.pid").unlink()
        trabajadores.encolar(
            raiz, "T-0902",
            trabajo=trabajo_demo(
                "T-0902", "--nieto", "--esperar", str(senales / "nunca"), "--senales", str(senales),
            ),
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(informe["arbol_creado"])
        procesos.append(informe["proceso"])
        esperar_a(lambda: (senales / "trabajo.pid").exists(), descripcion="arranque del trabajo")
        time.sleep(0.3)
        nieto = int((senales / "nieto.pid").read_text())
        trabajo = int((senales / "trabajo.pid").read_text())

        informe["proceso"].terminate()
        codigo = informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S)
        registro = Path(informe["registro"]).read_text(encoding="utf-8")
        assert codigo == 2, (codigo, registro[-600:])
        detalle = informe_del_registro(registro)
        assert detalle["resultado"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO
        assert "señal" in detalle["detalle"], detalle["detalle"]
        esperar_a(lambda: not nucleo.proceso_vivo(nieto) and not nucleo.proceso_vivo(trabajo), espera_s=5, descripcion="muerte del trabajo y su nieto")

        fila = fila_de(raiz, "T-0902")
        assert fila["estado"] == str(Estado.REABIERTO) and fila["pid"] is None, fila
        entrada = entrada_de(raiz, "T-0902")
        assert entrada["estado_cola"] == estado_global.COLA_FALLIDA
        assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO
        assert nucleo.reanudar(raiz)["revisadas"] == 0

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        borrar(raiz)
        borrar(senales)

    print("OK")


# ----------------------------------------------------------------------
# 24. Entorno del trabajo, ejecutables y argumentos
# ----------------------------------------------------------------------

def prueba_24_el_trabajo_ve_su_arbol_y_los_argumentos_no_se_confunden():
    print(" 24. el trabajo corre con el entorno de su árbol y ninguna identidad rompe el argv:", end=" ")

    raiz = crear_repositorio("entorno_")

    try:
        (raiz / "herramientas" / "entorno.py").write_text(
            "import os, pathlib, json, sys\n"
            "pathlib.Path('modulos/demostracion').mkdir(parents=True, exist_ok=True)\n"
            "pathlib.Path('modulos/demostracion/T-0901.py').write_text(json.dumps({\n"
            "    'PYTHONPATH': os.environ.get('PYTHONPATH'), 'cwd': os.getcwd(),\n"
            "    'sys_path': sys.path[:4]}))\n",
            encoding="utf-8",
        )
        _git(raiz, "add", "-A")
        _git(raiz, "commit", "-q", "-m", "entorno")

        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901", trabajo=[sys.executable, "herramientas/entorno.py"])
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe, codigo, registro = despachar_y_esperar(raiz)
        assert codigo == 0, registro[-400:]
        visto = json.loads((Path(informe["worktree"]) / "modulos" / "demostracion" / "T-0901.py").read_text())
        arbol = str(Path(informe["worktree"]))
        assert visto["cwd"] == arbol, visto
        assert visto["PYTHONPATH"].split(os.pathsep)[0] == arbol, visto
        assert str(RAIZ) not in visto["PYTHONPATH"], "El trabajo hereda el PYTHONPATH del Supervisor."

        # Ejecutables que un intérprete de órdenes reinterpretaría: no.
        for malo in (["tarea.bat"], ["C:\\x\\tarea.CMD", "a"]):
            try:
                trabajadores.encolar(raiz, "T-0901", trabajo=malo)
                raise AssertionError("Se encoló un guion de cmd.exe: " + repr(malo))
            except trabajadores.ErrorCola as choque:
                assert "cmd.exe" in str(choque)

        # Un ejecutable sin ruta se resuelve por PATH, no por el cwd del padre.
        resultado = trabajadores.correr_trabajo(["programa-que-no-existe-xyz"], raiz, 5)
        assert resultado["codigo"] is None and "PATH" in resultado["detalle"], resultado

        # Una identidad que empieza por `-` no puede romper el argv del
        # trabajador: se rechaza al despachar, y las opciones viajan como
        # `--clave=valor`.
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(raiz, "T-0902")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        for mala in ("-x", "--intruso", "", "con espacio"):
            try:
                trabajadores.despachar(raiz, "T-0902", lanzar=False, trabajador_id=mala)
                raise AssertionError("Se aceptó la identidad " + repr(mala))
            except trabajadores.ErrorCola:
                pass
        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.NUEVO)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 25. El trabajador averiado y el árbol que deja de serlo
# ----------------------------------------------------------------------

def prueba_25_un_arbol_roto_tras_el_trabajo_no_se_juzga_contra_la_raiz():
    print(" 25. un árbol roto tras el trabajo no se juzga contra la raíz, y lo no verificable no se borra:", end=" ")

    raiz = crear_repositorio("roto_")

    try:
        (raiz / "herramientas" / "rompe.py").write_text(
            "import pathlib\npathlib.Path('.git').unlink()\n", encoding="utf-8",
        )
        _git(raiz, "add", "-A")
        _git(raiz, "commit", "-q", "-m", "rompe")

        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901", trabajo=[sys.executable, "herramientas/rompe.py"])
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe, codigo, registro = despachar_y_esperar(raiz)
        detalle = informe_del_registro(registro)
        assert codigo == 6, (codigo, registro[-600:])
        assert detalle["resultado"] == "arbol_no_valido", detalle
        assert detalle["fuera_de_ambito"] == [], detalle["fuera_de_ambito"]
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.REABIERTO) and fila["pid"] is None
        entrada = entrada_de(raiz, "T-0901")
        assert entrada["estado_cola"] == estado_global.COLA_FALLIDA
        assert entrada["resultado"]["tipo"] == "arbol_no_valido"
        assert nucleo.reanudar(raiz)["revisadas"] == 0

        # El árbol sin `.git` no es verificable: no se borra.
        try:
            trabajadores.limpiar_arbol(raiz, "T-0901")
            raise AssertionError("Se borró un árbol que Git no reconoce.")
        except nucleo.ErrorWorktree:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
        assert Path(informe["worktree"]).is_dir()

        # Reparado a mano, y con Git incapaz de responder por sus cambios,
        # tampoco se borra: no se borra lo que no se puede verificar.
        _git(raiz, "worktree", "repair", informe["worktree"])
        _git(Path(informe["worktree"]), "checkout", "-q", "--", ".")
        original = nucleo.Git.cambios_del_arbol
        nucleo.Git.cambios_del_arbol = lambda self: None
        try:
            trabajadores.limpiar_arbol(raiz, "T-0901")
            raise AssertionError("Se borró sin poder verificar los cambios.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "no se puede verificar" in str(rechazo), str(rechazo)
        finally:
            nucleo.Git.cambios_del_arbol = original
        assert Path(informe["worktree"]).is_dir()

        assert trabajadores.limpiar_arbol(raiz, "T-0901")["limpiado"] is True
        METRICAS["WORKTREES_LIMPIADOS"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 26. Orden determinista también entre vivas mezcladas; restos reparables
# ----------------------------------------------------------------------

def prueba_26_el_orden_es_el_mismo_en_toda_lectura_y_los_restos_se_reparan():
    print(" 26. el orden es el mismo en toda lectura, y los restos de un árbol interrumpido se reparan:", end=" ")

    raiz = crear_repositorio("orden_")

    try:
        assert " ".join(trabajadores.ORDEN_DE_COLA.split()).lower() == "order by prioridad desc, secuencia asc"

        for numero in range(1, 9):
            identificador = "T-09%02d" % numero
            ficha_minima(raiz, identificador)
            trabajadores.encolar(raiz, identificador)
            METRICAS["TAREAS_ENCOLADAS"] += 1

        escribir_sql(raiz, "UPDATE cola SET estado_cola = 'despachada', trabajador_id = 'x', generacion = 1 WHERE secuencia IN (2, 4, 6, 8)")

        con = estado_global.abrir(estado_global.ruta_base(raiz))
        try:
            vivas = [u["secuencia"] for u in trabajadores._listar_entradas(con, estado_global.COLA_ESTADOS_VIVOS)]
            pendientes = [u["secuencia"] for u in trabajadores._listar_entradas(con, (estado_global.COLA_PENDIENTE,))]
        finally:
            con.close()
        assert vivas == list(range(1, 9)), vivas
        assert pendientes == [1, 3, 5, 7], pendientes
        salida = cli(raiz, "cola", "--json")
        assert [u["secuencia"] for u in json.loads(salida.stdout) if u["estado_cola"] in ("pendiente", "despachada")] == list(range(1, 9))
        escribir_sql(raiz, "UPDATE cola SET estado_cola = 'pendiente', trabajador_id = NULL, generacion = NULL WHERE secuencia IN (2, 4, 6, 8)")

        # Restos: una carpeta vacía en la ranura (murió nada más crearla) y
        # metadatos sin directorio (murió a mitad del `remove` o `rm -rf`).
        vacia = zona(raiz) / "T-0901"
        vacia.mkdir(parents=True)
        # Recién creada no se toca (podría ser de un `worktree add` ajeno en
        # curso, R2): se envejece para que cuente como resto.
        antigua = time.time() - 60
        os.utime(vacia, (antigua, antigua))
        despacho = trabajadores.despachar(raiz, "T-0901", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert despacho["arbol_creado"] is True
        METRICAS["WORKTREES_CREADOS"] += 1
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)

        shutil.rmtree(zona(raiz) / "T-0901")
        assert "prunable" in _git(raiz, "worktree", "list", "--porcelain").stdout
        despacho = trabajadores.despachar(raiz, "T-0901", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert despacho["arbol_creado"] is True
        METRICAS["WORKTREES_CREADOS"] += 1
        assert "prunable" not in _git(raiz, "worktree", "list", "--porcelain").stdout
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        trabajadores.desencolar(raiz, "T-0901")

        shutil.rmtree(zona(raiz) / "T-0901")
        resultado = trabajadores.limpiar_arbol(raiz, "T-0901")
        assert resultado["limpiado"] is True and "restos" in resultado["motivo"], resultado
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        assert "prunable" not in _git(raiz, "worktree", "list", "--porcelain").stdout

        # Una carpeta con contenido y sin `.git` NO se toca (puede ser de
        # alguien).
        (zona(raiz) / "T-0902").mkdir()
        (zona(raiz) / "T-0902" / "algo.txt").write_text("x", encoding="utf-8")
        try:
            trabajadores.despachar(raiz, "T-0902", lanzar=False)
            raise AssertionError("Se despachó sobre una carpeta ajena.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_ARBOL
        assert (zona(raiz) / "T-0902" / "algo.txt").is_file()

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 27. Base de la rama y commit inicial de cada ejecución
# ----------------------------------------------------------------------

def prueba_27_la_rama_nace_de_una_base_explicita_y_el_commit_inicial_es_de_cada_toma():
    print(" 27. la rama nace de una base explícita y el commit inicial es el de cada toma:", end=" ")

    raiz = crear_repositorio("base_")

    try:
        principal = _git(raiz, "rev-parse", "--short", "main").stdout.strip()
        _git(raiz, "checkout", "-q", "-b", "otra")
        (raiz / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(raiz, "add", "-A")
        _git(raiz, "commit", "-q", "-m", "extra en otra")
        otra = _git(raiz, "rev-parse", "--short", "HEAD").stdout.strip()

        ficha_minima(raiz, "T-0901")
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0902", base="otra")
        METRICAS["TAREAS_ENCOLADAS"] += 2

        uno = trabajadores.despachar(raiz, "T-0901", lanzar=False)
        dos = trabajadores.despachar(raiz, "T-0902", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 2
        METRICAS["WORKTREES_CREADOS"] += 2
        assert uno["rama_creada_desde"] == "main" and uno["commit_inicial"] == principal, uno
        assert dos["rama_creada_desde"] == "otra" and dos["commit_inicial"] == otra, dos
        assert not (Path(uno["worktree"]) / "extra.txt").exists()
        assert (Path(dos["worktree"]) / "extra.txt").exists()

        evento = next(e for e in eventos_de(raiz, "T-0901") if e["tipo"] == estado_global.EVENTO_COLA and "Despachada" in e["motivo"])
        assert evento["datos"]["rama_creada_desde"] == "main"

        ficha_minima(raiz, "T-0903")
        trabajadores.encolar(raiz, "T-0903", base="no-existe")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        try:
            trabajadores.despachar(raiz, "T-0903", lanzar=False)
            raise AssertionError("Se creó una rama desde una base inexistente.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_ARBOL and "base" in str(rechazo)

        for informe in (uno, dos):
            nucleo.devolver(raiz, informe["tarea"], "fin", trabajador_id=informe["trabajador_id"], generacion=informe["generacion"])

        # Segunda ejecución de T-0901 tras un commit en su rama: el commit
        # inicial es el nuevo HEAD del árbol, y el anterior queda anotado.
        arbol = Path(uno["worktree"])
        (arbol / "modulos" / "demostracion").mkdir(parents=True, exist_ok=True)
        (arbol / "modulos" / "demostracion" / "T-0901.py").write_text("# v1\n", encoding="utf-8")
        _git(arbol, "add", "-A")
        _git(arbol, "-c", "user.name=W", "-c", "user.email=w@x", "commit", "-q", "-m", "v1")
        # Y un commit a mano FUERA del ámbito: la segunda vuelta no debe
        # responder por él (con el commit inicial heredado se bloquearía).
        (arbol / "fuera_a_mano.txt").write_text("de una persona\n", encoding="utf-8")
        _git(arbol, "add", "-A")
        _git(arbol, "-c", "user.name=W", "-c", "user.email=w@x", "commit", "-q", "-m", "fuera a mano")
        nuevo = _git(arbol, "rev-parse", "--short", "HEAD").stdout.strip()

        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        segunda = trabajadores.despachar(raiz, "T-0901", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert segunda["generacion"] == 2 and segunda["commit_inicial"] == nuevo, segunda
        toma = next(e for e in eventos_de(raiz, "T-0901") if e["tipo"] == estado_global.EVENTO_TRANSICION and "tomada" in e["motivo"])
        assert toma["datos"]["commit_inicial_anterior"] == principal, toma["datos"]

        # Y con ese commit inicial, el trabajo de la segunda vuelta que sólo
        # toca su ámbito no se bloquea por lo confirmado en la primera.
        informe = trabajadores.ejecutar_trabajador(
            raiz, "T-0901", segunda["trabajador_id"], 2, segunda["secuencia"], segunda["worktree"],
            [], tiempo_limite_s=30, intervalo_latido_s=INTERVALO_LATIDO_S, pid_despacho=os.getpid(),
        )
        assert informe["resultado"] == trabajadores.RESULTADO_VERIFICADA, informe
        assert informe["fuera_de_ambito"] == []

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 28. Un trabajo que sobrevive a su trabajador (R2)
# ----------------------------------------------------------------------

def prueba_28_un_trabajo_huerfano_no_se_relanza_ni_se_limpia():
    print(" 28. un trabajo que sobrevive a su trabajador no se relanza ni se limpia, y la descendencia de un trabajo terminado muere:", end=" ")

    raiz = crear_repositorio("huerfano_")
    senales = Path(tempfile.mkdtemp(prefix="senales_"))
    procesos = []

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(
            raiz, "T-0901",
            trabajo=trabajo_demo("T-0901", "--esperar", str(senales / "sigue"), "--senales", str(senales)),
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(informe["arbol_creado"])
        procesos.append(informe["proceso"])
        secuencia = informe["secuencia"]

        esperar_a(lambda: (senales / "trabajo.pid").exists(), descripcion="arranque del trabajo")
        trabajo = int((senales / "trabajo.pid").read_text())

        # a) El PID del trabajo queda en la entrada nada más lanzarlo.
        esperar_a(
            lambda: entrada_de(raiz, "T-0901").get("pid_trabajo") == trabajo,
            descripcion="PID del trabajo anotado en la entrada",
        )

        # b) El trabajador muere en seco (SIGKILL, OOM, cierre forzoso): el
        #    trabajo, en su propia sesión, le sobrevive.
        informe["proceso"].kill()
        informe["proceso"].wait(timeout=10)
        assert nucleo.proceso_vivo(trabajo), "El trabajo murió con el trabajador: no es el escenario."

        despues = nucleo.ahora_datetime() + timedelta(seconds=200)
        recuperacion = nucleo.reanudar(raiz, ahora=despues)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0901"], recuperacion
        METRICAS["RECUPERACIONES"] += 1

        # c) La reconciliación (comprobación REAL) ve el trabajo vivo: duda.
        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["secuencia"] for u in cola["vivas_sin_tarea"]] == [secuencia], cola
        assert "trabajo" in str(cola["vivas_sin_tarea"][0]["motivo"]), cola["vivas_sin_tarea"]
        assert cola["reencoladas"] == [] and cola["cerradas"] == []
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_DESPACHADA
        METRICAS["CASOS_DUDOSOS_ESCALADOS"] += 1

        salida = cli(raiz, "reanudar")
        assert salida.returncode == 1 and "CON DUDA" in salida.stdout, salida.stdout
        assert "(trabajo)" in salida.stdout, salida.stdout

        # Ni se despacha encima, ni se limpia debajo.
        try:
            trabajadores.despachar(raiz, "T-0901", lanzar=False)
            raise AssertionError("Se despachó con el trabajo huérfano vivo.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)

        try:
            trabajadores.limpiar_arbol(raiz, "T-0901")
            raise AssertionError("Se limpió el árbol con el trabajo huérfano vivo.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "despachada" in str(rechazo), str(rechazo)

        assert nucleo.proceso_vivo(trabajo)

        # d) El trabajo termina: ahora sí vuelve a la cola, y el evento
        #    conserva lo que el reencolado anula.
        (senales / "sigue").write_text("x", encoding="utf-8")
        esperar_a(lambda: not nucleo.proceso_vivo(trabajo), descripcion="fin del trabajo huérfano")
        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [secuencia], cola
        METRICAS["RECUPERACIONES"] += 1
        evento = [
            e for e in eventos_de(raiz, "T-0901")
            if e["tipo"] == estado_global.EVENTO_COLA and "reconciliación" in e["motivo"]
        ][-1]
        assert evento["datos"]["trabajador_id"] == informe["trabajador_id"], evento
        assert evento["datos"]["generacion"] == informe["generacion"], evento
        assert evento["datos"]["pid"] == informe["pid_trabajador"], evento
        assert evento["datos"]["worktree"] == informe["worktree"], evento

        # e) Un trabajo que termina bien dejando un nieto: el nieto muere con
        #    él, no sigue escribiendo en el árbol después de PROPUESTO.
        if os.name != "nt":
            for nombre in ("trabajo.pid", "nieto.pid", "sigue"):
                (senales / nombre).unlink(missing_ok=True)
            ficha_minima(raiz, "T-0902")
            trabajadores.encolar(raiz, "T-0902", trabajo=trabajo_demo("T-0902", "--nieto", "--senales", str(senales)))
            METRICAS["TAREAS_ENCOLADAS"] += 1
            informe2, codigo, registro = despachar_y_esperar(raiz, "T-0902")
            procesos.append(informe2["proceso"])
            assert codigo == 0, registro[-600:]
            detalle = informe_del_registro(registro)
            assert detalle["trabajo"]["descendencia_matada"] is True, detalle["trabajo"]
            nieto = int((senales / "nieto.pid").read_text())
            esperar_a(lambda: not nucleo.proceso_vivo(nieto), espera_s=5, descripcion="muerte del nieto tras terminar el trabajo")
            assert detalle["pid_trabajo"] == int((senales / "trabajo.pid").read_text()), detalle
        else:
            OMITIDAS.append("28e: en Windows no hay grupo de procesos que matar al terminar (Job Object, V2).")

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        matar_trabajo_de(raiz, "T-0901")
        borrar(raiz)
        borrar(senales)

    print("OK")


# ----------------------------------------------------------------------
# 29. El respaldo del trabajador ante un candado (R2)
# ----------------------------------------------------------------------

def prueba_29_el_respaldo_del_trabajador_no_cierra_la_entrada_a_ciegas():
    print(" 29. el respaldo del trabajador reintenta ante un candado y nunca deja la tarea en ejecución con la entrada cerrada:", end=" ")

    raiz = crear_repositorio("respaldo_")
    original_verificar = nucleo.verificar
    original_devolver = nucleo.devolver
    espera = trabajadores.ESPERA_ADOPCION_S
    trabajadores.ESPERA_ADOPCION_S = 0.01

    def transicion_bloqueada(*_a, **_k):
        raise estado_global.ErrorEstadoGlobal(
            "No se pudo iniciar la transacción: database is locked (simulado)"
        )

    try:
        # a) La transición choca con un candado; `devolver` también, dos
        #    veces; a la tercera entra: REABIERTO + FALLIDA en UNA transacción.
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        intentos = []

        def devolver_con_candado(*argumentos, **claves):
            intentos.append(1)
            if len(intentos) <= 2:
                raise estado_global.ErrorEstadoGlobal("database is locked (simulado)")
            return original_devolver(*argumentos, **claves)

        nucleo.verificar = transicion_bloqueada
        nucleo.devolver = devolver_con_candado
        try:
            informe, sentencias = _con_rastro_sql(lambda: ejecutar_en_proceso(raiz, despacho))
        finally:
            nucleo.verificar = original_verificar
            nucleo.devolver = original_devolver

        assert informe["resultado"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO, informe
        assert len(intentos) == 3, intentos
        assert informe["estado_final"] == str(Estado.REABIERTO) and informe["entrada_cerrada"] is True, informe
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.REABIERTO) and fila["pid"] is None, fila
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_FALLIDA
        assert _misma_transaccion(sentencias, "UPDATE TAREAS SET", "TERMINADO_EN ="), "El cierre no viajó con la devolución."
        METRICAS["RECUPERACIONES"] += 1

        # b) El candado no se suelta: NO se cierra la entrada sola. Queda
        #    EN_EJECUCION + despachada, que la recuperación sabe arreglar.
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        nucleo.verificar = transicion_bloqueada
        nucleo.devolver = transicion_bloqueada
        try:
            informe = ejecutar_en_proceso(raiz, despacho)
        finally:
            nucleo.verificar = original_verificar
            nucleo.devolver = original_devolver

        assert informe["resultado"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO, informe
        assert informe["estado_final"] is None and informe["entrada_cerrada"] is None, informe
        assert "no se cierra" in informe["detalle"], informe["detalle"]
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.EN_EJECUCION) and fila["pid"] == os.getpid(), fila
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_DESPACHADA
        despues = nucleo.ahora_datetime() + timedelta(seconds=2000)
        recuperacion = nucleo.reanudar(raiz, ahora=despues, comprobar_proceso=lambda pid: False)
        assert [u["id"] for u in recuperacion["huerfanas"]] == ["T-0901"], recuperacion
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [despacho["secuencia"]], cola
        METRICAS["RECUPERACIONES"] += 1

        # c) Rechazada por ESTADO (una persona devolvió la tarea entre
        #    medias): la entrada, aún nuestra, se cierra sola.
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1

        def averia_tras_perderla(*_a, **_k):
            nucleo.devolver(raiz, "T-0901", "la retira una persona")
            raise RuntimeError("avería simulada")

        nucleo.verificar = averia_tras_perderla
        try:
            informe = ejecutar_en_proceso(raiz, despacho)
        finally:
            nucleo.verificar = original_verificar

        assert informe["resultado"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO, informe
        assert informe["entrada_cerrada"] is True, informe
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)
        entrada = entrada_de(raiz, "T-0901")
        assert entrada["estado_cola"] == estado_global.COLA_FALLIDA
        assert entrada["resultado"]["tipo"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO
        assert nucleo.reanudar(raiz)["revisadas"] == 0

        comprobar_integridad(raiz)
    finally:
        nucleo.verificar = original_verificar
        nucleo.devolver = original_devolver
        trabajadores.ESPERA_ADOPCION_S = espera
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 30. El espejo JSON que falla después del COMMIT (R2)
# ----------------------------------------------------------------------

def prueba_30_un_fallo_del_espejo_json_tras_el_commit_no_es_un_rechazo():
    print(" 30. un fallo del espejo JSON después del COMMIT es éxito con aviso, en el despacho, la adopción y la transición:", end=" ")

    raiz = crear_repositorio("espejo_")
    original = nucleo.guardar
    principal = threading.main_thread()

    def espejo_roto(raiz_, ficha, *argumentos, **claves):
        # El archivo no se puede escribir (como con otro proceso que lo
        # tiene abierto en Windows): el camino REAL de `_regenerar_espejo`
        # tiene que convertirlo en `ErrorEspejo`. Sólo en el hilo
        # principal: el latido automático sigue escribiendo.
        if threading.current_thread() is principal:
            raise OSError("el espejo JSON no se puede escribir (simulado)")
        return original(raiz_, ficha, *argumentos, **claves)

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1

        # a) Despacho: la toma se confirmó; se sigue con la fila, con aviso.
        nucleo.guardar = espejo_roto
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        assert despacho["avisos"] and "espejo" in despacho["avisos"][0].lower(), despacho
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.EN_EJECUCION) and fila["pid"] == os.getpid(), fila
        assert fila["trabajador_id"] == despacho["trabajador_id"]
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_DESPACHADA
        assert "--generacion=1" in despacho["argv"] and "--pid-despacho=" + str(os.getpid()) in despacho["argv"], despacho["argv"]
        assert nucleo.leer(raiz, "T-0901").estado == Estado.NUEVO, "El espejo se regeneró: la simulación no vale."

        # b) Adopción y verificación con el espejo roto: adoptada, verificada,
        #    TERMINADA, y el espejo pendiente hasta la siguiente escritura.
        informe = ejecutar_en_proceso(raiz, despacho)
        assert informe["adoptada"] is True and informe["resultado"] == trabajadores.RESULTADO_VERIFICADA, informe
        assert informe["estado_final"] == str(Estado.PROPUESTO) and informe["entrada_cerrada"] is True, informe
        assert len(informe["avisos"]) >= 2, informe["avisos"]
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.PROPUESTO)
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_TERMINADA
        assert nucleo.leer(raiz, "T-0901").estado == Estado.NUEVO
        nucleo.guardar = original
        nucleo.reabrir(raiz, "T-0901")
        assert nucleo.leer(raiz, "T-0901").estado == Estado.REABIERTO, "La siguiente escritura no regeneró el espejo."

        # c) Trabajo fallido con el espejo roto: devuelta (estado leído de la
        #    base), entrada FALLIDA.
        trabajadores.encolar(raiz, "T-0901", trabajo=trabajo_demo("T-0901", "--fallar", "3"))
        METRICAS["TAREAS_ENCOLADAS"] += 1
        nucleo.guardar = espejo_roto
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        informe = ejecutar_en_proceso(raiz, despacho)
        nucleo.guardar = original
        assert informe["resultado"] == trabajadores.RESULTADO_TRABAJO_FALLIDO, informe
        assert informe["estado_final"] == str(Estado.REABIERTO) and informe["entrada_cerrada"] is True, informe
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_FALLIDA
        METRICAS["RECUPERACIONES"] += 1

        # d) Una ficha ilegible al adoptar: «no es mía», sin traceback, y la
        #    fila conserva el PID del despacho para la recuperación.
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        archivo = fichas.carpeta_tareas(raiz) / "T-0901.json"
        intacto = archivo.read_text(encoding="utf-8")
        archivo.write_text("{corrupto", encoding="utf-8")
        try:
            informe = ejecutar_en_proceso(raiz, despacho)
        finally:
            archivo.write_text(intacto, encoding="utf-8")
        assert informe["adoptada"] is False and informe["resultado"] == "no_adoptada", informe
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.EN_EJECUCION) and fila["pid"] == os.getpid(), fila
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [despacho["secuencia"]], cola
        trabajadores.desencolar(raiz, "T-0901")

        comprobar_integridad(raiz)
    finally:
        nucleo.guardar = original
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 31. La duda no reencola ni cierra; `desencolar` es la salida (R2)
# ----------------------------------------------------------------------

def prueba_31_la_duda_no_reencola_ni_cierra_y_desencolar_es_la_salida():
    print(" 31. la duda (proceso vivo, otro equipo) no reencola ni cierra, y `desencolar` es la salida que decide una persona:", end=" ")

    raiz = crear_repositorio("duda2_")

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        secuencia = despacho["secuencia"]

        # a) Adoptada, con el PID (de este proceso) vivo y la tarea devuelta
        #    por una persona: duda; `desencolar` la retira y lo cuenta.
        escribir_sql(
            raiz, "UPDATE cola SET adoptado_en = 'x', pid = ?, pid_trabajo = NULL WHERE secuencia = ?",
            (os.getpid(), secuencia),
        )
        nucleo.devolver(raiz, "T-0901", "la devuelve una persona")
        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["secuencia"] for u in cola["vivas_sin_tarea"]] == [secuencia], cola
        assert "trabajador" in str(cola["vivas_sin_tarea"][0]["motivo"])
        assert cola["reencoladas"] == [] and cola["cerradas"] == []
        METRICAS["CASOS_DUDOSOS_ESCALADOS"] += 1

        try:
            trabajadores.limpiar_arbol(raiz, "T-0901")
            raise AssertionError("Se limpió con la entrada despachada en duda.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "despachada" in str(rechazo)

        salida = cli(raiz, "desencolar", "T-0901", "--motivo", "PID reutilizado")
        assert salida.returncode == 0, salida.stdout + salida.stderr
        assert "AVISO" in salida.stdout and str(os.getpid()) in salida.stdout, salida.stdout
        entrada = entrada_de(raiz, "T-0901")
        assert entrada["estado_cola"] == estado_global.COLA_RETIRADA, entrada
        assert entrada["resultado"]["estaba"] == estado_global.COLA_DESPACHADA, entrada["resultado"]
        assert entrada["resultado"]["pid"] == os.getpid() and entrada["resultado"]["motivo"] == "PID reutilizado"
        resultado = trabajadores.limpiar_arbol(raiz, "T-0901")
        assert resultado["limpiado"] is True, resultado
        METRICAS["WORKTREES_LIMPIADOS"] += 1

        # b) Un trabajador de OTRO equipo: adoptada es duda aunque el PID no
        #    diga nada aquí; sin adoptar, vuelve a la cola.
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        secuencia = despacho["secuencia"]
        ajeno = "otro-equipo/1234/abcd"
        escribir_sql(raiz, "UPDATE tareas SET trabajador_id = ? WHERE id = 'T-0901'", (ajeno,))
        escribir_sql(
            raiz, "UPDATE cola SET trabajador_id = ?, adoptado_en = 'x', pid = 4242 WHERE secuencia = ?",
            (ajeno, secuencia),
        )
        nucleo.devolver(raiz, "T-0901", "devuelta", trabajador_id=ajeno, generacion=despacho["generacion"])
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["vivas_sin_tarea"]] == [secuencia], cola
        assert "equipo" in str(cola["vivas_sin_tarea"][0]["motivo"])
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_DESPACHADA
        METRICAS["CASOS_DUDOSOS_ESCALADOS"] += 1
        escribir_sql(raiz, "UPDATE cola SET adoptado_en = NULL WHERE secuencia = ?", (secuencia,))
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [secuencia], cola
        METRICAS["RECUPERACIONES"] += 1
        trabajadores.desencolar(raiz, "T-0901")

        # c) Una orden humana cierra la tarea con el trabajo en marcha: la
        #    entrada NO se cierra mientras el trabajo viva; después, sí.
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        secuencia = despacho["secuencia"]
        # El trabajador (pid) ya murió; el TRABAJO (pid_trabajo) sigue vivo.
        escribir_sql(
            raiz, "UPDATE cola SET adoptado_en = 'x', pid = NULL, pid_trabajo = ? WHERE secuencia = ?",
            (os.getpid(), secuencia),
        )
        nucleo.bloquear(raiz, "T-0901", "lo bloquea una persona")
        cola = trabajadores.reconciliar_cola(raiz)
        assert [u["secuencia"] for u in cola["vivas_sin_tarea"]] == [secuencia], cola
        assert cola["cerradas"] == [] and "trabajo" in str(cola["vivas_sin_tarea"][0]["motivo"])
        METRICAS["CASOS_DUDOSOS_ESCALADOS"] += 1
        try:
            trabajadores.limpiar_arbol(raiz, "T-0901")
            raise AssertionError("Se limpió el árbol con el trabajo en marcha.")
        except trabajadores.ErrorLimpieza as rechazo:
            METRICAS["WORKTREES_RECHAZADOS"] += 1
            assert "despachada" in str(rechazo)
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["cerradas"]] == [secuencia], cola
        entrada = entrada_de(raiz, "T-0901")
        assert entrada["estado_cola"] == estado_global.COLA_TERMINADA
        assert entrada["resultado"]["estado"] == str(Estado.BLOQUEADO), entrada["resultado"]

        # d) `desencolar` de una despachada cuya ejecución SIGUE viva: rechazado.
        nucleo.reabrir(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        try:
            trabajadores.desencolar(raiz, "T-0901")
            raise AssertionError("Se retiró una entrada con la ejecución viva.")
        except trabajadores.ErrorCola as rechazo:
            assert "sigue viva" in str(rechazo), str(rechazo)
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_DESPACHADA
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        cola = trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert [u["secuencia"] for u in cola["reencoladas"]] == [despacho["secuencia"]], cola
        trabajadores.desencolar(raiz, "T-0901")

        # e) Una base con un esquema MÁS NUEVO se diagnostica; no se migra
        #    hacia atrás ni se toca.
        escribir_sql(
            raiz, "INSERT INTO esquema (version, aplicado_en) VALUES (?, ?)",
            (estado_global.VERSION_ESQUEMA + 1, nucleo.ahora_utc()),
        )
        diagnostico = estado_global.diagnostico(raiz)
        assert diagnostico["estado"] == "ESQUEMA_MAS_NUEVO", diagnostico
        salida = cli(raiz, "diagnostico")
        assert "ESQUEMA_MAS_NUEVO" in salida.stdout, salida.stdout
        escribir_sql(raiz, "DELETE FROM esquema WHERE version = ?", (estado_global.VERSION_ESQUEMA + 1,))
        assert estado_global.diagnostico(raiz)["estado"] != "ESQUEMA_MAS_NUEVO"

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 32. La limpieza y el despacho compiten de verdad (R1 TOCTOU, prueba real)
# ----------------------------------------------------------------------

def prueba_32_la_limpieza_y_el_despacho_compiten_con_el_candado():
    print(" 32. una limpieza que retira el árbol con el candado hace que el despacho que ya lo había validado se rechace:", end=" ")

    raiz = crear_repositorio("carrera_")
    original_preparar = trabajadores.preparar_arbol
    original_git = trabajadores._git
    preparado = threading.Event()
    sigue_despacho = threading.Event()
    en_remove = threading.Event()
    sigue_remove = threading.Event()

    try:
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        trabajadores.desencolar(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1

        def preparar_y_esperar(*argumentos, **claves):
            resultado = original_preparar(*argumentos, **claves)
            preparado.set()
            assert sigue_despacho.wait(20)
            return resultado

        def git_que_espera(raiz_, *argumentos, **claves):
            if tuple(argumentos[:2]) == ("worktree", "remove"):
                en_remove.set()
                assert sigue_remove.wait(20)
            return original_git(raiz_, *argumentos, **claves)

        trabajadores.preparar_arbol = preparar_y_esperar
        trabajadores._git = git_que_espera

        resultado_despacho = {}
        resultado_limpieza = {}

        def despachar_en_hilo():
            try:
                resultado_despacho["informe"] = trabajadores.despachar(raiz, "T-0901", lanzar=False)
            except BaseException as error:
                resultado_despacho["error"] = error

        def limpiar_en_hilo():
            try:
                resultado_limpieza["informe"] = trabajadores.limpiar_arbol(raiz, "T-0901")
            except BaseException as error:
                resultado_limpieza["error"] = error

        hilo_despacho = threading.Thread(target=despachar_en_hilo, daemon=True)
        hilo_despacho.start()
        assert preparado.wait(20), "El despacho no llegó a validar el árbol."

        # Con el árbol ya validado por el despacho, la limpieza toma el
        # candado y se queda en el `remove`.
        hilo_limpieza = threading.Thread(target=limpiar_en_hilo, daemon=True)
        hilo_limpieza.start()
        assert en_remove.wait(20), "La limpieza no llegó al remove: " + str(resultado_limpieza)

        # El despacho sigue hacia la toma... y espera al candado.
        sigue_despacho.set()
        hilo_despacho.join(0.5)
        assert hilo_despacho.is_alive(), "El despacho no esperó al candado de la limpieza: " + str(resultado_despacho)

        sigue_remove.set()
        hilo_limpieza.join(20)
        hilo_despacho.join(20)
        assert not hilo_despacho.is_alive() and not hilo_limpieza.is_alive()

        trabajadores.preparar_arbol = original_preparar
        trabajadores._git = original_git

        assert resultado_limpieza.get("informe", {}).get("limpiado") is True, resultado_limpieza
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        error = resultado_despacho.get("error")
        assert isinstance(error, trabajadores.ErrorDespacho), resultado_despacho
        assert error.motivo == trabajadores.RECHAZO_ARBOL and "desapareció" in str(error), str(error)
        contar_rechazo(error)
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.REABIERTO) and int(fila["generacion"]) == 1, fila
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_PENDIENTE

        # El siguiente despacho vuelve a crear el árbol y sale.
        despacho = trabajadores.despachar(raiz, "T-0901", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert despacho["arbol_creado"] is True and despacho["generacion"] == 2, despacho
        METRICAS["WORKTREES_CREADOS"] += 1
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])

        comprobar_integridad(raiz)
    finally:
        trabajadores.preparar_arbol = original_preparar
        trabajadores._git = original_git
        sigue_despacho.set()
        sigue_remove.set()
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 33. El gancho de la toma no puede alterar la fila (R1, sin prueba hasta R2)
# ----------------------------------------------------------------------

def prueba_33_el_gancho_de_la_toma_no_puede_alterar_la_fila():
    print(" 33. un gancho de la toma que altera la fila, o que lanza, deshace la toma entera:", end=" ")

    raiz = crear_repositorio("gancho_")

    try:
        ficha_minima(raiz, "T-0901")

        def gancho_que_altera(con, _informe, _momento):
            con.execute("UPDATE tareas SET trabajador_id = 'otro' WHERE id = 'T-0901'")

        try:
            nucleo.tomar(raiz, "T-0901", trabajador_id="W", al_conceder=gancho_que_altera)
            raise AssertionError("La toma se concedió con la fila alterada por el gancho.")
        except nucleo.ErrorSupervisor as rechazo:
            assert "alteró la fila" in str(rechazo), str(rechazo)

        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.NUEVO) and int(fila["generacion"] or 0) == 0, fila
        assert fila["trabajador_id"] is None
        assert not [e for e in eventos_de(raiz, "T-0901") if "tomada" in e["motivo"]], "Quedó el evento de una toma deshecha."

        def gancho_que_lanza(_con, _informe, _momento):
            raise RuntimeError("el gancho falla")

        try:
            nucleo.tomar(raiz, "T-0901", trabajador_id="W", al_conceder=gancho_que_lanza)
            raise AssertionError("La toma se concedió con el gancho lanzando.")
        except RuntimeError:
            pass

        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.NUEVO) and fila["trabajador_id"] is None, fila
        assert not [e for e in eventos_de(raiz, "T-0901") if "tomada" in e["motivo"]]

        # Y sin gancho, se toma con normalidad.
        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="W")
        assert ficha.generacion == 1
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id="W", generacion=1)

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# 34. La huella de la raíz: el `../..` mal calculado sí; el usuario, no (R2)
# ----------------------------------------------------------------------

def prueba_34_la_huella_de_la_raiz_caza_el_descuido_y_no_al_usuario():
    print(" 34. la huella de la raíz bloquea el `../..` mal calculado, la configuración, la exclusión y los hooks, y no la actividad del usuario:", end=" ")

    raiz = crear_repositorio("huella_")
    senales = Path(tempfile.mkdtemp(prefix="senales_"))
    procesos = []

    try:
        # a) El trabajo escribe EN LA RAÍZ, dentro de su ámbito: bloqueado con la ruta.
        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(
            raiz, "T-0901",
            trabajo=trabajo_demo("T-0901", "--fuera", str(raiz / "modulos" / "demostracion" / "T-0901.py")),
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        informe = ejecutar_en_proceso(raiz, despacho)
        assert informe["resultado"] == trabajadores.RESULTADO_FUERA_DE_AMBITO, informe
        assert "raíz: modulos/demostracion/T-0901.py" in informe["fuera_de_ambito"], informe["fuera_de_ambito"]
        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.BLOQUEADO)
        shutil.rmtree(raiz / "modulos")

        # b) La actividad del USUARIO en la raíz, fuera del ámbito, no bloquea
        #    un trabajo honesto (trabajador real, con el usuario escribiendo
        #    mientras el trabajo corre).
        ficha_minima(raiz, "T-0902")
        trabajadores.encolar(
            raiz, "T-0902",
            trabajo=trabajo_demo("T-0902", "--esperar", str(senales / "sigue"), "--senales", str(senales)),
        )
        METRICAS["TAREAS_ENCOLADAS"] += 1
        informe = trabajadores.despachar(raiz, intervalo_latido_s=INTERVALO_LATIDO_S)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(informe["arbol_creado"])
        procesos.append(informe["proceso"])
        esperar_a(lambda: (senales / "trabajo.pid").exists(), descripcion="arranque del trabajo")
        (raiz / "herramientas" / "notas_del_usuario.txt").write_text("apuntes\n", encoding="utf-8")
        (raiz / "herramientas" / "eco.py").write_text("# editado por el usuario\n", encoding="utf-8")
        (senales / "sigue").write_text("x", encoding="utf-8")
        codigo = informe["proceso"].wait(timeout=ESPERA_TRABAJADOR_S)
        registro = Path(informe["registro"]).read_text(encoding="utf-8", errors="replace")
        assert codigo == 0, registro[-600:]
        detalle = informe_del_registro(registro)
        assert detalle["fuera_de_ambito"] == [], detalle["fuera_de_ambito"]
        assert fila_de(raiz, "T-0902")["estado"] == str(Estado.PROPUESTO)
        (raiz / "herramientas" / "notas_del_usuario.txt").unlink()
        _git(raiz, "checkout", "--", "herramientas/eco.py")

        # c) La configuración del repositorio común, su `info/exclude` y sus
        #    hooks sí cuentan, escriba quien escriba.
        comun = Path(estado_global.git_common_dir(raiz))
        casos = (
            ("T-0903", ["git", "config", "prueba.huella", "si"], "configuración"),
            ("T-0904", trabajo_demo("T-0904", "--fuera", str(comun / "info" / "exclude")), "exclusión"),
            ("T-0905", trabajo_demo("T-0905", "--fuera", str(comun / "hooks" / "pre-commit")), "hooks"),
        )

        for identificador, trabajo, esperado in casos:
            ficha_minima(raiz, identificador)
            trabajadores.encolar(raiz, identificador, trabajo=trabajo)
            METRICAS["TAREAS_ENCOLADAS"] += 1
            despacho = trabajadores.despachar(raiz, identificador, lanzar=False)
            METRICAS["DESPACHOS_ACEPTADOS"] += 1
            METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
            informe = ejecutar_en_proceso(raiz, despacho)
            assert informe["resultado"] == trabajadores.RESULTADO_FUERA_DE_AMBITO, (identificador, informe)
            assert any(esperado in ruta for ruta in informe["fuera_de_ambito"]), (identificador, informe["fuera_de_ambito"])
            assert fila_de(raiz, identificador)["estado"] == str(Estado.BLOQUEADO)

        comprobar_integridad(raiz)
    finally:
        for proceso in procesos:
            matar(proceso)
        borrar(raiz)
        borrar(senales)

    print("OK")


# ----------------------------------------------------------------------
# 35. Señales en las ventanas que quedaban (R2)
# ----------------------------------------------------------------------

def prueba_35_una_senal_nada_mas_lanzar_o_una_segunda_senal_no_dejan_nada_colgado():
    print(" 35. una señal nada más lanzar el trabajo lo mata igual, y una segunda señal no corta la devolución:", end=" ")

    if os.name == "nt":
        OMITIDAS.append("35: en Windows el proceso desligado no recibe señales de consola (documentado).")
        print("OMITIDA")
        return

    raiz = crear_repositorio("senal_")
    senales = Path(tempfile.mkdtemp(prefix="senales_"))
    numeros = [getattr(signal, n) for n in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, n)]
    anteriores = {numero: signal.getsignal(numero) for numero in numeros}
    original_lanzar = trabajadores._lanzar_desligado
    original_verificar = nucleo.verificar
    original_devolver = nucleo.devolver
    del trabajador.SENALES_RECIBIDAS[:]
    trabajador._instalar_senales()

    try:
        # a) SIGTERM justo después del `Popen` del trabajo: se aplaza hasta
        #    que el trabajo está anotado, y entonces el manejador lo mata.
        lanzados = []

        def lanzar_y_senalar(argv, cwd, entorno, salida):
            proceso = original_lanzar(argv, cwd, entorno, salida)
            lanzados.append(proceso.pid)
            os.kill(os.getpid(), signal.SIGTERM)
            return proceso

        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901", trabajo=trabajo_demo("T-0901", "--esperar", str(senales / "nunca")))
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += int(despacho["arbol_creado"])
        trabajadores._lanzar_desligado = lanzar_y_senalar
        try:
            informe = ejecutar_en_proceso(raiz, despacho)
        finally:
            trabajadores._lanzar_desligado = original_lanzar

        # La señal aplazada llega al salir del bloqueo: si cae en la anotación
        # del PID, el trabajo cuenta como no lanzado (fallido); si cae
        # después, como avería. En los dos casos: trabajo muerto, tarea
        # devuelta, entrada cerrada.
        assert informe["resultado"] in (
            trabajadores.RESULTADO_TRABAJADOR_AVERIADO, trabajadores.RESULTADO_TRABAJO_FALLIDO,
        ), informe
        assert "señal" in informe["detalle"], informe["detalle"]
        assert lanzados, "El trabajo no llegó a lanzarse."
        esperar_a(lambda: not nucleo.proceso_vivo(lanzados[0]), espera_s=5, descripcion="muerte del trabajo lanzado en la ventana de la señal")
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.REABIERTO) and fila["pid"] is None, fila
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_FALLIDA
        assert trabajador.SENALES_RECIBIDAS == [int(signal.SIGTERM)], trabajador.SENALES_RECIBIDAS
        METRICAS["RECUPERACIONES"] += 1

        # b) La primera señal interrumpe; una segunda, mientras se devuelve
        #    la tarea, se anota y NO corta la devolución.
        del trabajador.SENALES_RECIBIDAS[:]
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1

        def verificar_interrumpida(*_a, **_k):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.5)
            raise AssertionError("La primera señal no interrumpió.")

        def devolver_con_segunda_senal(*argumentos, **claves):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
            return original_devolver(*argumentos, **claves)

        nucleo.verificar = verificar_interrumpida
        nucleo.devolver = devolver_con_segunda_senal
        try:
            informe = ejecutar_en_proceso(raiz, despacho)
        finally:
            nucleo.verificar = original_verificar
            nucleo.devolver = original_devolver

        assert informe["resultado"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO and "señal" in informe["detalle"], informe
        assert informe["estado_final"] == str(Estado.REABIERTO) and informe["entrada_cerrada"] is True, informe
        assert trabajador.SENALES_RECIBIDAS == [int(signal.SIGTERM)] * 2, trabajador.SENALES_RECIBIDAS
        fila = fila_de(raiz, "T-0901")
        assert fila["estado"] == str(Estado.REABIERTO) and fila["pid"] is None, fila
        assert entrada_de(raiz, "T-0901")["estado_cola"] == estado_global.COLA_FALLIDA
        assert nucleo.reanudar(raiz)["revisadas"] == 0
        METRICAS["RECUPERACIONES"] += 1

        comprobar_integridad(raiz)
    finally:
        for numero, manejador in anteriores.items():
            try:
                signal.signal(numero, manejador)
            except (OSError, ValueError, TypeError):
                pass
        del trabajador.SENALES_RECIBIDAS[:]
        trabajadores._lanzar_desligado = original_lanzar
        nucleo.verificar = original_verificar
        nucleo.devolver = original_devolver
        borrar(raiz)
        borrar(senales)

    print("OK")


# ----------------------------------------------------------------------
# 36. Poda dirigida, restos, zona enlazada, candado traducido, ejecutable (R2)
# ----------------------------------------------------------------------

def prueba_36_la_poda_es_dirigida_y_zona_candado_y_ejecutable_se_juzgan_con_cuidado():
    print(" 36. la poda no toca árboles ajenos, un resto reciente se respeta, la zona enlazada y el candado traducido se rechazan, y el ejecutable resuelto se vuelve a juzgar:", end=" ")

    raiz = crear_repositorio("poda_")
    usuario = raiz.parent / (raiz.name + "_usuario")
    desconectado = raiz.parent / (raiz.name + "_desconectado")
    original_which = shutil.which
    espera_checkout = trabajadores.ESPERA_CHECKOUT_S

    try:
        # a) Un worktree del USUARIO fuera de la zona, «desconectado» en ese
        #    instante (prunable): reparar un resto de la zona no lo poda.
        assert _git(raiz, "worktree", "add", "-q", "-b", "usuario", str(usuario)).returncode == 0
        (usuario / "preparado.txt").write_text("x\n", encoding="utf-8")
        _git(usuario, "add", "preparado.txt")
        usuario.rename(desconectado)
        assert "prunable" in _git(raiz, "worktree", "list", "--porcelain").stdout

        ficha_minima(raiz, "T-0901")
        trabajadores.encolar(raiz, "T-0901")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        nucleo.devolver(raiz, "T-0901", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        trabajadores.desencolar(raiz, "T-0901")
        shutil.rmtree(zona(raiz) / "T-0901")
        resultado = trabajadores.limpiar_arbol(raiz, "T-0901")
        assert resultado["limpiado"] is True and "restos" in resultado["motivo"], resultado
        METRICAS["WORKTREES_LIMPIADOS"] += 1
        listado = _git(raiz, "worktree", "list", "--porcelain").stdout
        assert "prunable" in listado and str(usuario) in listado, listado
        assert "T-0901" not in listado, listado
        desconectado.rename(usuario)
        estado = _git(usuario, "status", "--porcelain")
        assert estado.returncode == 0 and "A  preparado.txt" in estado.stdout, (estado.stdout, estado.stderr)
        _git(raiz, "worktree", "remove", "--force", str(usuario))
        _git(raiz, "branch", "-D", "usuario")

        # b) Un directorio vacío RECIÉN creado en la ranura no se toca (puede
        #    ser un `worktree add` ajeno en curso); envejecido, es un resto.
        vacia = zona(raiz) / "T-0902"
        vacia.mkdir(parents=True)
        assert trabajadores._reparar_restos(raiz, vacia) is False and vacia.is_dir()
        antigua = time.time() - 60
        os.utime(vacia, (antigua, antigua))
        assert trabajadores._reparar_restos(raiz, vacia) is True and not vacia.exists()

        # c) La zona como enlace colgante: rechazo limpio, no un traceback.
        shutil.rmtree(zona(raiz))
        try:
            os.symlink(raiz / "no_existe", zona(raiz))
        except (OSError, NotImplementedError):
            OMITIDAS.append("36c: el sistema no permite enlaces simbólicos.")
        else:
            ficha_minima(raiz, "T-0903")
            trabajadores.encolar(raiz, "T-0903")
            METRICAS["TAREAS_ENCOLADAS"] += 1
            try:
                trabajadores.despachar(raiz, "T-0903", lanzar=False)
                raise AssertionError("Se despachó con la zona como enlace colgante.")
            except trabajadores.ErrorDespacho as rechazo:
                contar_rechazo(rechazo)
                assert rechazo.motivo == trabajadores.RECHAZO_ARBOL and "enlace simbólico" in str(rechazo), str(rechazo)
            try:
                trabajadores.limpiar_arboles(raiz)
                raise AssertionError("Se limpió a través de la zona enlazada.")
            except trabajadores.ErrorLimpieza as rechazo:
                METRICAS["WORKTREES_RECHAZADOS"] += 1
                assert "enlace simbólico" in str(rechazo)
            os.unlink(zona(raiz))
            trabajadores.desencolar(raiz, "T-0903")

        # d) Un candado escrito por un git traducido cuenta como «creándose»;
        #    el despacho espera con tope y dice qué hacer. Y git corre sin
        #    traducir.
        assert nucleo.entorno_git_limpio()["LC_ALL"] == "C"
        ficha_minima(raiz, "T-0904")
        trabajadores.encolar(raiz, "T-0904")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        arbol = Path(despacho["worktree"])
        nucleo.devolver(raiz, "T-0904", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        assert _git(raiz, "worktree", "lock", "--reason", "inicializando", str(arbol)).returncode == 0
        _, descartados = nucleo._inventario_de_arboles(raiz)
        assert arbol.resolve() in descartados and "creando todavía" in descartados[arbol.resolve()], descartados
        trabajadores.ESPERA_CHECKOUT_S = 0.3
        try:
            trabajadores.despachar(raiz, "T-0904", lanzar=False)
            raise AssertionError("Se despachó sobre un árbol bloqueado como en creación.")
        except trabajadores.ErrorDespacho as rechazo:
            contar_rechazo(rechazo)
            assert rechazo.motivo == trabajadores.RECHAZO_ARBOL and "unlock" in str(rechazo), str(rechazo)
        finally:
            trabajadores.ESPERA_CHECKOUT_S = espera_checkout
        assert _git(raiz, "worktree", "unlock", str(arbol)).returncode == 0
        despacho = trabajadores.despachar(raiz, "T-0904", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        assert despacho["arbol_creado"] is False
        nucleo.devolver(raiz, "T-0904", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])

        # e) Lo RESUELTO se vuelve a juzgar: un `.cmd` que PATHEXT resolviera,
        #    o algo del directorio actual que no está en PATH, no se ejecutan;
        #    `trabajo.cmd.` (Windows recorta el punto) tampoco se encola.
        try:
            trabajadores._validar_trabajo(["trabajo.cmd."])
            raise AssertionError("Se aceptó un guion de cmd.exe con punto final.")
        except trabajadores.ErrorCola:
            pass
        # Un `.cmd` DENTRO de un directorio del PATH (como haría PATHEXT).
        en_path = [una for una in os.environ.get("PATH", "").split(os.pathsep) if una][0]
        shutil.which = lambda nombre, *a, **k: os.path.join(en_path, "npm.cmd")
        resultado = trabajadores.correr_trabajo(["npm", "x"], arbol, 5)
        assert resultado["codigo"] is None and "cmd.exe" in resultado["detalle"], resultado
        shutil.which = lambda nombre, *a, **k: os.path.join(os.getcwd(), "git")
        resultado = trabajadores.correr_trabajo(["git", "status"], arbol, 5)
        assert resultado["codigo"] is None and "PATH" in resultado["detalle"], resultado
        shutil.which = original_which
        resultado = trabajadores.correr_trabajo(["git", "status"], arbol, 5)
        assert resultado["codigo"] == 0, resultado

        # f) `fuera_del_arbol` con muchas rutas no se eterniza.
        muchas = ["ruta_%d.txt" % numero for numero in range(30000)]
        antes = {"raiz": muchas[:-1], "config": "a", "exclude": "b", "hooks": []}
        despues = {"raiz": muchas, "config": "a", "exclude": "b", "hooks": []}
        inicio = time.monotonic()
        assert trabajadores.fuera_del_arbol(antes, despues) == ["raíz: ruta_29999.txt"]
        assert time.monotonic() - inicio < 2.0

        # g) Una ficha con OTRA rama: su árbol se crea y se limpia en esa rama.
        ficha_minima(raiz, "T-0905")
        escribir_sql(raiz, "UPDATE tareas SET rama = 'ramas/otra' WHERE id = 'T-0905'")
        trabajadores.encolar(raiz, "T-0905")
        METRICAS["TAREAS_ENCOLADAS"] += 1
        despacho = trabajadores.despachar(raiz, "T-0905", lanzar=False)
        METRICAS["DESPACHOS_ACEPTADOS"] += 1
        METRICAS["WORKTREES_CREADOS"] += 1
        assert nucleo.Git(Path(despacho["worktree"])).rama_actual() == "ramas/otra", despacho
        nucleo.devolver(raiz, "T-0905", "fin", trabajador_id=despacho["trabajador_id"], generacion=despacho["generacion"])
        trabajadores.reconciliar_cola(raiz, comprobar_proceso=lambda pid: False)
        trabajadores.desencolar(raiz, "T-0905")
        resultado = trabajadores.limpiar_arbol(raiz, "T-0905")
        assert resultado["limpiado"] is True, resultado
        METRICAS["WORKTREES_LIMPIADOS"] += 1

        comprobar_integridad(raiz)
    finally:
        shutil.which = original_which
        trabajadores.ESPERA_CHECKOUT_S = espera_checkout
        for ruta in (usuario, desconectado):
            if ruta.exists():
                shutil.rmtree(ruta, ignore_errors=True)
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
    prueba_15_la_transicion_y_el_cierre_van_juntos()
    prueba_16_la_adopcion_es_exclusiva_y_aguanta_un_candado()
    prueba_17_un_lanzamiento_fallido_devuelve_la_tarea()
    prueba_18_una_ficha_invalida_no_para_la_cola()
    prueba_19_la_reconciliacion_no_pierde_trabajos_ni_reencola_con_el_proceso_vivo()
    prueba_20_la_toma_no_se_concede_sobre_un_arbol_que_desaparecio()
    prueba_21_un_enlace_en_la_zona_no_se_sigue()
    prueba_22_git_no_esconde_escrituras_fuera_del_ambito()
    prueba_23_el_trabajo_y_sus_nietos_mueren_con_el_trabajador()
    prueba_24_el_trabajo_ve_su_arbol_y_los_argumentos_no_se_confunden()
    prueba_25_un_arbol_roto_tras_el_trabajo_no_se_juzga_contra_la_raiz()
    prueba_26_el_orden_es_el_mismo_en_toda_lectura_y_los_restos_se_reparan()
    prueba_27_la_rama_nace_de_una_base_explicita_y_el_commit_inicial_es_de_cada_toma()
    prueba_28_un_trabajo_huerfano_no_se_relanza_ni_se_limpia()
    prueba_29_el_respaldo_del_trabajador_no_cierra_la_entrada_a_ciegas()
    prueba_30_un_fallo_del_espejo_json_tras_el_commit_no_es_un_rechazo()
    prueba_31_la_duda_no_reencola_ni_cierra_y_desencolar_es_la_salida()
    prueba_32_la_limpieza_y_el_despacho_compiten_con_el_candado()
    prueba_33_el_gancho_de_la_toma_no_puede_alterar_la_fila()
    prueba_34_la_huella_de_la_raiz_caza_el_descuido_y_no_al_usuario()
    prueba_35_una_senal_nada_mas_lanzar_o_una_segunda_senal_no_dejan_nada_colgado()
    prueba_36_la_poda_es_dirigida_y_zona_candado_y_ejecutable_se_juzgan_con_cuidado()

    duracion = time.monotonic() - inicio

    imprimir_metricas()

    if OMITIDAS:
        print("")
        print("  Casos OMITIDOS en esta corrida (el entorno no los admite)")
        print("  --------------------------------------------------------")
        for omitida in OMITIDAS:
            print("      · " + omitida)

    # Cotas mínimas: una corrida que no hiciera nada no puede salir verde.
    # Con margen para lo que Windows omite (23, 28e, 35; 21 y 36c sin
    # enlaces): en Linux la corrida da 94/85/50/7/66/19/24/26/8/38.
    minimos = {
        "TAREAS_ENCOLADAS": 80,
        "DESPACHOS_ACEPTADOS": 72,
        "DESPACHOS_RECHAZADOS": 42,
        "CONFLICTOS_DE_AMBITO_DETECTADOS": 2,
        "WORKTREES_CREADOS": 55,
        "WORKTREES_LIMPIADOS": 16,
        "WORKTREES_RECHAZADOS": 19,
        "RECUPERACIONES": 20,
        "CASOS_DUDOSOS_ESCALADOS": 6,
        "COMPROBACIONES_INTEGRIDAD": 34,
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
