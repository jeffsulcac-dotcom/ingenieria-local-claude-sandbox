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
| `__main__.py` | Interfaz de línea de comandos, en español |

Estado persistente:

    orquestacion/tareas/<id>.json

Una ficha JSON por tarea, versionada en Git. No hay ninguna otra fuente de
verdad.

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

Prueba correspondiente:

    pruebas/orquestacion/prueba_supervisor.py

### Cómo se invoca

Desde la raíz del repositorio:

    python -m orquestacion.ingenieria_supervisor estado
    python -m orquestacion.ingenieria_supervisor ver T-0001
    python -m orquestacion.ingenieria_supervisor pruebas --detalle
    python -m orquestacion.ingenieria_supervisor tomar T-0001
    python -m orquestacion.ingenieria_supervisor verificar T-0001
    python -m orquestacion.ingenieria_supervisor reanudar
    python -m orquestacion.ingenieria_supervisor aprobar T-0001
    python -m orquestacion.ingenieria_supervisor rechazar T-0001 --motivo "..."

---

## QUÉ NO EXISTE TODAVÍA

### Queda para V2

- Acciones desde el tablero web: hoy es de sólo lectura.
- Aprobación y rechazo desde la interfaz gráfica.
- Cola persistente de tareas.
- Priorización automática entre tareas pendientes.
- Notificaciones.

### Queda para el paralelismo

- Creación y destrucción real de worktrees de Git.
- Lanzamiento automático de agentes trabajadores.
- Varios trabajadores simultáneos sobre ámbitos disjuntos.

El campo `worktree` de la ficha ya existe y hoy permanece vacío: está
reservado para esa etapa. La detección de solapamiento de ámbitos ya está
implementada y probada, porque es el requisito previo para poder trabajar
en paralelo sin corromper nada.

### Queda para n8n

- Flujos de n8n que invoquen la línea de comandos del Supervisor.
- Disparadores programados.

n8n no forma parte de esta versión. La arquitectura está preparada para
que llame al mismo CLI, sin mover la lógica fuera de Python local:
si n8n desaparece, el Supervisor sigue funcionando igual.

### Descartado para esta versión

- PostgreSQL como estado de tareas.
- Redis como cola de tareas.

Git y las fichas JSON son el estado. No se duplica el origen de verdad.
PostgreSQL y Redis siguen disponibles para fases posteriores y otros usos.
