import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "nucleo"))

from ingenieria_nucleo.estados import Estado
from ingenieria_nucleo.modelos import Proyecto, Tarea


def prueba_nucleo():
    proyecto = Proyecto(nombre="Proyecto de prueba")
    tarea = Tarea(
        titulo="Verificar núcleo",
        modulo="sistema"
    )

    assert proyecto.nombre == "Proyecto de prueba"
    assert proyecto.estado == Estado.NUEVO
    assert tarea.estado == Estado.NUEVO
    assert proyecto.id != tarea.id

    print("PRUEBA_NUCLEO=OK")


if __name__ == "__main__":
    prueba_nucleo()
