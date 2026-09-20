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

Cuál actúa dónde, medido: en `tomar` la garantía la sostiene el mecanismo 1
(BEGIN IMMEDIATE serializa, y la segunda transacción lee la fila ya
cambiada y se rechaza en la comprobación de estado). Los mecanismos 2 y 3
son la garantía del primitivo `reclamar`, y quedan de red de seguridad; se
ejercitan de lleno en la carrera entre conexiones, que llama a `reclamar`
directamente y produce sus 420 rechazos por rowcount = 0.

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

Contención máxima que alcanza esa corrida: 10 procesos simultáneos sobre la
misma tarea (bloque "procesos x10") y 8 conexiones simultáneas sobre
BEGIN IMMEDIATE (bloque "conexiones x8"). En total arranca 22 procesos con
"spawn", nunca los 22 a la vez: los bloques abren y cierran su arnés uno
tras otro.

Fuera de la prueba, a mano, se comprobó además con 16 y con 24 procesos
simultáneos: un solo ganador en todas las rondas, sin agotar el
busy_timeout ni un error de SQLite. Eso NO forma parte de la prueba
automática; queda aquí como dato, no como evidencia repetible.

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

Repetido con Python 3.12, que es la versión de esta PC: regresión completa
6 de 6 en OK y estrés con los mismos 360 / 360 / 1520 / 0. También pasa con
3.13. Importa porque `borrar()` usa `shutil.rmtree(onexc=...)` desde 3.12 y
`onerror` antes: hasta ahora sólo se había ejercitado la rama de 3.11, y es
la de 3.12 la que correrá en Windows.

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
reproducidas antes de corregir; las de comportamiento, validadas después
por mutación del código):
- El rechazo por estado se decide antes que el rechazo por ámbito, dentro
  de la transacción, para que el motivo sea el verdadero. Antes, una tarea
  aprobada cuyo ámbito además se solapara se rechazaba por "ámbito en
  conflicto" y quien la pedía quedaba esperando a que se liberase un
  ámbito que no la iba a desbloquear nunca. Validado por mutación: quitar
  la comprobación hace fallar la comprobación L.
- El código de salida 3 de `tomar` estaba documentado pero no probado, y
  lo documentado no era cierto: una tarea inexistente sale con 2, no con
  3. Ahora hay una comprobación de extremo a extremo sobre la propia CLI.
- El informe de rechazo se extrajo a `estado_global.rechazo`, compartida
  por `reclamar` y por la comprobación previa, para que el rechazo se lea
  igual venga de donde venga.
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

Verificación en Windows: EJECUTADA Y APROBADA. PowerShell 7.6.6,
Python 3.12.4. Carrera nominal código 0 en unos 29,29 s. Estrés de 100
carreras código 0 en unos 82,99 s. Métricas acumuladas: 360 carreras,
360 tomas concedidas, 1520 rechazadas, 0 dobles tomas reales, 0 errores
SQLite, 0 excepciones inesperadas, integridad 8/8. Corredor completo 6/6 OK
en unos 45,99 s. Mutación TOCTOU detectada. Mutación de UPDATE incondicional
detectada. Carrera concurrente posterior a la integración: 84 carreras,
84 tomas, 508 rechazos, 0 dobles tomas, integridad 8/8.

El riesgo de rendimiento que A3.1 dejó anotado —las invocaciones repetidas
de `git rev-parse --git-common-dir`— quedó RESUELTO en A3.2, que es la
etapa cuya batería acercó la corrida al límite.

T-0001 y T-0002 siguen sin ejecutar. wip/columnas-pre-supervisor intacta.

Estado:
A3.1 = CERRADO_Y_APROBADO

Integrado en main como 629b3a4 "Integrar A3.1 toma atomica de tareas".

## Supervisor — A3.2: propiedad efectiva durante el ciclo

Base: 629b3a4.

A3.1 garantizaba que una tarea sólo pudiera ser TOMADA por un trabajador.
A3.2 garantiza que, después de la toma, sólo el propietario VIGENTE pueda
modificar el estado operativo de esa ejecución durante todo su ciclo.

El problema que cierra: A toma una tarea, la pierde, B pasa a ser el
propietario, y una orden que A había compuesto ANTES llega después. Antes de
A3.2 esa orden entraba, porque las nueve órdenes del ciclo escribían con
`persistir`, cuyo UPDATE era `WHERE id = ?` y nada más.

Mecanismo elegido:

- Columna nueva `tareas.generacion` (migración de esquema 2, desde la 1).
- Sólo la incrementa `reclamar`, con `generacion = generacion + 1` dentro
  del mismo UPDATE condicional que concede la toma, resuelto por el motor.
- La pareja (trabajador_id, generacion) identifica una EJECUCIÓN, no un
  trabajador. Por eso distingue "worker-01 ejecución vieja" de "worker-01
  ejecución nueva", que es el problema ABA.
- No se eligió el reloj porque `iniciado_en` está truncado a segundos, ni
  el PID porque el sistema los reutiliza. No hace falta criptografía: la
  base es local y de una sola PC.

Operaciones protegidas:

- `latido`, `devolver` y `verificar` exigen identidad Y generación.
- Todas las órdenes, incluidas las humanas, exigen la generación con la que
  se leyó la ficha: tampoco deben pisar una ejecución que empezó mientras
  su emisor decidía.
- Un rechazo lanza ErrorPropiedad dentro de la transacción: no escribe
  estado, ni eventos, ni intentos, ni espejo JSON, ni marcas de tiempo.
- Código de salida 4 en la línea de órdenes, atendido en un solo sitio para
  que valga en todas las órdenes.

Además:

- El ámbito de una tarea viva ya no se puede cambiar por la puerta de
  `cargar`. La huella no avanza mientras el cambio está congelado, así que
  el refresco se aplica solo cuando la tarea deja de estar viva.
- El arranque concurrente de la base ya no falla. Se corrigieron las dos
  carreras: la del esquema (la versión se relee dentro de la transacción) y
  la de la conversión inicial a WAL, que no estaba declarada en ninguna
  parte.
- Se aplicó la mitigación de `git_common_dir` que A3.1 dejó medida.

Evidencia medida en Linux (Python 3.11.15):

- Corredor completo: 7/7 OK, unos 13 s. Repetido 11 veces seguidas sin un
  solo fallo, después de que una corrida expusiera el fallo intermitente
  del WAL.
- Batería de A3.2: 31 comprobaciones, unos 3 s.
- Corrida ampliada (--rezagadas 500 --emisores 12 --ordenes 60): 1245
  órdenes, 1239 rechazadas, 0 escrituras indebidas, 0 errores SQLite,
  0 excepciones inesperadas, integridad 31/31.
- Estrés concurrente: 12 procesos disparando a la vez órdenes rezagadas
  contra el dueño vigente. 720 emitidas, 720 rechazadas, 0 aceptadas.
- Bootstrap concurrente: 6 procesos por ronda, 3 rondas, 0 fallos.
- Recursos: 352 conexiones SQLite abiertas, 0 vivas al terminar, y código 0
  bajo `-X dev -W error::ResourceWarning`.
- Coste en Git: 105 invocaciones en el proceso padre, 21 de ellas
  `rev-parse --git-common-dir` (una por repositorio temporal), frente a las
  439 y 355 de antes de memorizarlo.
- Mutaciones: 25 de 25 detectadas por la batería. La del bootstrap
  reprodujo el error original literal ("table tareas already exists").

Auditoría adversarial: 8 revisores de sólo lectura sobre copias protegidas.
Encontraron cuatro defectos REALES de esta misma etapa, todos corregidos y
todos con prueba propia:

- el testigo de propiedad se serializaba al JSON y por tanto era
  falsificable (crítico);
- la generación sola no cubría el avance del ciclo, y una orden humana
  rezagada revertía una transición ya confirmada (crítico);
- la toma no grababa el ámbito que acababa de validar, y por
  `requiere_revision` volvían a quedar dos escritores (crítico);
- la ruta de sólo lectura pedía el bloqueo de escritura de toda la base
  para no escribir nada (medio);
- la guarda de ámbito estaba a medias: la base no grababa el ámbito nuevo
  de una tarea viva, pero `cargar` seguía devolviendo el del JSON, así que
  el trabajador creía poseer archivos que nadie le concedió y otra tarea
  podía tomar legítimamente esa parte (crítico).

La ronda FOCALIZADA sobre esas correcciones encontró dos regresiones más,
introducidas por las propias correcciones, y también están cerradas:

- la salida rápida de `asegurar_ficha` delegaba y podía acabar escribiendo
  en autocommit, fuera de toda transacción (alto);
- la retoma grababa el ámbito declarado sin mirar si encogía, soltando el
  terreno que la retención protegía (alto);
- el reintento de la conversión a WAL estaba acotado en número de intentos
  pero no en tiempo: su peor caso real era de unos 41 s (medio);
- el diagnóstico de "no admite WAL" vivía en una rama inalcanzable, así que
  una unidad de red se reportaba como si otro proceso tuviera la base
  ocupada (medio);
- la guarda de "esquema más nuevo" no se evaluaba cuando no había ninguna
  migración que aplicar, que es el camino normal de cada orden (bajo);
- la memoria de `git_common_dir` distinguía si el directorio había
  desaparecido, pero no si esa ruta pertenecía ya a OTRO repositorio, y
  entonces devolvía la base global equivocada (bajo);
- `sincronizar_lista` pedía el bloqueo de escritura aunque todo lo
  pendiente estuviera congelado (bajo);
- la generación no tenía salida legible por máquina (bajo);
- borrar del JSON una decisión humana PENDIENTE le quitaba el freno a una
  tarea viva, desde una orden de sólo lectura: la misma clase que el
  ámbito, en otro campo (medio);
- `diagnostico` metía en la misma lista una definición que todavía no se ha
  aplicado y otra que NO SE VA A aplicar mientras la tarea siga viva, lo
  que deja al operador esperando un refresco que no va a llegar (bajo);
- la cuarta puerta del testigo de propiedad, el veto de
  `actualizar_si_propietario`, era la única sin prueba: estaba puesta, pero
  la batería no se enteraba si alguien la quitaba (bajo).

Y una equivocación propia, corregida con la medición delante: se retiró el
reintento de la conversión a WAL por considerarlo no verificado, y la
corrida completa del corredor lo desmintió en el acto (1 de 6 procesos
murió con "database is locked"). El reintento volvió.

Falso positivo descartado ejecutándolo: `cargar` NO borra una decisión
humana resuelta; `fusionar_decisiones` la conserva.

Lo que A3.2 NO cierra, dicho con precisión: `persistir` escribe las
dieciséis columnas operativas en bloque, así que dos órdenes con la misma
generación, identidad y estado siguen pudiendo pisarse campo a campo.
Pertenece a A3.3.

Pendiente de ejecución en Windows: es el entorno final real y esta corrida
fue en Linux. Los comandos están en orquestacion/README.md.

NO implementado en A3.2, reservado a A3.3: latidos automáticos, expiración
temporal de trabajadores, detección avanzada de huérfanos, recuperación
automática, `verificar()` dentro del worktree de la tarea.

NO implementado en A3.2, reservado a C: trabajadores paralelos, worktrees
automáticos, lanzador, cola y priorización.

T-0001 y T-0002 siguen sin ejecutar, en estado NUEVA.

Estado:
A3.2 = IMPLEMENTADO_PENDIENTE_DE_VERIFICACION_EN_WINDOWS
     (cerrada: ver la sección A3.3, que la integra y la amplía)

## Supervisor — A3.3: ejecución segura, recuperación y worktrees

Base: 419d15e (A3.2 integrada).

Qué cierra A3.3, que es lo último que faltaba antes de poder poner a
trabajar a varios escritores a la vez:

1. **Cada orden escribe sólo sus campos.** A3.2 impedía que entrara una
   orden AJENA, pero dos órdenes legítimas del mismo propietario seguían
   pisándose: `persistir` reescribía las dieciséis columnas operativas con la
   foto que tenía en memoria. Ahora declarar las columnas es obligatorio.
   Los contadores los incrementa el motor en SQL, no Python.

2. **Latido automático** durante las operaciones largas del Supervisor.
   Escribe sólo `ultimo_latido`, con identidad, generación, estado y una
   guarda de no regresión; se para solo al terminar; aguanta un
   `database is locked` sin apagarse; no resucita nada.

3. **Vitalidad en seis estados** (ACTIVA, LATIDO_VENCIDO, HUÉRFANA,
   REANUDABLE, ESPERA_HUMANA, FINALIZADA) que NUNCA declara abandono con una sola señal.
   Un proceso vivo y comprobable lo impide, dure lo que dure el silencio.
   Un trabajador de otra máquina no se libera solo: lo decide una persona.

4. **Recuperación idempotente** que no le quita la tarea a quien sigue
   vivo, ni siquiera si da señal justo entre la clasificación y la
   escritura.

5. **`verificar()` ejecuta en el worktree REGISTRADO de la tarea** —el que
   `git worktree list` conoce— y graba dónde corrió: árbol, rama, commit,
   generación y trabajador. Si el árbol se movió a mitad, la corrida no se
   graba: no se sabe qué se ejecutó.

6. **`crear` atómico**, con el archivo escrito después del COMMIT.

7. **El tablero y la consola dicen la verdad**: vitalidad, edad del
   latido, dónde se verificó, y si ese verde es de otra ejecución. El
   tablero ya no escribe nada.

Cómo se comprobó, porque «PASS» no es evidencia:

- 37 comprobaciones en `pruebas/orquestacion/prueba_ejecucion_segura.py`
  (34 en la corrida de Windows del 19/09, rotuladas hasta la 36; la
  revisión final cerró la numeración y añadió tres), con carreras reales
  entre procesos (`spawn` + barrera compartida) y
  métricas medidas: 0 actualizaciones perdidas, 0 robos indebidos, 0
  verificaciones en árbol incorrecto, 0 errores SQLite, 0 excepciones, 0
  fallos de integridad en 32 `PRAGMA integrity_check`. Las métricas
  positivas llevan cota mínima: sin ella, una corrida que no hiciera nada
  habría salido igual de verde.
- 22 mutaciones del código reintroducidas sobre copias temporales, 22
  detectadas por la batería.
- Dos rondas de auditoría adversarial con seis y tres revisores de sólo
  lectura, sobre un montaje inmutable del repositorio. Sesenta hallazgos
  confirmados en la primera ronda; todos los críticos y altos, cerrados.

Lo más grave que encontró la auditoría, y que ya está cerrado:

- Se podía ejecutar y grabar un VERDE FALSO: bastaba copiar un worktree a
  otro sitio, o declarar un subdirectorio cualquiera, o tener `GIT_DIR` en
  el entorno (lo está siempre dentro de un hook de Git).
- La rama y el commit se leían DESPUÉS de correr: quedaba grabado «commit
  X, todo en verde» cuando X nunca se ejecutó y estaba rojo.
- El umbral de abandono declaraba huérfana una ejecución sin mirar
  siquiera si el proceso seguía vivo.
- Un latido del dueño llegado a mitad de la recuperación no la frenaba.
- Dos `decidir` a la vez se pisaban: 24 de 25 carreras perdían una
  resolución humana, sin error y sin rastro.
- El espejo JSON borraba lo que una persona escribía durante la corrida,
  incluida una decisión que quería frenar la propuesta.
- El tablero «de sólo lectura» creaba tareas desde un GET de la API web.

Deuda conocida, marcada para revisión humana y anotada en
`orquestacion/README.md`: la reutilización de PID no se descarta
comparando la hora de arranque del proceso (es específico de cada sistema
y no se escribe a ciegas); el efecto está acotado en la dirección segura
—la tarea espera a una persona en vez de quitársele a nadie— y la salida
manual es `reabrir`.

Verificado en Windows (PowerShell 7.6.5 y Python 3.12.4) el 19/09/2026:

- `prueba_ejecucion_segura.py`: OK en 18.54 s; 0 actualizaciones perdidas,
  robos indebidos, errores SQLite, excepciones y fallos de integridad.
- Estrés de 40 rondas: 240 órdenes concurrentes aceptadas, 0 pérdidas y 0
  errores SQLite, en 18.55 s.
- La misma batería con `-X dev -W error::ResourceWarning`: OK, sin avisos.
- Corredor completo confirmado desde `verificar` sobre T-9003 temporal: 8 de
  8 pruebas OK, ejecutadas en un worktree Git real y registradas como
  verificadas allí.
- Rutas Windows `C:pruebas` y `\pruebas`: rechazadas con código 6; junction
  y ruta con espacios: aceptadas por la comprobación de worktree.
- La desaparición de un worktree no libera una ejecución fresca; la
  comprobación 17 fuerza el caso huérfano y exige el aviso correspondiente.

El guion del gate se corrigió para no devolver T-9004 antes de comprobar la
retoma: devolver libera el worktree por diseño y, por tanto, no podía probar
su desaparición. Las tareas de gate vivieron sólo en un clon temporal, sin
afectar T-0001 ni T-0002, que no se ejecutaron.

## Supervisor — A3.3: revisión final adversarial (20/09/2026)

Sobre la punta 17530d0, en Linux (Python 3.11, git 2.43), con siete
revisores de sólo lectura por dimensión (rutas y worktrees, concurrencia,
recuperación y vitalidad, pruebas, evidencia Windows, consola y tablero,
documentación frente a código) más tres de ojos frescos, y verificación
propia de cada hallazgo por reproducción antes de tocar nada. Todo lo
corregido lleva su comprobación y su mutante.

Defectos reales encontrados y cerrados en esta rama:

1. **Verde falso por worktree.** `resolver_worktree` sólo miraba que la
   ruta apareciera en `git worktree list`. Git sigue listando una entrada
   `prunable` (directorio borrado y recreado como carpeta corriente), y
   lista como válida una ruta que este repositorio registró y borró y que
   OTRO repositorio reutilizó para un worktree suyo: `verificar` corría
   allí y grababa rama y commit vacíos o ajenos. Ahora se descartan
   `prunable` y `bare`, y se exige que `git`, desde dentro de la ruta,
   responda con esa raíz y con el directorio común de este repositorio.
2. **El instante del latido se leía antes de pedir el candado**, al revés
   de lo que decían el README y el comentario del código; lo mismo en la
   toma. Corregido; la comprobación 33 lee el orden real.
3. **Un fallo del espejo JSON abortaba `reanudar` entero**: el `except`
   estaba alrededor de `_registrar_en_git`, que no lanza; el fallo real
   subía desde `persistir`. La primera tarea quedaba recuperada en la base
   con el JSON viejo y sin figurar en el informe, y las siguientes sin
   revisar. Reproducido y corregido (34).
4. **La consola de `reanudar` callaba** `inconsistentes_sin_tocar` y
   `espejo_no_regenerado` —la orden humana `reabrir` que el README manda
   no podía darse porque nadie sabía que hacía falta— y «Inconsistentes
   recuperadas» era un contador muerto. Ahora imprime todos los grupos y
   devuelve 1 cuando deja algo que una persona debe mirar (35).
5. `verificar`: un árbol que desaparece a mitad salía como traceback con
   código 1 (ahora `ErrorWorktree`, código 6); un árbol sucio se grababa
   como «commit X en verde» sin marca (ahora `sin_confirmar` en la
   evidencia, el tablero, `estado`, `ver` y la salida de `verificar`); y
   una edición a mitad en un árbol ya sucio pasaba por quieto (ahora se
   compara una huella del contenido, no un booleano). Comprobaciones 19 y
   20.
6. `reanudar` sólo avisaba del worktree ausente en las tareas que
   liberaba; una ejecución ACTIVA con el árbol borrado no se avisaba (17).
7. Un error transitorio al releer la fila tras un rechazo del latido se
   convertía en «propiedad perdida» y `verificar` descartaba la batería
   entera (30); el motivo de un rechazo por `exigir_iguales` se
   contradecía a sí mismo («en_ejecucion no admite esta orden; la
   admiten: en_ejecucion») y era lo que la consola enseñaba (28);
   `~usuario_inexistente` como worktree producía un traceback (5).
8. Consola y tablero: `ver` sobre una fila rota reventaba con traceback y
   código 1 (ahora avería, código 2, en español); la tarjeta de una fila
   ilegible culpaba al JSON sano e inventaba «0 / 0»; `estado --json`
   devolvía 0 con la base en ERROR; `verificar` no decía dónde corrió;
   `ver` no mostraba la última falla ni dónde se verificó; la web no
   avisaba de fichas sin importar ni de ejecuciones sin señal.
9. Pruebas: la batería tenía 34 comprobaciones rotuladas hasta la 36 y la
   documentación decía 36; 14 de 17 mutantes nuevos sobrevivían (entre
   ellos «acepta un worktree prunable» y «el latido lee el reloj fuera del
   candado»); la 37 contaba como aceptados latidos que la guarda había
   rechazado; las 23 y 24 sumaban «rechazadas» sin ninguna operación; y
   `prueba_api.py` exigía que las fichas aparecieran en el tablero, lo que
   sólo ocurre si la base ya las conoce: en un clon limpio el corredor daba
   7 de 8. Todo cerrado: 37 comprobaciones, 19 mutantes nuevos detectados
   (tabla en el README), corredor 8 de 8 en un clon limpio.
10. Documentación: «PENDIENTE DE EJECUTAR» con el gate ya ejecutado, un
    `devolver T-9003` imposible en el paso 10 del gate, un paso 8 cuyo
    resultado dependía de cuánto se tardara desde la toma, cifras de
    invocaciones rancias.
11. Segunda tanda (misma revisión, tras la ronda de ojos frescos y la
    verificación cruzada). Una regresión de la propia revisión: la huella
    del contenido contaba el espejo JSON del Supervisor, y una decisión
    resuelta a mitad de la batería abortaba la corrida; corregida antes de
    cerrar. Y defectos de la punta original que salieron con ella: la
    relectura de decisiones tras la corrida no releía nada (la lista en
    memoria mandaba sobre la base); sin `--worktree` el árbol era «la raíz
    de quien invoque la siguiente orden» (un trabajador tomando desde su
    worktree y un operador verificando desde `main` proponían una tarea
    cuyo trabajo nunca se ejecutó); un `database is locked` llega como
    `ErrorEstadoGlobal` y apagaba el latido al primer choque (la prueba
    lo simulaba con un tipo que en producción nunca llega); el `latido`
    manual podía retroceder la marca; `verificar` decidía BLOQUEADO con un
    `max_intentos` que ya no era el vigente; `aprobar` no exigía al motor
    lo que comprobaba en Python; la falla ámbar quedaba rancia; PROPUESTO
    y BLOQUEADO se rotulaban FINALIZADA; `reanudar` devolvía 0 con fichas
    ilegibles; `os.replace` del espejo sin reintento ante un lector en
    Windows; `prueba_api.py` ahora corre sobre un repositorio temporal.
12. Corrección de gate: en Windows la comprobación 5 falló por una
    comparación textual (introducida en esta revisión) entre una ruta de
    Python y la que imprime `git worktree list --porcelain`. Reproducido
    en Linux con `TMPDIR` a través de un enlace simbólico; corregido
    comparando rutas resueltas. Los cinco casos de seguridad de
    worktrees siguen detectándose al reintroducir cada defecto.

Marcado para decisión humana, sin cambiar el comportamiento (detalle en
la deuda conocida del README): la ventana de 120 s de una toma desde la
consola; que `verificar` no exija la rama de la tarea; que `ver` y el
tablero creen la base o importen fichas; identificadores con `/`;
`proceso_vivo` en Windows ante «acceso denegado»; marcas de latido sin
zona horaria; los errores de `argparse` en inglés; si una prueba
requerida sin versionar debe valer; `diagnostico` en rutas UNC.

Comprobado en esta rama (Linux):

- `prueba_ejecucion_segura.py`: 37 de 37, `PRUEBA_EJECUCION_SEGURA=OK`,
  ~17 s; las seis métricas de fallo en 0; con `-X dev -W
  error::ResourceWarning`: OK sin avisos; 446 invocaciones de `git` en el
  proceso padre y 483 conexiones SQLite, 0 vivas al terminar.
- Las otras cuatro baterías de orquestación y `prueba_api.py`: OK.
  Corredor único (`pruebas --detalle`): 8 de 8 APROBADO en un clon limpio.
- Mutación: las 29 reintroducciones de la tabla R1–R29 del README, una a
  una sobre copias limpias: 29 fallos de la batería, en la comprobación
  esperada cada una.

Pendiente en Windows: los cambios de esta revisión (`resolver_worktree`,
`verificar`, `reanudar`, la consola, la batería) sólo se han ejecutado en
Linux. Repetir el gate del README sobre la nueva punta y anotar los ocho
criterios; de la corrida del 19/09 no consta explícitamente el criterio 5
(corredor completo desde la raíz) ni la salida vacía del paso 9.

T-0001 y T-0002 no se ejecutaron ni se tocaron.

NO implementado en A3.3, reservado a C: lanzamiento de trabajadores,
trabajadores paralelos, worktrees automáticos, cola y priorización,
expiración automática de trabajadores y acciones desde el tablero.

T-0001 y T-0002 siguen sin ejecutar, en estado NUEVA.

Estado:
A3.2 = CERRADA
A3.3 = LISTO_PARA_REVISION
