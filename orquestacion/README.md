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

**Esto quedó RESUELTO EN A3.2**, que es la sección siguiente. Lo que se
describe arriba es cómo estaba el sistema al cerrar A3.1.

**Limitaciones conocidas de A3.1 (por diseño).**

- La unicidad de propietario está garantizada para la toma, no para el
  resto del ciclo de vida (ver la tabla anterior). Es la deuda principal
  que hereda A3.2. **RESUELTA EN A3.2**: ver la sección siguiente.
- `latido` y `verificar` no comprueban que quien llama sea el propietario
  de la tarea: cualquiera puede latir o verificar una tarea ajena. La
  propiedad efectiva del claim pertenece a A3.2. **RESUELTO EN A3.2**.
- `reanudar` puede arrebatar una tarea a un trabajador vivo si su latido
  vence. **PARCIALMENTE RESUELTO EN A3.2**: ya no puede arrebatársela a
  quien la tomó entre su lectura y su escritura. La política de expiración
  temporal sigue pendiente, y pasa a A3.3.
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
  en `inicializar()`). **RESUELTO EN A3.2**, con prueba automática de 6
  procesos por ronda. A3.2 encontró además un segundo modo de fallo que
  aquí no estaba declarado: la conversión inicial a WAL.
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
  anterior (`b5578d2b`). **RESUELTO EN A3.2** con una guarda en
  `sincronizar_ficha`, que es el punto único donde se escribe
  `ambito_archivos`. A3.2 comprobó además que el agujero era más ancho de
  lo descrito: bastaba una orden de sólo lectura, o cambiar sólo el
  título.
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
es bastante más lento.

Lo que sí se pudo descartar aquí: la prueba pasa con Python 3.11, 3.12 y
3.13, así que la rama de limpieza propia de 3.12 (`shutil.rmtree` con
`onexc`, que es la que usará esta PC) está ejercitada y no es una
incógnita. Tampoco quedan recursos sin cerrar: la corrida en modo
desarrollo (`python -X dev -W error::ResourceWarning`) no emite ni un
aviso, y un detector que instrumenta `sqlite3.connect` da cero conexiones
vivas al terminar las 23 comprobaciones. Es la condición que en Windows
decide si los temporales se pueden borrar.

Desde `C:\INGENIERIA_LOCAL\motor`, en PowerShell:

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

    # 4. Que no quedaron temporales ni procesos huérfanos (Windows no borra
    #    archivos abiertos: si aparece alguno, algo quedó vivo).
    Get-ChildItem $env:TEMP -Directory -Filter "toma_atomica_*"
    Get-Process git, python -ErrorAction SilentlyContinue |
        Where-Object { $_.StartTime -gt (Get-Date).AddMinutes(-5) }

PowerShell no se detiene cuando una orden nativa falla, así que después de
cada ejecución hay que mirar el código de salida; si no, una prueba que
muere con un error se ve igual que una que pasó:

    if ($LASTEXITCODE -ne 0) {
        Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red
    }

Criterio para decidir que Windows pasó, los cuatro a la vez:

1. La corrida (1) imprime `PRUEBA_TOMA_ATOMICA=OK`, sale con código 0 y
   reporta 0 dobles tomas.
2. Tarda claramente por debajo de los 120 s (ver el apartado anterior).
3. La (3) da 6 de 6 archivos de prueba en OK.
4. La (4) no devuelve nada: ni temporales ni procesos huérfanos.

**Qué esperar del cronómetro, y qué mirar si se agota.** Ésta es la parte
con riesgo real, y conviene contarla con las cifras medidas, no con la
intuición.

En Linux la corrida por omisión tarda unos 7 s. Los 22 procesos que arranca
`multiprocessing` no son el coste dominante: lo es **Git**. Cada operación
del Supervisor resuelve la ubicación de la base con
`git rev-parse --git-common-dir`, así que la tanda completa lanza unas 550
invocaciones de `git` sólo en el proceso padre (455 de ellas son ese mismo
`rev-parse`), más las de los procesos hijos. En Linux cada una cuesta unos
2 ms y no se nota. En Windows, arrancar `git.exe` con Defender vigilando la
carpeta del repositorio cuesta entre 50 y 250 ms, de modo que el
presupuesto pasa a medirse en decenas de segundos y **el límite de 120 s
del corredor único entra genuinamente en juego**.

Por eso el paso 1 de la comprobación es cronometrar. Si la corrida se
acerca al límite:

- Bajar `--carreras` ayuda, pero sólo hasta cierto punto: cada unidad de
  `--carreras` cuesta unas 38 invocaciones de `git` sobre un suelo fijo de
  unas 525 que no se toca reduciéndolas.
- El remedio que de verdad lo resuelve es memorizar el resultado de
  `git rev-parse --git-common-dir` por raíz dentro de
  `estado_global.git_common_dir`. **APLICADO EN A3.2**, porque su batería
  es la que acercó la corrida al límite. Medido después: este archivo pasa
  de unas 455 invocaciones a 23 en el proceso padre.

### A3.2 — Propiedad efectiva durante el ciclo

A3.1 garantizaba que una tarea sólo pudiera ser TOMADA por un trabajador.
A3.2 garantiza lo siguiente: que después de la toma, sólo el propietario
VIGENTE pueda modificar el estado operativo de esa ejecución.

**Qué es una orden rezagada.** No es una llamada nueva que relee la base:
la que relee ve al dueño actual y, si se acreditara con eso, lo estaría
suplantando. Es la orden COMPUESTA contra una lectura anterior y que llega
después, llevando la identidad y la generación que tenía entonces. Ese es
el caso que A3.2 cierra.

**El identificador de propiedad: `tareas.generacion`.**

Un entero por tarea. Sólo lo incrementa `reclamar`, con
`generacion = generacion + 1` dentro del mismo UPDATE condicional que
concede la toma, resuelto por el motor y no en Python: dos tomas que
partieran del mismo valor leído no pueden repetir número. La pareja
`(trabajador_id, generacion)` identifica una EJECUCIÓN, no un trabajador.

Por qué un contador y no otra cosa:

- `trabajador_id` solo no distingue una ejecución vieja de una nueva del
  MISMO trabajador. Ése es el problema ABA, y es el caso que un control
  por identidad deja pasar.
- El reloj no sirve: `iniciado_en` está truncado a segundos, así que dos
  tomas del mismo trabajador en el mismo segundo dejaban idénticas las
  cinco columnas de propiedad.
- El PID no sirve: el sistema los reutiliza, y la línea de órdenes
  registra el de un mandato que muere en el acto.
- No hace falta criptografía: la base es local y de una sola PC. Un
  entero es lo más simple que funciona, y es determinista y comprobable.

El testigo **no sale de SQLite**: no se serializa al JSON ni se lee de él, y
`fila_desde_ficha` graba siempre 0 al importar. El JSON es un archivo del
árbol de trabajo que cualquiera edita, y un testigo que el vigilado puede
escribir no vigila nada: con él se podía fijar o hacer retroceder la
generación y volver a hacer indistinguibles dos ejecuciones. `reclamar` es
la única que la mueve, y sólo sumando uno.

**Escrituras condicionadas.** `estado_global.actualizar_si_propietario`
lleva la precondición en el WHERE del UPDATE y decide por `rowcount`, sin
comprobación previa en Python. `supervisor.persistir` la usa siempre:

| Precondición | Cuándo se exige | Qué distingue |
|---|---|---|
| `generacion = ?` | SIEMPRE | dos EJECUCIONES de la misma tarea |
| `trabajador_id = ?` | En `latido`, `devolver` y `verificar` | dos TRABAJADORES |
| `estado IN (...)` | SIEMPRE, con el estado que se leyó | dos MOMENTOS del ciclo |

Las tres hacen falta y ninguna sobra:

- La generación sola no basta. Las transiciones NO la mueven, así que dos
  órdenes separadas por varias de ellas llevan el mismo testigo. Comprobado:
  una orden humana lenta revertía a PROPUESTO una tarea que entretanto había
  quedado BLOQUEADA, saltándose además la máquina de estados porque
  `transicionar` validó contra su foto vieja.
- La identidad sola no basta: es justamente el problema ABA.
- El estado solo no basta: no distingue quién ordena.

La generación se exige también en las órdenes humanas, que no tienen
propietario pero tampoco deben pisar una ejecución que empezó mientras su
emisor decidía.

**Cómo se acredita quien ordena.** Dos formas, y la diferencia importa:

- DECLARADA: `--trabajador` y `--generacion`, los valores que imprimió
  `tomar`. Es la única forma que detiene de verdad a una orden rezagada,
  porque la orden vieja lleva SU credencial, no la que haya ahora.
- IMPLÍCITA: sin argumentos, se toma la de la ficha recién leída. Es lo
  que hacía V1 y se conserva por compatibilidad. Protege del cambio de
  propiedad ENTRE la lectura y la escritura, que no es poco, pero no de
  un emisor que ya había perdido la tarea antes de leer.

**Qué pasa en un rechazo.** Se lanza `ErrorPropiedad` DENTRO de la
transacción, así que el ROLLBACK deshace todo: no se escribe el estado, no
se escriben los eventos pendientes (siguen en la ficha, sin consumirse), no
se gasta un intento, no se regenera el espejo JSON y `actualizado_en`
vuelve a su valor anterior. Un rechazo no deja rastro de haber pasado.

En la línea de órdenes, el rechazo por propiedad sale con **código 4**,
atendido en `principal` para que valga en todas las órdenes. Los demás
códigos siguen significando lo mismo: 0 éxito, 1 resultado no deseado,
2 error controlado (y también error de uso de argparse), 3 toma rechazada.

**Ámbito de una tarea viva.** La regla de un solo escritor se comprueba
contra el ámbito GRABADO, y `sincronizar_ficha` lo reescribía desde el JSON
sin mirar el estado operativo. Como `cargar` llama ahí y encabeza casi
todas las órdenes, bastaba editar el JSON y ejecutar una orden de SÓLO
LECTURA —o una toma que terminara RECHAZADA, o cambiar únicamente el
título— para estrechar el ámbito de una tarea ya tomada; la toma siguiente
no veía el solapamiento y quedaban dos escritores sobre el mismo archivo.

Ahora, mientras el estado de la tarea retenga su ámbito
(`en_ejecucion`, `requiere_revision`, `propuesto`), un cambio de ámbito no
se aplica y la huella de definición NO avanza. Eso último es lo que impide
que el refresco se pierda en silencio: vuelve a intentarse solo en cuanto
la tarea deja de estar viva. Se congela por un segundo motivo, de la misma clase: que DESAPAREZCA del
JSON una decisión humana pendiente. Una decisión pendiente frena la tarea
—`verificar` no puede llevarla a PROPUESTO mientras quede alguna— y
borrarla del archivo quitaba el freno; comprobado, bastaba un `ver`
después de editar. Añadir decisiones nuevas sí se permite: añade frenos,
no los quita.

Un cambio declarativo inocuo —el título, la descripción de una decisión,
los criterios— sigue sincronizándose con normalidad: sólo se frena lo que
rompería una garantía que la tarea tenía cuando se tomó.

Tres detalles que costaron una ronda de auditoría cada uno:

- Hay DOS sitios que escriben `ambito_archivos`, no uno, y conviene
  decirlo porque el argumento de seguridad depende de ello:
  `sincronizar_ficha`, que es donde vive la guarda, y `reclamar`, por la
  toma. El segundo no pasa por la guarda y no debe: la toma es el momento
  en que el ámbito se valida contra todas las demás tareas dentro de la
  misma transacción, así que ahí escribir es lo correcto. Cualquier tercer
  escritor que aparezca sí tendría que pasar por la guarda.
- El ámbito se compara por CONTENIDO, no por orden. `solapamientos` recorre
  el producto cartesiano, así que `['a','b']` y `['b','a']` garantizan lo
  mismo; comparar las listas tal cual congelaba toda la definición al
  reordenar un patrón.
- La TOMA valida y graba la UNIÓN del ámbito declarado con el que la tarea
  ya retenía. `requiere_revision` es el único estado que está a la vez en
  ESTADOS_TOMABLES y en ESTADOS_QUE_RETIENEN_AMBITO, así que una tarea
  podía tener el ámbito congelado y ser tomable al mismo tiempo, y ahí
  fallaba por los dos lados:

  - grabando sólo lo viejo, la toma concedía la propiedad sobre el ámbito
    declarado mientras la fila guardaba otro, y la siguiente toma
    comprobaba el solapamiento contra un ámbito que ya no usaba nadie;
  - grabando sólo lo declarado, una retoma con el ámbito ENCOGIDO soltaba
    el terreno que la retención protegía —la tarea conserva cambios sin
    confirmar sobre esos archivos— y otra tarea podía entrar en él.

  La unión resuelve los dos: ampliar se permite, porque lo nuevo se valida
  ahí mismo contra las demás; encoger no libera nada mientras la retención
  siga en pie, y se aplicará solo cuando la tarea deje de retener.
- La ruta de sólo lectura no pide el bloqueo de escritura. Como la huella
  no avanza a propósito, `necesita_sincronizacion` dice que sí para
  siempre; sin un atajo, cada `cargar` —incluido el de un `ver`— abriría un
  BEGIN IMMEDIATE para no escribir nada, y bajo concurrencia eso convierte
  una consulta en "database is locked".

  Ese atajo DEVUELVE el informe congelado y no delega en
  `sincronizar_ficha`. Delegar fue una regresión de esta misma etapa: esa
  función vuelve a leer la fila y a decidir por su cuenta, y como ahí ya no
  hay transacción, si la tarea dejaba de estar viva entre las dos lecturas
  acababa ejecutando su UPDATE y su evento EN AUTOCOMMIT. La salida que
  existe para no escribir podía escribir, y sin candado.

Y el resultado se informa: un ámbito congelado NO se cuenta como "sin
cambios", que le diría al usuario justo lo contrario de lo que pasó.

**Declarado y vigente son dos cosas distintas.** La ficha lleva los dos:

- `ambito_archivos` es lo que el JSON DECLARA. Se conserva tal cual y se
  aplicará cuando la tarea deje de estar viva.
- `ambito_vigente` es lo que la base CONCEDIÓ. Es lo único que cuenta para
  la regla de un solo escritor, y `ver` avisa cuando difieren.

Hacía falta separarlos porque la guarda estaba a medias: la base se negaba
a grabar el ámbito nuevo de una tarea viva, pero `cargar` seguía
devolviendo el del JSON, así que el trabajador creía poseer archivos que
nadie le había concedido. Reproducido: se ensanchaba el ámbito de la tarea
viva, otra tarea tomaba legítimamente la parte nueva, y quedaban dos
escritores sobre el mismo archivo.

Y hacía falta que fueran DOS campos, no uno pisando al otro: pisar el
declarado con el vigente hacía que `persistir`, al regenerar el espejo,
borrara del JSON la declaración que una persona acababa de escribir. El
cambio no quedaba en espera, desaparecía. Se comprobó rompiéndolo.

**Bootstrap concurrente.** Dos carreras, las dos reproducidas y las dos
corregidas:

- `inicializar` leía la versión de esquema en autocommit y migraba
  después, así que el perdedor ejecutaba `CREATE TABLE tareas` sobre una
  base que ya la tenía y moría con "table tareas already exists". Ahora la
  relee DENTRO de la transacción, con el bloqueo de escritura ya tomado.
  Añadir `IF NOT EXISTS` no bastaba: sólo desplazaba el error al INSERT
  contra la clave primaria de `esquema`.
- La conversión inicial `delete` -> `wal` es el único momento en que abrir
  la base necesita un bloqueo exclusivo. Ahora no se pide el cambio si la
  base ya está en WAL: a partir de la segunda apertura ningún proceso
  compite por un bloqueo que no necesita. Se comprueba contando las
  sentencias que llegan al motor, no midiendo tiempos.

  Y si hay que convertir, se reintenta de forma acotada EN TIEMPO, no sólo
  en número de intentos: durante la conversión se baja el temporizador de
  ocupado a 250 ms, porque el pragma sí lo respeta y con los 5 s normales
  el peor caso del bucle subía a unos 41 s. Con eso baja a unos 3 s, y no
  se pierde nada: el choque que hay que absorber aquí es inmediato, y el
  resto de operaciones conservan su temporizador completo.

  Los dos motivos por los que WAL puede no activarse se diagnostican
  distinto, que antes no era así: si el motor nunca se quejó de bloqueo no
  hay contención ninguna y lo que pasa es que el sistema de archivos no
  admite WAL. El mensaje de la unidad de red vivía en una rama inalcanzable
  de `abrir`; ahora sale de donde puede saberse. Esto costó una
  vuelta que merece quedar escrita. Primero se midió que la conversión SÍ
  respeta el `busy_timeout` —con un lector abierto esperó los 5,007 s
  completos antes de rendirse— y de ahí se concluyó que el reintento sobraba
  y se retiró. La corrida completa del corredor lo desmintió en el acto:
  con 6 procesos saliendo a la vez contra una base que no existe, 1 de 6
  murió con "database is locked" SIN esperar nada.

  Las dos observaciones son ciertas y no se contradicen: el temporizador
  cubre el conflicto con un LECTOR, pero no el choque entre varios que
  intentan CONVERTIR a la vez. Por eso el reintento es de pocos intentos y
  siestas cortas: el fallo que absorbe es inmediato, no una espera larga.

**Rendimiento.** Se aplicó la mitigación que A3.1 dejó medida y anotada:
`git_common_dir` memoriza su resultado por raíz y por proceso. Medido, en
el proceso padre: `prueba_propiedad_ciclo.py` pasa de 355 a 21
invocaciones de `git rev-parse --git-common-dir`, y `prueba_toma_atomica.py`
de unas 455 a 23 — una por repositorio temporal, el mínimo posible. No es
una caché global ni persistente, y antes de devolver lo memorizado
comprueba que el `.git` de esa raíz SIGA declarando ese mismo directorio
común: si es un directorio, el común es él; si es un archivo —un worktree
enlazado— se lee su `gitdir:`. Es una comprobación estructural, de un stat
y como mucho la lectura de un archivo de pocos bytes, no una llamada a Git.

Se intentó antes comparando metadatos y las dos variantes fallaron, cada
una a su manera, y merece quedar escrito: con mtime la memoria se
invalidaba casi en cada llamada —el mtime de `.git` cambia cada vez que se
escribe dentro, la propia base incluida— y dejaba de ahorrar nada; con el
inodo, el sistema de archivos los REUTILIZA, así que al borrar el `.git` de
un worktree y hacer `git init` en su lugar el directorio nuevo recibía el
mismo número y la memoria daba por bueno el común del repositorio anterior.
Eso último está reproducido bajo carga en la comprobación 17, que es la que
lo encontró.

**Lo que NO cierra A3.2, dicho con precisión.** `persistir` reescribe las
dieciséis columnas operativas con la foto que `cargar` leyó. Las tres
precondiciones deciden QUIÉN escribe y DESDE QUÉ momento, no QUÉ contenía
cada columna: dos órdenes que compartan generación, identidad y estado
—por ejemplo dos latidos del mismo propietario— siguen pudiendo pisarse
campo a campo. Cerrarlo exige que cada orden escriba sólo lo suyo, que es
un cambio en las nueve y pertenece a A3.3.

**Limitaciones conocidas de A3.2 (por diseño).**

- La credencial IMPLÍCITA sigue disponible, y un emisor que pueda quedarse
  rezagado debe declarar la suya. La prueba 5 de la batería lo demuestra
  en vez de esconderlo: con sólo el nombre, una orden del mismo trabajador
  entra; con la generación declarada, cae.
- Las órdenes humanas (`decidir`, `aprobar`, `rechazar`, `reabrir`,
  `bloquear`) exigen generación pero no identidad: no tienen propietario
  que acreditar. Eso es correcto para una persona, y significa que una
  persona puede intervenir una tarea viva a propósito.
- `reanudar` sigue juzgando por PID y latido. Ahora no puede arrebatarle
  la tarea a quien la tomó entre su lectura y su escritura (la anota en
  `reclamadas_mientras_tanto`), pero la política de expiración temporal
  sigue siendo de A3.3.
- La ventana del espejo JSON descrita en A3.1 se estrecha mucho —ninguna
  orden ajena puede ya cruzarse—, pero sigue existiendo entre dos órdenes
  legítimas del mismo propietario.
- `verificar` sigue ejecutando las pruebas sobre `raiz` y no sobre el
  worktree de la tarea. Eso es A3.3.

**Verificación en Windows (PENDIENTE DE EJECUTAR).** La evidencia de arriba
se obtuvo en Linux. Windows es el entorno final real y lo que puede
comportarse distinto es lo mismo que en A3.1: `multiprocessing` sólo tiene
`spawn` (la prueba ya lo fuerza, así que ejercita el mismo camino), el
bloqueo de SQLite usa otra API del sistema, un archivo abierto no se puede
borrar mientras alguna conexión siga viva, y arrancar procesos es más lento.

Lo que sí se pudo descartar aquí, que es justo la condición que en Windows
decide si los temporales se pueden borrar: la corrida con
`python -X dev -W error::ResourceWarning` sale con código 0 sin emitir un
solo aviso, y un detector que instrumenta `sqlite3.connect` cuenta 352
conexiones abiertas durante la tanda y **0 vivas al terminar**.

Sobre la conversión a WAL: el modo de fallo SÍ existe y se reprodujo en
Linux, dentro de la corrida completa del corredor (1 de 6 procesos). El
reintento que lo absorbe está puesto y verificado ahí. Lo que la prueba
automática comprueba de forma determinista es la otra mitad —que una base
ya en WAL no se reconvierte—, porque el choque en sí depende de la carga
de la máquina y un gate no puede depender de eso.

Sobre el cronómetro: el riesgo que A3.1 dejó anotado ya no aplica igual,
porque A3.2 memoriza `git_common_dir`. Este archivo hace 105 invocaciones
de `git` en el proceso padre, 21 de ellas de `rev-parse`. Aun en el peor
caso medido en esa PC (250 ms por invocación de `git`), eso son unos 26 s
de un límite de 120 s, y el resto de la corrida es SQLite local.

Desde `C:\INGENIERIA_LOCAL\motor`, en PowerShell 7:

    $env:PYTHONPATH = "$PWD;$PWD\nucleo;$PWD\orquestacion"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    # 1. La batería de propiedad, sola y cronometrada.
    Measure-Command { python .\pruebas\orquestacion\prueba_propiedad_ciclo.py } |
        Select-Object TotalSeconds
    python .\pruebas\orquestacion\prueba_propiedad_ciclo.py
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 2. Corrida de estrés ampliada.
    python .\pruebas\orquestacion\prueba_propiedad_ciclo.py --rezagadas 400 --emisores 10 --ordenes 60
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 3. Recursos sin cerrar (lo que decide si Windows puede borrar).
    python -X dev -W error::ResourceWarning .\pruebas\orquestacion\prueba_propiedad_ciclo.py
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 4. Regresión completa por el corredor único (incluye A3.1).
    python -m orquestacion.ingenieria_supervisor pruebas --detalle
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 5. El código de salida 4, comprobado a mano de punta a punta.
    python -m orquestacion.ingenieria_supervisor tomar T-9001 --trabajador W1
    #    (anotar la generación que imprime; suponiendo que sea 1)
    python -m orquestacion.ingenieria_supervisor devolver T-9001 --trabajador W1 --generacion 1
    python -m orquestacion.ingenieria_supervisor tomar T-9001 --trabajador W2
    python -m orquestacion.ingenieria_supervisor latido T-9001 --trabajador W1 --generacion 1
    Write-Host "Codigo esperado 4, obtenido: $LASTEXITCODE"

    # 6. Que no quedaron temporales ni procesos huérfanos.
    Get-ChildItem $env:TEMP -Directory -Filter "propiedad_ciclo_*"
    Get-ChildItem $env:TEMP -Directory -Filter "bootstrap_*"
    Get-ChildItem $env:TEMP -Directory -Filter "estres_propiedad_*"
    Get-Process git, python -ErrorAction SilentlyContinue |
        Where-Object { $_.StartTime -gt (Get-Date).AddMinutes(-5) }

El paso 5 usa una tarea de usar y tirar (`T-9001`); créala antes con
`crear` y bórrala después. **No se debe ejecutar sobre T-0001 ni T-0002**,
que tienen que seguir en estado NUEVA y sin ejecutar.

Criterio para decidir que Windows pasó, los seis a la vez:

1. La corrida (1) imprime `PRUEBA_PROPIEDAD_CICLO=OK`, sale con código 0 y
   reporta `ESCRITURAS_INDEBIDAS = 0`.
2. Tarda claramente por debajo de los 120 s.
3. La (2) reporta 0 aceptadas, 0 errores SQLite y 0 excepciones.
4. La (3) sale con código 0, sin avisos.
5. La (4) da 7 de 7 archivos de prueba en OK.
6. La (5) devuelve exactamente 4, y la (6) no devuelve nada.

Prueba correspondiente:

    pruebas/orquestacion/prueba_propiedad_ciclo.py

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
- (A3.2) Propiedad efectiva durante el ciclo: una orden emitida por un
  propietario anterior no entra, ni siquiera si es el mismo trabajador en
  una ejecución posterior. El ámbito de una tarea viva ya no se puede
  cambiar por la puerta de `cargar`. El arranque concurrente de la base ya
  no falla. Comprobado con órdenes rezagadas reales, con estrés
  concurrente entre procesos y con cinco mutaciones del código que la
  batería detecta.

Pruebas correspondientes:

    pruebas/orquestacion/prueba_supervisor.py
    pruebas/orquestacion/prueba_estado_global.py
    pruebas/orquestacion/prueba_toma_atomica.py
    pruebas/orquestacion/prueba_propiedad_ciclo.py

### Cómo se invoca

Desde la raíz del repositorio:

    python -m orquestacion.ingenieria_supervisor estado
    python -m orquestacion.ingenieria_supervisor diagnostico
    python -m orquestacion.ingenieria_supervisor inicializar-estado
    python -m orquestacion.ingenieria_supervisor sincronizar-definiciones
    python -m orquestacion.ingenieria_supervisor ver T-0001
    python -m orquestacion.ingenieria_supervisor pruebas --detalle
    python -m orquestacion.ingenieria_supervisor tomar T-0001
    python -m orquestacion.ingenieria_supervisor latido T-0001 --trabajador W --generacion 3
    python -m orquestacion.ingenieria_supervisor devolver T-0001 --trabajador W --generacion 3
    python -m orquestacion.ingenieria_supervisor verificar T-0001 --trabajador W --generacion 3
    python -m orquestacion.ingenieria_supervisor reanudar
    python -m orquestacion.ingenieria_supervisor aprobar T-0001
    python -m orquestacion.ingenieria_supervisor rechazar T-0001 --motivo "..."

---

## QUÉ NO EXISTE TODAVÍA

### A3.3 — pendiente, NO implementado

- Latidos automáticos.
- Expiración temporal de trabajadores y detección avanzada de huérfanos.
- Recuperación automática de tareas abandonadas.
- `verificar()` ejecutando dentro del worktree de la tarea.

### C — pendiente, NO implementado

- Lanzamiento de trabajadores (Claude) y varios trabajadores simultáneos.
- Creación y destrucción automática de worktrees de Git.
- Cola automática de tareas y priorización.

A2, A3.1 y A3.2 dejan la base para todo eso (una sola fuente operativa
compartida por los worktrees, transacciones, `busy_timeout`, una toma que
no admite dos ganadores y una propiedad que sobrevive a todo el ciclo),
pero no lo adelantan.

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
