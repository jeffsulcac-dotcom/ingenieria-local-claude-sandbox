"""
Pruebas automáticas de la interfaz HTTP local.

Se ejecutan en memoria con el cliente de pruebas de FastAPI: no abren ningún
puerto, no necesitan que el servidor esté levantado y no requieren Internet.

Cubren:
- las páginas visibles;
- la hoja de estilo local;
- el cálculo rápido de viga, con un caso válido y varios inválidos;
- el tablero del Supervisor y su origen de datos.
"""

import sys
import warnings
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "nucleo"))
sys.path.insert(0, str(RAIZ / "orquestacion"))


# Starlette avisa de un cambio futuro de cliente HTTP. No afecta al resultado
# y se silencia para que la salida de la prueba quede limpia.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*httpx.*")

from fastapi.testclient import TestClient

from aplicacion.ingenieria_app.servidor import app


cliente = TestClient(app)


# ----------------------------------------------------------------------
# Salud y páginas
# ----------------------------------------------------------------------

def prueba_salud():
    respuesta = cliente.get("/salud")

    assert respuesta.status_code == 200

    datos = respuesta.json()

    assert datos["estado"] == "activo"
    assert datos["modo"] == "local"
    assert datos["idioma"] == "español"


def prueba_inicio():
    respuesta = cliente.get("/")

    assert respuesta.status_code == 200
    assert "text/html" in respuesta.headers["content-type"]

    texto = respuesta.text

    assert "INGENIERÍA LOCAL" in texto
    assert "Cálculo rápido" in texto

    # El acceso al tablero de desarrollo debe estar visible desde el inicio.
    assert 'href="/desarrollo"' in texto
    assert "Supervisor" in texto


def prueba_pagina_viga():
    respuesta = cliente.get("/calculo-rapido/viga")

    assert respuesta.status_code == 200
    assert "text/html" in respuesta.headers["content-type"]

    texto = respuesta.text

    assert "PREDIMENSIONAMIENTO" in texto
    assert "Viga de concreto armado" in texto
    assert 'id="calcular"' in texto
    assert 'id="peralte-adoptado"' in texto


def prueba_hoja_de_estilo_local():
    respuesta = cliente.get("/estaticos/estilo.css")

    assert respuesta.status_code == 200
    assert "text/css" in respuesta.headers["content-type"]
    assert "--fondo" in respuesta.text


def prueba_ruta_inexistente():
    respuesta = cliente.get("/no-existe-esta-ruta")

    assert respuesta.status_code == 404


# ----------------------------------------------------------------------
# Cálculo rápido de viga
# ----------------------------------------------------------------------

def prueba_calculo_viga_caso_valido():
    respuesta = cliente.post(
        "/api/predimensionamiento/viga",
        json={
            "luz_m": 6.00,
            "condicion_apoyo": "simplemente_apoyada",
        },
    )

    assert respuesta.status_code == 200

    datos = respuesta.json()

    # Mismos números que la prueba del módulo: la API no puede desviarse.
    assert datos["luz_m"] == 6.0
    assert datos["condicion_apoyo"] == "simplemente_apoyada"
    assert datos["fy_mpa"] == 420.0
    assert datos["peralte_minimo_m"] == 0.375
    assert datos["peralte_adoptado_m"] == 0.40
    assert datos["ancho_minimo_sismico_m"] is None
    assert datos["norma"].startswith("RNE E.060")
    assert datos["advertencia"]


def prueba_calculo_viga_caso_sismorresistente():
    respuesta = cliente.post(
        "/api/predimensionamiento/viga",
        json={
            "luz_m": 6.00,
            "condicion_apoyo": "simplemente_apoyada",
            "sistema_sismorresistente": True,
        },
    )

    assert respuesta.status_code == 200

    datos = respuesta.json()

    assert datos["ancho_minimo_sismico_m"] == 0.25
    assert datos["cumple_relacion_luz_peralte"] is True
    assert datos["referencia_sismica"]


def prueba_calculo_viga_condicion_inexistente():
    respuesta = cliente.post(
        "/api/predimensionamiento/viga",
        json={
            "luz_m": 6.00,
            "condicion_apoyo": "apoyo_que_no_existe",
        },
    )

    assert respuesta.status_code == 422


def prueba_calculo_viga_sin_luz():
    respuesta = cliente.post(
        "/api/predimensionamiento/viga",
        json={"condicion_apoyo": "simplemente_apoyada"},
    )

    assert respuesta.status_code == 422


def prueba_calculo_viga_luz_no_numerica():
    respuesta = cliente.post(
        "/api/predimensionamiento/viga",
        json={
            "luz_m": "seis metros",
            "condicion_apoyo": "simplemente_apoyada",
        },
    )

    assert respuesta.status_code == 422


# ----------------------------------------------------------------------
# Tablero del Supervisor
# ----------------------------------------------------------------------

def prueba_pagina_desarrollo():
    respuesta = cliente.get("/desarrollo")

    assert respuesta.status_code == 200
    assert "text/html" in respuesta.headers["content-type"]

    texto = respuesta.text

    assert "Supervisor de Desarrollo" in texto
    assert "Agentes activos" in texto
    assert "Tareas totales" in texto
    assert "Requieren revisión" in texto
    assert "Bloqueadas" in texto
    assert "ACTIVIDAD RECIENTE" in texto

    # El tablero se alimenta de su propia API local, sin recursos externos.
    assert "/api/desarrollo/tareas" in texto

    minuscula = texto.lower()

    for prohibido in (
        "http://",
        "https://",
        "//cdn",
        "cdnjs",
        "unpkg",
        "jsdelivr",
        "googleapis",
        "gstatic",
        "@import url(",
    ):
        assert prohibido not in minuscula, (
            "El tablero referencia un recurso externo: " + prohibido
        )

    # Todo lo que carga la página debe ser una ruta local.
    import re

    for recurso in re.findall(r'(?:src|href)="([^"]*)"', texto):
        assert recurso.startswith("/"), (
            "Recurso no local en el tablero: " + recurso
        )


def prueba_api_desarrollo():
    respuesta = cliente.get("/api/desarrollo/tareas")

    assert respuesta.status_code == 200

    datos = respuesta.json()

    for clave in (
        "generado_en",
        "resumen",
        "tareas",
        "actividad",
        "fichas_ilegibles",
    ):
        assert clave in datos

    resumen = datos["resumen"]

    for clave in (
        "agentes_activos",
        "totales",
        "nuevas",
        "en_ejecucion",
        "propuestas",
        "requieren_revision",
        "bloqueadas",
        "reabiertas",
        "aprobadas",
        "rechazadas",
    ):
        assert clave in resumen
        assert isinstance(resumen[clave], int)

    assert resumen["totales"] == len(datos["tareas"])

    # Cada tarea publicada debe traer los campos que el tablero muestra.
    for tarea in datos["tareas"]:
        for clave in (
            "id",
            "titulo",
            "estado",
            "rama",
            "worktree",
            "intentos",
            "max_intentos",
            "pruebas_ok",
            "pruebas_total",
            "actualizado_en",
            "ultima_falla",
            "decisiones_pendientes",
            "trabajador_id",
            "pid",
            "ultimo_latido",
        ):
            assert clave in tarea, "Falta '" + clave + "' en " + tarea["id"]


# ----------------------------------------------------------------------
# Ejecución
# ----------------------------------------------------------------------

COMPROBACIONES = [
    ("GET /salud", prueba_salud),
    ("GET /", prueba_inicio),
    ("GET /calculo-rapido/viga", prueba_pagina_viga),
    ("GET /estaticos/estilo.css", prueba_hoja_de_estilo_local),
    ("GET ruta inexistente = 404", prueba_ruta_inexistente),
    ("POST viga, caso válido", prueba_calculo_viga_caso_valido),
    ("POST viga, caso sismorresistente",
     prueba_calculo_viga_caso_sismorresistente),
    ("POST viga, condición inexistente = 422",
     prueba_calculo_viga_condicion_inexistente),
    ("POST viga, sin luz = 422", prueba_calculo_viga_sin_luz),
    ("POST viga, luz no numérica = 422",
     prueba_calculo_viga_luz_no_numerica),
    ("GET /desarrollo", prueba_pagina_desarrollo),
    ("GET /api/desarrollo/tareas", prueba_api_desarrollo),
]


def prueba_api():
    for numero, (nombre, comprobacion) in enumerate(COMPROBACIONES, start=1):
        comprobacion()
        print("  " + str(numero).rjust(2) + ". " + nombre + ": OK")

    print("PRUEBA_API=OK")


if __name__ == "__main__":
    prueba_api()
