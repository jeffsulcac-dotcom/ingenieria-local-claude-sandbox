from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE = Path(__file__).resolve().parent

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


@app.get("/", response_class=HTMLResponse)
async def inicio(request: Request):
    return plantillas.TemplateResponse(
        request=request,
        name="inicio.html",
        context={},
    )


@app.get("/salud")
async def salud():
    return {
        "estado": "activo",
        "modo": "local",
        "idioma": "español",
    }
