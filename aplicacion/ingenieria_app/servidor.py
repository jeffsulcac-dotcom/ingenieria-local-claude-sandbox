from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from modulos.predimensionamiento.vigas import (
    CondicionApoyo,
    predimensionar_viga_rapida,
)

from orquestacion.ingenieria_supervisor import supervisor as supervisor_desarrollo


BASE = Path(__file__).resolve().parent

# Raíz del repositorio, donde vive el estado del Supervisor.
RAIZ = BASE.parents[1]

app = FastAPI(
    title="Ingeniería Local",
    description="Mesa de trabajo local de ingeniería",
)

app.mount(
    "/estaticos",
    StaticFiles(directory=BASE / "estaticos"),
    name="estaticos",
)

plantillas = Jinja2Templates(
    directory=BASE / "plantillas"
)


class EntradaVigaRapida(BaseModel):
    luz_m: float
    condicion_apoyo: CondicionApoyo
    fy_mpa: float = 420.0
    sistema_sismorresistente: bool = False


@app.get("/", response_class=HTMLResponse)
async def inicio(request: Request):
    return plantillas.TemplateResponse(
        request=request,
        name="inicio.html",
        context={},
    )


@app.get(
    "/calculo-rapido/viga",
    response_class=HTMLResponse,
)
async def pagina_viga(request: Request):
    return plantillas.TemplateResponse(
        request=request,
        name="viga_rapida.html",
        context={},
    )


@app.post("/api/predimensionamiento/viga")
async def api_predimensionamiento_viga(
    entrada: EntradaVigaRapida,
):
    resultado = predimensionar_viga_rapida(
        luz_m=entrada.luz_m,
        condicion_apoyo=entrada.condicion_apoyo,
        fy_mpa=entrada.fy_mpa,
        sistema_sismorresistente=
            entrada.sistema_sismorresistente,
    )

    return {
        "luz_m": resultado.luz_m,
        "condicion_apoyo":
            resultado.condicion_apoyo.value,
        "fy_mpa": resultado.fy_mpa,
        "peralte_minimo_m":
            resultado.peralte_minimo_m,
        "peralte_adoptado_m":
            resultado.peralte_adoptado_m,
        "ancho_minimo_sismico_m":
            resultado.ancho_minimo_sismico_m,
        "cumple_relacion_luz_peralte":
            resultado.cumple_relacion_luz_peralte,
        "norma": resultado.norma,
        "referencia_peralte":
            resultado.referencia_peralte,
        "referencia_sismica":
            resultado.referencia_sismica,
        "advertencia":
            resultado.advertencia,
    }


@app.get("/desarrollo", response_class=HTMLResponse)
async def pagina_desarrollo(request: Request):
    """Tablero de sólo lectura del Supervisor de Desarrollo."""
    return plantillas.TemplateResponse(
        request=request,
        name="desarrollo.html",
        context={},
    )


@app.get("/api/desarrollo/estado")
def api_desarrollo_estado():
    """
    Estado operativo del Supervisor leído de la base SQLite global.

    Las definiciones de tarea siguen viniendo de las fichas JSON versionadas
    en Git. No consulta PostgreSQL ni Redis, y no mantiene ninguna caché:
    cada petición lee la base.

    Es una función síncrona a propósito: FastAPI la ejecuta en su grupo de
    hilos y la lectura de disco no bloquea el bucle de eventos.
    """
    return supervisor_desarrollo.tablero(RAIZ)


@app.get("/api/desarrollo/tareas")
def api_desarrollo_tareas():
    """Ruta conservada de V1: misma respuesta que /api/desarrollo/estado."""
    return supervisor_desarrollo.tablero(RAIZ)


@app.get("/salud")
async def salud():
    return {
        "estado": "activo",
        "modo": "local",
        "idioma": "español",
    }
