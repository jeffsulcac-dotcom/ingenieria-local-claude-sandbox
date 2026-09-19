# PLAN MAESTRO — INGENIERÍA LOCAL

## Objetivo general

Construir una mesa de trabajo local de ingeniería y gestión de proyectos, modular, independiente, visual y orientada a disminuir drásticamente el trabajo repetitivo de modelamiento.

## Modos principales

### 1. Cálculo rápido
Uso independiente, sin necesidad de crear un proyecto.

Ejemplos:
- predimensionamiento de vigas
- columnas
- losas
- placas
- escaleras
- zapatas
- plateas
- cimentaciones
- muros de contención
- consultas normativas rápidas

Motor principal:
Python local y reglas deterministas.

### 2. Proyecto
Permite desarrollar un expediente completo o sólo partes.

### 3. Flujos automatizados
n8n coordina módulos independientes cuando se desea ejecutar procesos encadenados.

---

# MÓDULOS PRINCIPALES

## CAD
- lectura DWG
- limpieza de CAD sucio
- eliminación de duplicados y basura
- reconstrucción geométrica
- interpretación incluso con un solo layer
- lectura de hatches
- recuperación de contornos
- bloques explotados
- texto
- cotas
- mobiliario
- acabados
- reconocimiento semántico
- comparación original / limpio / interpretado
- corrección humana
- aprendizaje local
- QA

## Arquitectura
- CAD a Revit
- muros
- pisos
- cielos
- techos
- puertas
- ventanas
- escaleras
- barandas
- ambientes
- mobiliario
- aparatos
- acabados
- fachadas
- cortes
- detalles
- familias
- creación/adaptación de familias faltantes
- documentación
- láminas
- ploteo
- PDF
- DWG
- IFC

## Predimensionamiento estructural
Modos:
- rápido
- intermedio
- avanzado

Elementos:
- vigas
- columnas
- losas
- placas
- escaleras
- voladizos
- vigas de cimentación
- zapatas
- plateas

## Estructuras
- Revit Structure
- ETABS
- SAP2000
- materiales
- secciones
- cargas
- diafragmas
- combinaciones
- análisis
- diseño
- iteración
- optimización
- detallamiento

## Armadura
Modelo completo de acero en Revit:
- vigas
- columnas
- placas
- losas
- escaleras
- cimentaciones
- zonas de confinamiento
- anclajes
- traslapes
- ganchos
- detalles
- cuadros de acero
- metrados

Todo según cálculo y norma aplicable.

## Geotecnia
- parámetros de suelo
- cimentaciones
- asentamientos
- nivel freático
- capacidad admisible
- muros de contención
- herramientas de cálculo independientes

## Cimentaciones
- aisladas
- combinadas
- corridas
- plateas
- SAFE
- importación de reacciones desde ETABS/SAP
- ingreso manual independiente

## Sanitarias
- agua
- desagüe
- ventilación
- pluvial
- cálculo
- dimensionamiento
- modelamiento Revit
- planos
- detalles
- metrados

## Eléctricas
- cargas
- demanda
- circuitos
- tableros
- alimentadores
- protecciones
- caída de tensión
- puesta a tierra
- modelamiento Revit
- diagramas
- planos
- metrados

## Electromecánicas
Modelamiento y coordinación según las necesidades del proyecto.

## Coordinación
- arquitectura
- estructuras
- sanitarias
- eléctricas
- electromecánicas
- reservas
- pases
- interferencias
- issues
- aprobaciones

## Metrados
Desde modelos reales:
- concreto
- acero
- encofrado
- arquitectura
- acabados
- MEP
- materiales
- partidas

## Presupuesto
- vínculo elemento BIM ↔ partida
- recursos
- precios
- rendimientos

## Cronograma
- actividades
- partidas
- rendimientos
- recursos
- duraciones

## Documentación
- planos
- memorias
- especificaciones
- informes
- cuadros
- PDF
- DWG
- IFC

---

# PRINCIPIOS DE DATOS

Separar siempre:

1. Geometría
2. Ingeniería
3. Documentación

Debe existir un modelo semántico central que permita relacionar:

CAD ↔ Revit ↔ ETABS/SAP ↔ SAFE ↔ Metrados

Cada objeto debe conservar identidad y trazabilidad.

---

# INTERFAZ

Minimalista.

Tres entradas principales:

- Cálculo rápido
- Abrir proyecto
- Nuevo proyecto

Áreas:
- Arquitectura
- Estructuras
- Geotecnia
- Cimentaciones
- Sanitarias
- Eléctricas
- Electromecánicas
- Metrados
- Presupuesto
- Cronograma
- Documentación

Workspace central inspirado conceptualmente en Jupyter:
entrada → ejecutar → resultado.

---

# INFRAESTRUCTURA

Local:
- PowerShell
- Docker
- PostgreSQL
- Redis
- n8n
- Git
- Obsidian

El producto final no debe depender de servicios externos.

---

# RESILIENCIA

- checkpoints
- backups locales
- recuperación ante fallos
- cola persistente
- UPS
- reinicio seguro
- rollback

---

# DESARROLLO PARALELO

Se permite trabajo paralelo mediante agentes aislados y Git worktrees.

Regla:
muchos lectores / analizadores,
un único escritor por modelo.

---

# PRIORIDAD INICIAL

1. Infraestructura local
2. Interfaz mínima
3. Motor CAD de limpieza
4. Visor original / limpio
5. Interpretación CAD
6. Corrección humana
7. Modelo semántico
8. Predimensionamiento
9. Revit
10. ETABS/SAP

No expandir a fases posteriores hasta demostrar esta columna vertebral.
