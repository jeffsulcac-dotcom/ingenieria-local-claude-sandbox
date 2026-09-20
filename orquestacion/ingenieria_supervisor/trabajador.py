"""
Proceso trabajador V1 (T-0003).

Lo lanza `trabajadores.despachar` con un argv ESTRUCTURADO:

    python -m orquestacion.ingenieria_supervisor.trabajador
        --raiz=<raíz> --tarea=T-0003 --trabajador=<id> --generacion=<n>
        --secuencia=<entrada> --worktree=<árbol> --tiempo-limite=<s>
        --pid-despacho=<pid> -- <ejecutable del trabajo> <argumento> ...

Cada opción viaja como `--clave=valor` en un solo elemento, para que un
valor que empiece por `-` (una identidad `-x`) no parezca otra opción;
a mano, `--clave valor` también vale.

Todo lo que hay detrás de `--` es el trabajo encolado, argumento por
argumento, y se entrega a `subprocess` como lista: este proceso no
interpreta ninguna línea de órdenes, ni la suya ni la del trabajo.

Se puede invocar a mano con los mismos argumentos (para reproducir un
lanzamiento), pero sólo hace algo si la ejecución que declara sigue siendo
suya: identidad y generación viajan en el argv y se exigen en cada
escritura. Un trabajador lanzado para una ejecución que ya no existe sale
con código 4 sin tocar nada.

Códigos de salida:
  0   trabajo en verde, ámbito respetado y tarea PROPUESTA.
  1   terminó, pero la tarea no quedó propuesta (trabajo fallido, escritura
      fuera del ámbito, pruebas en rojo, decisión humana pendiente).
  2   el trabajador se averió; la tarea se devolvió si se pudo.
  4   la ejecución ya no era de este trabajador: al adoptar, no se
      modificó nada; si se perdió DESPUÉS de adoptar, el trabajo ya
      corrió en el árbol y la entrada se cierra como fallida.
  6   el árbol registrado no sirve.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from ingenieria_nucleo.estados import Estado

from . import trabajadores
from .__main__ import AyudaEnEspanol, _en_espanol, _preparar_salida


CODIGO_NO_PROPUESTA = 1
CODIGO_AVERIA = 2
CODIGO_NO_ES_MIA = 4
CODIGO_ARBOL = 6


def construir_analizador() -> argparse.ArgumentParser:
    analizador = argparse.ArgumentParser(
        prog="ingenieria_supervisor.trabajador",
        description="Proceso trabajador del Supervisor (Workers V1).",
        formatter_class=AyudaEnEspanol,
        add_help=False,
    )
    _en_espanol(analizador)

    analizador.add_argument(
        "-h", "--ayuda", action="help", help="Mostrar esta ayuda y salir."
    )
    analizador.add_argument("--raiz", required=True, help="Raíz del repositorio.")
    analizador.add_argument("--tarea", required=True, help="Identificador de la tarea.")
    analizador.add_argument(
        "--trabajador", required=True, help="Identidad concedida por el despacho."
    )
    analizador.add_argument(
        "--generacion", required=True, type=int,
        help="Generación de propiedad concedida por el despacho.",
    )
    analizador.add_argument(
        "--secuencia", required=True, type=int, help="Entrada de la cola."
    )
    analizador.add_argument(
        "--worktree", required=True, help="Árbol de la tarea, ya validado."
    )
    analizador.add_argument(
        "--tiempo-limite", dest="tiempo_limite", type=int,
        default=trabajadores.TIEMPO_LIMITE_TRABAJO_S,
        help="Segundos que puede durar el trabajo encolado.",
    )
    analizador.add_argument(
        "--intervalo-latido", dest="intervalo_latido", type=float, default=None,
        help="Segundos entre latidos (por omisión, el del Supervisor).",
    )
    analizador.add_argument(
        "--pid-despacho", dest="pid_despacho", type=int, required=True,
        help="PID con el que el despacho reclamó la fila; la adopción lo exige "
             "para ser exclusiva.",
    )
    analizador.add_argument(
        "--ejecutable", default=None,
        help="Intérprete con el que el corredor lanza las pruebas.",
    )
    analizador.add_argument(
        "trabajo", nargs="*",
        help="El trabajo encolado, tras `--`, argumento por argumento.",
    )

    return analizador


# El mismo tipo que usa `trabajadores` para cobrar una interrupción
# aplazada: `ejecutar_trabajador` tiene que verlas como una sola cosa.
Interrumpido = trabajadores.Interrumpido


# Las señales de terminación recibidas por este proceso, en orden.
SENALES_RECIBIDAS: list[int] = []


def _instalar_senales() -> None:
    """
    Una señal de terminación no mata al trabajador en seco: se convierte
    en una excepción que `ejecutar_trabajador` atiende como avería, así
    que el grupo del trabajo se mata, la tarea se devuelve y la entrada
    se cierra como fallida en vez de quedar EN_EJECUCION con un PID
    muerto (auditoría R1).
    """
    def manejador(numero, _marco):
        SENALES_RECIBIDAS.append(int(numero))

        # Si el trabajo se está lanzando AHORA MISMO, la señal se anota
        # y no se interrumpe: quien cierra esa ventana la cobra con el
        # trabajo ya anotado y lo mata (auditoría R3). Si no, se mata lo
        # que haya en curso.
        if trabajadores.aplazar_interrupcion(numero):
            return

        trabajadores.interrumpir_trabajo_en_curso()

        # Sólo la PRIMERA señal interrumpe: la segunda llegaba mientras
        # se devolvía la tarea, cortaba el `devolver` y dejaba la tarea
        # EN_EJECUCION con la entrada ya cerrada (auditoría R2). A
        # partir de la primera, el camino de devolución no se corta.
        if len(SENALES_RECIBIDAS) == 1:
            raise Interrumpido("señal " + str(numero))

    for nombre in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
        senal = getattr(signal, nombre, None)

        if senal is None:
            continue

        try:
            signal.signal(senal, manejador)
        except (OSError, ValueError):
            pass


def principal(argumentos_crudos: list[str] | None = None) -> int:
    _preparar_salida()
    _instalar_senales()

    argumentos = construir_analizador().parse_args(argumentos_crudos)

    informe = trabajadores.ejecutar_trabajador(
        Path(argumentos.raiz),
        argumentos.tarea,
        argumentos.trabajador,
        argumentos.generacion,
        argumentos.secuencia,
        argumentos.worktree,
        list(argumentos.trabajo),
        tiempo_limite_s=argumentos.tiempo_limite,
        ejecutable=argumentos.ejecutable,
        intervalo_latido_s=argumentos.intervalo_latido,
        pid_despacho=argumentos.pid_despacho,
    )

    # El registro del trabajador es su salida: una línea por hecho y el
    # informe completo en JSON al final, para que una persona o un guion
    # lo lea sin adivinar.
    print("TRABAJADOR " + argumentos.tarea + " (" + argumentos.trabajador
          + ", generación " + str(argumentos.generacion) + ")")
    print("  Adoptada: " + ("sí" if informe["adoptada"] else "no"))
    print("  Resultado: " + str(informe["resultado"]))
    print("  Estado final: " + str(informe["estado_final"]))
    print("  Detalle: " + str(informe["detalle"]))
    print("  Entrada cerrada: " + str(informe["entrada_cerrada"]))
    print("INFORME_JSON=" + json.dumps(informe, ensure_ascii=False, sort_keys=True))

    if not informe["adoptada"] and informe["resultado"] == "no_adoptada":
        return CODIGO_NO_ES_MIA

    if informe["resultado"] == trabajadores.RESULTADO_PROPIEDAD_PERDIDA:
        return CODIGO_NO_ES_MIA

    if informe["resultado"] == trabajadores.RESULTADO_TRABAJADOR_AVERIADO:
        return CODIGO_AVERIA

    if informe["resultado"] == "arbol_no_valido":
        return CODIGO_ARBOL

    if informe["estado_final"] == str(Estado.PROPUESTO):
        return 0

    return CODIGO_NO_PROPUESTA


if __name__ == "__main__":
    sys.exit(principal())
