"""
Corredor único de pruebas del repositorio.

Descubre automáticamente:

    pruebas/**/prueba_*.py

Cada prueba se ejecuta como subproceso aislado, con tiempo límite y con
PYTHONPATH fijado de forma controlada, para que el resultado no dependa de
la carpeta desde la que se invoque el corredor.

Una prueba se considera APROBADA únicamente si cumple LAS DOS condiciones:

    1. termina con código de salida 0;
    2. imprime en su salida estándar una marca explícita  PRUEBA_XXXX=OK

Código 0 sin marca  ->  INDETERMINADO  (nunca se cuenta como aprobado)
Código distinto de 0 ->  FALLO
Sin respuesta a tiempo -> TIEMPO_AGOTADO

Este módulo no conoce el concepto de tarea: sólo ejecuta y juzga.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# La marca debe ocupar su propia línea completa.
PATRON_MARCA = re.compile(r"^(PRUEBA_[A-Z0-9_]+)=OK[ \t]*$", re.MULTILINE)

PATRON_ARCHIVOS = "prueba_*.py"
CARPETA_PRUEBAS = "pruebas"

# Segundos que se le conceden a cada prueba individual.
TIEMPO_LIMITE_S = 120

# Caracteres de salida conservados por prueba dentro de la ficha.
LIMITE_SALIDA = 4000

VEREDICTO_OK = "OK"
VEREDICTO_FALLO = "FALLO"
VEREDICTO_INDETERMINADO = "INDETERMINADO"
VEREDICTO_TIEMPO_AGOTADO = "TIEMPO_AGOTADO"

RESULTADO_APROBADO = "APROBADO"
RESULTADO_FALLO = "FALLO"


def ahora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def entorno_controlado(raiz: Path) -> dict:
    """
    Entorno determinista para los subprocesos de prueba.

    PYTHONPATH se fija explícitamente con las tres raíces de paquetes del
    repositorio: la raíz (para 'modulos' y 'aplicacion'), 'nucleo' (para
    'ingenieria_nucleo') y 'orquestacion' (para 'ingenieria_supervisor').
    Se reemplaza cualquier PYTHONPATH heredado, para que dos máquinas
    distintas obtengan exactamente el mismo resultado.
    """
    raiz = Path(raiz)

    entorno = dict(os.environ)

    entorno["PYTHONPATH"] = os.pathsep.join(
        [
            str(raiz),
            str(raiz / "nucleo"),
            str(raiz / "orquestacion"),
        ]
    )

    # No ensuciar el repositorio con archivos compilados.
    entorno["PYTHONDONTWRITEBYTECODE"] = "1"

    # La salida del sistema está en español: se fuerza UTF-8 para que los
    # acentos no dependan de la página de códigos de la consola.
    entorno["PYTHONIOENCODING"] = "utf-8"
    entorno["PYTHONUTF8"] = "1"

    # Las variables que redirigen a `git` no se heredan. Si el Supervisor se
    # invoca desde un hook, `GIT_DIR` está puesto y apunta al repositorio de
    # quien llamó: una prueba que consultara Git respondería por OTRO árbol
    # y su resultado no diría nada del que se está verificando.
    for nombre in (
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    ):
        entorno.pop(nombre, None)

    return entorno


def descubrir(raiz: Path) -> list[Path]:
    """Todas las pruebas del repositorio, en orden estable."""
    carpeta = Path(raiz) / CARPETA_PRUEBAS

    if not carpeta.is_dir():
        return []

    return sorted(
        ruta
        for ruta in carpeta.rglob(PATRON_ARCHIVOS)
        if ruta.is_file()
    )


def _recortar(texto: str) -> str:
    texto = texto or ""

    if len(texto) <= LIMITE_SALIDA:
        return texto

    recortado = len(texto) - LIMITE_SALIDA

    return (
        "[...se omitieron " + str(recortado) + " caracteres...]\n"
        + texto[-LIMITE_SALIDA:]
    )


def _decodificar(valor) -> str:
    if valor is None:
        return ""

    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")

    return str(valor)


def buscar_marca(salida: str) -> str | None:
    """Devuelve la marca PRUEBA_XXXX=OK encontrada, o None."""
    encontrada = PATRON_MARCA.search(salida or "")

    if encontrada is None:
        return None

    return encontrada.group(1)


def ejecutar_una(
    ruta: Path,
    raiz: Path,
    tiempo_limite_s: int = TIEMPO_LIMITE_S,
    ejecutable: str | None = None,
) -> dict:
    """Ejecuta una prueba aislada y emite su veredicto."""
    raiz = Path(raiz)
    ruta = Path(ruta)

    ejecutable = ejecutable or sys.executable

    try:
        nombre = ruta.resolve().relative_to(raiz.resolve()).as_posix()
    except ValueError:
        nombre = ruta.as_posix()

    inicio = time.monotonic()

    agotado = False
    codigo = None
    salida_estandar = ""
    salida_error = ""

    try:
        proceso = subprocess.run(
            [ejecutable, str(ruta)],
            cwd=str(raiz),
            env=entorno_controlado(raiz),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=tiempo_limite_s,
        )

        codigo = proceso.returncode
        salida_estandar = proceso.stdout or ""
        salida_error = proceso.stderr or ""

    except subprocess.TimeoutExpired as error:
        agotado = True
        salida_estandar = _decodificar(error.stdout)
        salida_error = _decodificar(error.stderr)

    duracion = round(time.monotonic() - inicio, 3)

    # La marca sólo se acepta en la salida estándar.
    marca = buscar_marca(salida_estandar)

    if agotado:
        veredicto = VEREDICTO_TIEMPO_AGOTADO
    elif codigo != 0:
        veredicto = VEREDICTO_FALLO
    elif marca is None:
        veredicto = VEREDICTO_INDETERMINADO
    else:
        veredicto = VEREDICTO_OK

    salida_completa = salida_estandar

    if salida_error:
        salida_completa = salida_completa + "\n[error]\n" + salida_error

    return {
        "prueba": nombre,
        "codigo": codigo,
        "marca": marca,
        "veredicto": veredicto,
        "duracion_s": duracion,
        "salida": _recortar(salida_completa.strip()),
    }


def ejecutar_todas(
    raiz: Path,
    tiempo_limite_s: int = TIEMPO_LIMITE_S,
    ejecutable: str | None = None,
) -> dict:
    """
    Ejecuta todas las pruebas descubiertas y resume la corrida.

    El resultado global es APROBADO sólo si se descubrió al menos una prueba
    y absolutamente todas quedaron en OK.
    """
    raiz = Path(raiz)

    detalle = [
        ejecutar_una(ruta, raiz, tiempo_limite_s, ejecutable)
        for ruta in descubrir(raiz)
    ]

    def contar(veredicto: str) -> int:
        return sum(1 for uno in detalle if uno["veredicto"] == veredicto)

    total = len(detalle)
    aprobadas = contar(VEREDICTO_OK)

    corrida = {
        "tipo": "corrida",
        "fecha": ahora_utc(),
        "total": total,
        "ok": aprobadas,
        "fallidas": contar(VEREDICTO_FALLO),
        "indeterminadas": contar(VEREDICTO_INDETERMINADO),
        "agotadas": contar(VEREDICTO_TIEMPO_AGOTADO),
        "detalle": detalle,
    }

    if total == 0:
        corrida["resultado"] = RESULTADO_FALLO
        corrida["motivo"] = "No se descubrió ninguna prueba."
    elif aprobadas == total:
        corrida["resultado"] = RESULTADO_APROBADO
    else:
        corrida["resultado"] = RESULTADO_FALLO
        corrida["motivo"] = (
            "No todas las pruebas quedaron en OK ("
            + str(aprobadas) + " de " + str(total) + ")."
        )

    return corrida


def problemas_de_pruebas_requeridas(
    corrida: dict,
    pruebas_requeridas: list[str],
) -> list[str]:
    """
    Comprueba las pruebas que la ficha exige específicamente.

    Devuelve la lista de problemas encontrados, en español. Lista vacía
    significa que todas las pruebas requeridas existen y quedaron en OK.
    """
    problemas = []

    por_nombre = {uno["prueba"]: uno for uno in corrida.get("detalle", [])}

    for requerida in pruebas_requeridas:
        normalizada = requerida.replace("\\", "/").lstrip("./")

        resultado = por_nombre.get(normalizada)

        if resultado is None:
            problemas.append(
                "Falta la prueba requerida '" + normalizada
                + "': no fue descubierta por el corredor."
            )
            continue

        if resultado["veredicto"] != VEREDICTO_OK:
            problemas.append(
                "La prueba requerida '" + normalizada + "' quedó en "
                + resultado["veredicto"] + "."
            )

    return problemas


def resumen_en_texto(corrida: dict) -> str:
    """Resumen legible de una corrida, en español."""
    lineas = [
        "Resultado global: " + corrida.get("resultado", "?"),
        "Total: " + str(corrida.get("total", 0))
        + "   OK: " + str(corrida.get("ok", 0))
        + "   Fallidas: " + str(corrida.get("fallidas", 0))
        + "   Indeterminadas: " + str(corrida.get("indeterminadas", 0))
        + "   Tiempo agotado: " + str(corrida.get("agotadas", 0)),
        "",
    ]

    for uno in corrida.get("detalle", []):
        lineas.append(
            "  ["
            + uno["veredicto"].ljust(14)
            + "] "
            + uno["prueba"]
            + "   código="
            + str(uno["codigo"])
            + "   marca="
            + str(uno["marca"])
        )

    if corrida.get("motivo"):
        lineas.append("")
        lineas.append("Motivo: " + corrida["motivo"])

    return "\n".join(lineas)
