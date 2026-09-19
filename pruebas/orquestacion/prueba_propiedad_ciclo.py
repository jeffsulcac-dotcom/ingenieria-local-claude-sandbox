"""
Pruebas de A3.2: PROPIEDAD EFECTIVA DURANTE TODO EL CICLO.

A3.1 ya demostró que una tarea sólo puede ser TOMADA por un trabajador.
Aquí hay que demostrar lo siguiente: que después de la toma, sólo el
PROPIETARIO VIGENTE puede modificar el estado operativo de esa ejecución, y
que una orden emitida por un dueño anterior no entra aunque llegue tarde.

Qué es exactamente una "orden rezagada"
---------------------------------------
No es una llamada nueva que relee la base. Una llamada que relee ve al dueño
actual y, si se acreditara con lo que acaba de leer, estaría suplantándolo:
ninguna condición del mundo podría distinguirla del dueño legítimo.

Una orden rezagada es la que se COMPUSO contra una lectura anterior y llega
después: lleva consigo la identidad y la generación que tenía entonces. Eso
es lo que se reproduce aquí, de las dos maneras en que puede ocurrir:

1. En proceso: se conserva la ficha que el trabajador tenía en memoria y se
   emite la orden con ella, que es literalmente lo que pasa cuando un
   trabajador lento termina su tarea después de haberla perdido.

2. Por la línea de órdenes: se acredita con `--trabajador` y `--generacion`,
   los valores que `tomar` imprimió en su momento. Es como un guion, n8n o
   un trabajador externo emitiría una orden que quedó en cola.

Por qué la generación y no el nombre del trabajador
---------------------------------------------------
Porque `trabajador_id` no distingue una ejecución vieja de una nueva del
MISMO trabajador. La prueba del problema ABA (grupo 2) lo demuestra: el
mismo worker toma, pierde y vuelve a tomar la tarea, y su orden antigua
tiene que ser rechazada aunque el nombre coincida carácter por carácter.

Escrituras indebidas
--------------------
Toda comprobación de rechazo termina verificando que el estado del dueño
vigente quedó intacto: propietario, generación, estado, intentos, latido y
marcas de tiempo. Cualquier diferencia cuenta como ESCRITURA INDEBIDA y se
acumula en las métricas. El valor esperado es 0, siempre.

Todo es hermético: repositorios Git temporales con su propia base SQLite.
Jamás se toca el repositorio real ni su base.

Ejecución por omisión: pensada para terminar muy por debajo del tiempo
límite del corredor único (120 s). Para la corrida de estrés:

    python pruebas/orquestacion/prueba_propiedad_ciclo.py --rezagadas 200
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
from ingenieria_supervisor import supervisor as nucleo
from ingenieria_supervisor import tarea as fichas


PRUEBA_VERDE = "print('PRUEBA_VERDE=OK')\n"

# Órdenes rezagadas que lanza la corrida de estrés cuando no se indica otra.
REZAGADAS_POR_OMISION = 60

# Procesos que arrancan a la vez contra una base nueva (sección 9).
PROCESOS_BOOTSTRAP = 6

# Emisores concurrentes de órdenes rezagadas y órdenes por emisor.
EMISORES_POR_OMISION = 6
ORDENES_POR_EMISOR = 15

# Rondas de la carrera de bootstrap.
RONDAS_BOOTSTRAP = 3

# Ninguna espera es indefinida, y todas quedan MUY por debajo del límite que
# el corredor único concede al archivo (120 s), para que un cuelgue lo
# diagnostique esta prueba y no el corredor.
ESPERA_BARRERA_S = 20
ESPERA_PROCESO_S = 60
ESPERA_SUBPROCESO_S = 60
ESPERA_GIT_S = 30


# ----------------------------------------------------------------------
# Métricas reales acumuladas durante toda la ejecución
# ----------------------------------------------------------------------

METRICAS = {
    "ORDENES_TOTALES": 0,
    "ORDENES_ACEPTADAS": 0,
    "ORDENES_RECHAZADAS": 0,
    "ESCRITURAS_INDEBIDAS": 0,
    "ERRORES_SQLITE": 0,
    "EXCEPCIONES_INESPERADAS": 0,
    "COMPROBACIONES_INTEGRIDAD": 0,
    "FALLOS_INTEGRIDAD": 0,
    "PROCESOS_BOOTSTRAP": 0,
    "FALLOS_BOOTSTRAP": 0,
    "CAMBIOS_DE_PROPIEDAD": 0,
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


def crear_repositorio(prefijo="propiedad_ciclo_") -> Path:
    """Repositorio Git temporal con su propia base SQLite global."""
    raiz = Path(tempfile.mkdtemp(prefix=prefijo))

    inicio = _git(raiz, "init", "-q", "-b", "main")
    assert inicio.returncode == 0, inicio.stderr

    _git(raiz, "config", "user.name", "Prueba A3.2")
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
        "titulo": "Tarea con dueño " + identificador,
        "objetivo": "Comprobar la propiedad durante el ciclo.",
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
# Testigo del estado del dueño vigente: detecta ESCRITURAS INDEBIDAS
# ----------------------------------------------------------------------

# Columnas cuyo cambio significaría que una orden rechazada escribió.
COLUMNAS_VIGILADAS = (
    "estado",
    "trabajador_id",
    "generacion",
    "pid",
    "iniciado_en",
    "ultimo_latido",
    "intentos",
    "actualizado_en",
    "ultima_falla",
    "ambito_archivos",
    "rama",
)


def testigo(raiz: Path, identificador: str) -> dict:
    """Foto de las columnas que una orden rechazada no debe tocar."""
    fila = fila_de(raiz, identificador)

    return {columna: fila[columna] for columna in COLUMNAS_VIGILADAS}


def exigir_intacto(raiz: Path, identificador: str, antes: dict, etiqueta: str):
    """
    El dueño vigente no cambió. Cualquier diferencia es escritura indebida.

    Comprueba las columnas una a una en vez de comparar los diccionarios de
    golpe: así el fallo dice QUÉ campo se movió, que es lo que hace falta
    para diagnosticarlo.
    """
    despues = testigo(raiz, identificador)

    diferencias = [
        columna + ": " + repr(antes[columna]) + " -> " + repr(despues[columna])
        for columna in COLUMNAS_VIGILADAS
        if antes[columna] != despues[columna]
    ]

    if diferencias:
        METRICAS["ESCRITURAS_INDEBIDAS"] += len(diferencias)

    assert not diferencias, (
        etiqueta + ": una orden rechazada modificó el estado del propietario "
        "vigente. Cambios: " + "; ".join(diferencias)
    )


def exigir_ficha_sin_marcas_falsas(ficha, antes_actualizado, antes_creado,
                                   etiqueta: str):
    """
    Tras un rechazo, la ficha en memoria tampoco debe parecer escrita.

    Si `persistir` dejase puesto el `actualizado_en` que calculó antes de
    intentar la escritura, la ficha diría que se actualizó cuando no se
    actualizó nada. Quien la tuviera en la mano —o quien la volcara a un
    JSON— estaría propagando una marca de tiempo falsa.
    """
    assert ficha.actualizado_en == antes_actualizado, (
        etiqueta + ": la ficha rechazada quedó con `actualizado_en` nuevo ("
        + repr(antes_actualizado) + " -> " + repr(ficha.actualizado_en)
        + "), como si la orden hubiera entrado."
    )
    assert ficha.creado_en == antes_creado, (
        etiqueta + ": la ficha rechazada quedó con `creado_en` cambiado."
    )


def exigir_rechazo(funcion, etiqueta: str, motivo_esperado=None):
    """
    Ejecuta la orden esperando ErrorPropiedad y devuelve su informe.

    Un rechazo por propiedad NUNCA debe llegar como excepción genérica: eso
    lo dejaría indistinguible de una avería y, en la línea de órdenes, con
    el código de salida equivocado.
    """
    METRICAS["ORDENES_TOTALES"] += 1

    try:
        funcion()
    except nucleo.ErrorPropiedad as rechazo:
        METRICAS["ORDENES_RECHAZADAS"] += 1

        if motivo_esperado is not None:
            assert rechazo.motivo == motivo_esperado, (
                etiqueta + ": se esperaba el motivo '" + str(motivo_esperado)
                + "' y llegó '" + str(rechazo.motivo) + "'."
            )

        return rechazo.informe
    except sqlite3.Error as error:
        METRICAS["ERRORES_SQLITE"] += 1
        raise AssertionError(
            etiqueta + ": la orden rechazada produjo un error de SQLite: "
            + str(error)
        ) from error
    except Exception as error:
        METRICAS["EXCEPCIONES_INESPERADAS"] += 1
        raise AssertionError(
            etiqueta + ": se esperaba ErrorPropiedad y llegó "
            + type(error).__name__ + ": " + str(error)
        ) from error

    METRICAS["ORDENES_ACEPTADAS"] += 1

    raise AssertionError(
        etiqueta + ": la orden rezagada fue ACEPTADA. Debía rechazarse."
    )


# ----------------------------------------------------------------------
# Montaje del escenario: A toma, A pierde, B toma
# ----------------------------------------------------------------------

def escenario_relevo(raiz: Path, identificador="T-0901", trabajador_b="worker-B"):
    """
    Deja la tarea en manos de B y devuelve la ficha VIEJA de A.

    Esa ficha vieja es la orden rezagada en estado puro: se compuso cuando A
    era el dueño y conserva su identidad y su generación.
    """
    ficha_minima(raiz, identificador)

    vieja = nucleo.tomar(raiz, identificador, trabajador_id="worker-A")
    METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1

    assert vieja.trabajador_id == "worker-A"
    assert vieja.generacion >= 1, (
        "La toma debe conceder una generación; llegó: "
        + repr(vieja.generacion)
    )

    # La copia se hace ANTES de soltar la tarea: es lo que A tenía en la
    # mano. Si se leyera después, ya no sería una orden rezagada.
    congelada = copy.deepcopy(vieja)

    nucleo.devolver(raiz, identificador)

    nueva = nucleo.tomar(raiz, identificador, trabajador_id=trabajador_b)
    METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1

    assert nueva.trabajador_id == trabajador_b
    assert nueva.generacion > congelada.generacion, (
        "Cada toma tiene que subir la generación: "
        + str(congelada.generacion) + " -> " + str(nueva.generacion)
    )

    return congelada, nueva


# ----------------------------------------------------------------------
# Las órdenes rezagadas, emitidas tal y como las emitiría su dueño viejo
# ----------------------------------------------------------------------

def latido_rezagado(raiz: Path, vieja):
    """`latido` compuesto por A con su ficha de entonces."""
    ficha = copy.deepcopy(vieja)
    ficha.ultimo_latido = "2020-01-01T00:00:00+00:00"

    nucleo.persistir(
        raiz,
        ficha,
        exigir_propietario=vieja.trabajador_id,
        estados_admitidos={Estado.EN_EJECUCION},
        exigir_generacion=vieja.generacion,
    )


def devolucion_rezagada(raiz: Path, vieja):
    """`devolver` compuesto por A: dejaría la tarea TOMABLE."""
    ficha = copy.deepcopy(vieja)
    nucleo._liberar_trabajador(ficha)
    nucleo.transicionar(
        ficha, Estado.REABIERTO, "Devolución rezagada.", nucleo.ORIGEN_AUTOMATICO
    )

    nucleo.persistir(
        raiz,
        ficha,
        exigir_propietario=vieja.trabajador_id,
        estados_admitidos={Estado.EN_EJECUCION},
        exigir_generacion=vieja.generacion,
    )


def verificacion_rezagada(raiz: Path, vieja):
    """
    `verificar` compuesto por A.

    Es el caso con la ventana más ancha del sistema: entre leer la tarea y
    escribir el resultado, `verificar` corre la batería entera de pruebas.
    """
    ficha = copy.deepcopy(vieja)
    ficha.intentos = ficha.intentos + 1
    nucleo._liberar_trabajador(ficha)
    nucleo.transicionar(
        ficha,
        Estado.REQUIERE_REVISION,
        "Verificación rezagada.",
        nucleo.ORIGEN_AUTOMATICO,
    )

    nucleo.persistir(
        raiz,
        ficha,
        exigir_propietario=vieja.trabajador_id,
        estados_admitidos={Estado.EN_EJECUCION},
        exigir_generacion=vieja.generacion,
    )


def persistencia_rezagada(raiz: Path, vieja):
    """
    `persistir` a secas: una orden sin propietario declarado.

    Representa a las órdenes humanas (decidir, aprobar, bloquear...), que no
    tienen dueño pero tampoco deben pisar una ejecución que empezó mientras
    su emisor decidía.
    """
    ficha = copy.deepcopy(vieja)
    ficha.ultima_falla = {"motivo": "Escritura rezagada sin propietario."}

    nucleo.persistir(raiz, ficha)


REZAGADAS = (
    ("latido", latido_rezagado),
    ("devolver", devolucion_rezagada),
    ("verificar", verificacion_rezagada),
    ("persistir", persistencia_rezagada),
)


# ----------------------------------------------------------------------
# GRUPO 1 — Órdenes rezagadas
# ----------------------------------------------------------------------

def prueba_a_ordenes_rezagadas_rechazadas():
    print("  1. órdenes rezagadas de A no tocan a B:", end=" ")

    for nombre, emitir in REZAGADAS:
        raiz = crear_repositorio()

        try:
            vieja, _ = escenario_relevo(raiz)

            antes = testigo(raiz, "T-0901")

            informe = exigir_rechazo(
                lambda: emitir(raiz, vieja),
                "orden rezagada '" + nombre + "'",
                estado_global.MOTIVO_GENERACION_VENCIDA,
            )

            assert informe["resultado"] == estado_global.ESCRITURA_RECHAZADA
            assert informe["propietario_vigente"] == "worker-B", (
                "El informe debe nombrar al dueño vigente; dijo: "
                + repr(informe["propietario_vigente"])
            )
            assert informe["generacion"] < informe["generacion_vigente"]

            exigir_intacto(raiz, "T-0901", antes, "rezagada '" + nombre + "'")
            comprobar_integridad(raiz)
        finally:
            borrar(raiz)

        print(nombre, end=" ")

    print("OK")


def prueba_b_el_rechazo_no_gasta_intentos_ni_eventos():
    print("  2. un rechazo no gasta intentos ni deja eventos:", end=" ")

    raiz = crear_repositorio()

    try:
        vieja, nueva = escenario_relevo(raiz)

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            eventos_antes = estado_global.contar_eventos(con)
        finally:
            con.close()

        intentos_antes = fila_de(raiz, "T-0901")["intentos"]
        antes = testigo(raiz, "T-0901")

        # `verificar` rezagado es el que más daño haría: sube `intentos` y
        # cambia de estado. Ni una cosa ni la otra pueden ocurrir.
        exigir_rechazo(
            lambda: verificacion_rezagada(raiz, vieja),
            "verificación rezagada",
            estado_global.MOTIVO_GENERACION_VENCIDA,
        )

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            eventos_despues = estado_global.contar_eventos(con)
        finally:
            con.close()

        assert eventos_despues == eventos_antes, (
            "El rechazo escribió " + str(eventos_despues - eventos_antes)
            + " evento(s). Una orden rechazada no deja historial de éxito."
        )

        assert fila_de(raiz, "T-0901")["intentos"] == intentos_antes, (
            "El rechazo consumió un intento."
        )

        exigir_intacto(raiz, "T-0901", antes, "rechazo sin efectos")

        # Y el dueño vigente sigue pudiendo trabajar: el rechazo del otro no
        # le dejó la tarea en un estado imposible.
        latido = nucleo.latido(
            raiz,
            "T-0901",
            trabajador_id=nueva.trabajador_id,
            generacion=nueva.generacion,
        )
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        assert latido.trabajador_id == "worker-B"

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_c_el_espejo_json_no_se_regenera_en_un_rechazo():
    print("  3. un rechazo no reescribe el espejo JSON:", end=" ")

    raiz = crear_repositorio()

    try:
        vieja, _ = escenario_relevo(raiz)

        ruta_json = fichas.ruta_ficha(raiz, "T-0901")
        antes_json = ruta_json.read_text(encoding="utf-8")
        antes = testigo(raiz, "T-0901")

        # Se emite con una ficha concreta para poder mirarla DESPUÉS: no
        # basta con que la base quede intacta, la ficha en memoria tampoco
        # puede quedar con marcas de tiempo que sugieran que la orden entró.
        rezagada = copy.deepcopy(vieja)
        rezagada.ultimo_latido = "2020-01-01T00:00:00+00:00"

        # Centinela inconfundible. `ahora_utc()` se trunca a segundos, así
        # que comparar contra la marca real de la ficha no valdría: en una
        # prueba rápida las dos caerían en el mismo segundo y una
        # sobrescritura pasaría por idéntica.
        rezagada.actualizado_en = "2020-01-01T00:00:00+00:00"
        rezagada.creado_en = "2019-01-01T00:00:00+00:00"
        marca_actualizado = rezagada.actualizado_en
        marca_creado = rezagada.creado_en

        exigir_rechazo(
            lambda: nucleo.persistir(
                raiz,
                rezagada,
                exigir_propietario=vieja.trabajador_id,
                estados_admitidos={Estado.EN_EJECUCION},
                exigir_generacion=vieja.generacion,
            ),
            "latido rezagado",
            estado_global.MOTIVO_GENERACION_VENCIDA,
        )

        exigir_ficha_sin_marcas_falsas(
            rezagada, marca_actualizado, marca_creado, "latido rezagado"
        )

        assert ruta_json.read_text(encoding="utf-8") == antes_json, (
            "El espejo JSON cambió tras un rechazo. SQLite y el espejo "
            "habrían quedado contando historias distintas."
        )

        exigir_intacto(raiz, "T-0901", antes, "espejo tras rechazo")
        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 2 — Mismo trabajador, nueva ejecución (problema ABA)
# ----------------------------------------------------------------------

def prueba_d_mismo_trabajador_nueva_ejecucion():
    print("  4. mismo trabajador, ejecución nueva (ABA):", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        # E1
        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-01")
        METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1
        congelada = copy.deepcopy(primera)

        nucleo.devolver(raiz, "T-0901")

        # E2: el MISMO trabajador vuelve a tomarla.
        segunda = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-01")
        METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1

        assert segunda.trabajador_id == congelada.trabajador_id, (
            "El escenario exige que el trabajador sea idéntico; si no, no "
            "se está probando el problema ABA."
        )
        assert segunda.generacion > congelada.generacion, (
            "Dos ejecuciones del mismo trabajador tienen que diferenciarse "
            "por la generación."
        )

        antes = testigo(raiz, "T-0901")

        # Cada orden de E1 debe caer, pese a que el nombre coincide.
        for nombre, emitir in REZAGADAS:
            informe = exigir_rechazo(
                lambda: emitir(raiz, congelada),
                "E1 '" + nombre + "' con worker-01 repetido",
                estado_global.MOTIVO_GENERACION_VENCIDA,
            )

            assert informe["propietario_vigente"] == "worker-01", (
                "El dueño vigente es el mismo nombre: por eso el nombre no "
                "puede ser el criterio."
            )

            exigir_intacto(raiz, "T-0901", antes, "ABA '" + nombre + "'")

        # Contraprueba: la MISMA orden, con la credencial de E2, entra.
        ficha_viva = nucleo.cargar(raiz, "T-0901")
        ficha_viva.ultimo_latido = "2030-01-01T00:00:00+00:00"

        nucleo.persistir(
            raiz,
            ficha_viva,
            exigir_propietario=segunda.trabajador_id,
            estados_admitidos={Estado.EN_EJECUCION},
            exigir_generacion=segunda.generacion,
        )
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        assert fila_de(raiz, "T-0901")["ultimo_latido"] == (
            "2030-01-01T00:00:00+00:00"
        ), (
            "La credencial vigente tiene que funcionar. Si no, la prueba "
            "anterior podría estar pasando por el motivo equivocado."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_e_solo_el_nombre_no_basta():
    print("  5. el nombre del trabajador por sí solo no acredita:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-01")
        congelada = copy.deepcopy(primera)
        nucleo.devolver(raiz, "T-0901")
        segunda = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-01")

        antes = testigo(raiz, "T-0901")

        # Se acredita SÓLO con el nombre, sin generación: la orden pasa,
        # porque el nombre coincide. Esto no es un defecto, es la razón por
        # la que la generación existe y por la que un emisor que pueda
        # quedarse rezagado DEBE declararla.
        aceptada = nucleo.latido(
            raiz, "T-0901", trabajador_id="worker-01"
        )
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        assert aceptada.generacion == segunda.generacion

        # Con la generación de E1 declarada, la misma orden cae.
        antes = testigo(raiz, "T-0901")

        exigir_rechazo(
            lambda: nucleo.latido(
                raiz,
                "T-0901",
                trabajador_id="worker-01",
                generacion=congelada.generacion,
            ),
            "latido con generación de E1",
            estado_global.MOTIVO_GENERACION_VENCIDA,
        )

        exigir_intacto(raiz, "T-0901", antes, "nombre sin generación")
        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_f_otro_trabajador_es_rechazado():
    print("  6. un trabajador ajeno no puede escribir:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")

        antes = testigo(raiz, "T-0901")

        # Mismo generación vigente, identidad distinta: el motivo tiene que
        # ser el de propiedad, no el de generación.
        informe = exigir_rechazo(
            lambda: nucleo.latido(
                raiz, "T-0901", trabajador_id="worker-intruso"
            ),
            "latido de un intruso",
            estado_global.MOTIVO_OTRO_PROPIETARIO,
        )

        assert informe["propietario_vigente"] == "worker-B"

        exigir_intacto(raiz, "T-0901", antes, "intruso")
        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_g_orden_sobre_tarea_sin_dueno():
    print("  7. orden rezagada sobre una tarea ya soltada:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        congelada = copy.deepcopy(primera)

        nucleo.devolver(raiz, "T-0901")

        # Nadie la ha vuelto a tomar: la generación NO cambió, pero la fila
        # ya no tiene dueño. Sin el predicado de identidad, esta orden
        # entraría: es la razón por la que no basta con la generación.
        antes = testigo(raiz, "T-0901")

        assert antes["trabajador_id"] is None
        assert antes["generacion"] == congelada.generacion, (
            "Devolver no cambia la generación: por eso hace falta también "
            "el predicado de identidad."
        )

        exigir_rechazo(
            lambda: latido_rezagado(raiz, congelada),
            "latido tras soltar",
        )

        exigir_intacto(raiz, "T-0901", antes, "tarea sin dueño")
        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 3 — Seguridad de ámbito: `cargar` no puede invalidar la garantía
# ----------------------------------------------------------------------

def _reescribir_ambito(raiz: Path, identificador: str, ambito: list):
    """Edita el JSON en disco, como haría una persona con el editor."""
    import json

    ruta = fichas.ruta_ficha(raiz, identificador)
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["ambito_archivos"] = list(ambito)
    ruta.write_text(
        json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _reescribir_titulo(raiz: Path, identificador: str, titulo: str):
    import json

    ruta = fichas.ruta_ficha(raiz, identificador)
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["titulo"] = titulo
    ruta.write_text(
        json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prueba_h_cargar_no_estrecha_el_ambito_de_una_tarea_viva():
    print("  9. `cargar` no estrecha el ámbito de una tarea viva:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=[
                "modulos/comun/uno.py",
                "modulos/comun/dos.py",
            ],
        )
        ficha_minima(
            raiz, "T-0902", ambito_archivos=["modulos/comun/dos.py"]
        )

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        # Alguien estrecha el ámbito de la tarea VIVA en el JSON.
        _reescribir_ambito(raiz, "T-0901", ["modulos/comun/uno.py"])

        antes = testigo(raiz, "T-0901")

        # Una orden de SÓLO LECTURA. Antes de A3.2 bastaba ésta para que el
        # ámbito grabado se estrechara y el solapamiento desapareciera.
        nucleo.cargar(raiz, "T-0901")
        nucleo.cargar(raiz, "T-0902")

        fila = fila_de(raiz, "T-0901")

        assert fila["ambito_archivos"] == [
            "modulos/comun/uno.py",
            "modulos/comun/dos.py",
        ], (
            "`cargar` estrechó el ámbito de una tarea viva. El ámbito "
            "grabado quedó en: " + repr(fila["ambito_archivos"])
        )

        exigir_intacto(raiz, "T-0901", antes, "cargar sobre tarea viva")

        # La consecuencia que de verdad importa: T-0902 sigue bloqueada.
        METRICAS["ORDENES_TOTALES"] += 1

        try:
            nucleo.tomar(raiz, "T-0902", trabajador_id="worker-B")
        except nucleo.ErrorSolapamiento:
            METRICAS["ORDENES_RECHAZADAS"] += 1
        else:
            METRICAS["ORDENES_ACEPTADAS"] += 1
            METRICAS["ESCRITURAS_INDEBIDAS"] += 1
            raise AssertionError(
                "Dos escritores sobre 'modulos/comun/dos.py': el ámbito de "
                "una tarea viva se pudo invalidar por la puerta de `cargar`."
            )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_i_una_toma_rechazada_tampoco_pisa_el_ambito():
    print(" 10. una toma RECHAZADA tampoco pisa el ámbito:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=["modulos/comun/uno.py", "modulos/comun/dos.py"],
        )

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        _reescribir_ambito(raiz, "T-0901", ["modulos/comun/uno.py"])

        antes = testigo(raiz, "T-0901")

        # Una toma que va a fracasar igualmente: la tarea ya está tomada.
        # El camino de `cargar` se recorre entero antes de fracasar.
        METRICAS["ORDENES_TOTALES"] += 1

        try:
            nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")
        except nucleo.ErrorToma:
            METRICAS["ORDENES_RECHAZADAS"] += 1
        else:
            METRICAS["ORDENES_ACEPTADAS"] += 1
            raise AssertionError("La tarea ya estaba tomada: no podía volver a tomarse.")

        exigir_intacto(raiz, "T-0901", antes, "toma rechazada")

        assert fila_de(raiz, "T-0901")["ambito_archivos"] == [
            "modulos/comun/uno.py",
            "modulos/comun/dos.py",
        ]

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_j_una_sincronizacion_inocua_no_rompe_una_tarea_viva():
    print(" 11. un cambio declarativo inocuo sí se sincroniza:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901", ambito_archivos=["modulos/comun/uno.py"])

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        antes = testigo(raiz, "T-0901")

        # Sólo el título: no toca el ámbito, así que no hay nada que
        # congelar y la sincronización debe seguir funcionando.
        _reescribir_titulo(raiz, "T-0901", "Título corregido por una persona")

        ficha = nucleo.cargar(raiz, "T-0901")

        assert ficha.titulo == "Título corregido por una persona"

        fila = fila_de(raiz, "T-0901")

        assert fila["titulo"] == "Título corregido por una persona", (
            "Un cambio inocuo quedó bloqueado sin motivo: la guarda de "
            "ámbito no puede congelar TODA la definición."
        )
        assert fila["ambito_archivos"] == ["modulos/comun/uno.py"]

        exigir_intacto(raiz, "T-0901", antes, "sincronización inocua")

        # Y el dueño sigue pudiendo trabajar con normalidad.
        nucleo.latido(raiz, "T-0901", trabajador_id="worker-A")
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_k_el_ambito_se_refresca_cuando_la_tarea_deja_de_estar_viva():
    print(" 12. el ámbito congelado se refresca al cerrarse la tarea:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=["modulos/comun/uno.py", "modulos/comun/dos.py"],
        )

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        _reescribir_ambito(raiz, "T-0901", ["modulos/comun/uno.py"])

        nucleo.cargar(raiz, "T-0901")

        assert len(fila_de(raiz, "T-0901")["ambito_archivos"]) == 2, (
            "Mientras está viva, el ámbito no cambia."
        )

        # La tarea se cierra: ya no retiene el ámbito.
        nucleo.devolver(raiz, "T-0901")
        nucleo.bloquear(raiz, "T-0901", "Cerrada para la prueba.")

        # El refresco pendiente se aplica solo. La huella nunca avanzó, así
        # que no se perdió en silencio.
        nucleo.cargar(raiz, "T-0901")

        assert fila_de(raiz, "T-0901")["ambito_archivos"] == [
            "modulos/comun/uno.py"
        ], (
            "El cambio de ámbito se perdió para siempre en vez de aplicarse "
            "cuando la tarea dejó de estar viva."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 4 — Bootstrap concurrente
# ----------------------------------------------------------------------

def _inicializador(ruta_raiz: str, barrera) -> dict:
    """
    Un proceso que intenta inicializar la base a la vez que los demás.

    Devuelve lo que pasó, nunca lanza: lo que se juzga es el conjunto.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _raiz_repo = _Path(ruta_raiz)

    for sufijo in ("orquestacion", "nucleo"):
        destino = str(_Path(__file__).resolve().parents[2] / sufijo)
        if destino not in _sys.path:
            _sys.path.insert(0, destino)

    from ingenieria_supervisor import estado_global as global_

    try:
        barrera.wait(timeout=ESPERA_BARRERA_S)
    except Exception as error:
        return {"ok": False, "clase": "barrera", "detalle": str(error)}

    try:
        informe = global_.inicializar_base(_raiz_repo)

        return {
            "ok": True,
            "clase": "exito",
            "version": informe["esquema"]["version_actual"],
        }
    except global_.ErrorEstadoGlobal as error:
        # Un error controlado del Supervisor es una salida ordenada, pero en
        # esta carrera NO debería producirse ninguno: se informa para que la
        # comprobación lo juzgue y no se pierda.
        return {"ok": False, "clase": "controlado", "detalle": str(error)}
    except sqlite3.Error as error:
        return {"ok": False, "clase": "sqlite", "detalle": str(error)}
    except Exception as error:
        return {
            "ok": False,
            "clase": "inesperada",
            "detalle": type(error).__name__ + ": " + str(error),
        }


def _ronda_bootstrap(procesos: int) -> dict:
    """Lanza N procesos contra una base NUEVA, todos soltados a la vez."""
    contexto = multiprocessing.get_context("spawn")
    raiz = crear_repositorio("bootstrap_")

    try:
        assert not estado_global.ruta_base(raiz).exists(), (
            "La carrera sólo prueba algo si la base todavía no existe."
        )

        with contexto.Manager() as gestor:
            barrera = gestor.Barrier(procesos)
            reserva = gestor.Pool(processes=procesos)

            try:
                pendientes = [
                    reserva.apply_async(
                        _inicializador, (str(raiz), barrera)
                    )
                    for _ in range(procesos)
                ]

                resultados = [
                    pendiente.get(timeout=ESPERA_PROCESO_S)
                    for pendiente in pendientes
                ]
            finally:
                reserva.close()
                reserva.join()

        METRICAS["PROCESOS_BOOTSTRAP"] += procesos

        fallidos = [uno for uno in resultados if not uno["ok"]]
        METRICAS["FALLOS_BOOTSTRAP"] += len(fallidos)

        for uno in fallidos:
            if uno["clase"] == "sqlite":
                METRICAS["ERRORES_SQLITE"] += 1
            elif uno["clase"] == "inesperada":
                METRICAS["EXCEPCIONES_INESPERADAS"] += 1

        assert not fallidos, (
            "El arranque concurrente falló en " + str(len(fallidos))
            + " de " + str(procesos) + " procesos: "
            + "; ".join(uno["clase"] + ": " + uno["detalle"] for uno in fallidos)
        )

        # Esquema válido, una sola estructura, versión correcta.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            versiones = [
                fila["version"]
                for fila in con.execute(
                    "SELECT version FROM esquema ORDER BY version"
                )
            ]
            tablas = sorted(
                fila[0]
                for fila in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            )
            version = estado_global.version_esquema(con)
        finally:
            con.close()

        assert version == estado_global.VERSION_ESQUEMA, (
            "La base quedó en la versión " + str(version) + " y se esperaba "
            + str(estado_global.VERSION_ESQUEMA) + "."
        )
        assert versiones == sorted(set(versiones)), (
            "La tabla `esquema` tiene versiones repetidas: " + repr(versiones)
        )
        assert tablas.count("tareas") == 1 and tablas.count("eventos") == 1, (
            "El esquema no quedó con una sola estructura: " + repr(tablas)
        )

        comprobar_integridad(raiz)

        # Y el arranque posterior sigue siendo idempotente.
        repetido = estado_global.inicializar_base(raiz)

        assert repetido["esquema"]["aplicadas"] == [], (
            "Un arranque posterior volvió a aplicar migraciones: "
            + repr(repetido["esquema"]["aplicadas"])
        )

        comprobar_integridad(raiz)

        return {"procesos": procesos, "fallidos": len(fallidos)}
    finally:
        borrar(raiz)


def prueba_l_bootstrap_concurrente():
    print(
        " 13. bootstrap concurrente ("
        + str(PROCESOS_BOOTSTRAP)
        + " procesos x "
        + str(RONDAS_BOOTSTRAP)
        + " rondas):",
        end=" ",
    )

    for _ in range(RONDAS_BOOTSTRAP):
        _ronda_bootstrap(PROCESOS_BOOTSTRAP)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 5 — Línea de órdenes: código de salida estable
# ----------------------------------------------------------------------

def _cli(raiz: Path, *argumentos) -> subprocess.CompletedProcess:
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = os.pathsep.join(
        [str(RAIZ / "orquestacion"), str(RAIZ / "nucleo")]
    )
    entorno["PYTHONIOENCODING"] = "utf-8"

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ingenieria_supervisor",
            "--raiz",
            str(raiz),
            *argumentos,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=ESPERA_SUBPROCESO_S,
        env=entorno,
    )


def prueba_l2_el_journal_no_se_reconvierte_en_cada_apertura():
    """
    Abrir una base que YA está en WAL no vuelve a pedir la conversión.

    Ésa es la mitigación que de verdad importa del arranque concurrente: la
    conversión `delete` -> `wal` es el único momento en que una apertura
    necesita un bloqueo exclusivo, y a partir de la segunda apertura no
    hace ninguna falta. Si se pidiera igualmente, cada proceso que arranca
    competiría por un bloqueo que no necesita.

    Lo que se mide es el número de veces que se emite la sentencia, no un
    tiempo: un tiempo dependería de la máquina y no probaría nada.

    Lo que esta prueba NO cubre, y se dice aquí para no aparentar más de lo
    que hay: el reintento acotado de `_activar_journal`. Ese modo de fallo
    SÍ existe y se reprodujo —dentro de la corrida completa del corredor,
    1 de 6 procesos murió con "database is locked"—, pero depende de la
    carga de la máquina, y un gate que a veces se dispara y a veces no no
    sirve de gate. Aquí se comprueba la mitad determinista; la otra la
    respalda aquella observación, anotada en orquestacion/README.md.
    """
    print(" 14. una base ya en WAL no se reconvierte al abrirla:", end=" ")

    raiz = crear_repositorio("wal_")

    try:
        # Primera apertura: la base no existe, hay que convertirla.
        estado_global.inicializar_base(raiz)

        ruta = estado_global.ruta_base(raiz)

        con = estado_global.abrir(ruta)

        try:
            modo = str(con.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            con.close()

        assert modo.lower() == estado_global.JOURNAL_MODE, (
            "La base no quedó en WAL: '" + modo + "'."
        )

        # Aperturas siguientes: se cuentan las conversiones solicitadas.
        #
        # Se usa el rastreador de SQLite y no un parche sobre `execute`,
        # porque `sqlite3.Connection` es un tipo inmutable de C. El
        # rastreador ve la sentencia tal y como llega al motor.
        conversiones = []
        conectar = sqlite3.connect

        def conectar_vigilado(*argumentos, **claves):
            con_nueva = conectar(*argumentos, **claves)
            con_nueva.set_trace_callback(
                lambda sentencia: conversiones.append(str(sentencia))
                if "journal_mode" in str(sentencia).lower()
                and "=" in str(sentencia)
                else None
            )

            return con_nueva

        sqlite3.connect = conectar_vigilado

        try:
            for _ in range(5):
                estado_global.abrir(ruta).close()
        finally:
            sqlite3.connect = conectar

        assert not conversiones, (
            "Una base que ya está en WAL pidió la conversión "
            + str(len(conversiones)) + " vez/veces: " + repr(conversiones)
            + ". Cada una compite por un bloqueo exclusivo que no hace falta."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_m_codigo_de_salida_por_propiedad():
    print(" 15. la CLI devuelve 4 al rechazar por propiedad:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        vieja = primera.generacion
        nucleo.devolver(raiz, "T-0901")
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")

        antes = testigo(raiz, "T-0901")

        for orden in ("latido", "devolver"):
            salida = _cli(
                raiz,
                orden,
                "T-0901",
                "--trabajador",
                "worker-A",
                "--generacion",
                str(vieja),
            )
            METRICAS["ORDENES_TOTALES"] += 1

            if salida.returncode == 0:
                METRICAS["ORDENES_ACEPTADAS"] += 1
            else:
                METRICAS["ORDENES_RECHAZADAS"] += 1

            assert salida.returncode == 4, (
                "La orden '" + orden + "' rezagada devolvió "
                + str(salida.returncode) + " y se esperaba 4.\n"
                + salida.stdout + salida.stderr
            )
            assert "ORDEN RECHAZADA" in salida.stdout, (
                "El rechazo tiene que ser legible, no un fallo desnudo:\n"
                + salida.stdout
            )
            assert "worker-B" in salida.stdout, (
                "El rechazo debe decir quién es el dueño vigente:\n"
                + salida.stdout
            )

        exigir_intacto(raiz, "T-0901", antes, "CLI rechazada")

        # El 4 no puede confundirse con los demás: una orden buena da 0.
        buena = _cli(raiz, "latido", "T-0901", "--trabajador", "worker-B")
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        assert buena.returncode == 0, (
            "El dueño vigente no pudo emitir su latido:\n"
            + buena.stdout + buena.stderr
        )

        # Y un error de uso sigue dando 2, no 4.
        malo = _cli(raiz, "latido", "T-0901", "--generacion", "1")
        assert malo.returncode == 2, (
            "Declarar generación sin trabajador debe ser un error "
            "controlado (2), no un rechazo por propiedad (4). Dio: "
            + str(malo.returncode)
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 6 — Recuperación: `reanudar` no le quita la tarea a un dueño nuevo
# ----------------------------------------------------------------------

def prueba_n_reanudar_respeta_una_toma_reciente():
    print(" 16. `reanudar` no arrebata una tarea recién tomada:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B", pid=1)

        antes = testigo(raiz, "T-0901")

        # `reanudar` juzga por PID y latido. Se le hace creer que el proceso
        # murió, y además se le fuerza a considerar vencido el latido, que
        # es la situación en la que declararía la tarea huérfana.
        informe = nucleo.reanudar(
            raiz,
            comprobar_proceso=lambda _pid: False,
            latido_maximo_s=0,
            latido_gracia_s=0,
        )

        # Con la foto que leyó, la tarea le parecía abandonada. La escritura
        # se condiciona igualmente, así que si alguien la hubiera tomado
        # entretanto no se la quitaría. Aquí nadie lo hizo, luego la
        # recupera: lo que se comprueba es que el camino existe y que el
        # informe lo cuenta.
        assert "reclamadas_mientras_tanto" in informe, (
            "El informe de recuperación debe poder contar que una tarea fue "
            "reclamada mientras se la juzgaba."
        )
        assert informe["revisadas"] == 1

        # Ahora el caso que importa: una tarea VIVA y sana no se toca.
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-C", pid=os.getpid())
        METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1

        vivo = testigo(raiz, "T-0901")

        segundo = nucleo.reanudar(raiz, comprobar_proceso=lambda _pid: True)

        assert not segundo["huerfanas"], (
            "Una tarea con proceso vivo y latido reciente no es huérfana."
        )

        exigir_intacto(raiz, "T-0901", vivo, "reanudar sobre tarea viva")
        comprobar_integridad(raiz)

        assert antes["trabajador_id"] == "worker-B"
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 7 — Estrés: muchas órdenes rezagadas contra un dueño nuevo
# ----------------------------------------------------------------------

def prueba_o_estres_de_ordenes_rezagadas(rezagadas: int):
    print(
        " 24. estrés: " + str(rezagadas) + " órdenes rezagadas contra el "
        "dueño vigente:",
        end=" ",
    )

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")

        # Se acumulan credenciales de ejecuciones sucesivas, alternando el
        # mismo trabajador y otros distintos: así el montón de órdenes
        # rezagadas contiene los dos casos peligrosos a la vez.
        historicas = []
        nombres = ("worker-01", "worker-02", "worker-01", "worker-03")

        for vuelta, nombre in enumerate(nombres):
            tomada = nucleo.tomar(raiz, "T-0901", trabajador_id=nombre)
            METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1
            historicas.append(copy.deepcopy(tomada))

            if vuelta + 1 < len(nombres):
                nucleo.devolver(raiz, "T-0901")

        vigente = historicas[-1]
        antes = testigo(raiz, "T-0901")

        rechazadas = 0

        for numero in range(rezagadas):
            vieja = historicas[numero % (len(historicas) - 1)]
            _nombre, emitir = REZAGADAS[numero % len(REZAGADAS)]

            exigir_rechazo(
                lambda: emitir(raiz, vieja),
                "estrés #" + str(numero),
            )
            rechazadas += 1

            # El testigo se comprueba en CADA orden, no sólo al final: una
            # escritura indebida que otra orden posterior revirtiera pasaría
            # desapercibida en una comprobación única.
            exigir_intacto(raiz, "T-0901", antes, "estrés #" + str(numero))

        assert rechazadas == rezagadas

        # El dueño vigente conserva la tarea y puede seguir trabajando.
        final = fila_de(raiz, "T-0901")

        assert final["trabajador_id"] == vigente.trabajador_id
        assert final["generacion"] == vigente.generacion
        assert final["estado"] == str(Estado.EN_EJECUCION)

        nucleo.latido(
            raiz,
            "T-0901",
            trabajador_id=vigente.trabajador_id,
            generacion=vigente.generacion,
        )
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 3b — El ámbito congelado no puede colarse por la toma
# ----------------------------------------------------------------------

def _forzar_estado(raiz: Path, identificador: str, estado: Estado) -> None:
    """
    Coloca la tarea en un estado concreto, sólo para montar el escenario.

    Se hace con SQL directo y no con las órdenes del Supervisor porque
    llegar a `requiere_revision` por el camino normal exige correr la
    batería entera dentro de la prueba. Lo que se comprueba después sí pasa
    por las órdenes reales.
    """
    con = estado_global.abrir(estado_global.ruta_base(raiz))

    try:
        with estado_global.transaccion(con):
            estado_global.actualizar_tarea(
                con, identificador, {"estado": str(estado)}
            )
    finally:
        con.close()


def prueba_u_la_toma_graba_el_ambito_que_valido():
    """
    Una tarea en `requiere_revision` no puede tomarse con un ámbito que la
    base acaba de negarse a grabar.

    `requiere_revision` es el ÚNICO estado que está a la vez en
    ESTADOS_TOMABLES y en ESTADOS_QUE_RETIENEN_AMBITO. Eso abría una puerta
    trasera a la guarda de A3.2, encontrada por la auditoría adversarial:
    la toma concedía la propiedad sobre el ámbito DECLARADO mientras la
    fila seguía guardando el viejo, y la siguiente toma comprobaba el
    solapamiento contra un ámbito que ya no usaba nadie. Resultado: dos
    escritores sobre los mismos archivos, que es exactamente lo que la
    guarda existe para impedir.
    """
    print(" 17. la toma graba el ámbito que acaba de validar:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=["modulos/comun/uno.py", "modulos/comun/dos.py"],
        )
        ficha_minima(raiz, "T-0902", ambito_archivos=["modulos/comun/tres.py"])

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")
        _forzar_estado(raiz, "T-0901", Estado.REQUIERE_REVISION)

        # Se AMPLÍA el ámbito en el JSON mientras la tarea lo retiene.
        _reescribir_ambito(
            raiz,
            "T-0901",
            [
                "modulos/comun/uno.py",
                "modulos/comun/dos.py",
                "modulos/comun/tres.py",
            ],
        )

        # La guarda lo congela: la fila conserva el ámbito de dos.
        nucleo.cargar(raiz, "T-0901")

        assert len(fila_de(raiz, "T-0901")["ambito_archivos"]) == 2

        # Y ahora se vuelve a tomar, que es legítimo: está en un estado
        # tomable. La toma valida el ámbito DECLARADO —el de tres— contra
        # las demás tareas y, si lo concede, tiene que grabarlo.
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-B")

        grabado = fila_de(raiz, "T-0901")["ambito_archivos"]

        assert sorted(grabado) == [
            "modulos/comun/dos.py",
            "modulos/comun/tres.py",
            "modulos/comun/uno.py",
        ], (
            "La toma concedió la propiedad sobre un ámbito que no grabó. La "
            "fila quedó con: " + repr(grabado)
        )

        # La consecuencia que importa: T-0902 declara 'tres.py' y ahora sí
        # se ve el solapamiento.
        METRICAS["ORDENES_TOTALES"] += 1

        try:
            nucleo.tomar(raiz, "T-0902", trabajador_id="worker-C")
        except nucleo.ErrorSolapamiento:
            METRICAS["ORDENES_RECHAZADAS"] += 1
        else:
            METRICAS["ORDENES_ACEPTADAS"] += 1
            METRICAS["ESCRITURAS_INDEBIDAS"] += 1
            raise AssertionError(
                "Dos escritores sobre 'modulos/comun/tres.py': el ámbito "
                "congelado se coló por la puerta de la toma."
            )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_u2_el_ambito_vigente_no_miente_al_trabajador():
    """
    Un trabajador nunca cree poseer lo que la base no le concedió.

    La guarda de ámbito sólo estaba a medias, y lo encontró la auditoría:
    la base se negaba a grabar el ámbito nuevo de una tarea viva —correcto—
    pero `cargar` seguía devolviendo el del JSON. Reproducido: se ensanchaba
    el ámbito de la tarea viva, el trabajador veía el ámbito ancho, la base
    seguía con el estrecho, y otra tarea tomaba legítimamente la parte
    nueva. Dos escritores sobre el mismo archivo.

    La ficha lleva ahora los dos datos por separado, y cada uno dice la
    verdad de lo suyo: `ambito_archivos` es lo DECLARADO y se aplicará
    cuando la tarea deje de estar viva; `ambito_vigente` es lo CONCEDIDO y
    es lo único que cuenta para la regla de un solo escritor.

    Que estén separados no es un capricho: pisar el declarado con el
    vigente hacía que `persistir`, al regenerar el espejo, borrara del JSON
    la declaración que una persona acababa de escribir. Se comprobó
    rompiéndolo.
    """
    print(" 18. el ámbito vigente no le miente al trabajador:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901", ambito_archivos=["modulos/comun/uno.py"])
        ficha_minima(raiz, "T-0902", ambito_archivos=["modulos/comun/dos.py"])

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        # Se ENSANCHA el ámbito de la tarea viva para invadir a la otra.
        _reescribir_ambito(
            raiz, "T-0901", ["modulos/comun/uno.py", "modulos/comun/dos.py"]
        )

        ficha = nucleo.cargar(raiz, "T-0901")

        assert ficha.ambito_vigente == ["modulos/comun/uno.py"], (
            "La ficha no informa del ámbito realmente concedido: "
            + repr(ficha.ambito_vigente)
        )
        assert set(ficha.ambito_archivos) == {
            "modulos/comun/uno.py",
            "modulos/comun/dos.py",
        }, "La declaración del JSON tiene que conservarse, no borrarse."

        # El espejo JSON conserva lo que la persona escribió.
        import json

        espejo = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        assert set(espejo["ambito_archivos"]) == {
            "modulos/comun/uno.py",
            "modulos/comun/dos.py",
        }, "Una operación posterior borró del JSON la declaración pendiente."

        # T-0902 conserva lo suyo: T-0901 nunca llegó a poseerlo.
        nucleo.tomar(raiz, "T-0902", trabajador_id="worker-B")

        assert fila_de(raiz, "T-0902")["trabajador_id"] == "worker-B"
        assert fila_de(raiz, "T-0901")["ambito_archivos"] == [
            "modulos/comun/uno.py"
        ]

        # Y la CLI lo avisa, en vez de dejar que alguien lo descubra solo.
        salida = _cli(raiz, "ver", "T-0901")

        assert salida.returncode == 0, salida.stdout + salida.stderr
        assert "ámbito distinto del vigente" in salida.stdout, (
            "`ver` no avisa de que la ficha declara un ámbito que no está "
            "en vigor:\n" + salida.stdout
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_v_reordenar_el_ambito_no_congela_nada():
    """
    Reordenar los patrones no es cambiar el ámbito.

    `supervisor.solapamientos` recorre el producto cartesiano de los dos
    ámbitos, así que ['a','b'] y ['b','a'] garantizan lo mismo. Si la
    guarda comparase las listas tal cual, reordenar sin mover un archivo
    congelaría toda la definición y bloquearía de paso cualquier arreglo
    que viajara en la misma edición.
    """
    print(" 19. reordenar el ámbito no congela la definición:", end=" ")

    raiz = crear_repositorio()

    try:
        import json

        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=["modulos/comun/uno.py", "modulos/comun/dos.py"],
        )
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        ruta = fichas.ruta_ficha(raiz, "T-0901")
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        datos["ambito_archivos"] = [
            "modulos/comun/dos.py",
            "modulos/comun/uno.py",
        ]
        datos["titulo"] = "Título corregido en la misma edición"
        ruta.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        nucleo.cargar(raiz, "T-0901")

        fila = fila_de(raiz, "T-0901")

        assert fila["titulo"] == "Título corregido en la misma edición", (
            "Reordenar el ámbito congeló una corrección de título que no "
            "tenía nada que ver."
        )
        assert sorted(fila["ambito_archivos"]) == [
            "modulos/comun/dos.py",
            "modulos/comun/uno.py",
        ]

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_w_el_ambito_congelado_no_pide_el_bloqueo_de_escritura():
    """
    Una orden de sólo lectura sobre una tarea congelada no escribe ni pide
    el candado.

    La huella NO avanza mientras el ámbito está congelado —es el propio
    diseño de la guarda—, así que `necesita_sincronizacion` dice que sí
    para siempre. Si `asegurar_ficha` abriera igualmente su transacción,
    cada `cargar`, incluido el de un `ver`, pediría el bloqueo de escritura
    de toda la base para no escribir nada; bajo concurrencia eso convierte
    una consulta en una espera que acaba en "database is locked".
    """
    print(" 20. una consulta sobre tarea congelada no pide el candado:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=["modulos/comun/uno.py", "modulos/comun/dos.py"],
        )
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        _reescribir_ambito(raiz, "T-0901", ["modulos/comun/uno.py"])

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
            for _ in range(3):
                nucleo.cargar(raiz, "T-0901")
        finally:
            sqlite3.connect = conectar

        candados = [
            una for una in sentencias if "BEGIN IMMEDIATE" in una.upper()
        ]

        assert not candados, (
            "Una orden de sólo lectura pidió el bloqueo de escritura "
            + str(len(candados)) + " vez/veces sobre una tarea cuyo ámbito "
            "está congelado y que por tanto no se va a escribir."
        )

        escrituras = [
            una for una in sentencias
            if una.strip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
        ]

        assert not escrituras, (
            "La ruta de sólo lectura escribió: " + repr(escrituras[:3])
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 7b — El testigo de propiedad no se puede falsificar
# ----------------------------------------------------------------------

def prueba_r_la_generacion_no_sale_de_sqlite():
    """
    La generación no se puede fijar ni hacer retroceder desde fuera.

    Encontrado por la auditoría adversarial de esta misma etapa, y era un
    defecto INTRODUCIDO por A3.2: al añadir `generacion` al contrato de la
    ficha, el testigo pasaba a escribirse en el JSON versionado, que es un
    archivo del árbol de trabajo que cualquiera edita. Reproducido antes de
    cerrarlo: escribir "generacion": 999 en la ficha y forzar su
    reimportación dejaba la fila con esa generación.

    Un testigo que el vigilado puede escribir no vigila nada: con él se
    podía volver a hacer indistinguibles dos ejecuciones, que es justo el
    problema ABA que la columna existe para cerrar.
    """
    print(" 21. la generación no se puede falsificar desde el JSON:", end=" ")

    raiz = crear_repositorio()

    try:
        import json

        ficha_minima(raiz, "T-0901")
        tomada = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        assert tomada.generacion >= 1

        # (a) No viaja al espejo JSON.
        espejo = json.loads(
            fichas.ruta_ficha(raiz, "T-0901").read_text(encoding="utf-8")
        )

        assert "generacion" not in espejo, (
            "El testigo de propiedad aparece en el JSON versionado, que es "
            "editable por cualquiera: " + repr(espejo.get("generacion"))
        )

        # (b) Un valor inyectado en el JSON se descarta al leer la ficha.
        ruta = fichas.ruta_ficha(raiz, "T-0901")
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        datos["generacion"] = 999
        ruta.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        leida = fichas.leer(raiz, "T-0901")

        assert leida.generacion == 0, (
            "`desde_dict` aceptó una generación del JSON: "
            + repr(leida.generacion)
        )

        # (c) Y una fila importada desde ese JSON arranca su contador en 0,
        #     no en el valor inyectado.
        ficha_minima(raiz, "T-0902")
        ruta2 = fichas.ruta_ficha(raiz, "T-0902")
        datos2 = json.loads(ruta2.read_text(encoding="utf-8"))
        datos2["generacion"] = 4242
        ruta2.write_text(
            json.dumps(datos2, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                con.execute("DELETE FROM eventos WHERE tarea_id = 'T-0902'")
                con.execute("DELETE FROM tareas WHERE id = 'T-0902'")
        finally:
            con.close()

        nucleo.cargar(raiz, "T-0902")

        assert fila_de(raiz, "T-0902")["generacion"] == 0, (
            "Una fila importada heredó la generación que decía el JSON."
        )

        # (d) `actualizar_tarea`, el último UPDATE incondicional que queda,
        #     tampoco puede tocarla.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                estado_global.actualizar_tarea(
                    con, "T-0901", {"generacion": 500}
                )
        except estado_global.ErrorEstadoGlobal:
            pass
        else:
            raise AssertionError(
                "`actualizar_tarea` fijó la generación con un UPDATE "
                "incondicional."
            )
        finally:
            con.close()

        assert fila_de(raiz, "T-0901")["generacion"] == tomada.generacion

        # (e) `fila_desde_ficha` graba cero aunque la ficha traiga otra cosa,
        #     porque alimenta al ALTA de una tarea que la base no conocía.
        suelta = fichas.leer(raiz, "T-0901")
        suelta.generacion = 777

        assert estado_global.fila_desde_ficha(suelta)["generacion"] == 0, (
            "`fila_desde_ficha` copió la generación de la ficha en vez de "
            "empezar el contador de cero."
        )

        # (f) `reclamar` no deja fijarla por la puerta de `campos_extra`.
        con = estado_global.abrir(estado_global.ruta_base(raiz))

        try:
            with estado_global.transaccion(con):
                estado_global.reclamar(
                    con,
                    "T-0902",
                    trabajador_id="worker-X",
                    pid=1,
                    momento="2030-01-01T00:00:00+00:00",
                    estados_reclamables={Estado.NUEVO},
                    estado_destino=str(Estado.EN_EJECUCION),
                    campos_extra={"generacion": 900},
                )
        except estado_global.ErrorEstadoGlobal:
            pass
        else:
            raise AssertionError(
                "`reclamar` aceptó fijar la generación desde campos_extra."
            )
        finally:
            con.close()

        # (g) Y `persistir` no puede tocarla, porque no es columna operativa.
        assert "generacion" not in nucleo.COLUMNAS_OPERATIVAS, (
            "`generacion` entró en COLUMNAS_OPERATIVAS: cualquier orden del "
            "ciclo podría reescribirla con lo que trajera su ficha."
        )

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_s2_la_misma_ficha_se_puede_persistir_dos_veces():
    """
    Una ficha ya escrita sigue sirviendo para la siguiente escritura.

    Es la cara amable de la precondición de estado: tras confirmar, lo que
    se acaba de escribir pasa a ser "lo leído". Sin eso, la segunda
    escritura sobre la MISMA ficha en memoria seguiría exigiendo el estado
    anterior a la primera y se rechazaría a sí misma. La precondición debe
    detener órdenes rezagadas, no al propietario legítimo trabajando.
    """
    print("  8. la misma ficha admite dos escrituras seguidas:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        ficha = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        assert ficha.estado_leido == Estado.EN_EJECUCION, (
            "La toma tiene que dejar la ficha sabiendo en qué estado quedó."
        )

        # Dos latidos seguidos con la MISMA ficha en memoria.
        for marca in ("2030-01-01T00:00:00+00:00", "2030-01-02T00:00:00+00:00"):
            ficha.ultimo_latido = marca
            nucleo.persistir(
                raiz,
                ficha,
                exigir_propietario="worker-A",
                exigir_generacion=ficha.generacion,
            )
            METRICAS["ORDENES_TOTALES"] += 1
            METRICAS["ORDENES_ACEPTADAS"] += 1

            assert fila_de(raiz, "T-0901")["ultimo_latido"] == marca

        # Y una transición sobre esa misma ficha también entra.
        nucleo._liberar_trabajador(ficha)
        nucleo.transicionar(
            ficha, Estado.REABIERTO, "Devuelta.", nucleo.ORIGEN_AUTOMATICO
        )
        nucleo.persistir(raiz, ficha)
        METRICAS["ORDENES_TOTALES"] += 1
        METRICAS["ORDENES_ACEPTADAS"] += 1

        assert fila_de(raiz, "T-0901")["estado"] == str(Estado.REABIERTO)
        assert ficha.estado_leido == Estado.REABIERTO

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_s_orden_humana_rezagada_no_revierte_una_transicion():
    """
    Una orden humana lenta no deshace una transición ya confirmada.

    También lo encontró la auditoría, y también era real: las transiciones
    NO mueven la generación, así que dos órdenes separadas por varias de
    ellas seguían llevando el mismo testigo y el predicado no las
    distinguía. Reproducido: una orden humana compuesta cuando la tarea
    estaba EN_EJECUCION la resucitaba a PROPUESTO después de que otra la
    hubiera dejado BLOQUEADA, saltándose además la máquina de estados,
    porque `transicionar` validó contra su propia foto vieja.

    Lo cierra la tercera precondición: la escritura exige que la fila siga
    en el estado que tenía cuando se leyó.
    """
    print(" 22. una orden humana rezagada no revierte el ciclo:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0901")
        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        # La foto de la orden humana lenta.
        vieja = nucleo.cargar(raiz, "T-0901")

        assert vieja.estado == Estado.EN_EJECUCION

        # Entretanto el ciclo avanza dos veces. Ninguna mueve la generación.
        nucleo.devolver(raiz, "T-0901")
        nucleo.bloquear(raiz, "T-0901", "Bloqueada mientras el humano decidía.")

        antes = testigo(raiz, "T-0901")

        assert antes["estado"] == str(Estado.BLOQUEADO)
        assert antes["generacion"] == vieja.generacion, (
            "El escenario exige que la generación NO haya cambiado; si "
            "cambiara, la prueba pasaría por el motivo equivocado."
        )

        nucleo.transicionar(
            vieja, Estado.PROPUESTO, "Orden humana rezagada.", nucleo.ORIGEN_HUMANO
        )

        informe = exigir_rechazo(
            lambda: nucleo.persistir(raiz, vieja),
            "orden humana rezagada",
            estado_global.MOTIVO_ESTADO_INCOMPATIBLE,
        )

        assert informe["estado"] == str(Estado.BLOQUEADO)

        exigir_intacto(raiz, "T-0901", antes, "orden humana rezagada")
        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


def prueba_t_el_ambito_congelado_se_informa():
    """
    Un ámbito que no se aplicó no puede informarse como "sin cambios".

    Si la sincronización dijera que no había nada que hacer, el usuario
    creería que su edición entró. La regla 5 del proyecto pide que toda
    función importante produzca un resultado visible y verificable, y un
    cambio en espera es justo eso.
    """
    print(" 23. el ámbito congelado se informa, no se disimula:", end=" ")

    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0901",
            ambito_archivos=["modulos/comun/uno.py", "modulos/comun/dos.py"],
        )

        nucleo.tomar(raiz, "T-0901", trabajador_id="worker-A")

        _reescribir_ambito(raiz, "T-0901", ["modulos/comun/uno.py"])

        informe = estado_global.sincronizar_definiciones(raiz)

        congelados = informe.get("ambito_congelado") or []

        assert [uno["id"] for uno in congelados] == ["T-0901"], (
            "La sincronización no informó del ámbito congelado. Informe: "
            + repr({k: v for k, v in informe.items() if k != "fecha"})
        )
        assert "T-0901" not in informe["sin_cambios"], (
            "El ámbito congelado se informó como 'sin cambios', que le dice "
            "al usuario justo lo contrario de lo que pasó."
        )
        assert congelados[0]["estado"] == str(Estado.EN_EJECUCION)
        assert "no se aplica" in congelados[0]["detalle"]

        comprobar_integridad(raiz)
    finally:
        borrar(raiz)

    print("OK")


# ----------------------------------------------------------------------
# GRUPO 8 — Estrés CONCURRENTE: procesos reales contra la propiedad viva
# ----------------------------------------------------------------------

def _emisor_rezagado(ruta_raiz: str, tarea: str, credencial: dict,
                     ordenes: int, barrera) -> dict:
    """
    Proceso que dispara órdenes rezagadas contra la tarea, sin descanso.

    Lleva una credencial CONGELADA, la de una ejecución que ya terminó. No
    relee quién es el dueño: eso es precisamente lo que la convierte en
    rezagada y no en una suplantación.

    Nunca lanza: devuelve el recuento para que lo juzgue quien coordina.
    """
    import sys as _sys
    from pathlib import Path as _Path

    for sufijo in ("orquestacion", "nucleo"):
        destino = str(_Path(__file__).resolve().parents[2] / sufijo)
        if destino not in _sys.path:
            _sys.path.insert(0, destino)

    from ingenieria_supervisor import estado_global as global_

    raiz = _Path(ruta_raiz)

    recuento = {
        "emitidas": 0,
        "aceptadas": 0,
        "rechazadas": 0,
        "errores_sqlite": 0,
        "inesperadas": 0,
        "detalles": [],
    }

    try:
        barrera.wait(timeout=ESPERA_BARRERA_S)
    except Exception as error:
        recuento["inesperadas"] += 1
        recuento["detalles"].append("barrera: " + str(error))
        return recuento

    for numero in range(ordenes):
        recuento["emitidas"] += 1

        try:
            con = global_.abrir(global_.ruta_base(raiz))

            try:
                with global_.transaccion(con):
                    informe = global_.actualizar_si_propietario(
                        con,
                        tarea,
                        {
                            "ultimo_latido": "1999-01-01T00:00:0"
                            + str(numero % 10) + "+00:00",
                            "ultima_falla": None,
                        },
                        generacion=credencial["generacion"],
                        momento="1999-01-01T00:00:00+00:00",
                        trabajador_id=credencial["trabajador_id"],
                        estados_admitidos=None,
                    )
            finally:
                con.close()

            if informe["resultado"] == global_.ESCRITURA_ACEPTADA:
                recuento["aceptadas"] += 1
                recuento["detalles"].append(
                    "ACEPTADA la orden " + str(numero) + " con generación "
                    + str(credencial["generacion"])
                )
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


def prueba_p_estres_concurrente(emisores: int, ordenes: int):
    """
    Varios procesos disparan órdenes rezagadas mientras la propiedad cambia.

    Lo que se mide no es que el sistema aguante, sino que NINGUNA de esas
    órdenes entre. Una sola aceptada es un fallo, y se nombra cuál fue.
    """
    print(
        " 25. estrés concurrente: " + str(emisores) + " procesos x "
        + str(ordenes) + " órdenes rezagadas:",
        end=" ",
    )

    contexto = multiprocessing.get_context("spawn")
    raiz = crear_repositorio("estres_propiedad_")

    try:
        ficha_minima(raiz, "T-0901")

        # La credencial que llevarán los emisores: una ejecución que ya
        # habrá terminado cuando disparen.
        primera = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-viejo")
        METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1

        credencial = {
            "trabajador_id": primera.trabajador_id,
            "generacion": primera.generacion,
        }

        nucleo.devolver(raiz, "T-0901")

        vigente = nucleo.tomar(raiz, "T-0901", trabajador_id="worker-vigente")
        METRICAS["CAMBIOS_DE_PROPIEDAD"] += 1

        antes = testigo(raiz, "T-0901")

        with contexto.Manager() as gestor:
            barrera = gestor.Barrier(emisores)
            reserva = gestor.Pool(processes=emisores)

            try:
                pendientes = [
                    reserva.apply_async(
                        _emisor_rezagado,
                        (str(raiz), "T-0901", credencial, ordenes, barrera),
                    )
                    for _ in range(emisores)
                ]

                recuentos = [
                    pendiente.get(timeout=ESPERA_PROCESO_S)
                    for pendiente in pendientes
                ]
            finally:
                reserva.close()
                reserva.join()

        emitidas = sum(uno["emitidas"] for uno in recuentos)
        aceptadas = sum(uno["aceptadas"] for uno in recuentos)
        rechazadas = sum(uno["rechazadas"] for uno in recuentos)
        errores = sum(uno["errores_sqlite"] for uno in recuentos)
        raras = sum(uno["inesperadas"] for uno in recuentos)

        METRICAS["ORDENES_TOTALES"] += emitidas
        METRICAS["ORDENES_ACEPTADAS"] += aceptadas
        METRICAS["ORDENES_RECHAZADAS"] += rechazadas
        METRICAS["ERRORES_SQLITE"] += errores
        METRICAS["EXCEPCIONES_INESPERADAS"] += raras
        METRICAS["ESCRITURAS_INDEBIDAS"] += aceptadas

        detalles = [
            texto for uno in recuentos for texto in uno["detalles"]
        ][:5]

        assert aceptadas == 0, (
            "Entraron " + str(aceptadas) + " órdenes rezagadas de "
            + str(emitidas) + ". Ejemplos: " + "; ".join(detalles)
        )
        assert errores == 0, (
            "Hubo " + str(errores) + " errores de SQLite bajo concurrencia: "
            + "; ".join(detalles)
        )
        assert raras == 0, (
            "Hubo " + str(raras) + " excepciones inesperadas: "
            + "; ".join(detalles)
        )
        assert emitidas == emisores * ordenes
        assert rechazadas == emitidas

        # El dueño vigente sobrevivió al bombardeo, entero.
        exigir_intacto(raiz, "T-0901", antes, "estrés concurrente")

        final = fila_de(raiz, "T-0901")
        assert final["trabajador_id"] == "worker-vigente"
        assert final["generacion"] == vigente.generacion

        comprobar_integridad(raiz)

        print(
            "OK (" + str(emitidas) + " emitidas, " + str(rechazadas)
            + " rechazadas, 0 aceptadas)"
        )
    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# Corredor de este archivo
# ----------------------------------------------------------------------

COMPROBACIONES = (
    prueba_a_ordenes_rezagadas_rechazadas,
    prueba_b_el_rechazo_no_gasta_intentos_ni_eventos,
    prueba_c_el_espejo_json_no_se_regenera_en_un_rechazo,
    prueba_d_mismo_trabajador_nueva_ejecucion,
    prueba_e_solo_el_nombre_no_basta,
    prueba_f_otro_trabajador_es_rechazado,
    prueba_g_orden_sobre_tarea_sin_dueno,
    prueba_s2_la_misma_ficha_se_puede_persistir_dos_veces,
    prueba_h_cargar_no_estrecha_el_ambito_de_una_tarea_viva,
    prueba_i_una_toma_rechazada_tampoco_pisa_el_ambito,
    prueba_j_una_sincronizacion_inocua_no_rompe_una_tarea_viva,
    prueba_k_el_ambito_se_refresca_cuando_la_tarea_deja_de_estar_viva,
    prueba_l_bootstrap_concurrente,
    prueba_l2_el_journal_no_se_reconvierte_en_cada_apertura,
    prueba_m_codigo_de_salida_por_propiedad,
    prueba_n_reanudar_respeta_una_toma_reciente,
    prueba_u_la_toma_graba_el_ambito_que_valido,
    prueba_u2_el_ambito_vigente_no_miente_al_trabajador,
    prueba_v_reordenar_el_ambito_no_congela_nada,
    prueba_w_el_ambito_congelado_no_pide_el_bloqueo_de_escritura,
    prueba_r_la_generacion_no_sale_de_sqlite,
    prueba_s_orden_humana_rezagada_no_revierte_una_transicion,
    prueba_t_el_ambito_congelado_se_informa,
)


def imprimir_metricas() -> None:
    print("")
    print("  Métricas reales de esta corrida")
    print("  -------------------------------")

    for clave in (
        "ORDENES_TOTALES",
        "ORDENES_ACEPTADAS",
        "ORDENES_RECHAZADAS",
        "ESCRITURAS_INDEBIDAS",
        "ERRORES_SQLITE",
        "EXCEPCIONES_INESPERADAS",
        "CAMBIOS_DE_PROPIEDAD",
        "PROCESOS_BOOTSTRAP",
        "FALLOS_BOOTSTRAP",
        "COMPROBACIONES_INTEGRIDAD",
        "FALLOS_INTEGRIDAD",
    ):
        print("  " + clave.ljust(26) + " = " + str(METRICAS[clave]))

    print("")


def prueba_propiedad_ciclo(
    rezagadas: int = REZAGADAS_POR_OMISION,
    emisores: int = EMISORES_POR_OMISION,
    ordenes: int = ORDENES_POR_EMISOR,
) -> None:
    print("")
    print("PRUEBA: propiedad efectiva durante el ciclo (A3.2)")
    print("")

    inicio = time.monotonic()

    for comprobacion in COMPROBACIONES:
        comprobacion()

    prueba_o_estres_de_ordenes_rezagadas(rezagadas)
    prueba_p_estres_concurrente(emisores, ordenes)

    duracion = time.monotonic() - inicio

    imprimir_metricas()

    # El veredicto no se declara: se comprueba contra lo medido.
    assert METRICAS["ESCRITURAS_INDEBIDAS"] == 0, (
        "Hubo " + str(METRICAS["ESCRITURAS_INDEBIDAS"])
        + " escritura(s) indebida(s)."
    )
    assert METRICAS["ERRORES_SQLITE"] == 0
    assert METRICAS["EXCEPCIONES_INESPERADAS"] == 0
    assert METRICAS["FALLOS_INTEGRIDAD"] == 0
    assert METRICAS["FALLOS_BOOTSTRAP"] == 0
    assert METRICAS["ORDENES_RECHAZADAS"] > 0, (
        "Ninguna orden fue rechazada: la prueba no ejercitó nada."
    )

    print("  Tiempo: " + str(round(duracion, 2)) + " s")
    print("")
    print("PRUEBA_PROPIEDAD_CICLO=OK")


def principal() -> int:
    analizador = argparse.ArgumentParser(
        description="Pruebas de propiedad efectiva durante el ciclo (A3.2)."
    )
    analizador.add_argument(
        "--rezagadas",
        type=int,
        default=REZAGADAS_POR_OMISION,
        help="Órdenes rezagadas de la corrida de estrés.",
    )

    analizador.add_argument(
        "--emisores",
        type=int,
        default=EMISORES_POR_OMISION,
        help="Procesos que disparan órdenes rezagadas a la vez.",
    )
    analizador.add_argument(
        "--ordenes",
        type=int,
        default=ORDENES_POR_EMISOR,
        help="Órdenes rezagadas que dispara cada emisor.",
    )

    argumentos = analizador.parse_args()

    prueba_propiedad_ciclo(
        argumentos.rezagadas, argumentos.emisores, argumentos.ordenes
    )

    return 0


if __name__ == "__main__":
    sys.exit(principal())
