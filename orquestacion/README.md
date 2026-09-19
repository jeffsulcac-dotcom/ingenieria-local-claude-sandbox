# ORQUESTACIÓN

Coordina trabajos, agentes y procesos.

La orquestación decide:
- qué tarea ejecutar
- cuándo ejecutarla
- quién la ejecuta
- qué hacer ante error

NO realiza cálculos de ingeniería.
NO contiene lógica técnica de Revit, CAD o ETABS.

---

## QUÉ EXISTE REALMENTE HOY

### Supervisor de Desarrollo V1

Programa local de línea de comandos:

    orquestacion/ingenieria_supervisor/

| Archivo | Responsabilidad |
|---|---|
| `tarea.py` | Contrato de la ficha, escritura atómica, lectura tolerante a fallos |
| `pruebas.py` | Corredor único de pruebas y veredicto OK / FALLO / INDETERMINADO |
| `supervisor.py` | Máquina de estados, ámbitos, recuperación, límites de commit |
| `estado_global.py` | Base SQLite global: ubicación, esquema, transacciones, bootstrap, diagnóstico (A2) |
| `__main__.py` | Interfaz de línea de comandos, en español |

Estado persistente (desde A2, ver la sección siguiente):

    <git-common-dir>/ingenieria-supervisor.sqlite3   estado operativo (autoridad)
    orquestacion/tareas/<id>.json                    definición versionada en Git

### A2 — Estado operativo global en SQLite

**Qué resuelve.** V1 guardaba todo el estado en la ficha JSON de cada
worktree: dos worktrees del mismo repositorio podían tener dos verdades
distintas. A2 introduce UNA base SQLite por repositorio, compartida por
la rama principal y por todos sus worktrees.

**Dónde vive.** Se resuelve con Git, nunca con rutas fijas:

    git rev-parse --git-common-dir   ->   <común>/ingenieria-supervisor.sqlite3

Desde `main` Git responde `.git`; desde un worktree enlazado responde la
ruta absoluta del `.git` principal. Ambas conducen al MISMO archivo. Al
estar dentro de `.git/` no se versiona ni aparece en `git status`; si el
repositorio se mueve de carpeta, la ruta se vuelve a resolver sola. Sin
Git no hay base global: el Supervisor falla de forma explícita en lugar
de crear una base local que pudiera divergir.

**Autoridad.**

| Dato | Autoridad | Dónde |
|---|---|---|
| id, título, objetivo, criterios, ámbito, pruebas requeridas | Definición | JSON |
| Decisiones humanas: clave y descripción | Definición | JSON |
| estado, rama, worktree, intentos, max_intentos | Operativo | SQLite |
| trabajador_id, pid, iniciado_en, ultimo_latido, actualizado_en | Operativo | SQLite |
| ultima_falla, última verificación, commit_inicial | Operativo | SQLite |
| Decisiones humanas: resuelta, resolución, fecha, origen | Operativo | SQLite |
| Historial de eventos | Operativo | SQLite (tabla `eventos`) |

Los campos operativos que siguen presentes en el JSON son un **espejo
derivado**: cada operación confirma primero la transacción SQLite y sólo
después regenera el JSON con la escritura atómica de V1. Al cargar una
tarea, SQLite se superpone a lo que diga el JSON, de modo que editar el
JSON a mano no cambia el estado operativo. El espejo se conserva para que
el commit automático de la ficha siga dejando rastro en Git y para que las
pruebas de V1 sigan valiendo. Nunca se escribe de forma independiente.

**Esquema (versión 1).** Tres tablas: `esquema` (versiones aplicadas),
`tareas` (una fila por tarea, con la referencia a su JSON y la huella de su
definición) y `eventos` (historial operativo: creación, importación,
sincronización, transición, verificación, decisión, recuperación). Pragmas:
`foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`,
`busy_timeout=5000`. Transacciones explícitas `BEGIN IMMEDIATE / COMMIT`
con `ROLLBACK` ante cualquier error. Migraciones simples versionadas en un
diccionario; nunca se edita una versión publicada, se añade la siguiente.

**Bootstrap.** `sincronizar-definiciones` (o cualquier consulta) importa
las fichas JSON legibles que aún no estén en la base copiando, una única
vez, el estado operativo que traían; después sólo refresca la definición
cuando su huella cambia. Es idempotente, no escribe JSON, no ejecuta
tareas, no cambia estados y no resuelve decisiones. T-0001 y T-0002
quedaron en la base tal como estaban: NUEVO, sin ejecutar, con las tres
decisiones humanas de T-0001 pendientes.

**Qué escribe en SQLite.** Todas las operaciones del ciclo: crear, tomar,
latido, devolver, verificar, decidir, aprobar, rechazar, reabrir, bloquear
y reanudar. `estado`, `ver`, la API y el tablero `/desarrollo` leen SQLite.

**Limitaciones conocidas de A2 (por diseño).**

- Borrar una ficha JSON no borra la tarea del estado global: el
  identificador queda reservado, la tarea sigue visible marcada como
  "definición no legible" y no puede volver a crearse con el mismo id.
- La comprobación de solapamiento de ámbitos y la escritura de `tomar` no
  son atómicas entre procesos.
- `verificar` ejecuta las pruebas sobre la raíz indicada, no sobre el
  worktree registrado de la tarea.
- `latido` y `reanudar` siguen siendo órdenes manuales.

Prueba correspondiente:

    pruebas/orquestacion/prueba_estado_global.py

### Implementado y probado

- Ficha de tarea con contrato completo y validación.
- Escritura atómica: temporal, validación, reemplazo.
- Máquina de estados sobre los estados comunes de `nucleo`.
- Prohibición estructural de autoaprobación: el Supervisor llega como
  máximo a `propuesto`, `requiere_revision` o `bloqueado`.
- Freno por decisión humana pendiente.
- Corredor único que descubre `pruebas/**/prueba_*.py`, aísla cada prueba
  en un subproceso con tiempo límite y exige código de salida 0 **y** marca
  `PRUEBA_XXXX=OK`.
- Comprobación de las pruebas que cada ficha declara como requeridas.
- Detección de solapamiento de ámbitos entre tareas activas:
  un solo escritor por archivo en conflicto.
- Límite de intentos y bloqueo automático al agotarlos.
- Recuperación tras cierre o apagón: `reanudar` distingue tarea activa,
  ejecución huérfana y ficha inconsistente, sin confiar únicamente en el PID.
- Commit automático estrictamente limitado a la ficha de la tarea, dentro
  de la rama de la tarea, nunca en `main`.
- Tablero web de sólo lectura en `/desarrollo`.
- (A2) Estado operativo en la base SQLite global; API
  `GET /api/desarrollo/estado`; indicador "Base global SQLite" en el
  tablero; órdenes `diagnostico`, `inicializar-estado` y
  `sincronizar-definiciones`.

Pruebas correspondientes:

    pruebas/orquestacion/prueba_supervisor.py
    pruebas/orquestacion/prueba_estado_global.py

### Cómo se invoca

Desde la raíz del repositorio (forma canónica, sin alias):

    python -m orquestacion.ingenieria_supervisor estado
    python -m orquestacion.ingenieria_supervisor diagnostico
    python -m orquestacion.ingenieria_supervisor inicializar-estado
    python -m orquestacion.ingenieria_supervisor sincronizar-definiciones
    python -m orquestacion.ingenieria_supervisor ver T-0001
    python -m orquestacion.ingenieria_supervisor pruebas --detalle
    python -m orquestacion.ingenieria_supervisor tomar T-0001
    python -m orquestacion.ingenieria_supervisor verificar T-0001
    python -m orquestacion.ingenieria_supervisor reanudar
    python -m orquestacion.ingenieria_supervisor aprobar T-0001
    python -m orquestacion.ingenieria_supervisor rechazar T-0001 --motivo "..."

---

## QUÉ NO EXISTE TODAVÍA

### A3/B — pendiente, NO implementado

- Toma atómica concurrente de tareas.
- Locks.
- Latidos automáticos.
- Detección avanzada de trabajadores huérfanos.
- `verificar()` ejecutando dentro del worktree de la tarea.
- Lanzamiento de trabajadores (Claude) y varios trabajadores simultáneos.
- Creación y destrucción automática de worktrees de Git.
- Cola automática de tareas.

A2 deja la base para todo eso (una sola fuente operativa compartida por los
worktrees, transacciones y `busy_timeout`), pero no lo adelanta.

### Queda para V2

- Acciones desde el tablero web: hoy es de sólo lectura.
- Aprobación y rechazo desde la interfaz gráfica.
- Priorización automática entre tareas pendientes.
- Notificaciones.

El campo `worktree` de la tarea ya existe en SQLite y hoy permanece vacío:
está reservado para el paralelismo. La detección de solapamiento de
ámbitos ya está implementada y probada, porque es el requisito previo para
poder trabajar en paralelo sin corromper nada.

### Queda para n8n

- Flujos de n8n que invoquen la línea de comandos del Supervisor.
- Disparadores programados.

n8n no forma parte de esta versión. La arquitectura está preparada para
que llame al mismo CLI, sin mover la lógica fuera de Python local:
si n8n desaparece, el Supervisor sigue funcionando igual.

### Descartado para esta versión

- PostgreSQL como estado del Supervisor.
- Redis como cola de tareas.
- n8n ejecutando tareas.

SQLite (estado operativo) y las fichas JSON en Git (definición) son el
estado. No se duplica el origen de verdad. PostgreSQL y Redis siguen
disponibles para fases posteriores y otros usos.
