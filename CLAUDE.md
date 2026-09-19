# REGLAS MAESTRAS PARA CLAUDE

## Idioma
Toda interacción, documentación, mensajes, resultados, estados, reportes e interfaz visible al usuario debe estar en ESPAÑOL.

Los nombres internos exigidos por APIs, librerías o lenguajes pueden permanecer en inglés cuando técnicamente corresponda.

## Objetivo
Construir una plataforma local y modular de ingeniería que viva en la PC del usuario y reduzca al mínimo el modelamiento repetitivo.

## Requisitos no negociables

1. El producto final debe poder funcionar sin Internet.
2. Claude es una herramienta de desarrollo, no una dependencia del producto final.
3. Cada módulo debe poder utilizarse independientemente.
4. No existe una secuencia obligatoria entre módulos.
5. Toda función importante debe producir un resultado visible y verificable.
6. No considerar "PASS" como evidencia suficiente.
7. No crear carpetas, scripts, backups, snapshots o documentación no solicitados.
8. No modificar archivos fuente originales de proyectos.
9. Los cálculos técnicos repetibles deben ser deterministas.
10. La IA sólo se utilizará donde aporte valor real.
11. Muchos agentes pueden analizar en paralelo, pero sólo un escritor puede modificar simultáneamente un mismo modelo.
12. Todo cambio debe ser trazable y reversible.
13. Los cambios manuales hechos por el usuario en Revit, ETABS, SAFE, SAP2000 o CAD deben poder detectarse.
14. El sistema debe recuperarse después de apagados o fallos.
15. La interfaz debe ser minimalista y cómoda, inspirada conceptualmente en un entorno tipo Jupyter.
16. La complejidad debe mostrarse progresivamente: primero modo rápido, luego parámetros avanzados.
17. No sobreingeniería.
18. No ampliar el alcance de una tarea sin autorización.
19. Mantener el repositorio limpio.
20. Antes de cerrar una tarea, ejecutar las pruebas correspondientes.

## Software principal

- AutoCAD 2026
- Revit 2026
- ETABS
- SAP2000
- SAFE
- Python
- C# / .NET
- PowerShell
- n8n
- PostgreSQL
- Redis
- Obsidian
- Git
- Docker

## Filosofía

El usuario debe poder trabajar de dos formas:

### Herramienta independiente
Ejemplo:
- predimensionar una viga
- verificar una columna
- crear un ETABS
- calcular una zapata
- convertir CAD a Revit
- exportar Revit a DWG

sin ejecutar ningún flujo anterior.

### Proyecto integral
Los mismos módulos pueden conectarse para desarrollar un expediente completo.

## Desarrollo

No avanzar una tarea solamente porque el código compile.

El resultado debe comprobarse mediante:
- pruebas,
- comparación de entrada/salida,
- resultados visuales cuando corresponda,
- métricas,
- auditoría.

Si existe incertidumbre técnica que requiera decisión de ingeniería, detenerse y marcarla para revisión humana.
