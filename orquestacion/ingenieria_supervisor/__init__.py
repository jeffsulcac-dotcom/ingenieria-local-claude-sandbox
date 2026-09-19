"""
Supervisor de Desarrollo V1 de Ingeniería Local.

Coordina el ciclo: tarea -> desarrollo -> prueba -> corrección -> aprobación.

El estado persistente vive únicamente en fichas JSON versionadas en Git.
No utiliza PostgreSQL, Redis ni n8n como origen de verdad.
No requiere Internet.

Este paquete no realiza cálculos de ingeniería.
"""

import sys
from pathlib import Path

# Raíz del repositorio: <raíz>/orquestacion/ingenieria_supervisor/__init__.py
RAIZ = Path(__file__).resolve().parents[2]

# El repositorio no se instala como paquete: las importaciones se resuelven
# por ruta, igual que ya hacían las pruebas existentes. Se registran las tres
# raíces de paquetes del proyecto para que el Supervisor funcione sin importar
# desde qué carpeta se invoque.
RAICES_DE_PAQUETES = (
    RAIZ,
    RAIZ / "nucleo",
    RAIZ / "orquestacion",
)

for _raiz in RAICES_DE_PAQUETES:
    _texto = str(_raiz)
    if _texto not in sys.path:
        sys.path.insert(0, _texto)

del _raiz, _texto
