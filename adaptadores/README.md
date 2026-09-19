# ADAPTADORES

Contiene únicamente la comunicación con programas o formatos externos.

Ejemplos futuros:
- AutoCAD
- Revit
- ETABS
- SAP2000
- SAFE
- Excel
- IFC

Responsabilidades:
- leer
- escribir
- transformar datos
- ejecutar comandos permitidos por cada API

NO deben contener decisiones de ingeniería.

Ejemplo:
ETABS Adapter puede crear una viga.
No debe decidir qué sección debe tener esa viga.
