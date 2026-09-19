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

## Supervisor — A2: estado operativo global en SQLite

Segundo componente de orquestación. Sustituye las fichas JSON como fuente
del estado operativo por una única base SQLite por repositorio, compartida
por la rama principal y por todos sus worktrees.

Base:
- ruta: `<git rev-parse --git-common-dir>/ingenieria-supervisor.sqlite3`
  (en esta PC: C:\INGENIERIA_LOCAL\motor\.git\ingenieria-supervisor.sqlite3)
- no versionada, no aparece en `git status`; red de seguridad en .gitignore
- sqlite3 de la biblioteca estándar; sin ORM; sin dependencias nuevas
- esquema versión 1: tablas `esquema`, `tareas`, `eventos`
- journal_mode = wal, synchronous = FULL, busy_timeout = 5000 ms,
  foreign_keys = ON
- transacciones BEGIN IMMEDIATE / COMMIT con ROLLBACK ante error

Autoridad:
- SQLite: estado operativo (estado, rama, worktree, intentos, trabajador,
  latido, fallas, última verificación, resolución de decisiones humanas,
  historial de eventos)
- JSON: definición versionada (id, título, objetivo, criterios, ámbito,
  pruebas requeridas, decisiones humanas declaradas)
- los campos operativos del JSON son un espejo regenerado desde SQLite
  tras cada operación; nunca son entrada

Componentes:
- orquestacion/ingenieria_supervisor/estado_global.py (nuevo)
- supervisor.py: todas las operaciones del ciclo persisten en SQLite
- __main__.py: `estado` y `ver` leen SQLite; nuevas órdenes `diagnostico`,
  `inicializar-estado`, `sincronizar-definiciones`
- servidor.py: `GET /api/desarrollo/tareas` sirve el estado desde SQLite
  (misma ruta de V1, sin endpoints nuevos)
- desarrollo.html: indicador "Base global SQLite", última actividad
  global, última verificación y decisión humana por tarea

Bootstrap verificado sobre el repositorio real:
- T-0001: NUEVO, 0 intentos, sin trabajador, 3 decisiones pendientes
  (D-1, D-2, D-3) intactas y sin resolver
- T-0002: NUEVO, 0 intentos, sin trabajador, sin decisiones
- repetir el bootstrap no duplica ni altera nada; los JSON no se reescriben

Pruebas ejecutadas (Python 3.11.15, una corrida por archivo):
- PRUEBA_NUCLEO=OK
- PRUEBA_VIGA_RAPIDA=OK
- PRUEBA_SUPERVISOR=OK        31 de 31 comprobaciones
- PRUEBA_ESTADO_GLOBAL=OK     16 de 16 comprobaciones
- PRUEBA_API=OK               12 de 12 comprobaciones

5 de 5 archivos de prueba en OK.

Comprobación adicional del estado compartido, fuera de las pruebas: sobre
un clon con dos worktrees, ambos resuelven la misma ruta de base; el
worktree B crea y toma una tarea y el árbol A la ve en EN_EJECUCION sin
tener siquiera su ficha JSON; un JSON alterado a mano no cambia el estado.

NO implementado en A2 (reservado a A3/B): toma atómica concurrente, locks,
latidos automáticos, detección avanzada de huérfanos, verificar() en el
worktree de la tarea, lanzamiento de Claude, workers paralelos, worktrees
automáticos, cola automática, n8n ejecutando tareas, Redis como cola,
PostgreSQL como estado.

(La toma atómica de esa lista ya está implementada: la trajo A3.1, más
abajo. El resto sigue pendiente.)

Deuda conocida de A2, no corregida por quedar fuera de su alcance:
- `inicializar()` lee la versión del esquema antes de abrir la
  transacción. Dos procesos que creen la base a la vez pueden intentar la
  misma migración; el segundo falla con error explícito, sin corromper
  nada. SIGUE VIGENTE tras A3.1, y ahora está reproducida: 6 procesos
  creando la base a la vez dejan a algunos con "table tareas already
  exists". Nunca produjo doble propietario. Pertenece a A3.2.
- Borrar una ficha JSON no borra la tarea del estado global: el
  identificador queda reservado y no puede volver a crearse.
- `prueba_api.py` lee el repositorio real, de modo que ejecutarla crea la
  base global en `.git/` si no existía. No modifica ninguna tarea.

T-0001 y T-0002 siguen sin ejecutar. wip/columnas-pre-supervisor intacta.

Estado:
A2 = IMPLEMENTADO_PENDIENTE_DE_REVISION

## Supervisor — A3.1: toma atómica de tareas

Tercer componente de orquestación. Corrige un defecto real de concurrencia,
no una hipótesis.

Defecto corregido:
- `supervisor.tomar()` hacía leer -> comprobar -> escribir con TRES conexiones
  distintas y un UPDATE incondicional. Dos trabajadores que competían por la
  misma tarea pasaban ambos la comprobación y el segundo pisaba al primero:
  DOBLE PROPIETARIO. Era un TOCTOU real.

Solución:
- Toda la decisión de la toma ocurre dentro de UNA sola transacción
  BEGIN IMMEDIATE sobre la base global:
  comprobación de estado -> comprobación de ámbitos ->
  UPDATE condicional -> evento -> COMMIT.

Por qué no admite dos ganadores (tres mecanismos, no uno):
1. BEGIN IMMEDIATE toma el bloqueo de escritura en el primer instante de la
   transacción. SQLite admite un solo escritor: la segunda toma espera
   (busy_timeout = 5000 ms) a que la primera confirme o anule.
2. El estado esperado viaja en la propia cláusula WHERE del UPDATE. El motor
   lo comprueba contra la fila REAL en el momento de escribir, no contra una
   lectura anterior: no queda ventana entre comprobar y escribir.
3. La decisión se toma con `rowcount`, las filas que el motor modificó de
   verdad. 1 = ganó; 0 = alguien se adelantó. No se deduce de ninguna
   lectura hecha por Python.

Además, conceder la toma cambia el estado a uno que ya no es reclamable, de
modo que el propio cambio cierra la puerta al siguiente aspirante.

Componentes:
- estado_global.py: `reclamar()`, el primitivo de toma atómica; un conflicto
  NO es excepción, se devuelve descrito
- supervisor.py: `tomar()` reescrita, `ErrorToma`, `conflictos_de_ambito()`
  convertida en función pura para poder ejecutarse dentro de la transacción
- __main__.py: código de salida 3 para la toma rechazada, que distingue
  "perdí la carrera" de "el Supervisor está roto"
- pruebas/orquestacion/prueba_toma_atomica.py (nuevo)

Sin cambios de esquema en SQLite: la garantía sale de cómo se escribe, no
de tablas ni columnas nuevas.

Evidencia de concurrencia REAL (no "PASS"). Corrida de estrés, medida con:

    python pruebas/orquestacion/prueba_toma_atomica.py --carreras 100

- Carreras ejecutadas [RACE_RUNS] = 360, todas con barrera de
  sincronización (100 con 2 procesos + 100 con 10 procesos + 100 de
  ámbitos cruzados + 60 entre conexiones; el bloque de control no suma)
- Tomas concedidas [CLAIMS_SUCCESS] = 360: exactamente una por carrera
- Tomas rechazadas [CLAIMS_REJECTED] = 1520
- Dobles tomas [DOUBLE_CLAIM_EVENTS] = 0
- Errores de SQLite = 0; excepciones inesperadas = 0
- PRAGMA integrity_check: 8 de 8 bases en "ok", sin claves foráneas rotas
- Contención medida hasta 24 procesos simultáneos: un solo ganador
  siempre, sin agotar el busy_timeout ni un solo error de SQLite

El arnés se validó por MUTACIÓN del código, no por confianza:
- reintroducido el TOCTOU en `tomar` (decisión fuera de la transacción):
  2 de 2 y 10 de 10 contendientes ganaban a la vez; las carreras lo
  detectaron en la primera ronda
- quitada la condición de estado del WHERE de `reclamar`: 8 de 8 hilos
  ganaban; lo detectaron la carrera entre conexiones y las comprobaciones
  D, E y L
- la propia prueba incluye un control permanente que ejecuta una toma
  deliberadamente ingenua por el mismo arnés y EXIGE dobles tomas > 0

Decisión de diseño registrada:
- El WHERE condiciona sólo por `estado`, no por `trabajador_id IS NULL`.
  Exigir además el dueño nulo dejaría permanentemente intomable una fila
  reclamable con propietario residual, y la recuperación automática está
  fuera de A3.1. En su lugar la toma desplaza al residual y lo anota en el
  evento (datos.propietario_desplazado), para que el cambio sea trazable.

Pruebas ejecutadas (Python 3.11.15, Linux):
- PRUEBA_NUCLEO=OK
- PRUEBA_VIGA_RAPIDA=OK
- PRUEBA_SUPERVISOR=OK        31 comprobaciones
- PRUEBA_ESTADO_GLOBAL=OK     16 comprobaciones
- PRUEBA_API=OK               12 comprobaciones
- PRUEBA_TOMA_ATOMICA=OK      23 comprobaciones (matriz A..N)

6 de 6 archivos de prueba en OK. La corrida por omisión de
prueba_toma_atomica tarda unos 7 s, muy por debajo del límite de 120 s que
el corredor único concede a cada archivo.

NO implementado en A3.1 (reservado a A3.2/B): latidos automáticos,
expiración de trabajadores, detección de trabajadores muertos, recuperación
automática de tareas abandonadas, cola o planificador, lanzamiento
automático de trabajadores, paralelismo de agentes escritores, verificar()
en el worktree de la tarea.

Hasta dónde llega la garantía (importante, medido, no supuesto):
- La toma es atómica: varios trabajadores que reclaman la misma tarea
  producen exactamente un ganador.
- El resto del ciclo de vida NO lo es. `latido`, `devolver`, `verificar` y
  las órdenes humanas siguen escribiendo con `persistir`, cuyo UPDATE es
  incondicional. Reproducido: A emite un latido; antes de que su escritura
  llegue, un humano devuelve la tarea y C la toma legítimamente; la
  escritura de A pisa a C y la fila vuelve a decir A. Corregirlo exige
  UPDATE condicional también en `persistir`, es decir, rehacer las nueve
  órdenes del ciclo: eso es A3.2, no A3.1.

Correcciones aplicadas durante la auditoría adversarial (todas
reproducidas antes de corregir y validadas por mutación después):
- `tomar` refrescaba TODAS las definiciones antes de comprobar los
  ámbitos, con lo que reescribía el `ambito_archivos` registrado de una
  tarea que otro trabajador tenía en ejecución en otra rama; acto seguido
  no veía el solapamiento y concedía la toma. Reproducido: dos tareas
  EN_EJECUCION sobre `modulos/comun/**`. Ahora sólo incorpora las tareas
  ausentes (`solo_importar=True`); la definición de la tarea que se toma ya
  la pone al día `cargar`.
- El ROLLBACK y el COMMIT del gestor `transaccion` se ejecutaban sin
  protección. Sin espacio en la base, SQLite deshace la transacción por su
  cuenta y el ROLLBACK explícito lanzaba "cannot rollback - no transaction
  is active", que sustituía a la causa real ("database or disk is full") y,
  al no ser `ErrorEstadoGlobal`, la línea de órdenes no sabía traducirla.
  Ahora la causa real llega en español con su código de salida.
- `tomar` leía la fila DESPUÉS del COMMIT: otra orden podía colarse en
  medio y devolver al trabajador una ficha que ya no era suya. Ahora se lee
  dentro de la transacción.
- Tres afirmaciones de la documentación que la auditoría demostró falsas
  ("nunca hay dos propietarios a la vez", "una toma rechazada no escribe
  absolutamente nada", y el alcance de la garantía en la cabecera del
  módulo) quedaron corregidas, no suavizadas.

Deuda conocida de A3.1, no corregida por quedar fuera de su alcance:
- La unicidad de propietario fuera de la toma (ver arriba). Es la deuda
  principal que hereda A3.2.
- `latido()` y `verificar()` no comprueban que quien llama sea el
  propietario de la tarea: cualquiera puede latir o verificar una tarea
  ajena. Pertenece a A3.2 (propiedad efectiva del claim).
- `reanudar()` puede arrebatar una tarea a un trabajador vivo si su latido
  vence; la política de expiración es A3.2.
- Crear la base desde cero con varios procesos a la vez sigue fallando en
  los perdedores con "table tareas already exists" (deuda ya declarada de
  A2, en `inicializar()`). Reproducido con 6 procesos: falla de forma
  explícita, sin corromper nada, y NUNCA produjo doble propietario (un solo
  ganador en 8 de 8 rondas). Basta con crear la base una vez antes de
  lanzar trabajadores.
- El refresco de definiciones todavía puede pisar el ámbito de una tarea
  viva por la puerta de `cargar`. A3.1 cerró la puerta ancha (que `tomar`
  refrescara las definiciones de las demás tareas), pero `cargar` sigue
  refrescando la de la tarea que se pide sin mirar si otro la tiene en
  ejecución. Reproducido: un `tomar T-0001` RECHAZADO por estar ya tomada
  basta para encoger su ámbito registrado, y la toma siguiente de otra
  tarea deja dos escritores sobre el mismo archivo. COMPROBADO que NO lo
  introdujo A3.1: el mismo caso se reproduce idéntico sobre b5578d2b.
  Cerrarlo exige que `sincronizar_ficha` (de A2, compartida con el
  bootstrap y con `sincronizar-definiciones`) congele el ámbito mientras la
  tarea lo retiene. Pertenece a A3.2.
- Como contrapartida, ampliar el ámbito de una tarea ya viva no se tiene en
  cuenta hasta que deje de estarlo o hasta ejecutar
  `sincronizar-definiciones`.
- La deuda de A2 sigue vigente salvo la atomicidad de `tomar`, ya resuelta.

Pendiente de ejecución en Windows: es el entorno final real y esta corrida
fue en Linux. Ver los comandos de verificación en orquestacion/README.md.

T-0001 y T-0002 siguen sin ejecutar. wip/columnas-pre-supervisor intacta.

Estado:
A3.1 = IMPLEMENTADO_PENDIENTE_DE_VERIFICACION_EN_WINDOWS
