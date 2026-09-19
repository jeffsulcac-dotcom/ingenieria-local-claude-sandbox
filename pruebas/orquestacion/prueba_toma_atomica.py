"""
Pruebas de A3.1: TOMA ATÓMICA de tareas.

Lo que hay que demostrar aquí no es que el código funcione cuando nadie
compite, sino que NUNCA hay dos propietarios de la misma tarea cuando
varios trabajadores la reclaman a la vez.

Cómo se provoca la colisión de verdad
-------------------------------------
Una prueba de concurrencia que no colisiona da verde sin haber probado
nada. Por eso:

1. Los contendientes son PROCESOS reales, creados con el contexto "spawn"
   (el mismo que usa Windows), y esperan en una `multiprocessing.Barrier`
   colocada justo antes de la toma: todos salen en el mismo instante.

2. Los procesos son PERSISTENTES y se reutilizan entre rondas. Arrancar un
   proceso por ronda costaría más que la propia carrera.

3. Cada trabajador tiene SU PROPIA cola de entrada. Con una cola
   compartida un trabajador puede llevarse dos mensajes y la barrera se
   queda bloqueada para siempre.

4. Existe además una carrera entre CONEXIONES (hilos, cada uno con su
   conexión SQLite propia) cuya barrera cae exactamente sobre
   `BEGIN IMMEDIATE`, que es la sección crítica real.

5. Y, sobre todo, una PRUEBA DE CONTROL: el mismo arnés ejecuta una toma
   deliberadamente ingenua (leer -> esperar -> escribir sin condición).
   Si el arnés no produjera dobles tomas con esa implementación, el verde
   de las demás comprobaciones no significaría nada.

Todas las esperas llevan tiempo límite. Ningún bucle es infinito.

Ejecución por omisión: pensada para terminar muy por debajo del tiempo
límite del corredor único (120 s). Para la corrida de estrés:

    python pruebas/orquestacion/prueba_toma_atomica.py --carreras 100

Todo es hermético: repositorios Git temporales con su propia base SQLite.
Jamás se toca el repositorio real ni su base.
"""

import argparse
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

# Rondas de carrera por configuración cuando no se indica otra cosa.
RONDAS_POR_OMISION = 8

# Rondas de la carrera entre conexiones (hilos): son mucho más baratas.
RONDAS_HILOS = 60

# Contendientes de la carrera entre conexiones.
HILOS_EN_CARRERA = 8

# Ninguna espera es indefinida.
ESPERA_BARRERA_S = 60
ESPERA_RESULTADO_S = 120
ESPERA_CIERRE_S = 20

# Ventana que la toma ingenua deja abierta entre leer y escribir.
VENTANA_INGENUA_S = 0.01

MODO_ATOMICO = "atomico"
MODO_INGENUO = "ingenuo"

ESTADOS_RECLAMABLES = tuple(
    sorted(str(estado) for estado in nucleo.ESTADOS_TOMABLES)
)


# ----------------------------------------------------------------------
# Métricas reales acumuladas durante toda la ejecución
# ----------------------------------------------------------------------

METRICAS = {
    "TOTAL_RACES": 0,
    "TOTAL_SUCCESSFUL_CLAIMS": 0,
    "TOTAL_REJECTED_CLAIMS": 0,
    "DOUBLE_CLAIM_EVENTS": 0,
    "SQLITE_ERRORS": 0,
    "UNEXPECTED_EXCEPTIONS": 0,
    "INTEGRITY_CHECKS": 0,
    "INTEGRITY_FAILURES": 0,
}

# Detalle por bloque de carreras, para poder reportar sin inventar nada.
INFORMES = []


def _acumular(informe: dict) -> None:
    """Suma un bloque de carreras a las métricas globales."""
    METRICAS["TOTAL_RACES"] += informe["RACE_RUNS"]
    METRICAS["TOTAL_SUCCESSFUL_CLAIMS"] += informe["CLAIMS_SUCCESS"]
    METRICAS["TOTAL_REJECTED_CLAIMS"] += informe["CLAIMS_REJECTED"]
    METRICAS["DOUBLE_CLAIM_EVENTS"] += informe["DOUBLE_CLAIMS"]
    METRICAS["SQLITE_ERRORS"] += informe["SQLITE_ERRORS"]
    METRICAS["UNEXPECTED_EXCEPTIONS"] += informe["UNEXPECTED_EXCEPTIONS"]

    INFORMES.append(informe)


# ----------------------------------------------------------------------
# Repositorio temporal de juguete
# ----------------------------------------------------------------------

def _git(raiz: Path, *argumentos: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argumentos],
        cwd=str(raiz),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def crear_repositorio() -> Path:
    """Repositorio Git temporal con su propia base SQLite global."""
    raiz = Path(tempfile.mkdtemp(prefix="toma_atomica_"))

    inicio = _git(raiz, "init", "-q", "-b", "main")
    assert inicio.returncode == 0, inicio.stderr

    _git(raiz, "config", "user.name", "Prueba A3.1")
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
    # `onexc` existe desde Python 3.12; `onerror` es la vía equivalente en
    # 3.11 y anteriores. La función de limpieza es la misma en ambos casos.
    if sys.version_info >= (3, 12):
        shutil.rmtree(raiz, onexc=_quitar_solo_lectura, ignore_errors=False)
    else:
        shutil.rmtree(raiz, onerror=_quitar_solo_lectura, ignore_errors=False)


def ficha_minima(raiz: Path, identificador="T-0901", **extras):
    parametros = {
        "titulo": "Tarea en disputa " + identificador,
        "objetivo": "Comprobar la toma atómica.",
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


def eventos_de(raiz: Path, identificador: str) -> list:
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        return estado_global.listar_eventos(con, identificador)
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

    METRICAS["INTEGRITY_CHECKS"] += 1

    if veredicto.lower() != "ok" or claves:
        METRICAS["INTEGRITY_FAILURES"] += 1

    assert not claves, (
        "La base quedó con claves foráneas rotas: " + repr(claves)
    )

    return veredicto


# ----------------------------------------------------------------------
# Arnés de carrera: procesos persistentes que colisionan en una barrera
# ----------------------------------------------------------------------

def _toma_ingenua(raiz: str, tarea: str, trabajador: str, pid: int) -> dict:
    """
    Toma DELIBERADAMENTE mal hecha: leer -> esperar -> escribir sin condición.

    Reproduce el defecto que A3.1 corrige. Sólo existe dentro de esta
    prueba, como patrón de control del arnés: si el arnés no consigue que
    ESTA implementación produzca dobles tomas, tampoco estaría probando
    nada cuando la implementación real sale en verde.
    """
    ruta = estado_global.ruta_base(Path(raiz))
    con = estado_global.abrir(ruta)

    try:
        fila = con.execute(
            "SELECT estado FROM tareas WHERE id = ?", (tarea,)
        ).fetchone()

        if fila is None:
            return {"otorgado": False, "motivo": "inexistente"}

        if str(fila["estado"]) not in ESTADOS_RECLAMABLES:
            return {"otorgado": False, "motivo": "estado_no_reclamable"}

        # La ventana entre comprobar y escribir: exactamente el TOCTOU.
        time.sleep(VENTANA_INGENUA_S)

        momento = nucleo.ahora_datetime().isoformat(timespec="seconds")

        con.execute(
            """
            UPDATE tareas
               SET estado = ?, trabajador_id = ?, pid = ?, iniciado_en = ?,
                   ultimo_latido = ?, actualizado_en = ?
             WHERE id = ?
            """,
            (
                str(Estado.EN_EJECUCION), trabajador, pid, momento,
                momento, momento, tarea,
            ),
        )

        return {"otorgado": True, "motivo": None, "propietario": trabajador}
    finally:
        con.close()


def _toma_real(raiz: str, tarea: str, trabajador: str, pid: int) -> dict:
    """La toma del Supervisor, tal cual la usa cualquier trabajador."""
    try:
        ficha = nucleo.tomar(
            Path(raiz), tarea, trabajador_id=trabajador, pid=pid
        )
    except nucleo.ErrorToma as rechazo:
        return {
            "otorgado": False,
            "motivo": rechazo.motivo,
            "estado": rechazo.estado,
            "propietario": rechazo.propietario,
            "propia": rechazo.propia,
        }
    except nucleo.ErrorSolapamiento as choque:
        return {"otorgado": False, "motivo": "solapamiento",
                "detalle": str(choque)}

    return {
        "otorgado": True,
        "motivo": None,
        "propietario": ficha.trabajador_id,
        "estado": str(ficha.estado),
    }


def _trabajador_de_carrera(entrada, salida, barrera) -> None:
    """
    Proceso contendiente. Vive entre rondas; sólo espera órdenes.

    Cada orden lo lleva a la barrera y, en cuanto se abre, a intentar la
    toma. El resultado se devuelve siempre, también cuando es un fallo:
    una carrera sin respuesta sería un verde falso.
    """
    while True:
        try:
            orden = entrada.get(timeout=ESPERA_RESULTADO_S)
        except Exception as error:      # cola rota o padre desaparecido
            salida.put({"fatal": "sin órdenes: " + repr(error)})
            return

        if orden.get("orden") == "fin":
            return

        informe = {
            "ronda": orden["ronda"],
            "trabajador": orden["trabajador"],
        }

        try:
            barrera.wait(timeout=ESPERA_BARRERA_S)
        except threading.BrokenBarrierError:
            informe["fatal"] = "la barrera se rompió: la carrera no ocurrió"
            salida.put(informe)
            continue

        try:
            if orden["modo"] == MODO_INGENUO:
                resultado = _toma_ingenua(
                    orden["raiz"], orden["tarea"],
                    orden["trabajador"], os.getpid(),
                )
            else:
                resultado = _toma_real(
                    orden["raiz"], orden["tarea"],
                    orden["trabajador"], os.getpid(),
                )
        except sqlite3.Error as error:
            informe["sqlite"] = repr(error)
            salida.put(informe)
            continue
        except BaseException as error:  # noqa: BLE001 - se reporta, no se traga
            informe["inesperada"] = repr(error)
            salida.put(informe)
            continue

        informe.update(resultado)
        salida.put(informe)


class Arnes:
    """
    K procesos persistentes que colisionan en una barrera común.

    Se usa con `with`: cerrar siempre, aunque la comprobación falle.
    """

    def __init__(self, contendientes: int):
        self.contendientes = contendientes
        self.contexto = multiprocessing.get_context("spawn")
        self.barrera = self.contexto.Barrier(contendientes)
        self.salida = self.contexto.Queue()

        # UNA COLA POR TRABAJADOR: con una cola compartida un proceso puede
        # llevarse dos órdenes y dejar la barrera esperando para siempre.
        self.entradas = [self.contexto.Queue() for _ in range(contendientes)]

        self.procesos = [
            self.contexto.Process(
                target=_trabajador_de_carrera,
                args=(entrada, self.salida, self.barrera),
                daemon=True,
            )
            for entrada in self.entradas
        ]

        for proceso in self.procesos:
            proceso.start()

    def __enter__(self):
        return self

    def __exit__(self, *_excepcion):
        self.cerrar()
        return False

    def ronda(self, raiz: Path, tarea: str, modo: str, numero: int) -> list:
        """Una carrera: todos salen a la vez, todos responden."""
        for indice, entrada in enumerate(self.entradas):
            entrada.put(
                {
                    "orden": "carrera",
                    "ronda": numero,
                    "raiz": str(raiz),
                    "tarea": tarea,
                    "modo": modo,
                    "trabajador": "equipo/t" + str(indice) + "/r" + str(numero),
                }
            )

        respuestas = []

        for _ in range(self.contendientes):
            respuestas.append(self.salida.get(timeout=ESPERA_RESULTADO_S))

        return respuestas

    def cerrar(self) -> None:
        for entrada in self.entradas:
            try:
                entrada.put({"orden": "fin"})
            except Exception:           # la cola ya no existe: da igual
                pass

        for proceso in self.procesos:
            proceso.join(timeout=ESPERA_CIERRE_S)

            if proceso.is_alive():
                proceso.terminate()
                proceso.join(timeout=ESPERA_CIERRE_S)


def _juzgar_carreras(etiqueta: str, respuestas_por_ronda: list) -> dict:
    """
    Convierte las respuestas en las métricas exigidas. No inventa nada.

    EXPECTED_WINNERS / EXPECTED_REJECTIONS son lo que la regla de un solo
    escritor obliga: una toma concedida por carrera y todas las demás
    rechazadas.
    """
    informe = {
        "ETIQUETA": etiqueta,
        "RACE_RUNS": len(respuestas_por_ronda),
        "CONTENDIENTES": 0,
        "CLAIMS_SUCCESS": 0,
        "CLAIMS_REJECTED": 0,
        "DOUBLE_CLAIMS": 0,
        "EXPECTED_WINNERS": 0,
        "EXPECTED_REJECTIONS": 0,
        "SQLITE_ERRORS": 0,
        "UNEXPECTED_EXCEPTIONS": 0,
        "FATALES": [],
    }

    for respuestas in respuestas_por_ronda:
        informe["CONTENDIENTES"] = max(
            informe["CONTENDIENTES"], len(respuestas)
        )

        informe["EXPECTED_WINNERS"] += 1
        informe["EXPECTED_REJECTIONS"] += max(len(respuestas) - 1, 0)

        ganadores = 0

        for respuesta in respuestas:
            if respuesta.get("fatal"):
                informe["FATALES"].append(respuesta["fatal"])
                continue

            if respuesta.get("sqlite"):
                informe["SQLITE_ERRORS"] += 1
                continue

            if respuesta.get("inesperada"):
                informe["UNEXPECTED_EXCEPTIONS"] += 1
                continue

            if respuesta.get("otorgado"):
                ganadores += 1
                informe["CLAIMS_SUCCESS"] += 1
            else:
                informe["CLAIMS_REJECTED"] += 1

        if ganadores > 1:
            informe["DOUBLE_CLAIMS"] += 1

    return informe


def _liberar(raiz: Path, tarea: str) -> None:
    """Devuelve la tarea al pozo común para la siguiente ronda."""
    nucleo.devolver(raiz, tarea, "Devuelta entre rondas de carrera.")


# ----------------------------------------------------------------------
# A. Toma normal, sin competencia
# ----------------------------------------------------------------------

def prueba_a_toma_normal():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/solo/1")

        assert ficha.estado == Estado.EN_EJECUCION, ficha.estado
        assert ficha.trabajador_id == "equipo/solo/1", ficha.trabajador_id
        assert ficha.iniciado_en, "la toma debe fechar el inicio"
        assert ficha.ultimo_latido, "la toma debe dejar un primer latido"
        assert ficha.rama == "tarea/T-0901", ficha.rama

        fila = fila_de(raiz, "T-0901")

        assert fila["estado"] == str(Estado.EN_EJECUCION), fila["estado"]
        assert fila["trabajador_id"] == "equipo/solo/1", fila
        assert fila["pid"] == os.getpid(), fila["pid"]

        # Lo que `tomar` devuelve es exactamente lo que la transacción
        # confirmó, no una relectura posterior que otro pudiera haber
        # cambiado: la ficha y la fila dicen lo mismo, campo por campo.
        for columna in ("estado", "trabajador_id", "pid", "iniciado_en",
                        "ultimo_latido", "rama", "commit_inicial"):
            devuelto = getattr(ficha, columna)
            devuelto = str(devuelto) if columna == "estado" else devuelto

            assert devuelto == fila[columna], (
                "La ficha devuelta y la fila confirmada difieren en '"
                + columna + "': " + repr(devuelto) + " vs "
                + repr(fila[columna])
            )

        transiciones = [
            evento for evento in eventos_de(raiz, "T-0901")
            if evento["tipo"] == estado_global.EVENTO_TRANSICION
        ]

        assert len(transiciones) == 1, transiciones
        assert transiciones[0]["estado_anterior"] == str(Estado.NUEVO)
        assert transiciones[0]["estado_nuevo"] == str(Estado.EN_EJECUCION)
        assert transiciones[0]["datos"]["trabajador_id"] == "equipo/solo/1"
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# B y C. Carreras reales entre procesos: 2 y 10 contendientes
# ----------------------------------------------------------------------

def _carrera_entre_procesos(contendientes: int, rondas: int) -> dict:
    """
    K procesos reclaman LA MISMA tarea a la vez, `rondas` veces seguidas.

    Entre rondas la tarea se devuelve (REABIERTO) para que vuelva a estar
    en disputa. Cada ronda se comprueba en el acto: un solo ganador y el
    propietario registrado en SQLite es ese mismo ganador.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        respuestas_por_ronda = []

        with Arnes(contendientes) as arnes:
            for numero in range(1, rondas + 1):
                respuestas = arnes.ronda(raiz, "T-0901", MODO_ATOMICO, numero)
                respuestas_por_ronda.append(respuestas)

                fatales = [r["fatal"] for r in respuestas if r.get("fatal")]
                assert not fatales, (
                    "La carrera " + str(numero) + " no llegó a ocurrir: "
                    + "; ".join(fatales)
                )

                ganadores = [
                    r for r in respuestas if r.get("otorgado")
                ]

                assert len(ganadores) == 1, (
                    "Ronda " + str(numero) + " con "
                    + str(len(ganadores)) + " ganadores: " + repr(respuestas)
                )

                fila = fila_de(raiz, "T-0901")

                assert fila["estado"] == str(Estado.EN_EJECUCION), fila
                assert fila["trabajador_id"] == ganadores[0]["propietario"], (
                    "El propietario en SQLite no es el ganador declarado: "
                    + repr(fila) + " vs " + repr(ganadores[0])
                )

                # Los perdedores deben saber que perdieron, y por qué.
                for respuesta in respuestas:
                    if respuesta.get("otorgado"):
                        continue

                    assert respuesta.get("motivo") in (
                        estado_global.MOTIVO_YA_RECLAMADA,
                        estado_global.MOTIVO_ESTADO_NO_RECLAMABLE,
                    ), respuesta

                _liberar(raiz, "T-0901")

        informe = _juzgar_carreras(
            "procesos x" + str(contendientes), respuestas_por_ronda
        )

        informe["INTEGRIDAD"] = comprobar_integridad(raiz)

        return informe
    finally:
        borrar(raiz)


def _exigir_un_solo_ganador(informe: dict) -> None:
    assert informe["DOUBLE_CLAIMS"] == 0, informe
    assert informe["SQLITE_ERRORS"] == 0, informe
    assert informe["UNEXPECTED_EXCEPTIONS"] == 0, informe
    assert not informe["FATALES"], informe
    assert informe["CLAIMS_SUCCESS"] == informe["EXPECTED_WINNERS"], informe
    assert informe["CLAIMS_REJECTED"] == informe["EXPECTED_REJECTIONS"], informe


def prueba_b_dos_trabajadores_compitiendo():
    informe = _carrera_entre_procesos(2, ARGUMENTOS.carreras)

    _acumular(informe)
    _exigir_un_solo_ganador(informe)

    assert informe["INTEGRIDAD"].lower() == "ok", informe


def prueba_c_diez_contendientes():
    informe = _carrera_entre_procesos(10, ARGUMENTOS.carreras)

    _acumular(informe)
    _exigir_un_solo_ganador(informe)

    assert informe["INTEGRIDAD"].lower() == "ok", informe


# ----------------------------------------------------------------------
# Prueba de CONTROL: el arnés sí detecta una carrera perdida
# ----------------------------------------------------------------------

def prueba_el_detector_detecta_la_carrera():
    """
    El mismo arnés, contra una toma ingenua, DEBE producir dobles tomas.

    Sin esta comprobación no habría forma de distinguir "la toma atómica
    funciona" de "el arnés nunca consiguió que colisionaran".
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        respuestas_por_ronda = []

        with Arnes(4) as arnes:
            for numero in range(1, 6):
                respuestas = arnes.ronda(raiz, "T-0901", MODO_INGENUO, numero)
                respuestas_por_ronda.append(respuestas)

                fatales = [r["fatal"] for r in respuestas if r.get("fatal")]
                assert not fatales, "; ".join(fatales)

                _liberar(raiz, "T-0901")

        informe = _juzgar_carreras("control ingenuo x4", respuestas_por_ronda)

        # NO se acumula en las métricas globales: este bloque mide el arnés,
        # no la implementación real, y sus dobles tomas son deliberadas.
        INFORMES.append(informe)

        assert informe["DOUBLE_CLAIMS"] > 0, (
            "El arnés no produjo ni una sola carrera perdida con la toma "
            "ingenua: no está colisionando y el verde de las demás "
            "comprobaciones no probaría nada. " + repr(informe)
        )

        assert informe["CLAIMS_SUCCESS"] > informe["EXPECTED_WINNERS"], informe
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# Carrera entre CONEXIONES: la barrera cae sobre BEGIN IMMEDIATE
# ----------------------------------------------------------------------

def prueba_carrera_entre_conexiones():
    """
    Hilos, cada uno con SU conexión, colisionando en la sección crítica.

    La conexión se abre ANTES de la barrera, de modo que la contención cae
    exactamente sobre `BEGIN IMMEDIATE` y no sobre el coste de conectar.
    Es mucho más barata que la carrera entre procesos, así que hace muchas
    más rondas del primitivo `reclamar`.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        ruta = estado_global.ruta_base(raiz)
        barrera = threading.Barrier(HILOS_EN_CARRERA)
        respuestas_por_ronda = []

        # Estado compartido de la ronda en curso: lo escribe cada hilo en su
        # propia posición, así que no necesita cerrojo.
        casillas = [None] * HILOS_EN_CARRERA

        def contendiente(indice: int, ronda: int) -> None:
            informe = {"trabajador": "hilo/" + str(indice) + "/r" + str(ronda)}

            try:
                con = estado_global.abrir(ruta)
            except BaseException as error:  # noqa: BLE001
                informe["inesperada"] = repr(error)
                casillas[indice] = informe

                try:
                    barrera.wait(timeout=ESPERA_BARRERA_S)
                except threading.BrokenBarrierError:
                    pass

                return

            try:
                try:
                    barrera.wait(timeout=ESPERA_BARRERA_S)
                except threading.BrokenBarrierError:
                    informe["fatal"] = "la barrera se rompió"
                    casillas[indice] = informe
                    return

                momento = nucleo.ahora_datetime().isoformat(timespec="seconds")

                with estado_global.transaccion(con):
                    resultado = estado_global.reclamar(
                        con,
                        "T-0901",
                        trabajador_id=informe["trabajador"],
                        pid=os.getpid(),
                        momento=momento,
                        estados_reclamables=nucleo.ESTADOS_TOMABLES,
                        estado_destino=str(Estado.EN_EJECUCION),
                    )

                informe["otorgado"] = (
                    resultado["resultado"] == estado_global.CLAIM_OTORGADO
                )
                informe["motivo"] = resultado["motivo"]
                informe["propietario"] = resultado["propietario"]
            except sqlite3.Error as error:
                informe["sqlite"] = repr(error)
            except BaseException as error:      # noqa: BLE001
                informe["inesperada"] = repr(error)
            finally:
                con.close()
                casillas[indice] = informe

        for ronda in range(1, RONDAS_HILOS + 1):
            barrera.reset()

            for posicion in range(HILOS_EN_CARRERA):
                casillas[posicion] = None

            hilos = [
                threading.Thread(target=contendiente, args=(indice, ronda))
                for indice in range(HILOS_EN_CARRERA)
            ]

            for hilo in hilos:
                hilo.start()

            for hilo in hilos:
                hilo.join(timeout=ESPERA_RESULTADO_S)
                assert not hilo.is_alive(), (
                    "Un contendiente quedó colgado en la ronda "
                    + str(ronda) + "."
                )

            respuestas = [casilla for casilla in casillas if casilla]

            assert len(respuestas) == HILOS_EN_CARRERA, respuestas

            respuestas_por_ronda.append(respuestas)

            ganadores = [r for r in respuestas if r.get("otorgado")]

            assert len(ganadores) == 1, (
                "Ronda " + str(ronda) + " con " + str(len(ganadores))
                + " ganadores: " + repr(respuestas)
            )

            fila = fila_de(raiz, "T-0901")

            assert fila["trabajador_id"] == ganadores[0]["trabajador"], (
                repr(fila) + " vs " + repr(ganadores[0])
            )

            _liberar(raiz, "T-0901")

        informe = _juzgar_carreras(
            "conexiones x" + str(HILOS_EN_CARRERA), respuestas_por_ronda
        )

        _acumular(informe)
        _exigir_un_solo_ganador(informe)

        informe["INTEGRIDAD"] = comprobar_integridad(raiz)

        assert informe["INTEGRIDAD"].lower() == "ok", informe
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# El ámbito se decide DENTRO de la misma transacción que la toma
# ----------------------------------------------------------------------

def prueba_ambito_se_decide_en_la_misma_transaccion():
    """
    Dos tareas DISTINTAS con ámbitos que se solapan, reclamadas a la vez.

    La regla de un solo escritor también tiene que ser atómica: si la
    comprobación de ámbitos viviera fuera de la transacción de la toma,
    ambas tareas podrían pasarla y quedarían dos escritores sobre el mismo
    archivo.
    """
    raiz = crear_repositorio()

    try:
        compartido = "modulos/demostracion/compartido.py"

        ficha_minima(raiz, "T-0901", ambito_archivos=[compartido])
        ficha_minima(raiz, "T-0902", ambito_archivos=["modulos/demostracion/*.py"])

        respuestas_por_ronda = []

        with Arnes(2) as arnes:
            for numero in range(1, ARGUMENTOS.carreras + 1):
                # Cada contendiente pide una tarea distinta: la colisión no
                # es por la fila, sino por el ámbito que ambas declaran.
                for indice, entrada in enumerate(arnes.entradas):
                    entrada.put(
                        {
                            "orden": "carrera",
                            "ronda": numero,
                            "raiz": str(raiz),
                            "tarea": "T-090" + str(indice + 1),
                            "modo": MODO_ATOMICO,
                            "trabajador": "equipo/a" + str(indice)
                            + "/r" + str(numero),
                        }
                    )

                respuestas = [
                    arnes.salida.get(timeout=ESPERA_RESULTADO_S)
                    for _ in range(2)
                ]

                respuestas_por_ronda.append(respuestas)

                fatales = [r["fatal"] for r in respuestas if r.get("fatal")]
                assert not fatales, "; ".join(fatales)

                ganadores = [r for r in respuestas if r.get("otorgado")]

                assert len(ganadores) == 1, (
                    "Dos tareas con ámbitos solapados no pueden estar ambas "
                    "en ejecución. Ronda " + str(numero) + ": "
                    + repr(respuestas)
                )

                perdedor = [r for r in respuestas if not r.get("otorgado")][0]

                assert perdedor.get("motivo") == "solapamiento", perdedor

                en_ejecucion = [
                    identificador
                    for identificador in ("T-0901", "T-0902")
                    if fila_de(raiz, identificador)["estado"]
                    == str(Estado.EN_EJECUCION)
                ]

                assert len(en_ejecucion) == 1, en_ejecucion

                _liberar(raiz, en_ejecucion[0])

        informe = _juzgar_carreras("ámbitos cruzados x2", respuestas_por_ronda)

        _acumular(informe)
        _exigir_un_solo_ganador(informe)

        informe["INTEGRIDAD"] = comprobar_integridad(raiz)

        assert informe["INTEGRIDAD"].lower() == "ok", informe
    finally:
        borrar(raiz)


def prueba_la_toma_no_pisa_el_ambito_de_una_tarea_viva():
    """
    Refrescar definiciones no puede borrar el ámbito de una tarea en marcha.

    La base global es única, pero cada worktree puede estar en una rama
    distinta y traer su propia versión de la MISMA ficha. Si `tomar`
    refrescara todas las definiciones antes de comprobar los ámbitos,
    reescribiría el `ambito_archivos` registrado de una tarea que otro
    trabajador tiene en ejecución, y acto seguido no vería el solapamiento:
    dos trabajadores escribiendo los mismos archivos.

    Por eso `tomar` incorpora las tareas que faltan pero no refresca las
    que ya están. La suya propia sí está al día: la sincronizó `cargar`.
    """
    raiz = crear_repositorio()

    try:
        compartido = "modulos/comun/*.py"

        ficha_minima(raiz, "T-0901", ambito_archivos=[compartido])
        nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/vivo/1")

        # Otra rama declara la MISMA tarea con otro ámbito. Llega al árbol
        # sin pasar por el Supervisor, igual que un `git checkout`.
        otra_rama = fichas.leer(raiz, "T-0901")
        otra_rama.ambito_archivos = ["modulos/otro/*.py"]
        fichas.guardar(raiz, otra_rama)

        ficha_minima(raiz, "T-0902", ambito_archivos=[compartido])

        try:
            nucleo.tomar(raiz, "T-0902", trabajador_id="equipo/intruso/1")
        except nucleo.ErrorSolapamiento as choque:
            assert "T-0901" in str(choque), str(choque)
        else:
            raise AssertionError(
                "Dos tareas quedaron en ejecución sobre el mismo ámbito: el "
                "refresco de definiciones borró el ámbito de la tarea viva."
            )

        viva = fila_de(raiz, "T-0901")

        assert viva["ambito_archivos"] == [compartido], (
            "El ámbito registrado de la tarea viva se reescribió: "
            + repr(viva["ambito_archivos"])
        )
        assert viva["trabajador_id"] == "equipo/vivo/1", viva

        intrusa = fila_de(raiz, "T-0902")

        assert intrusa["estado"] == str(Estado.NUEVO), intrusa
        assert intrusa["trabajador_id"] is None, intrusa
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# D, E, F. Segundas tomas sobre una tarea ya reclamada
# ----------------------------------------------------------------------

def prueba_d_toma_sobre_tarea_ya_reclamada():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/primero/1")

        antes = fila_de(raiz, "T-0901")

        try:
            nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/segundo/1")
        except nucleo.ErrorToma as rechazo:
            assert rechazo.motivo == estado_global.MOTIVO_YA_RECLAMADA
            assert rechazo.propietario == "equipo/primero/1", rechazo.propietario
            assert rechazo.estado == str(Estado.EN_EJECUCION), rechazo.estado
            assert rechazo.propia is False
        else:
            raise AssertionError("La segunda toma no debió concederse.")

        # Un rechazo no escribe NADA.
        assert fila_de(raiz, "T-0901") == antes, "el rechazo modificó la fila"
    finally:
        borrar(raiz)


def prueba_e_mismo_trabajador_reclama_otra_vez():
    """
    Reclamar dos veces con la misma identidad se RECHAZA, no se concede.

    Conceder la segunda reiniciaría `iniciado_en` y emitiría una transición
    en_ejecucion -> en_ejecucion, que la máquina de estados no admite.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/mismo/1")

        antes = fila_de(raiz, "T-0901")
        eventos_antes = len(eventos_de(raiz, "T-0901"))

        try:
            nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/mismo/1")
        except nucleo.ErrorToma as rechazo:
            assert rechazo.motivo == estado_global.MOTIVO_YA_RECLAMADA
            assert rechazo.propia is True, "debe saber que es suya"
            assert rechazo.propietario == "equipo/mismo/1"
        else:
            raise AssertionError("Reclamar dos veces no debió concederse.")

        despues = fila_de(raiz, "T-0901")

        assert despues == antes, "la segunda toma modificó la fila"
        assert despues["iniciado_en"] == antes["iniciado_en"], (
            "la segunda toma reinició el inicio de la ejecución"
        )
        assert len(eventos_de(raiz, "T-0901")) == eventos_antes, (
            "la segunda toma emitió una transición ilegal"
        )
    finally:
        borrar(raiz)


def prueba_f_trabajador_distinto_segunda_toma():
    """Tras devolverla, otro trabajador sí puede tomarla."""
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/primero/1")

        try:
            nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/segundo/1")
        except nucleo.ErrorToma:
            pass
        else:
            raise AssertionError("No debió concederse con la tarea ocupada.")

        nucleo.devolver(raiz, "T-0901", "Se suelta para el siguiente.")

        fila = fila_de(raiz, "T-0901")

        assert fila["estado"] == str(Estado.REABIERTO), fila
        assert fila["trabajador_id"] is None, "devolver debe liberar al dueño"

        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/segundo/1")

        assert ficha.trabajador_id == "equipo/segundo/1", ficha.trabajador_id
        assert fila_de(raiz, "T-0901")["trabajador_id"] == "equipo/segundo/1"
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# G. Rollback ante excepción a mitad de la toma
# ----------------------------------------------------------------------

def _tomar_con_evento_roto(raiz: Path, tarea: str, error):
    """Ejecuta `tomar` con `insertar_evento` averiado a propósito."""
    original = estado_global.insertar_evento

    def romper(*_argumentos, **_claves):
        raise error

    estado_global.insertar_evento = romper

    try:
        nucleo.tomar(raiz, tarea, trabajador_id="equipo/roto/1")
    finally:
        estado_global.insertar_evento = original


def prueba_g_rollback_ante_excepcion():
    """
    Si algo falla DESPUÉS del UPDATE, la toma entera se deshace.

    Se comprueban las dos vías: un error de SQLite (que `transaccion`
    convierte en ErrorEstadoGlobal) y una excepción cualquiera (que
    `transaccion` deja pasar tras el ROLLBACK). En ambas la fila tiene que
    quedar exactamente como estaba: reclamable y sin dueño.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        antes = fila_de(raiz, "T-0901")
        eventos_antes = len(eventos_de(raiz, "T-0901"))

        for error in (
            sqlite3.OperationalError("fallo deliberado de SQLite"),
            RuntimeError("fallo deliberado ajeno a SQLite"),
        ):
            try:
                _tomar_con_evento_roto(raiz, "T-0901", error)
            except (estado_global.ErrorEstadoGlobal, RuntimeError):
                pass
            else:
                raise AssertionError(
                    "La toma no debió completarse con el evento roto."
                )

            despues = fila_de(raiz, "T-0901")

            assert despues == antes, (
                "El ROLLBACK no deshizo la toma: " + repr(despues)
            )
            assert despues["estado"] == str(Estado.NUEVO), despues
            assert despues["trabajador_id"] is None, despues
            assert len(eventos_de(raiz, "T-0901")) == eventos_antes, (
                "quedó un evento de una toma que se deshizo"
            )

        # La base sigue usable: la toma siguiente, ya sin avería, funciona.
        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/sano/1")

        assert ficha.estado == Estado.EN_EJECUCION, ficha.estado

        assert comprobar_integridad(raiz).lower() == "ok"
    finally:
        borrar(raiz)


def prueba_g_rollback_con_la_base_sin_espacio():
    """
    Sin espacio en la base, el fallo llega en español y nada queda colgado.

    Es el caso que más fácilmente rompe una limpieza mal hecha: cuando la
    escritura falla por falta de espacio, SQLite deshace la transacción
    por su cuenta, de modo que el ROLLBACK explícito llega a una
    transacción que ya no existe. Si ese segundo error escapara, taparía
    al primero y quien llama recibiría un fallo desnudo en inglés en lugar
    de `ErrorEstadoGlobal` (que es lo único que la línea de órdenes sabe
    traducir a un código de salida).

    `PRAGMA max_page_count` produce exactamente el mismo error que un
    disco lleno, sin necesidad de llenar ningún disco.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            paginas = con.execute("PRAGMA page_count").fetchone()[0]
            con.execute("PRAGMA max_page_count = " + str(paginas))

            try:
                with estado_global.transaccion(con):
                    for numero in range(20000):
                        con.execute(
                            "INSERT INTO esquema (version, aplicado_en) "
                            "VALUES (?, ?)",
                            (1000 + numero, "relleno"),
                        )
            except estado_global.ErrorEstadoGlobal as error:
                assert "full" in str(error.__cause__ or ""), (
                    "Se perdió la causa real del fallo: "
                    + repr(error.__cause__)
                )
            except sqlite3.Error as error:
                raise AssertionError(
                    "Escapó un error crudo de SQLite en lugar de "
                    "ErrorEstadoGlobal: " + repr(error)
                )
            else:
                raise AssertionError(
                    "La transacción no debió confirmarse sin espacio."
                )

            # Y no quedó ninguna transacción abierta bloqueando la base.
            con.execute("PRAGMA max_page_count = 0")

            with estado_global.transaccion(con):
                estado_global.actualizar_tarea(
                    con, "T-0901", {"max_intentos": 7}
                )
        finally:
            con.close()

        assert fila_de(raiz, "T-0901")["max_intentos"] == 7, (
            "La base quedó inutilizable tras el fallo de espacio."
        )

        # Y la toma normal sigue funcionando.
        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/tras/1")

        assert ficha.estado == Estado.EN_EJECUCION, ficha.estado
        assert comprobar_integridad(raiz).lower() == "ok"
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# H e I. La toma sobrevive al proceso que la hizo
# ----------------------------------------------------------------------

LECTOR_EXTERNO = """
import json
import sys
from pathlib import Path

from ingenieria_supervisor import estado_global

con = estado_global.abrir(estado_global.ruta_base(Path(sys.argv[1])))

try:
    fila = estado_global.obtener_tarea(con, sys.argv[2])
finally:
    con.close()

print(json.dumps({
    "estado": fila["estado"],
    "trabajador_id": fila["trabajador_id"],
    "pid": fila["pid"],
    "iniciado_en": fila["iniciado_en"],
}))
"""


def prueba_h_proceso_nuevo_lee_al_propietario():
    """Un proceso que nunca participó ve al propietario real."""
    import json

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/dueño/1", pid=4242)

        lectura = subprocess.run(
            [sys.executable, "-c", LECTOR_EXTERNO, str(raiz), "T-0901"],
            cwd=str(RAIZ),
            env=corredor.entorno_controlado(RAIZ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

        assert lectura.returncode == 0, lectura.stderr

        visto = json.loads(lectura.stdout.strip().splitlines()[-1])

        assert visto["estado"] == str(Estado.EN_EJECUCION), visto
        assert visto["trabajador_id"] == "equipo/dueño/1", visto
        assert visto["pid"] == 4242, visto
    finally:
        borrar(raiz)


def prueba_i_persistencia_del_claim():
    """
    Lo que la toma confirmó sigue ahí al reabrir la base, y el JSON lo refleja.
    """
    import json

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/dueño/2")

        fila = fila_de(raiz, "T-0901")

        assert fila["trabajador_id"] == "equipo/dueño/2", fila
        assert fila["iniciado_en"] == ficha.iniciado_en, fila

        # El espejo JSON se regeneró a partir de la fila confirmada.
        espejo = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        assert espejo["estado"] == str(Estado.EN_EJECUCION), espejo["estado"]
        assert espejo["trabajador_id"] == "equipo/dueño/2", espejo
        assert espejo["historial"], "la toma debe dejar rastro en el historial"

        # Recargar desde cero devuelve exactamente lo mismo.
        recargada = nucleo.cargar(raiz, "T-0901")

        assert recargada.trabajador_id == "equipo/dueño/2"
        assert recargada.iniciado_en == ficha.iniciado_en
    finally:
        borrar(raiz)


TOMA_QUE_MUERE = """
import os
import sys
from pathlib import Path

from ingenieria_supervisor import supervisor as nucleo


def morir(raiz, ficha):
    # Muerte súbita justo después del COMMIT, al ir a escribir el espejo:
    # el peor instante posible para un corte de energía.
    os._exit(9)


nucleo._regenerar_espejo = morir

nucleo.tomar(Path(sys.argv[1]), "T-0901", trabajador_id="equipo/apagon/1",
             pid=777)

print("no debería llegar aquí")
"""


def prueba_i_la_toma_sobrevive_al_apagon():
    """
    Un corte entre el COMMIT y el espejo no pierde la toma ni bloquea la base.

    Es el requisito de recuperación llevado al peor instante: la
    transacción ya confirmó, pero el proceso muere antes de escribir el
    JSON. SQLite manda, así que la toma sigue ahí; el espejo queda
    atrasado y lo regenera la operación siguiente.
    """
    import json

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        espejo_antes = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        muerte = subprocess.run(
            [sys.executable, "-c", TOMA_QUE_MUERE, str(raiz)],
            cwd=str(RAIZ),
            env=corredor.entorno_controlado(RAIZ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        assert muerte.returncode == 9, (
            "El proceso debía morir de golpe: " + repr(muerte.returncode)
            + " " + muerte.stderr[-400:]
        )

        fila = fila_de(raiz, "T-0901")

        assert fila["estado"] == str(Estado.EN_EJECUCION), fila
        assert fila["trabajador_id"] == "equipo/apagon/1", fila
        assert fila["pid"] == 777, fila

        espejo = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        assert espejo["estado"] == espejo_antes["estado"], (
            "el espejo no debía haberse escrito"
        )

        # La base no quedó bloqueada por el proceso muerto.
        recargada = nucleo.cargar(raiz, "T-0901")

        assert recargada.estado == Estado.EN_EJECUCION, recargada.estado
        assert recargada.trabajador_id == "equipo/apagon/1"

        # Y la operación siguiente regenera el espejo desde SQLite.
        nucleo.devolver(raiz, "T-0901", "Recuperada tras el corte.")

        espejo = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        assert espejo["estado"] == str(Estado.REABIERTO), espejo["estado"]
        assert espejo["trabajador_id"] is None, espejo

        assert comprobar_integridad(raiz).lower() == "ok"
    finally:
        borrar(raiz)


def _cli(raiz: Path, *argumentos) -> subprocess.CompletedProcess:
    """Invoca la línea de órdenes canónica en un proceso nuevo."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "orquestacion.ingenieria_supervisor",
            "--raiz",
            str(raiz),
            *argumentos,
        ],
        cwd=str(RAIZ),
        env=corredor.entorno_controlado(RAIZ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def prueba_h_codigo_de_salida_de_la_toma_rechazada():
    """
    Perder la carrera tiene su propio código de salida: 3, no 2.

    Quien automatice el Supervisor (n8n, un guion) necesita distinguir
    "perdí la carrera, paso a otra tarea" de "el Supervisor está roto,
    me detengo". Si ambos casos salieran con el mismo código, un
    orquestador reintentaría en bucle sobre una avería real.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        primera = _cli(raiz, "tomar", "T-0901", "--trabajador", "equipo/cli/1")

        assert primera.returncode == 0, primera.stdout + primera.stderr

        perdida = _cli(raiz, "tomar", "T-0901", "--trabajador", "equipo/cli/2")

        assert perdida.returncode == 3, (
            "Perder la carrera debía salir con 3: "
            + str(perdida.returncode) + "\n" + perdida.stdout
        )
        assert "TOMA RECHAZADA" in perdida.stdout, perdida.stdout
        assert "equipo/cli/1" in perdida.stdout, (
            "El rechazo debe decir quién tiene la tarea: " + perdida.stdout
        )
        assert estado_global.MOTIVO_YA_RECLAMADA in perdida.stdout, (
            perdida.stdout
        )

        # Un error de verdad NO se confunde con perder la carrera.
        inexistente = _cli(raiz, "tomar", "T-9999", "--trabajador", "equipo/x")

        assert inexistente.returncode == 2, (
            "Una tarea inexistente es un error, no una carrera perdida: "
            + str(inexistente.returncode) + "\n" + inexistente.stdout
        )

        # El ámbito en conflicto tampoco: no es una carrera reintentable.
        ficha_minima(
            raiz, "T-0902",
            ambito_archivos=["modulos/demostracion/T-0901.py"],
        )

        choque = _cli(raiz, "tomar", "T-0902", "--trabajador", "equipo/cli/3")

        assert choque.returncode == 2, (
            "El ámbito en conflicto debía salir con 2: "
            + str(choque.returncode) + "\n" + choque.stdout
        )
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# J. Integridad de la base tras las carreras
# ----------------------------------------------------------------------

def prueba_j_integridad_sqlite():
    """
    Ninguna carrera de esta ejecución dejó la base dañada.

    Primero somete una base propia a una carrera y la revisa a fondo; sólo
    después repasa los `PRAGMA integrity_check` que los demás bloques
    ejecutaron sobre SUS bases antes de borrarlas. En ese orden la
    comprobación vale igual ejecutada sola que dentro de la tanda completa.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        with Arnes(4) as arnes:
            for numero in range(1, 4):
                respuestas = arnes.ronda(raiz, "T-0901", MODO_ATOMICO, numero)

                assert sum(
                    1 for r in respuestas if r.get("otorgado")
                ) == 1, respuestas

                _liberar(raiz, "T-0901")

        assert comprobar_integridad(raiz).lower() == "ok"

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            huerfanos = con.execute(
                """
                SELECT COUNT(*) FROM eventos
                 WHERE tarea_id NOT IN (SELECT id FROM tareas)
                """
            ).fetchone()[0]

            duplicadas = con.execute(
                "SELECT COUNT(*) FROM (SELECT id FROM tareas GROUP BY id "
                "HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        finally:
            con.close()

        assert huerfanos == 0, "quedaron eventos sin tarea: " + str(huerfanos)
        assert duplicadas == 0, "quedaron identificadores repetidos"
    finally:
        borrar(raiz)

    # Y ninguna de las bases que esta ejecución sometió a carreras quedó
    # dañada. La de aquí arriba ya cuenta, así que el mínimo se cumple
    # aunque esta comprobación se ejecute sola.
    assert METRICAS["INTEGRITY_CHECKS"] > 0, (
        "No se comprobó la integridad de ninguna base."
    )
    assert METRICAS["INTEGRITY_FAILURES"] == 0, METRICAS


# ----------------------------------------------------------------------
# K y L. Tarea inexistente y estado no reclamable
# ----------------------------------------------------------------------

def prueba_k_toma_de_tarea_inexistente():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                informe = estado_global.reclamar(
                    con,
                    "T-0999",
                    trabajador_id="equipo/fantasma/1",
                    pid=os.getpid(),
                    momento=nucleo.ahora_datetime().isoformat(
                        timespec="seconds"
                    ),
                    estados_reclamables=nucleo.ESTADOS_TOMABLES,
                    estado_destino=str(Estado.EN_EJECUCION),
                )

            assert informe["resultado"] == estado_global.CLAIM_RECHAZADO
            assert informe["motivo"] == estado_global.MOTIVO_INEXISTENTE
            assert informe["estado"] is None, informe
            assert informe["propietario"] is None, informe

            assert estado_global.contar_tareas(con) == 1, (
                "una toma rechazada no debe crear tareas"
            )
        finally:
            con.close()

        # Por la vía del Supervisor el fallo llega antes: no hay ficha.
        try:
            nucleo.tomar(raiz, "T-0999", trabajador_id="equipo/fantasma/2")
        except (fichas.ErrorFicha, nucleo.ErrorSupervisor):
            pass
        else:
            raise AssertionError("Tomar una tarea inexistente no debe pasar.")
    finally:
        borrar(raiz)


def prueba_l_toma_en_estado_no_reclamable():
    """Ni PROPUESTO ni APROBADO ni BLOQUEADO admiten toma."""
    raiz = crear_repositorio()

    try:
        for identificador, estado in (
            ("T-0901", Estado.PROPUESTO),
            ("T-0902", Estado.APROBADO),
            ("T-0903", Estado.BLOQUEADO),
        ):
            ficha_minima(raiz, identificador)

            con = estado_global.abrir(estado_global.ruta_base(raiz))

            try:
                with estado_global.transaccion(con):
                    estado_global.actualizar_tarea(
                        con, identificador, {"estado": str(estado)}
                    )

                with estado_global.transaccion(con):
                    informe = estado_global.reclamar(
                        con,
                        identificador,
                        trabajador_id="equipo/aspirante/1",
                        pid=os.getpid(),
                        momento=nucleo.ahora_datetime().isoformat(
                            timespec="seconds"
                        ),
                        estados_reclamables=nucleo.ESTADOS_TOMABLES,
                        estado_destino=str(Estado.EN_EJECUCION),
                    )
            finally:
                con.close()

            assert informe["resultado"] == estado_global.CLAIM_RECHAZADO, (
                identificador, informe
            )
            assert (
                informe["motivo"] == estado_global.MOTIVO_ESTADO_NO_RECLAMABLE
            ), informe
            assert informe["estado"] == str(estado), informe

            fila = fila_de(raiz, identificador)

            assert fila["estado"] == str(estado), fila
            assert fila["trabajador_id"] is None, fila

            # Y por la vía del Supervisor, el mismo rechazo controlado.
            try:
                nucleo.tomar(
                    raiz, identificador, trabajador_id="equipo/aspirante/2"
                )
            except nucleo.ErrorToma as rechazo:
                assert rechazo.motivo == (
                    estado_global.MOTIVO_ESTADO_NO_RECLAMABLE
                ), rechazo.motivo
                assert rechazo.estado == str(estado), rechazo.estado
            else:
                raise AssertionError(
                    "No debió tomarse una tarea en " + str(estado) + "."
                )
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# M. Datos mal formados y propietario residual
# ----------------------------------------------------------------------

def _reclamar_suelto(raiz: Path, identificador: str, **cambios):
    """Llama a `reclamar` con los parámetros que se quieran romper."""
    parametros = {
        "trabajador_id": "equipo/valido/1",
        "pid": os.getpid(),
        "momento": nucleo.ahora_datetime().isoformat(timespec="seconds"),
        "estados_reclamables": nucleo.ESTADOS_TOMABLES,
        "estado_destino": str(Estado.EN_EJECUCION),
    }
    parametros.update(cambios)

    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        with estado_global.transaccion(con):
            return estado_global.reclamar(con, identificador, **parametros)
    finally:
        con.close()


def prueba_m_datos_mal_formados():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        antes = fila_de(raiz, "T-0901")

        malos = [
            ("identidad vacía", {"trabajador_id": ""}),
            ("identidad en blanco", {"trabajador_id": "   "}),
            ("identidad que no es texto", {"trabajador_id": 17}),
            ("identidad nula", {"trabajador_id": None}),
            ("pid cero", {"pid": 0}),
            ("pid negativo", {"pid": -3}),
            ("pid booleano", {"pid": True}),
            ("pid que no es entero", {"pid": "1234"}),
            ("sin estados reclamables", {"estados_reclamables": []}),
            (
                "destino también reclamable",
                {"estado_destino": str(Estado.REABIERTO)},
            ),
            (
                "columna que la toma no puede fijar",
                {"campos_extra": {"trabajador_id": "otro"}},
            ),
            (
                "columna inexistente",
                {"campos_extra": {"columna_inventada": 1}},
            ),
        ]

        for etiqueta, cambios in malos:
            try:
                _reclamar_suelto(raiz, "T-0901", **cambios)
            except estado_global.ErrorEstadoGlobal:
                pass
            else:
                raise AssertionError(
                    "Debió rechazarse por datos mal formados: " + etiqueta
                )

            assert fila_de(raiz, "T-0901") == antes, (
                "Los datos mal formados modificaron la fila: " + etiqueta
            )

        # pid ausente sí es legítimo: un trabajador puede no declararlo.
        informe = _reclamar_suelto(raiz, "T-0901", pid=None)

        assert informe["resultado"] == estado_global.CLAIM_OTORGADO, informe
        assert fila_de(raiz, "T-0901")["pid"] is None
    finally:
        borrar(raiz)


def prueba_m_propietario_residual_no_bloquea():
    """
    Una fila reclamable con propietario residual se puede tomar, y queda anotado.

    El WHERE del UPDATE condiciona sólo por `estado`, no por
    `trabajador_id IS NULL`. Exigir además el dueño nulo dejaría esa fila
    intomable para siempre, y la recuperación automática está fuera de
    A3.1. En su lugar la toma desplaza al residual y lo registra en el
    evento, para que el cambio sea trazable.

    Este estado no lo produce el Supervisor: devolver, verificar y reanudar
    liberan siempre al trabajador. Sólo puede venir de una ficha V1
    importada a medias o de una edición externa.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                estado_global.actualizar_tarea(
                    con,
                    "T-0901",
                    {
                        "estado": str(Estado.REABIERTO),
                        "trabajador_id": "equipo/fantasma/9",
                        "pid": 999999,
                    },
                )
        finally:
            con.close()

        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/nuevo/1")

        assert ficha.trabajador_id == "equipo/nuevo/1", ficha.trabajador_id

        transiciones = [
            evento for evento in eventos_de(raiz, "T-0901")
            if evento["tipo"] == estado_global.EVENTO_TRANSICION
        ]

        assert transiciones, "la toma debe dejar su transición"

        datos = transiciones[-1]["datos"]

        assert datos.get("propietario_desplazado") == "equipo/fantasma/9", (
            "El desplazamiento del propietario residual no quedó anotado: "
            + repr(datos)
        )
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# N. Idempotencia: un rechazo no deja rastro
# ----------------------------------------------------------------------

def prueba_n_idempotencia_del_rechazo():
    """
    Repetir una toma rechazada no cambia nada, por muchas veces que se repita.

    Un rechazo no es una operación a medias: no escribe la fila, no añade
    eventos y no mueve `actualizado_en`.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/dueño/1")

        antes = fila_de(raiz, "T-0901")
        eventos_antes = eventos_de(raiz, "T-0901")

        for intento in range(5):
            try:
                nucleo.tomar(
                    raiz, "T-0901",
                    trabajador_id="equipo/insistente/" + str(intento),
                )
            except nucleo.ErrorToma:
                pass
            else:
                raise AssertionError("Ninguna de estas tomas debió concederse.")

        assert fila_de(raiz, "T-0901") == antes, "un rechazo modificó la fila"
        assert eventos_de(raiz, "T-0901") == eventos_antes, (
            "un rechazo añadió eventos"
        )

        # Y conceder la toma sí es idempotente en su efecto: el estado final
        # tras devolver y volver a tomar es equivalente, no acumulativo.
        nucleo.devolver(raiz, "T-0901", "Se suelta.")
        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/ciclo/1")
        nucleo.devolver(raiz, "T-0901", "Se suelta otra vez.")
        segunda = nucleo.tomar(raiz, "T-0901", trabajador_id="equipo/ciclo/2")

        assert primera.estado == segunda.estado == Estado.EN_EJECUCION
        assert segunda.trabajador_id == "equipo/ciclo/2"
        assert fila_de(raiz, "T-0901")["pid"] == os.getpid()
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# Informe de métricas
# ----------------------------------------------------------------------

def imprimir_metricas() -> None:
    print("")
    print("  MÉTRICAS DE CONCURRENCIA (medidas, no estimadas)")
    print("")

    for informe in INFORMES:
        print(
            "    · " + informe["ETIQUETA"].ljust(24)
            + "  carreras=" + str(informe["RACE_RUNS"]).rjust(4)
            + "  contendientes=" + str(informe["CONTENDIENTES"]).rjust(3)
            + "  tomas=" + str(informe["CLAIMS_SUCCESS"]).rjust(4)
            + "  rechazos=" + str(informe["CLAIMS_REJECTED"]).rjust(5)
            + "  dobles=" + str(informe["DOUBLE_CLAIMS"]).rjust(3)
        )

    print("")
    print("    RACE_RUNS                = " + str(METRICAS["TOTAL_RACES"]))
    print(
        "    CLAIMS_SUCCESS           = "
        + str(METRICAS["TOTAL_SUCCESSFUL_CLAIMS"])
    )
    print(
        "    CLAIMS_REJECTED          = "
        + str(METRICAS["TOTAL_REJECTED_CLAIMS"])
    )
    print(
        "    DOUBLE_CLAIM_EVENTS      = "
        + str(METRICAS["DOUBLE_CLAIM_EVENTS"])
    )
    print("    SQLITE_ERRORS            = " + str(METRICAS["SQLITE_ERRORS"]))
    print(
        "    UNEXPECTED_EXCEPTIONS    = "
        + str(METRICAS["UNEXPECTED_EXCEPTIONS"])
    )
    print(
        "    SQLITE_INTEGRITY         = "
        + str(METRICAS["INTEGRITY_CHECKS"]) + " comprobaciones, "
        + str(METRICAS["INTEGRITY_FAILURES"]) + " fallos"
    )
    print("")


# ----------------------------------------------------------------------
# Corredor del archivo
# ----------------------------------------------------------------------

COMPROBACIONES = [
    ("A. toma normal sin competencia", prueba_a_toma_normal),
    ("el arnés detecta la carrera (control)",
     prueba_el_detector_detecta_la_carrera),
    ("B. dos trabajadores compitiendo",
     prueba_b_dos_trabajadores_compitiendo),
    ("C. diez contendientes", prueba_c_diez_contendientes),
    ("carrera entre conexiones sobre BEGIN IMMEDIATE",
     prueba_carrera_entre_conexiones),
    ("ámbitos solapados decididos en la misma transacción",
     prueba_ambito_se_decide_en_la_misma_transaccion),
    ("la toma no pisa el ámbito de una tarea viva",
     prueba_la_toma_no_pisa_el_ambito_de_una_tarea_viva),
    ("D. toma sobre tarea ya reclamada",
     prueba_d_toma_sobre_tarea_ya_reclamada),
    ("E. mismo trabajador reclamando otra vez",
     prueba_e_mismo_trabajador_reclama_otra_vez),
    ("F. trabajador distinto, segunda toma",
     prueba_f_trabajador_distinto_segunda_toma),
    ("G. rollback ante excepción", prueba_g_rollback_ante_excepcion),
    ("G. rollback con la base sin espacio",
     prueba_g_rollback_con_la_base_sin_espacio),
    ("H. proceso nuevo lee al propietario",
     prueba_h_proceso_nuevo_lee_al_propietario),
    ("H. código de salida de la toma rechazada",
     prueba_h_codigo_de_salida_de_la_toma_rechazada),
    ("I. persistencia de la toma", prueba_i_persistencia_del_claim),
    ("I. la toma sobrevive al apagón", prueba_i_la_toma_sobrevive_al_apagon),
    ("J. integridad de SQLite", prueba_j_integridad_sqlite),
    ("K. toma de tarea inexistente", prueba_k_toma_de_tarea_inexistente),
    ("L. toma en estado no reclamable",
     prueba_l_toma_en_estado_no_reclamable),
    ("M. datos mal formados", prueba_m_datos_mal_formados),
    ("M. propietario residual no bloquea",
     prueba_m_propietario_residual_no_bloquea),
    ("N. idempotencia del rechazo", prueba_n_idempotencia_del_rechazo),
]


def prueba_toma_atomica() -> None:
    inicio = time.monotonic()

    for numero, (nombre, comprobacion) in enumerate(COMPROBACIONES, start=1):
        arranque = time.monotonic()
        comprobacion()
        print(
            "  " + str(numero).rjust(2) + ". " + nombre + ": OK"
            + "  (" + format(time.monotonic() - arranque, ".1f") + " s)"
        )

    imprimir_metricas()

    assert METRICAS["DOUBLE_CLAIM_EVENTS"] == 0, METRICAS
    assert METRICAS["TOTAL_SUCCESSFUL_CLAIMS"] == METRICAS["TOTAL_RACES"], (
        "Debe haber exactamente una toma concedida por carrera: " + repr(METRICAS)
    )
    assert METRICAS["INTEGRITY_FAILURES"] == 0, METRICAS

    print(
        "  Tiempo total: "
        + format(time.monotonic() - inicio, ".1f") + " s"
    )
    print("PRUEBA_TOMA_ATOMICA=OK")


def leer_argumentos(argumentos=None):
    analizador = argparse.ArgumentParser(
        description="Pruebas de la toma atómica de tareas (A3.1).",
    )

    analizador.add_argument(
        "--carreras",
        type=int,
        default=RONDAS_POR_OMISION,
        help=(
            "Rondas de carrera por configuración. El valor por omisión ("
            + str(RONDAS_POR_OMISION)
            + ") está calibrado para el corredor único; súbelo para una "
            "corrida de estrés."
        ),
    )

    leidos = analizador.parse_args(argumentos)

    if leidos.carreras < 1:
        analizador.error("--carreras debe ser 1 o más.")

    return leidos


# Valores por omisión para cuando el módulo se importa (o lo reimporta un
# proceso hijo de "spawn", que no recibe la línea de órdenes del padre).
ARGUMENTOS = leer_argumentos([])


if __name__ == "__main__":
    ARGUMENTOS = leer_argumentos()

    prueba_toma_atomica()
