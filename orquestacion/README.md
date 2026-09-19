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
| `estado_global.py` | Base SQLite global: ubicación, esquema, transacciones, bootstrap, diagnóstico (A2) y toma atómica (A3.1) |
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
derivado**: cada operación del ciclo confirma primero la transacción
SQLite y sólo después regenera el JSON con la escritura atómica de V1.
La excepción es `crear`, que escribe antes el JSON porque la definición es
el contrato: sin ficha versionada no hay tarea que registrar. Al cargar una
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
tareas, no cambia estados y no resuelve decisiones.

**Qué escribe en SQLite.** Todas las operaciones del ciclo: crear, tomar,
latido, devolver, verificar, decidir, aprobar, rechazar, reabrir, bloquear
y reanudar. `estado`, `ver`, la API y el tablero `/desarrollo` leen SQLite.

**Limitaciones conocidas de A2 (por diseño).**

- Borrar una ficha JSON no borra la tarea del estado global: el
  identificador queda reservado, la tarea sigue visible marcada como
  "definición no legible" y no puede volver a crearse con el mismo id.
- `verificar` ejecuta las pruebas sobre la raíz indicada, no sobre el
  worktree registrado de la tarea.
- `latido` y `reanudar` siguen siendo órdenes manuales.

(La atomicidad de `tomar` entre procesos era también una limitación de A2;
la resuelve A3.1, en la sección siguiente.)

Prueba correspondiente:

    pruebas/orquestacion/prueba_estado_global.py

### A3.1 — Toma atómica de tareas

**Qué resuelve.** Hasta A2, `tomar` leía el estado, comprobaba si la tarea
estaba libre y escribía, cada paso con una conexión distinta y con un
UPDATE incondicional. Dos trabajadores que competían por la misma tarea
pasaban ambos la comprobación y el segundo pisaba al primero: **doble
propietario**. Era un TOCTOU real, no teórico.

**Cómo se resuelve.** Todo lo que decide la toma ocurre dentro de UNA sola
transacción `BEGIN IMMEDIATE` sobre la base global:

    comprobación de estado  ->  comprobación de ámbitos
    ->  UPDATE condicional  ->  evento  ->  COMMIT

La comprobación de estado que abre la secuencia SÍ deniega: es la que, en
la práctica, rechaza al perdedor de una carrera, porque al leerse la fila
dentro de esta misma transacción ya ve el estado que la toma anterior dejó.
Está ahí para que el motivo sea el verdadero y no "ámbito en conflicto"
cuando el problema es que la tarea está aprobada. Lo que NO hace es
conceder: eso lo sigue decidiendo el UPDATE condicional.

Por qué no puede haber dos ganadores. No es un mecanismo, son tres, y
conviene saber cuál actúa dónde:

1. `BEGIN IMMEDIATE` pide el bloqueo de escritura en el primer instante de
   la transacción. SQLite admite un solo escritor: la segunda toma espera
   (`busy_timeout = 5000 ms`) a que la primera confirme o anule. **Éste es
   el que sostiene la garantía en `tomar`**: cuando la segunda transacción
   por fin entra, lee la fila ya cambiada y se rechaza.
2. El estado esperado viaja en la propia cláusula `WHERE` del UPDATE. El
   motor lo comprueba contra la fila REAL en el momento de escribir, no
   contra una lectura anterior: no queda ventana entre comprobar y escribir.
3. La decisión se toma con `rowcount`, el número de filas que el motor
   modificó de verdad. 1 = ganó; 0 = alguien se adelantó. No se deduce de
   ninguna lectura hecha por Python.

Los mecanismos 2 y 3 son la garantía de `estado_global.reclamar`, el
primitivo, y se ejercitan de lleno en la carrera entre conexiones, que lo
llama directamente. Desde `tomar`, en cambio, el perdedor ya se rechaza en
la comprobación de estado, así que el `rowcount = 0` no llega a ocurrir:
el UPDATE condicional queda de red de seguridad, no de primera línea. Se
dice porque es la verdad medible, y porque quien use `reclamar` por su
cuenta sí depende de los tres.

Además, conceder la toma cambia el estado a uno que ya no es reclamable, de
modo que el propio cambio cierra la puerta al siguiente aspirante.

**Qué se añadió.**

| Dónde | Qué |
|---|---|
| `estado_global.reclamar` | Primitivo de toma atómica: UPDATE condicional resuelto por `rowcount`. Un conflicto NO es excepción, se devuelve descrito |
| `estado_global.rechazo` | Describe por qué no se concede una toma, siempre con el mismo formato, venga del UPDATE o de la comprobación previa |
| `supervisor.ErrorToma` | Rechazo controlado con tarea, motivo, estado, propietario y si la tarea ya era propia. Hereda de `ErrorSupervisor` |
| `supervisor.conflictos_de_ambito` | Pasa a ser función pura, para poder ejecutarse dentro de la transacción de la toma |
| `supervisor.tomar` | Reescrita alrededor de la transacción única |
| `__main__.py` | Código de salida 3 para la toma rechazada |

**Leer el árbol y consultar Git quedan FUERA de la transacción a
propósito**: son esperas de disco, no deciden nada, y sostener el bloqueo de
escritura mientras tanto castigaría a los demás trabajadores. El UPDATE
vuelve a validar lo único que importa.

**Códigos de salida de `tomar`** (la orden de la línea de comandos):

| Código | Significado | Ejemplo |
|---|---|---|
| 0 | Tarea tomada | — |
| 2 | Error del Supervisor | ficha inexistente o inválida, ámbito en conflicto con otra tarea activa, base ilegible |
| 3 | **Toma rechazada** | otro trabajador se adelantó, o el estado de la tarea no admite toma (`propuesto`, `aprobado`, `bloqueado`…) |

El 3 distingue "perdí la carrera" de "el Supervisor está roto". Un
orquestador (n8n, un script) puede pasar a otra tarea ante un 3 y detenerse
ante un 2.

El ámbito en conflicto sale por el 2 a propósito: no es una carrera que se
pueda reintentar, sino una tarea que no se podrá tomar mientras la otra
siga activa.

**Decisión de diseño.** El `WHERE` condiciona sólo por `estado`, no por
`trabajador_id IS NULL`. Añadir esa condición dejaría permanentemente
intomable una fila que estuviera en estado reclamable pero conservara un
propietario residual (ficha V1 importada a medias, edición externa), y la
recuperación automática está fuera del alcance de A3.1. En su lugar la toma
desplaza al residual y lo anota en el evento
(`datos.propietario_desplazado`), para que el cambio sea trazable.

El invariante en el que se apoya esto —que un estado reclamable (`nuevo`,
`reabierto`, `requiere_revision`) no conserva propietario— se cumple para
toda fila que escribe el propio Supervisor, porque `verificar`, `devolver`
y `reanudar` liberan al trabajador. NO se cumple para las que llegan por
importación de una ficha V1 o por una edición externa de la base, y esas
son justamente las que la condición extra habría dejado intomables para
siempre. Medido sobre el uso normal: 43.035 muestras de un vigilante
durante ciclos concurrentes de toma, latido y devolución, sin una sola
violación.

**Hasta dónde llega la garantía.** A3.1 hace atómica LA TOMA. No hace
atómico el resto del ciclo de vida, y conviene tenerlo claro porque la
diferencia importa:

| Situación | ¿Garantizado? |
|---|---|
| Varios trabajadores reclaman la misma tarea a la vez | Sí: gana exactamente uno |
| Dos tareas con ámbitos solapados reclamadas a la vez | Sí: se concede una sola |
| El propietario que ganó sobrevive a las demás órdenes | **No**: ver abajo |

`latido`, `devolver`, `verificar` y las órdenes humanas siguen escribiendo
con `persistir`, cuyo UPDATE es incondicional. Una de esas órdenes que
llegue con una lectura vieja puede sobrescribir al trabajador que acababa
de ganar la toma. Ejemplo medido: A emite un latido; antes de que su
escritura llegue, un humano devuelve la tarea y C la toma legítimamente; la
escritura de A pisa entonces a C y la fila vuelve a decir A. Resolverlo
exige que cada orden exija ser el propietario (UPDATE condicional también
en `persistir`), que es A3.2. No se adelantó aquí para no rehacer las nueve
órdenes del ciclo dentro de una etapa cuyo alcance es la toma.

**Limitaciones conocidas de A3.1 (por diseño).**

- La unicidad de propietario está garantizada para la toma, no para el
  resto del ciclo de vida (ver la tabla anterior). Es la deuda principal
  que hereda A3.2.
- `latido` y `verificar` no comprueban que quien llama sea el propietario
  de la tarea: cualquiera puede latir o verificar una tarea ajena. La
  propiedad efectiva del claim pertenece a A3.2.
- `reanudar` puede arrebatar una tarea a un trabajador vivo si su latido
  vence; la política de expiración pertenece a A3.2.
- No hay latidos automáticos, expiración de trabajadores, detección de
  trabajadores muertos ni recuperación automática de tareas abandonadas.
- El espejo JSON se escribe DESPUÉS del COMMIT, con lo que la transacción
  confirmó. Si otra orden se cruza en esa ventana (de microsegundos), el
  JSON queda desfasado respecto de SQLite hasta la siguiente operación
  sobre esa tarea, y el commit automático de la ficha puede dejar ese
  valor obsoleto en Git. SQLite es la autoridad: `cargar`, la API y el
  tablero leen de ahí, así que nadie decide nada con el JSON atrasado.

  La ventana existe igual en `persistir`, que también escribe el espejo
  después del COMMIT: dos órdenes concurrentes pueden confirmar en un
  orden y escribir el JSON en el contrario. La diferencia es otra. Al
  pisar la base con un UPDATE incondicional, `persistir` deja base y
  espejo de acuerdo en un valor que puede ser el equivocado; `tomar`, que
  no pisa, deja el espejo atrasado respecto de un valor correcto. Preferir
  lo segundo es deliberado. La ventana se cierra sola con A3.2: cuando
  ninguna orden pueda tocar una tarea ajena, nada podrá cruzarse ahí.

  Si el proceso muere justo en esa ventana, el estado operativo está a
  salvo: está comprobado que la toma sobrevive, que la base no queda
  bloqueada y que la orden siguiente pone el JSON al día.
- Crear la base desde cero con varios procesos a la vez sigue fallando en
  los perdedores con "table tareas already exists" (deuda declarada de A2,
  en `inicializar()`). Está comprobado que no produce dos propietarios:
  falla de forma explícita, sin corromper nada. Basta con crear la base
  una vez (`inicializar-estado`) antes de lanzar trabajadores. Comprobado
  a mano con 6 procesos, un solo ganador en 8 de 8 rondas; no hay prueba
  automática que lo cubra, porque corregirlo es A3.2.
- **El refresco de definiciones todavía puede pisar el ámbito de una tarea
  viva, por la puerta de `cargar`.** A3.1 cerró la puerta ancha: `tomar` ya
  no refresca las definiciones de las demás tareas. Pero `cargar`, que es
  de A2, sigue refrescando la definición de la tarea que se pide, sin
  mirar si está en ejecución en manos de otro. Reproducido: con T-0001 en
  ejecución y su ficha declarando otro ámbito en esta rama, basta un
  `tomar T-0001` —que se RECHAZA por estar tomada— para dejarle el ámbito
  encogido; la toma siguiente de otra tarea ya no ve el solapamiento y
  quedan dos escritores sobre el mismo archivo.

  No es nuevo de A3.1: el mismo caso se reproduce igual sobre el código
  anterior (`b5578d2b`). Cerrarlo del todo exige que la definición de una
  tarea que retiene ámbito no se refresque mientras lo retiene, y eso vive
  en `sincronizar_ficha`, que es de A2 y la usan también el bootstrap y la
  orden `sincronizar-definiciones`. Queda como deuda de A3.2.
- En contrapartida de lo anterior, **ampliar el ámbito de una tarea que ya
  está viva no se tiene en cuenta hasta que la tarea deje de estarlo** (o
  hasta que se ejecute `sincronizar-definiciones`). Cambiar el ámbito de
  una tarea en marcha es, justamente, lo que rompe la garantía; el sistema
  prefiere quedarse con el ámbito que la tarea declaraba cuando se tomó.

Prueba correspondiente:

    pruebas/orquestacion/prueba_toma_atomica.py

Admite `--carreras N` para una corrida de estrés; el valor por omisión está
calibrado para el tiempo límite del corredor único.

**Verificación en Windows.** La evidencia de concurrencia registrada en
`ESTADO.md` se obtuvo en Linux. Windows es el entorno final real y hay
cuatro cosas que pueden comportarse distinto: `multiprocessing` sólo tiene
`spawn` (la prueba ya lo fuerza en Linux, así que ejercita el mismo
camino), el bloqueo de SQLite usa otra API del sistema, un archivo abierto
no se puede borrar mientras alguna conexión siga viva, y arrancar procesos
es bastante más lento. Desde `C:\INGENIERIA_LOCAL\motor`, en PowerShell:

    $env:PYTHONPATH = "$PWD;$PWD\nucleo;$PWD\orquestacion"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    # 1. La prueba de la toma atómica, sola y cronometrada.
    Measure-Command { python .\pruebas\orquestacion\prueba_toma_atomica.py } |
        Select-Object TotalSeconds
    python .\pruebas\orquestacion\prueba_toma_atomica.py

    # 2. Corrida de estrés (100 carreras por configuración).
    python .\pruebas\orquestacion\prueba_toma_atomica.py --carreras 100

    # 3. Regresión completa por el corredor único.
    python -m orquestacion.ingenieria_supervisor pruebas --detalle

    # 4. Que no quedaron temporales sin borrar (Windows no borra archivos
    #    abiertos: si aparece alguno, alguna conexión quedó viva).
    Get-ChildItem $env:TEMP -Directory -Filter "toma_atomica_*"

La corrida (1) debe terminar muy por debajo de los 120 s del corredor
único, imprimir `PRUEBA_TOMA_ATOMICA=OK` y reportar 0 dobles tomas.
La (4) no debe devolver nada.

**Qué esperar del cronómetro.** En Linux la corrida por omisión tarda unos
7 s y arranca 22 procesos con `spawn`, a 23-44 ms cada uno (medido). En
Windows crear un proceso es bastante más caro y un antivirus lo empeora,
así que lo razonable es entre 15 y 50 s. Sigue habiendo margen frente a los
120 s, pero es la cifra que hay que mirar primero: si se acercara al
límite, el remedio no es bajar `--carreras` (las carreras casi no cuestan;
lo caro es arrancar los procesos) sino reducir el número de contendientes.

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
- (A2) Estado operativo en la base SQLite global, servido por la misma
  ruta de V1 `GET /api/desarrollo/tareas`; indicador "Base global SQLite"
  en el tablero; órdenes `diagnostico`, `inicializar-estado` y
  `sincronizar-definiciones`.
- (A3.1) Toma atómica de tareas: compitan los trabajadores que compitan por
  la misma tarea, gana exactamente uno. Comprobado con carreras reales
  entre procesos y entre conexiones, y con una prueba de control que pasa
  por el mismo arnés una toma deliberadamente ingenua —con una ventana de
  10 ms entre leer y escribir— y EXIGE dobles tomas: si ni con esa ventana
  colisionara, el arnés estaría roto y el verde de todo lo demás no
  significaría nada. Que el arnés detecta el defecto REAL, sin ventana
  artificial, se comprobó aparte por mutación del código.

Pruebas correspondientes:

    pruebas/orquestacion/prueba_supervisor.py
    pruebas/orquestacion/prueba_estado_global.py
    pruebas/orquestacion/prueba_toma_atomica.py

### Cómo se invoca

Desde la raíz del repositorio:

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

### A3.2/B — pendiente, NO implementado

- Latidos automáticos.
- Expiración de trabajadores y detección avanzada de huérfanos.
- Recuperación automática de tareas abandonadas.
- Propiedad efectiva del claim: que `latido` y `verificar` exijan ser el
  propietario de la tarea.
- `verificar()` ejecutando dentro del worktree de la tarea.
- Lanzamiento de trabajadores (Claude) y varios trabajadores simultáneos.
- Creación y destrucción automática de worktrees de Git.
- Cola automática de tareas.

A2 y A3.1 dejan la base para todo eso (una sola fuente operativa compartida
por los worktrees, transacciones, `busy_timeout` y una toma que no admite
dos ganadores), pero no lo adelantan.

El campo `worktree` de la tarea ya existe en SQLite y hoy permanece vacío:
está reservado para el paralelismo. La detección de solapamiento de
ámbitos ya está implementada y probada, porque es el requisito previo para
poder trabajar en paralelo sin corromper nada.

### Queda para V2

- Acciones desde el tablero web: hoy es de sólo lectura.
- Aprobación y rechazo desde la interfaz gráfica.
- Priorización automática entre tareas pendientes.
- Notificaciones.

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
