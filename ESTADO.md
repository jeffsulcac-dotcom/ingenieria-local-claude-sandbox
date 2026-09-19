# ESTADO ACTUAL

## Proyecto
INGENIERÍA LOCAL

## Estado
INICIO DEL DESARROLLO

## Fecha de base
18/09/2026

## Infraestructura instalada

- PowerShell 7.6.6: OK
- Python 3.12.4: OK
- Git 2.55.0: OK
- Node.js 24.21.0: OK
- npm 11.19.0: OK
- Claude Code 2.1.276: OK
- .NET SDK 8.0.424: OK
- Docker Desktop: OK
- WSL2: OK
- Visual Studio: OK
- AutoCAD 2026: OK
- Revit 2026: OK
- ETABS: OK
- SAP2000: OK
- SAFE: OK
- Obsidian: OK

## Infraestructura Docker

- PostgreSQL: ACTIVO
- Redis: ACTIVO
- n8n: ACTIVO

## Repositorio

Ruta:
C:\INGENIERIA_LOCAL\motor

Rama:
main

Primer commit:
c5a6d50 - Infraestructura base local

## Próximo objetivo

Definir y crear la estructura interna mínima del motor.

Todavía NO iniciar:
- motor CAD
- Revit
- ETABS
- SAFE
- agentes paralelos

hasta cerrar la arquitectura base del repositorio.

## Regla de continuidad

Este archivo debe actualizarse cuando cambie el estado general del proyecto.

Una sesión nueva debe poder leer:
1. CLAUDE.md
2. PLAN_MAESTRO.md
3. ESTADO.md

y comprender inmediatamente dónde continuar.

## Hito actual

Núcleo mínimo implementado y probado.

Componentes:
- Proyecto
- Módulo
- Tarea
- Observación
- Aprobación
- Estados comunes

Prueba:
PRUEBA_NUCLEO=OK

Estado:
NÚCLEO_BASE = APROBADO

## Interfaz local

Primera interfaz visual implementada y verificada en navegador.

Características actuales:
- interfaz 100 % en español
- funcionamiento local
- navegación por disciplinas
- acceso a cálculo rápido
- acceso a proyectos
- espacio de trabajo preparado para modelo tipo cuaderno técnico

Prueba:
- GET / = 200
- GET /salud = 200
- CSS local = OK

Estado:
INTERFAZ_BASE = APROBADA

## Control principal del sistema

Se implementó y verificó el comando central:

- .\ingenieria.ps1 iniciar
- .\ingenieria.ps1 estado
- .\ingenieria.ps1 detener

Pruebas realizadas:

- inicio de PostgreSQL: OK
- inicio de Redis: OK
- inicio de n8n: OK
- inicio de Ingeniería Local: OK
- detección de estado: OK
- apagado controlado: OK
- reinicio completo: OK

Estado:
CONTROL_LOCAL = APROBADO

## Predimensionamiento rápido — Vigas

Primer módulo de ingeniería implementado y verificado.

Características:
- funcionamiento independiente
- motor Python local
- modo rápido
- luz de viga
- condición de apoyo
- fy configurable
- opción de sistema sismorresistente
- peralte mínimo
- peralte adoptado
- comprobaciones geométricas preliminares
- referencia normativa visible
- integración con la interfaz local

Pruebas:
- PRUEBA_VIGA_RAPIDA=OK
- interfaz de cálculo: OK
- caso L = 6.00 m simplemente apoyada:
  - peralte mínimo = 0.375 m
  - peralte adoptado = 0.40 m

Estado:
PREDIMENSIONAMIENTO_VIGA_RAPIDA = APROBADO

## Predimensionamiento rápido — Columnas

NO INTEGRADO. Trabajo preservado, no terminado.

El módulo de columnas existía sin commit y sin integración. Se guardó tal
cual, antes de iniciar el Supervisor, en la rama:

wip/columnas-pre-supervisor

Commit:
12f2b41 - Preservar predimensionamiento de columnas antes del Supervisor

Contenido exacto del commit:
- modulos/predimensionamiento/columnas.py
- pruebas/predimensionamiento/prueba_columna_rapida.py

Lo que NO tiene:
- ruta en la API
- acceso desde la interfaz
- pruebas de API

La rama main no contiene ningún archivo de columnas.

Su integración formal está registrada como tarea T-0002 y todavía no se
ha ejecutado.

Estado:
PREDIMENSIONAMIENTO_COLUMNA = PRESERVADO_SIN_INTEGRAR

## Supervisor de Desarrollo V1

Primer componente de orquestación implementado.

Permite que el desarrollo se ejecute de forma autónoma, trazable y
reanudable, con Claude Code como trabajador, Git como estado y las pruebas
como filtro.

Componentes:
- orquestacion/ingenieria_supervisor/tarea.py
- orquestacion/ingenieria_supervisor/pruebas.py
- orquestacion/ingenieria_supervisor/supervisor.py
- orquestacion/ingenieria_supervisor/__main__.py
- orquestacion/tareas/ (una ficha JSON por tarea, versionada en Git)
- aplicacion/ingenieria_app/plantillas/desarrollo.html

Características verificadas:
- ficha de tarea autosuficiente: no depende del historial de ninguna sesión
- escritura atómica de fichas: temporal, validación, reemplazo
- corredor único de pruebas con subproceso aislado, tiempo límite y
  PYTHONPATH controlado
- una prueba se aprueba sólo con código de salida 0 Y marca PRUEBA_XXXX=OK
- código de salida 0 sin marca se registra como INDETERMINADO, no aprobado
- el Supervisor llega automáticamente como máximo a PROPUESTO
- APROBADO exige siempre acción humana explícita
- una decisión humana pendiente impide llegar a PROPUESTO
- detección de solapamiento de ámbitos: un solo escritor por archivo
- límite de intentos y bloqueo automático al agotarlos
- recuperación tras cierre o apagón sin perder tareas ni historial
- commit automático limitado a la ficha, dentro de la rama de la tarea,
  nunca en main
- tablero web de sólo lectura en /desarrollo

Pruebas:
- PRUEBA_SUPERVISOR=OK   (31 comprobaciones)
- PRUEBA_API=OK          (12 comprobaciones)
- PRUEBA_NUCLEO=OK
- PRUEBA_VIGA_RAPIDA=OK

Corredor único: 4 de 4 pruebas en OK.

Verificación visual:
- GET /desarrollo = 200
- GET /api/desarrollo/tareas = 200
- tablero sin ningún recurso externo ni CDN

Fichas creadas, ambas en estado NUEVO y sin ejecutar:
- T-0001 Auditoría y corrección normativa del predimensionamiento de vigas
  (3 decisiones humanas registradas como pendientes)
- T-0002 Integración del predimensionamiento rápido de columnas

Estado:
SUPERVISOR_V1 = IMPLEMENTADO_PENDIENTE_DE_REVISION
