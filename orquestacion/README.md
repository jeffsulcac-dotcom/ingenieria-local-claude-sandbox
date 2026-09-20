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
| `estado_global.py` | Base SQLite global: ubicación, esquema, transacciones, bootstrap, diagnóstico (A2), toma atómica (A3.1) y cola persistente de trabajadores (migración 3, T-0003) |
| `trabajadores.py` | Cola, despacho, worktrees automáticos, limpieza, reconciliación y cuerpo del proceso trabajador (T-0003) |
| `trabajador.py` | El proceso trabajador: argv estructurado, señales, códigos de salida (T-0003) |
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
Desde A3.3 también `crear` escribe el JSON después del COMMIT (puede
faltar la ficha, nunca sobrar). Al cargar una
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
- `verificar` ejecutaba las pruebas sobre la raíz indicada, no sobre el
  árbol de la ejecución (resuelto en A3.3).
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
| `trabajador_id = ?` | En `latido`, `devolver`, `verificar`, `adoptar` y `bloquear` con credencial (T-0003) | dos TRABAJADORES |
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
  persona puede intervenir una tarea viva a propósito. Desde T-0003,
  `bloquear` acepta además una credencial declarada: es como la emite el
  trabajador cuando el trabajo escribió fuera de su ámbito.
- `reanudar` sigue juzgando por PID y latido. Ahora no puede arrebatarle
  la tarea a quien la tomó entre su lectura y su escritura (la anota en
  `reclamadas_mientras_tanto`), pero la política de expiración temporal
  sigue siendo de A3.3.
- La ventana del espejo JSON descrita en A3.1 se estrecha mucho —ninguna
  orden ajena puede ya cruzarse—, pero sigue existiendo entre dos órdenes
  legítimas del mismo propietario.
- `verificar` sigue ejecutando las pruebas sobre `raiz` y no sobre el
  worktree de la tarea. Eso es A3.3.

**Verificación en Windows (cubierta por el gate de A3.3, ejecutado el
19/09/2026; resultados en `ESTADO.md`).** La evidencia de arriba se obtuvo
en Linux. Windows es el entorno final real y lo que puede comportarse
distinto es lo mismo que en A3.1: `multiprocessing` sólo tiene
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

### A3.3 — Ejecución segura, recuperación y worktrees

A3.1 cerró la toma. A3.2 cerró la propiedad durante todo el ciclo. A3.3
cierra lo que faltaba para que varios trabajadores puedan ejecutar de
verdad al mismo tiempo: que cada orden escriba sólo lo suyo, que una
ejecución larga dé señales de vida, que se sepa distinguir una ejecución
viva de una muerta sin equivocarse, que se pueda recuperar lo
interrumpido sin hacer daño, y que las pruebas corran donde dicen que
corren.

Dos rondas de auditoría adversarial —seis y tres revisores de sólo
lectura— confirmaron sesenta hallazgos en la primera sobre la primera
implementación, y los críticos y altos se cerraron; una tercera ronda, la
revisión final del 20/09/2026, cerró los que se describen más abajo. Lo
que sigue describe el estado DESPUÉS de cerrarlos; donde el defecto
explica la decisión, se dice cuál era.

**1. Cada orden escribe sólo sus campos.**

Hasta A3.2, cualquier orden reescribía las dieciséis columnas operativas con
la foto que tenía en memoria. Con la precondición del WHERE puesta, una
orden ajena ya no podía colarse, pero dos órdenes LEGÍTIMAS del mismo
propietario seguían pisándose: la segunda devolvía a su valor viejo todo
lo que la primera había cambiado y que ella no sabía.

Ahora `persistir` EXIGE `campos_propios` y el UPDATE lleva sólo esas
columnas. Es obligatorio a propósito: el valor por omisión era escribirlo
todo, es decir, el defecto esperando a la siguiente orden que alguien
añadiera por descuido.

| Orden | Columnas que escribe |
|---|---|
| `latido` | `ultimo_latido` |
| `devolver` | `estado` + liberación |
| `verificar` | `estado`, liberación, `ultima_falla`, `ultima_verificacion`, `ejecuciones`, y `intentos` como incremento cuando consume intento |
| `decidir` | `decisiones`, `requiere_decision_humana` |
| `reabrir` | `estado`, liberación, `intentos` |
| `reanudar` | `estado`, liberación, `ejecuciones`, `ultima_falla` |
| `adoptar` (T-0003) | `pid`, `ultimo_latido` |
| `bloquear` (T-0003, con o sin credencial) | `estado` + liberación |

La «liberación» es `trabajador_id`, `pid`, `iniciado_en`, `ultimo_latido`
y `worktree`. `actualizado_en` se añade siempre, porque toda escritura lo
es.

**Los contadores los cuenta el motor.** `intentos` no se lee en Python
para volver a escribirlo: se pasa como incremento y el UPDATE emite
`intentos = intentos + 1`. Leer-sumar-escribir desde dos procesos pierde
cuentas aunque las precondiciones estén bien puestas.

**Y tampoco dentro de una columna.** `decisiones` es UNA columna con el
JSON de todas las decisiones dentro, así que repartir columnas no bastaba:
dos `decidir` sobre claves DISTINTAS son órdenes válidas —misma
generación, mismo estado, sin propietario que exigir— y la segunda
revertía a la primera sin error y sin rastro. Medido: 24 de 25 carreras
entre dos procesos perdían una resolución humana. Ahora la resolución la
hace el motor, leyendo la fila dentro de la misma transacción.

**2. Latido automático (`LatidoAutomatico`).**

A3.2 hizo segura la orden `latido`; alguien tenía que emitirla. Sin ella,
una verificación de diez minutos deja la tarea sin señal todo ese rato y
la recuperación la ve caducada: el trabajo honesto parece abandono.

No es un demonio de trabajadores. Es un acompañante de UNA operación
concreta, que empieza y termina con ella (es un gestor de contexto, así
que `__exit__` corre también cuando el trabajo lanza). Escribe sólo
`ultimo_latido`, con las tres precondiciones de A3.2 —identidad,
generación y estado `EN_EJECUCION`— y una cuarta propia: la marca no
puede RETROCEDER.

Las cuatro hacen falta:

- La **generación** distingue el turno viejo del nuevo cuando es el MISMO
  trabajador quien devuelve la tarea y la vuelve a tomar. Un latido
  rezagado del turno anterior certificaría como viva una ejecución que
  nadie está haciendo, y el reloj de abandono no vencería nunca.
- La **no regresión** existe porque el reloj de pared no es monótono.
  `Event.wait` sí usa reloj monótono, pero el valor ESCRITO es hora de
  pared: un salto hacia atrás —NTP, cambio de zona, una máquina virtual
  restaurada— hacía que el propio latido redujera la antigüedad de la
  señal hasta que la tarea parecía huérfana. El mecanismo que la defiende
  se convertía en el que la mata. El instante se toma además con el
  candado ya pedido, no antes: bajo contención se grababa una marca ya
  rancia. La revisión final encontró que el código leía el reloj ANTES
  de pedir el candado, al revés de lo que decía este párrafo y el
  comentario del propio código; está corregido —también en la toma, cuyo
  `momento` es el primer latido de la ejecución— y la comprobación 33 lee
  el orden real con el trazador de SQLite: BEGIN, reloj, UPDATE.

Un fallo de escritura no lo apaga. `database is locked` es transitorio y
esperable justo en este escenario, y rendirse al primero dejaba la
operación sin señal el resto del tiempo, en silencio: se reintenta hasta
cinco fallos seguidos, y si se rinde queda anotado y `verificar` lo dice.
Tampoco se puede reutilizar la instancia (daba cero latidos sin ninguna
señal de avería), ni construirla con intervalo cero (bucle apretado contra
la base compartida), y si el cierre no cabe en el plazo queda constancia
en `cierre_incompleto`.

**3. Vitalidad: seis estados, nunca una sola señal.**

El estado dice en qué punto del ciclo está la tarea. La vitalidad dice si
alguien la está ejecutando ahora. Son cosas distintas, y una tarea puede
quedarse en `EN_EJECUCION` para siempre porque el proceso que la tomó
murió.

    ACTIVA          hay una ejecución y da señales
    LATIDO_VENCIDO  hay ejecución, el latido caducó, pero NO está
                    demostrada muerta
    HUERFANA        hay una ejecución y está demostrada perdida
    REANUDABLE      no hay ejecución y la tarea se puede tomar
    ESPERA_HUMANA   no hay ejecución y la tarea espera a una persona
                    (PROPUESTO, BLOQUEADO)
    FINALIZADA      no hay ejecución y la tarea está cerrada

Nunca se juzga por el PID a secas ni por el latido a secas, y **ningún
umbral decide solo**:

- Un latido vencido (más de `LATIDO_MAXIMO_S = 900` s) no basta. Hace
  falta una segunda señal: el proceso confirmado muerto.
- Un **proceso vivo y comprobable impide declarar abandono**, dure lo que
  dure el silencio. Antes el umbral de abandono (`LATIDO_ABANDONO_S =
  3600` s) hacía `return` por delante de todo y `comprobar_proceso` no se
  llegaba a llamar: un trabajador con su proceso vivo perdía la tarea por
  llevar una hora sin latir, que es exactamente lo que pasa si el hilo del
  latido muere. Hoy eso sale como LATIDO_VENCIDO, diciendo que lleva más
  del umbral y que probablemente su latido murió.
- Si el trabajador declara OTRA máquina, el PID local no significa nada y
  **no hay segunda señal posible**, así que tampoco se libera solo: se
  informa y decide una persona. Bastaban 61 minutos de desfase de reloj
  para desposeer a un trabajador remoto perfectamente vivo.
  «Declara otra» no es lo mismo que «no declara ninguna»: un identificador
  sin equipo es desconocido, no ajeno, y para él la señal del proceso vale.
- Un latido en el FUTURO no es frescura: es un reloj mal puesto o una hora
  local sin zona horaria. Tratándolo como reciente, la tarea quedaba
  ACTIVA para siempre —la antigüedad negativa no supera ningún umbral—
  bloqueando su ámbito sin que ninguna recuperación pudiera tocarla.
- **Una toma desde la consola deja el PID de un proceso ya muerto** (el
  de `tomar`), así que para ella la «segunda señal» está presente desde el
  principio y la ventana real es el margen de cortesía (`LATIDO_GRACIA_S
  = 120` s) contado desde la toma o desde el último `latido`, no los
  900 s. Es el diseño heredado de A2, que las pruebas de A2 fijan: si una
  persona trabaja más de dos minutos sin `latido` ni `verificar` y alguien
  ejecuta `reanudar`, la tarea se recupera. Decisión de ingeniería
  pendiente, marcada en la revisión final: si una toma sin `--pid` debe
  juzgarse sólo por el latido y el umbral de 900 s.

Una fila incompleta (sin PID, sin `iniciado_en`, con un latido ilegible)
es INCONSISTENTE, y se informa como LATIDO_VENCIDO: HUÉRFANA afirma que
la ejecución está demostrada perdida, y una fila rota no demuestra nada
sobre el trabajador. Lo desconocido cae del mismo lado: el mapa fallaba
ABIERTO y devolvía ACTIVA, el valor más tranquilizador y el peor por
omisión.

`vitalidad()` informa y no cambia nada; `clasificar_ejecucion()` decide si
la recuperación toca o no toca. Están separadas a propósito, pero la
primera se apoya en la segunda, y también publica `requiere_atencion`: el
tablero y la consola no reimplementan el criterio. Cuando lo hacían, la
plantilla marcaba en alerta toda tarea NUEVA o REABIERTA —el estado normal
de lo que nadie ha tomado— y un tablero donde lo normal está en rojo deja
de leerse.

**4. Recuperación idempotente, y que no le quita nada a nadie.**

`reanudar` se puede ejecutar dos veces seguidas sin duplicar
interrupciones, intentos ni eventos. Tres cosas más:

- La escritura lleva la generación de la foto Y el latido sobre el que se
  clasificó. Un latido no mueve ni el estado ni la generación, así que si
  el dueño daba señal de vida entre la clasificación y la escritura, el
  UPDATE casaba igual y se le quitaba la tarea a alguien que acababa de
  demostrar que seguía ahí. Ahora cae en `reclamadas_mientras_tanto`.
- Las filas incompletas van a `inconsistentes_sin_tocar` y **no se
  liberan**. La salida es `reabrir`, que es una orden humana.
- Los temporales sólo se borran si llevan parados más de cinco minutos.
  Antes se borraban todos, incluido el que otro proceso estaba escribiendo
  en ese instante: su `os.replace` fallaba con ENOENT y la pasada entera
  se abortaba a medias. Y un fallo del espejo ya no tumba el resto: se
  anota en `espejo_no_regenerado`. La revisión final encontró que ese
  `except` estaba puesto alrededor de `_registrar_en_git`, que ni regenera
  el espejo ni lanza; el fallo real, dentro de `persistir`, sí abortaba la
  pasada entera y dejaba la primera tarea recuperada en la base, con el
  JSON viejo y sin figurar en el informe. Corregido; comprobación 34.
- `reanudar` informa en `worktree_ausente` de TODA ejecución revisada cuyo
  árbol ya no resuelve —también las activas y las de latido vencido—, sin
  tocarlas. La consola imprime todos los grupos del informe, incluidos
  `inconsistentes_sin_tocar` y `espejo_no_regenerado`, que antes callaba
  (comprobación 35), y devuelve 1 cuando deja algo que una persona debe
  mirar.

**5. `verificar()` ejecuta en el worktree de la tarea, y lo demuestra.**

Antes corría el corredor sobre la raíz desde la que se invocó el
Supervisor. Una tarea que vivía en el worktree A y se verificaba desde
`main` ejecutaba las pruebas de `main` y grababa ese resultado como suyo.

Ahora el árbol se resuelve desde la tarea, el corredor se ejecuta allí, y
el resultado guarda **dónde** se ejecutó: `raiz`, `rama`, `commit`,
`es_worktree`, y además la generación y el trabajador a los que
pertenece. Eso último hace falta porque `ultima_verificacion` sobrevive a
`reabrir`, a `reanudar` y a una retoma: el tablero seguía enseñando el
verde de una ejecución muerta como si fuera de la actual.

**La evidencia se toma antes y después.** Leerla sólo al final era una
afirmación falsa esperando a ocurrir: entre el arranque de la batería
—hasta dos minutos por archivo— y la lectura del commit cabe cualquier
`commit`, `rebase` o `checkout` del propio trabajador. Quedaba grabado
«commit X, todo en verde» cuando X nunca se ejecutó y encima estaba rojo.
Se comparan rama, commit y una huella del contenido sin confirmar
(`git diff HEAD` sobre los archivos versionados): un cambio sin confirmar
no mueve el hash, y comparar sólo «¿hay cambios?» antes y después dejaba
pasar una edición a mitad en un árbol que ya estaba sucio. Si el árbol se
movió, la corrida no se graba. Si tenía cambios sin confirmar, la
evidencia lo dice (`sin_confirmar`): el commit grabado no contiene lo que
se ejecutó, y el tablero, `estado`, `ver` y la propia salida de
`verificar` lo marcan. Y si el árbol desaparece a mitad de la corrida, se
termina en `ErrorWorktree` (código 6), no en un traceback.

Mientras corre la batería, un `LatidoAutomatico` mantiene viva la
ejecución; si durante ese rato se pierde la propiedad, `verificar` termina
en `ErrorPropiedad` con el motivo REAL del rechazo, no con uno fijo.

**6. Qué cuenta como worktree.**

Sólo lo que `git worktree list --porcelain` conoce. Comparar el directorio
común de Git no servía: ese valor no lo decide el repositorio, lo decide
un archivo `.git` de una línea que vive en el directorio candidato.
Copiar un worktree con `cp -a`, moverlo sin `git worktree repair`, o
escribirlo a mano en cualquier carpeta, producía un directorio aceptado
que Git no ha listado nunca, y la rama y el commit se leían de ese `.git`
prestado: el historial afirmaba haber verificado un commit que nadie
ejecutó. Un subdirectorio cualquiera tampoco vale: el corredor descubre
`<arbol>/pruebas/**/prueba_*.py`, así que uno con una sola prueba verde
dentro bastaba para llegar a PROPUESTO saltándose la batería.

Se pregunta con el **entorno saneado**. `GIT_DIR` está puesto siempre
dentro de un hook de Git, y también en `git rebase --exec` o
`git bisect run`: con él, `git` ignora el directorio de trabajo, las dos
consultas devuelven lo mismo y se llegaba a aceptar `/tmp` como worktree
de la tarea. Los subprocesos del corredor tampoco lo heredan.

Y **sin memorizar**: una decisión de seguridad no puede depender de si el
proceso ya había mirado antes ese directorio, porque entonces el mismo
estado del disco da veredictos opuestos.

**Y estar en la lista no basta** (revisión final). Git sigue listando una
entrada cuyo directorio o cuyo `.git` desaparecieron, marcada `prunable`,
y esa línea se ignoraba: un worktree borrado con `rm -rf` y vuelto a crear
como carpeta corriente con una prueba verde dentro se aceptaba, y
`verificar` corría allí grabando rama y commit vacíos; y si el worktree
estaba anidado en la raíz, `git` desde dentro SUBE y respondía con la rama
y el commit de `main` sobre las pruebas del worktree. Peor: Git sólo
comprueba que `<ruta>/.git` exista, así que una ruta que este repositorio
registró y borró, y que OTRO repositorio reutilizó después para un
worktree suyo, seguía en la lista como válida con el checkout del otro
(reproducido: rama y commit ajenos grabados como evidencia de esta tarea).
Ahora se descartan las entradas `prunable` y `bare`, y se exige que,
preguntando desde DENTRO de la ruta con el entorno saneado, `git` responda
con esa misma ruta como raíz del checkout y con el directorio común de
ESTE repositorio. Comprobación 5.

Las formas incómodas pero legítimas se aceptan: espacios —incluido uno
final, que antes se recortaba y tumbaba un worktree válido—, `..` en
medio, enlaces simbólicos, barra final. En Windows, `C:pruebas` y
`\pruebas` no son absolutas pero al unirlas descartarían la raíz y
acabarían resolviéndose contra el directorio actual del proceso: se
rechazan con mensaje propio. Una ruta demasiado larga o un volumen
desmontado dicen que no se pudo consultar, no que no exista.

**El árbol pertenece a la EJECUCIÓN, no a la tarea.** Se declara en cada
toma (`tomar --worktree`) y se suelta con el turno. Sin declararlo, el
árbol es la raíz desde la que se TOMA, y queda grabado igual: grabar
«nada» significaba «la raíz de quien invoque la siguiente orden», y un
trabajador que tomaba desde su worktree sin declararlo y un operador que
verificaba desde `main` proponían una tarea cuyo trabajo nunca se ejecutó
(revisión final, comprobación 4). Heredarlo era una
trampa: el trabajador siguiente, que no puede saberlo, acababa verificando
en el árbol del anterior, sobre trabajo ajeno, y el resultado se grababa
como suyo. Tampoco se importa desde el JSON: es estado de ejecución, y
escribir `"worktree": "cualquier/cosa"` en una ficha a mano bastaba para
que `verificar` corriera ahí. Si el árbol desapareció, `reanudar` lo dice
en `worktree_ausente` para toda ejecución revisada —también las activas—
y conserva la ruta en el informe (en la fila se suelta con el turno), y
declararlo explícitamente se rechaza en la TOMA, no al verificar con el
trabajo ya hecho.

**7. `crear` es atómico, y el archivo va después.**

La comprobación de existencia y la inserción viven en la misma
transacción. Con ocho procesos creando la misma tarea a la vez, la crea
uno y los otros siete reciben `ErrorCreacion`.

El JSON se escribe **tras el COMMIT**. El sistema de archivos no es
transaccional: `os.replace` es visible en el acto y ningún ROLLBACK lo
deshace, así que con la escritura dentro un Ctrl-C o un apagón dejaban
ficha sin fila. Y el huérfano no era inerte: `crear` fallaba para siempre
con ese identificador, y la primera orden de LECTURA lo importaba en
silencio, de modo que la tarea que no creó nadie acababa existiendo con la
definición del proceso muerto. El invariante es ahora «puede faltar la
ficha, nunca sobrar», que sí es reparable desde la base.

**8. El espejo JSON no pisa a la persona.**

El espejo sólo reescribe los campos OPERATIVOS. Lo declarativo —objetivo,
criterios, pruebas requeridas, ámbito, `max_intentos`, decisiones
declaradas— se releía al EMPEZAR la orden y se volcaba encima del archivo
al terminar: con `verificar` esa ventana es la batería entera, minutos, y
lo que el ingeniero escribiera mientras tanto desaparecía sin aviso.
Incluida una decisión humana recién declarada, con lo que la tarea se iba
a PROPUESTO saltándose justo la decisión que esa persona quería forzar.
Ahora se relee el archivo justo antes de escribirlo, y el veredicto de
`verificar` se dicta releyendo las decisiones —de la base y del archivo—
después de la corrida.

`max_intentos` pasa a ser declarativo: entra en la huella de definición y
se sincroniza. Estaba fuera y ninguna orden lo escribía, así que una
persona podía editarlo y no pasaba nada, y encima el espejo le deshacía la
edición.

**9. Lo que se ve.**

El tablero web y la consola muestran, cuando el dato existe: generación,
vitalidad con su motivo y su señal de atención, edad del latido, worktree,
y el árbol, la rama y el commit en los que se verificó, marcando «de otra
ejecución» cuando el verde no es de la generación vigente. Sin rediseñar
nada: son filas dentro de la ficha que ya existía.

El tablero **no escribe filas**: ni importa fichas ni pide `BEGIN
IMMEDIATE` sobre los datos. Lo único que puede crear es la propia base y
su esquema si todavía no existen, igual que cualquier otra orden (anotado
abajo como deuda). Antes pedía `BEGIN IMMEDIATE` —el candado de escritura
de toda la base— e insertaba filas desde un GET de la API web: una tarea
podía nacer con refrescar la página. Las fichas que la base no
conoce se reportan en `sin_importar` y se incorporan con
`sincronizar-definiciones`, que es una orden explícita. Y una fila con un
JSON operativo malformado se degrada a una tarjeta que lo dice, en vez de
tumbar el tablero entero y mostrar «sin conexión con el motor local», que
además era falso.

«Agentes activos» cuenta los que dan señal; los que no, aparecen aparte,
también en la web, junto a las fichas que la base no conoce. Una fila de
la base que no se puede interpretar se muestra como tal, sin culpar al
JSON, que puede estar perfectamente bien.

`verificar` imprime dónde corrió (árbol, rama y commit, y si había cambios
sin confirmar); `ver` muestra la última falla y, por cada corrida, dónde
se ejecutó y si es de otra ejecución; `estado --json` devuelve 1 con la
base en ERROR, como ya hacía la salida de texto; y una avería que no es de
las conocidas sale con código 2 y un mensaje en español, no como un
traceback con código 1.

La consola tiene `tomar --worktree` —sin él, la funcionalidad central de
A3.3 era inalcanzable desde la única interfaz que funciona sin
navegador—, códigos de salida documentados en la ayuda, un código propio
(6) para un árbol que no vale, y los rótulos en español.

**Lo que A3.3 NO hace, y es deliberado.** (Lo que T-0003 añadió encima
se describe en su propia sección, más abajo.)

- No lanza trabajadores ni ejecuta nada por su cuenta. El latido
  automático acompaña a una operación del Supervisor; no vigila tareas
  ajenas. Lanzar trabajadores lo hace T-0003 (`despachar`).
- No crea ni destruye worktrees de Git. Usa el que la toma declara.
  T-0003 los crea y retira, sólo dentro de `.arboles/`.
- No expira trabajadores por su cuenta: `reanudar` sigue siendo una orden
  manual. Lo que A3.3 añade es que ahora acierta al juzgar.
- No hay cola automática ni priorización: son de T-0003 (cola
  persistente con prioridad manual).
- No hay acciones desde el tablero: sigue siendo de sólo lectura.
- Los umbrales son números fijos, no política configurable por tarea.

**Deuda conocida, marcada para revisión humana.**

- **Reutilización de PID.** `iniciado_en` se exige pero no participa en
  ninguna decisión, y es justo el dato que permitiría descartar que el PID
  fue reciclado por otro programa tras un reinicio (comparándolo con la
  hora de arranque del proceso). Hacerlo bien es específico de cada
  sistema —`/proc/<pid>/stat` en Linux, `GetProcessTimes` en Windows— y
  ese código no se escribe a ciegas desde aquí. Mientras tanto, el efecto
  está acotado en la dirección segura: un PID reciclado hace que la tarea
  se informe como LATIDO_VENCIDO y espere a una persona, no que se le
  quite a nadie. La salida manual es `reabrir`.
- **Dos acompañantes solapados** sobre la misma tarea no se detectan. Con
  la guarda de no regresión no se hacen daño, pero sus contadores dejan de
  ser evidencia de nada.
- **`persistir` no restaura la ficha en memoria tras un rechazo.** Hoy
  ningún camino la reutiliza —el único que captura `ErrorPropiedad` y
  sigue es `reanudar`, que descarta la ficha—, pero es una mina para
  quien añada el siguiente consumidor.

Y lo que la revisión final (20/09/2026) deja marcado para decisión humana,
sin cambiar el comportamiento:

- **El espejo JSON se regenera fuera de la transacción**, con la fila que
  cada orden leyó dentro de la suya. Dos órdenes cruzadas pueden dejar el
  archivo un paso por detrás de la base hasta la siguiente orden que
  confirme. La base es la autoridad y el archivo siempre es una ficha
  legal; sólo el orden puede quedar rancio.
- **`ver` incorpora a la base una ficha que ésta no conocía** (bootstrap
  de `cargar`, heredado de A2), y **cualquier orden —el tablero incluido—
  crea la base y su esquema si no existen**. Es coherente con el resto del
  sistema, pero una orden de consulta escribe.
- **`verificar` no exige que la rama del árbol sea la rama de la tarea**;
  sólo el commit automático lo exige. Una tarea puede quedar PROPUESTA con
  evidencia de otra rama, y el registro en Git fallar en silencio. Decidir
  si es un error o un aviso.
- **Una toma desde la consola se juzga con la ventana de 120 s** (ver la
  sección 3).
- **Identificadores de trabajador con `/`** —`equipo/juan`— se interpretan
  como «de otra máquina» y ya no se recuperan solos; el equipo se compara
  con igualdad exacta de `hostname`. Documentar o restringir el formato al
  que genera `nuevo_trabajador_id` (`equipo/pid/uuid8`).
- **En Windows, `proceso_vivo` trata «acceso denegado» como «no
  existe»** (`OpenProcess` sin consultar `GetLastError`); en Linux el
  mismo caso vale «existe». Un trabajador bajo otra cuenta podría
  declararse huérfano con el latido vencido. Es código específico de
  Windows y no se cambia a ciegas desde Linux.
- **Una marca de latido sin zona horaria se lee como UTC.** Al oeste de
  UTC —el entorno del proyecto— una marca local escrita a mano parece
  horas en el pasado. El motor siempre escribe con zona; sólo afecta a
  escrituras externas.
- **Los mensajes de error de `argparse` siguen en inglés**, y `--sin-git`
  sólo se acepta antes de la orden.
- **Una prueba requerida SIN versionar satisface `pruebas_requeridas`.**
  Los archivos sin versionar quedan fuera de la huella a propósito (los
  informes que dejan las propias pruebas), así que una prueba nueva sin
  `git add` da un verde con «commit X» que no la contiene, y su edición a
  mitad no se ve. Decidir si `verificar` debe exigir que las pruebas
  requeridas estén versionadas.
- **`diagnostico` en una ruta UNC.** `abrir(solo_lectura=True)` construye
  el URI con `as_uri()`, que en `\\servidor\...` produce una autoridad que
  SQLite rechaza; el resto de órdenes abren por ruta y funcionan.

**Revisión final (20/09/2026).** Una tercera ronda adversarial —siete
revisores de sólo lectura por dimensión más tres de ojos frescos, sobre
la punta 17530d0— encontró, reprodujo y cerró lo siguiente (cada punto
con la comprobación que ahora lo fija):

- Un worktree `prunable`, anidado sin `.git`, o reutilizado por otro
  repositorio se aceptaba como árbol de ejecución (5).
- El instante del latido y el de la toma se leían antes de pedir el
  candado, al revés de lo documentado (33).
- Un fallo del espejo JSON abortaba `reanudar` entero y el grupo
  `espejo_no_regenerado` era inalcanzable (34).
- La consola de `reanudar` callaba `inconsistentes_sin_tocar` y
  `espejo_no_regenerado`, y su código de salida no distinguía nada (35).
- Un árbol que desaparecía a mitad de la corrida salía como traceback con
  código 1 (19); un árbol sucio se grababa como «commit X en verde» sin
  marca, y una edición a mitad en un árbol ya sucio pasaba por quieto
  (20); `worktree_ausente` sólo se informaba para las tareas liberadas
  (17).
- Un error transitorio al releer la fila tras un rechazo del latido se
  convertía en «propiedad perdida» y `verificar` tiraba la batería (30);
  el motivo de un rechazo por `exigir_iguales` se contradecía a sí mismo
  (28); `~usuario_inexistente` producía un traceback (5).
- La batería tenía 34 comprobaciones rotuladas hasta la 36, y 14 de 17
  mutantes nuevos sobrevivían: se cerraron los huecos de cobertura (2, 11,
  12, 18, 21, 23, 24, 27, 29, 31, 37) y `prueba_api.py` dependía de que
  la base ya conociera las fichas (fallaba 7 de 8 en un clon limpio).
- Documentación: «36 comprobaciones», «PENDIENTE DE EJECUTAR» con el gate
  ya ejecutado, un `devolver` imposible en el paso 10 del gate, un paso 8
  que dependía del reloj, cifras rancias.
- Segunda tanda, tras la ronda de ojos frescos y la verificación cruzada:
  la huella del contenido contaba el espejo JSON del propio Supervisor y
  una decisión resuelta a mitad abortaba la corrida —regresión de la
  propia revisión, destapada por su crítico de completitud antes de
  cerrar—; la relectura de decisiones tras la corrida no releía nada (la
  lista en memoria iba primero y `fusionar_decisiones` se queda con la
  primera aparición); sin `--worktree` el árbol era «la raíz de quien
  invoque la siguiente orden»; un `database is locked` llega envuelto en
  `ErrorEstadoGlobal` y apagaba el latido al primer choque, mientras la
  prueba lo simulaba con un error que en producción nunca llega así; el
  `latido` manual podía retroceder la marca; `verificar` decidía BLOQUEADO
  con un `max_intentos` que ya no era el vigente; `aprobar` no exigía al
  motor lo que comprobaba en Python; la falla ámbar quedaba rancia tras
  resolver la última decisión; PROPUESTO y BLOQUEADO se rotulaban
  FINALIZADA; `reanudar` devolvía 0 con fichas ilegibles; el reemplazo del
  espejo no toleraba un lector concurrente en Windows; y `prueba_api.py`
  pasó a correr sobre un repositorio temporal (3, 4, 14, 20, 29, 30, 31).
- Al repetir la batería en Windows, la comprobación 5 falló por una
  comparación TEXTUAL de la revisión con la salida de `git worktree list`
  (`C:/Users/...` frente a `C:\Users\...`); reproducido en Linux con
  `TMPDIR` a través de un enlace simbólico y corregido comparando rutas
  resueltas, como hace el motor. Los cinco casos de seguridad (propio,
  `prunable`, copiado, anidado sin `.git`, reutilizado por otro
  repositorio) siguen fallando la batería cuando se reintroducen, y la
  red para un Git sin línea `prunable` tiene ahora su caso (R29).

**Evidencia real de esta implementación.** Batería de 37 comprobaciones
(`PRUEBA_EJECUCION_SEGURA=OK`), con métricas medidas y comprobadas, no
declaradas, y con cota mínima en las positivas: sin ella el bloque era
decorativo, porque los contadores de fallo se incrementan justo antes de
un assert que ya aborta y una corrida que no hiciera nada salía igual de
verde. Los casos que un entorno no admite se imprimen al final como
omitidos, no desaparecen.

    ACTUALIZACIONES_PERDIDAS           = 0
    ROBOS_INDEBIDOS                    = 0
    VERIFICACIONES_EN_ARBOL_INCORRECTO = 0
    ERRORES_SQLITE                     = 0
    EXCEPCIONES                        = 0
    FALLOS_INTEGRIDAD                  = 0   (32 `PRAGMA integrity_check`)

Las carreras entre procesos usan `multiprocessing` con contexto `spawn` y
una barrera compartida, de modo que arrancan a la vez de verdad. La
atomicidad de `crear` se comprueba además de forma determinista, leyendo
el orden real de las sentencias emitidas (`BEGIN` < `SELECT` < `INSERT`),
porque una carrera puede pasar por suerte y un gate no puede depender de
eso.

**Pruebas de mutación: 22 defectos reintroducidos, 22 detectados.**

| | Defecto reintroducido |
|---|---|
| A | el latido escribe columnas de más |
| B | el latido ignora la generación que se le dio |
| C | un PID muerto basta, sin margen de cortesía |
| D | `verificar` usa el directorio actual en vez del worktree |
| E | se registra el commit de la raíz aunque ejecutara otro árbol |
| F | `crear` pierde la atomicidad |
| G | se acepta cualquier ruta como worktree |
| H | `persistir` vuelve a reescribir todas las columnas |
| I | el árbol no se suelta al terminar el turno |
| J | `verificar` traga el error de worktree y cae a la raíz |
| K | se trata a un trabajador remoto como local |
| L | una fila incompleta se informa como viva |
| M | el tablero inventa la vitalidad |
| N | el tablero inventa dónde se verificó |
| O | se cuenta como activa una ejecución muerta |
| P | el umbral de abandono vuelve a decidir solo |
| Q | `reanudar` ignora un latido llegado entre medias |
| R | el latido se rinde al primer fallo transitorio |
| S | el latido pierde la guarda de no regresión |
| T | `decidir` reescribe el bloque desde memoria |
| U | el espejo vuelve a pisar lo declarativo |
| V | el tablero vuelve a escribir |

Once de estos veintidós no se detectaban cuando se probaron por primera
vez. Las comprobaciones 9, 17 y 19 a 32 son exactamente los huecos que
destaparon: el motor hacía lo correcto y nada lo comprobaba.

**Veintinueve mutaciones más, de la revisión final: 29 de 29 detectadas.**

| | Defecto reintroducido | La detecta |
|---|---|---|
| R1 | se acepta un worktree `prunable` | 5 |
| R2 | no se comprueba que el árbol responda por este repositorio | 5 |
| R3 | el latido lee el reloj antes de pedir el candado | 33 |
| R4 | la toma lee el reloj antes de pedir el candado | 33 |
| R5 | un fallo del espejo vuelve a abortar `reanudar` | 34 |
| R6 | la consola calla `inconsistentes_sin_tocar` | 35 |
| R7 | la consola calla `latido_vencido` | 35 |
| R8 | un árbol que desaparece a mitad vuelve a salir como traceback | 19 |
| R9 | `arbol_estable` compara sólo «¿hay cambios?» y no el contenido | 20 |
| R10 | `sin_confirmar` no llega a la evidencia | 20 |
| R11 | un error al releer la fila vuelve a ser «propiedad perdida» | 30 |
| R12 | el rechazo por `exigir_iguales` vuelve a decir «estado incompatible» | 28 |
| R13 | `decidir` escribe columnas ajenas desde su foto | 2 |
| R14 | el espejo deja de fusionar la resolución de las decisiones | 31 |
| R15 | el acompañante sólo late al entrar y no repite el bucle | 11 |
| R16 | una excepción que no es de SQLite deja el latido girando | 29 |
| R17 | se admite un intervalo de latido de cero | 11 |
| R18 | se puede reutilizar un acompañante cerrado | 11 |
| R19 | `verificar` no devuelve `latido_error` | 12 |
| R20 | la huella vuelve a contar el espejo JSON del propio Supervisor | 20 |
| R21 | la toma sin `--worktree` vuelve a grabar «nada» | 4 |
| R22 | el latido vuelve a capturar sólo `sqlite3.Error` | 29 |
| R23 | el `latido` manual pierde la guarda de no regresión | 30 |
| R24 | `verificar` decide con el `max_intentos` de la foto | 3 |
| R25 | `aprobar` no exige `requiere_decision_humana = 0` al motor | 31 |
| R26 | la lista en memoria vuelve a mandar sobre la base al releer decisiones | 20 |
| R27 | `decidir` deja la falla ámbar rancia | 31 |
| R28 | PROPUESTO y BLOQUEADO vuelven a rotularse FINALIZADA | 14 |
| R29 | no se exige que la raíz del checkout sea la ruta (la red para un Git sin `prunable`) | 5 |

**Verificación en Windows (gate ejecutado el 19/09/2026 sobre la punta
17530d0; resultados en `ESTADO.md`).** Los cambios de la revisión final
del 20/09 —`resolver_worktree`, `verificar`, `reanudar`, la consola y la
batería— sólo se han verificado en Linux: el gate de abajo debe repetirse
sobre la nueva punta. Lo que puede comportarse distinto en Windows:

- `multiprocessing` sólo tiene `spawn` (las pruebas ya lo fuerzan, así que
  ejercitan el mismo camino).
- Un archivo abierto no se puede borrar mientras alguna conexión siga
  viva. Es lo que decide si los temporales y los worktrees se limpian.
- Los worktrees y los enlaces: la comprobación 18 prueba el enlace
  simbólico fuera de Windows y anota como omitido lo que no puede probar;
  la junction se prueba a mano, en el paso 7 del gate. Las letras de
  unidad y las mayúsculas entran en juego al comparar rutas. Las rutas
  relativas a unidad (`C:pruebas`, `\pruebas`) deben RECHAZARSE, no
  resolverse contra el directorio actual: la 18 lo ejercita sólo en
  Windows y en Linux lo deja anotado como omitido.
- Arrancar procesos y `git.exe` es más lento, sobre todo con Defender
  vigilando la carpeta. Este archivo hace 446 invocaciones de `git` en el
  proceso padre (140 `rev-parse`, 44 `worktree`, 33 `status`, 30 `diff`;
  eran 295 antes de la revisión final). A 250 ms por invocación —el peor
  caso medido alguna vez en esa PC— serían unos 112 s frente al límite de
  120 s; con el sobrecoste real de la corrida del 19/09 (unos 35 ms por
  invocación sobre los 17 s de Linux) son unos 32 s. Es el punto que más
  conviene cronometrar al repetir el gate; si se acercara al límite, la
  batería se parte en dos archivos antes que subir el límite.

Lo que sí se pudo descartar aquí, que es justo la condición que en
Windows decide si se puede borrar: la corrida con
`python -X dev -W error::ResourceWarning` sale con código 0 sin un solo
aviso, y un detector que instrumenta `sqlite3.connect` cuenta 483
conexiones abiertas durante la tanda y **0 vivas al terminar**.

Desde `C:\INGENIERIA_LOCAL\motor`, en PowerShell 7:

    $env:PYTHONPATH = "$PWD;$PWD\nucleo;$PWD\orquestacion"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    # 1. La batería de A3.3, sola y cronometrada.
    Measure-Command { python .\pruebas\orquestacion\prueba_ejecucion_segura.py } |
        Select-Object TotalSeconds
    python .\pruebas\orquestacion\prueba_ejecucion_segura.py
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 2. Corrida de estrés ampliada.
    python .\pruebas\orquestacion\prueba_ejecucion_segura.py --rondas 40
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 3. Recursos sin cerrar (lo que decide si Windows puede borrar).
    python -X dev -W error::ResourceWarning .\pruebas\orquestacion\prueba_ejecucion_segura.py
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 4. Regresión completa por el corredor único (A3.1 y A3.2 incluidas).
    python -m orquestacion.ingenieria_supervisor pruebas --detalle
    if ($LASTEXITCODE -ne 0) { Write-Host "FALLO: codigo $LASTEXITCODE" -ForegroundColor Red }

    # 5. Un worktree REAL de Windows, de punta a punta.
    git worktree add ..\wt_gate_a33 -b gate/a33
    python -m orquestacion.ingenieria_supervisor crear T-9003 `
        --titulo "Gate A3.3" --objetivo "Comprobar el worktree en Windows" `
        --ambito "modulos\gate\*.py" --prueba "pruebas\nucleo\prueba_nucleo.py"
    python -m orquestacion.ingenieria_supervisor tomar T-9003 --trabajador W1 `
        --worktree ..\wt_gate_a33
    python -m orquestacion.ingenieria_supervisor verificar T-9003 --trabajador W1 --generacion 1
    python -m orquestacion.ingenieria_supervisor estado
    #    A ojo: "Verificado en" debe nombrar wt_gate_a33, NO el motor.

    # 6. Rutas de Windows que deben RECHAZARSE (codigo 6 en las dos).
    python -m orquestacion.ingenieria_supervisor tomar T-9003 --trabajador W2 --worktree "C:pruebas"
    Write-Host "Codigo esperado 6, obtenido: $LASTEXITCODE"
    python -m orquestacion.ingenieria_supervisor tomar T-9003 --trabajador W2 --worktree "\pruebas"
    Write-Host "Codigo esperado 6, obtenido: $LASTEXITCODE"

    # 7. Una junction y una ruta con espacios: deben ACEPTARSE.
    git worktree add "..\wt con espacios" -b gate/a33-espacios
    New-Item -ItemType Junction -Path ..\wt_enlace -Target "..\wt con espacios"
    python -m orquestacion.ingenieria_supervisor crear T-9004 `
        --titulo "Gate A3.3 rutas" --objetivo "Rutas incomodas" `
        --ambito "modulos\gate2\*.py" --prueba "pruebas\nucleo\prueba_nucleo.py"
    python -m orquestacion.ingenieria_supervisor tomar T-9004 --trabajador W1 `
        --worktree ..\wt_enlace
    Write-Host "Codigo esperado 0, obtenido: $LASTEXITCODE"

    # 8. El worktree que desaparece.
    git worktree remove "..\wt con espacios" --force
    python -m orquestacion.ingenieria_supervisor latido T-9004 --trabajador W1 --generacion 1
    python -m orquestacion.ingenieria_supervisor reanudar
    Write-Host "Codigo esperado 1 (deja algo que mirar), obtenido: $LASTEXITCODE"
    #    El latido de la línea anterior conserva T-9004 dentro del margen
    #    de cortesía (120 s desde la marca de vida que deja la toma o el
    #    último latido; sin él, el resultado dependía de cuánto se tardara
    #    entre el paso 7 y éste): debe quedar ACTIVA, no liberarse por la
    #    desaparición del árbol, y aun así `reanudar` tiene que avisar
    #    WORKTREE REGISTRADO QUE YA NO EXISTE y devolver 1. La batería
    #    (comprobación 17) fuerza además un latido viejo y exige que
    #    entonces sí se recupere, con el mismo aviso.

    # 9. Que no quedaron temporales ni procesos huérfanos. Los prefijos son
    #    los de la batería de A3.3; el corredor completo (paso 4) usa
    #    además supervisor_, estado_global_, toma_atomica_, worktree_a2_,
    #    memoria_wt_, sin_git_, tablero_sin_git_ y propiedad_ciclo_.
    Get-ChildItem $env:TEMP -Directory | Where-Object {
        $_.Name -match '^(ejecucion_segura_|arboles_|arboles_wt_|ajena_|ajeno_otro_|crear_|movil_|perdida_|latido_a_tiempo_|transitorio_|monotonia_|decisiones_|instante_|espejo_falla_|consola_|estres_|tablero_|rutas con espacios)'
    }
    Get-Process git, python -ErrorAction SilentlyContinue |
        Where-Object { $_.StartTime -gt (Get-Date).AddMinutes(-5) }

    # 10. Limpieza del gate. T-9003 ya no está en ejecución (verificar la
    #     dejó PROPUESTA o en REQUIERE_REVISION), así que no se devuelve.
    python -m orquestacion.ingenieria_supervisor devolver T-9004 --trabajador W1 --generacion 1
    Remove-Item ..\wt_enlace -Force
    git worktree remove ..\wt_gate_a33 --force
    git worktree prune
    git branch -D gate/a33 gate/a33-espacios

Los pasos 5 a 8 usan tareas de usar y tirar (`T-9003`, `T-9004`); bórralas
después. **No se deben ejecutar sobre T-0001 ni T-0002**, que tienen que
seguir en estado NUEVA y sin ejecutar.

Como SQLite conserva los identificadores operativos aunque se quite su JSON,
el gate completo debe hacerse en un **clon local temporal** cuando se quiera
que la base operativa del motor permanezca sin esas T-900x. Un clon conserva
la comprobación real de Git, worktrees, junctions y rutas Windows, pero deja
su SQLite dentro de su propio `.git`; al borrar el clon se eliminan también
las tareas desechables. No se ejecutan T-0001 ni T-0002 en ese clon.

Criterio para decidir que Windows pasó, los ocho a la vez:

1. La corrida (1) imprime `PRUEBA_EJECUCION_SEGURA=OK`, sale con código 0
   y reporta las seis métricas de arriba en 0.
2. Tarda claramente por debajo de los 120 s.
3. La (2) reporta 0 actualizaciones perdidas y 0 errores SQLite.
4. La (3) sale con código 0, sin avisos.
5. La (4) da 8 de 8 archivos de prueba en OK.
6. En la (5), `Verificado en` nombra el worktree y no la raíz del motor.
7. La (6) devuelve 6 las dos veces, y la (7) devuelve 0.
8. En la (8), T-9004 sigue ACTIVA dentro del margen de cortesía —perder el
   árbol no autoriza a liberar una ejecución fresca—, `reanudar` avisa
   `WORKTREE REGISTRADO QUE YA NO EXISTE` y devuelve 1. La comprobación 17
   de la batería (1) fuerza además una ejecución huérfana y exige el mismo
   aviso con la tarea recuperada; la (9) no devuelve nada.

La corrida del 19/09/2026 (`ESTADO.md`) cubre explícitamente los criterios
1 a 4 y 6 a 8 sobre 17530d0; del criterio 5 (corredor completo desde la
raíz) sólo consta el corredor lanzado desde `verificar` en el worktree, y
del 8 no consta la salida vacía de la (9). Al repetir el gate sobre la
punta de la revisión final, anotar los ocho.

Prueba correspondiente:

    pruebas/orquestacion/prueba_ejecucion_segura.py

### T-0003 — Workers V1: cola persistente, despacho, worktrees automáticos y trabajadores

A3.3 dejó la base para trabajar en paralelo sin corromper nada; T-0003
pone encima lo que faltaba de C para LANZAR el trabajo: una cola que
sobrevive a reinicios, un despacho que reclama la tarea y crea su árbol,
un proceso trabajador que hace el trabajo donde debe y una limpieza que
no borra lo que no puede verificar. Todo se apoya en las primitivas ya
probadas —`tomar`, `persistir`, `LatidoAutomatico`, `verificar`,
`reanudar`, `resolver_worktree`— y no cambia ninguna de sus garantías.
Lo nuevo vive en `trabajadores.py` (cola, despacho, árboles, limpieza,
reconciliación y cuerpo del trabajador) y `trabajador.py` (el proceso).
La ficha `T-0003.json` es el contrato original del usuario; la rama se
desarrolló con un único escritor y se cerró tras una ronda de auditoría
adversarial (siete revisores de sólo lectura, R1) y otra focalizada (R2),
descritas al final de esta sección.

**1. La cola vive en la base global (tabla `cola`, migración 3).**

Una entrada por lanzamiento: `secuencia` (AUTOINCREMENT, nunca se
reutiliza), `tarea_id`, `prioridad`, `estado_cola` (`pendiente`,
`despachada`, `terminada`, `fallida`, `retirada`), `trabajo` (una LISTA
JSON de argumentos), `tiempo_limite_s`, `base` (de dónde nace la rama si
no existe), y lo que el despacho y el trabajador van dejando: trabajador,
generación, PID del proceso que adoptó, árbol, registro, adopción, último
rechazo y resultado. Un índice único parcial garantiza en el motor que
una tarea tiene como mucho UNA entrada viva (pendiente o despachada): dos
`encolar` a la vez dejan una, y el perdedor recibe un error propio, no una
avería. `encolar` rechaza además una ficha sin ámbito (la toma nunca la
concedería y la cabeza de la cola quedaría envenenada), una tarea aprobada
(no vuelve a ningún estado tomable), una prioridad que no cabe en 64 bits
y un trabajo que no sea una lista de cadenas o cuyo ejecutable sea un
guion de `cmd.exe` (`.bat`, `.cmd`: Windows lo pasa por un intérprete que
reinterpreta la línea).

El orden es `prioridad DESC, secuencia ASC` y lo resuelve SQLite en una
sola cláusula (`ORDEN_DE_COLA`), de modo que la consola, el despacho y la
reconciliación ven exactamente lo mismo, antes y después de un reinicio,
desde cualquier proceso, también entre pendientes y despachadas mezcladas.
Retirar y volver a encolar da una entrada nueva (y un puesto nuevo);
volver a la cola por recuperación conserva la secuencia (y el puesto).
Cada movimiento deja un evento de tipo `cola` en el historial de la tarea,
con el estado de la tarea en ese momento.

**2. El despacho toma y marca en UNA transacción.**

`despachar` elige la primera entrada pendiente del orden que se pueda
tomar ahora —o la de la tarea indicada—, prepara su árbol y llama a
`tomar` con el gancho `al_conceder`: dentro de la misma transacción
`BEGIN IMMEDIATE` que concede la toma, y después de insertar el evento de
la toma, un `UPDATE cola ... WHERE secuencia = ? AND estado_cola =
'pendiente'` marca la entrada; si su `rowcount` no es 1 (otro despacho se
adelantó, alguien la retiró), el gancho lanza y el ROLLBACK deshace
también la toma. O se confirman las dos cosas o ninguna. Antes de
confirmar, `tomar` relee su fila y comprueba que el gancho no la tocó: es
la única puerta por la que una escritura sobre `tareas` no pasa por
`reclamar` ni por `persistir`, y se cierra ahí mismo. Dos procesos
despachando la misma entrada tienen exactamente un ganador, y lo decide
el motor, no una lectura previa. Con el candado ya tomado, el gancho
comprueba además (un `stat`, sin Git) que el árbol validado sigue ahí: una
limpieza que lo hubiera retirado entre la validación y la toma no deja
una ejecución sin árbol.

La regla de un solo escritor por ámbito la sigue aplicando `tomar` dentro
de esa transacción. Una entrada cuyo ámbito choca con una tarea viva no
pierde el puesto: se salta, se anota el motivo (`ultimo_rechazo`, visible
en `cola`) y sale en cuanto el ámbito se libera; mientras tanto, las que
no chocan progresan. Lo mismo con una entrada cuya ficha no se puede leer,
no declara ámbito o lo declara con patrones no relativos: es un rechazo
de ESA entrada (`ficha_invalida`), anotado, y la cola sigue. Antes de la
transacción hay una lectura SIN candado del estado, los ámbitos (la unión
del declarado en el árbol y el retenido en la base, igual que `tomar`) y
las fichas ilegibles: no decide nada, sólo evita crear un árbol en disco
para una tarea que no se va a poder tomar. La batería lo demuestra
cegando esa lectura y comprobando que la transacción sigue negando.

Si después de confirmar la toma el proceso trabajador no se puede lanzar
(intérprete inexistente, carpeta de registros sin permisos), el despacho
DEVUELVE la tarea con la credencial recién concedida y cierra la entrada
como fallida, en una sola transacción, y sigue con las demás: nunca queda
una ejecución con el PID del despacho y sin nadie detrás.

**3. Los árboles se crean sólo en la zona controlada `.arboles/`.**

`<raíz>/.arboles/<tarea>`, en la rama de la tarea: `git worktree add -b
tarea/<id> <ruta> <base>` si la rama no existe —`base` es la que pida la
entrada; si no, `main` si existe; si no, el HEAD de la raíz; el commit de
partida queda en el informe y en el evento del despacho—, y `git worktree
add <ruta> tarea/<id>` si existe. Git se niega solo a extraer dos veces la
misma rama, y ese rechazo es el del despacho. La zona se anota en
`<común>/info/exclude` al crearla (local, no versionado: `git status` de la
raíz no la ve, sin tocar el `.gitignore` del proyecto).

Un árbol que ya existe se REUTILIZA sólo si pasa `resolver_worktree` (Git
lo lista, no es prunable, bare ni `locked initializing` —el estado en que
Git deja un árbol mientras lo está extrayendo—, responde por este
repositorio con esa raíz), es exactamente la ranura de la tarea (ni la
zona ni la ranura pueden ser enlaces simbólicos: con `<zona>/T-0201 ->
<zona>/T-0202` toda comprobación sobre la fila de una se aplicaría al
árbol de la otra) y está en la rama de la tarea. Dos despachos preparando
a la vez el mismo árbol esperan a que el ganador termine el checkout
(`locked initializing` e `index.lock`), y la toma decide. Los restos de
un `worktree add`/`remove` interrumpido se reparan solos si no contienen
nada de nadie: una carpeta vacía que Git no lista se retira con `rmdir`, y
los metadatos de un árbol cuyo directorio ya no existe con `git worktree
prune` (que no toca ramas ni archivos). Una carpeta con contenido pero sin
`.git` no se toca: puede ser de alguien.

Un árbol preparado por un despacho que luego pierde la toma se QUEDA: es
de la tarea, no de quien lo creó, y quien ganó pudo haberlo grabado como
suyo un instante antes. Un árbol de más es barato; `limpiar-arboles` lo
recoge cuando ya no sostenga nada.

**4. El proceso trabajador recibe todo por argv, y sólo por argv.**

```
python -m orquestacion.ingenieria_supervisor.trabajador
    --raiz=<raíz> --tarea=T-0003 --trabajador=<id> --generacion=<n>
    --secuencia=<entrada> --worktree=<árbol> --tiempo-limite=<s>
    --pid-despacho=<pid> -- <ejecutable> <argumento> ...
```

Cada opción viaja como `--clave=valor` en un solo elemento (una identidad
que empezara por `-` no puede confundirse con otra opción; `despachar`
rechaza además identidades vacías, con espacios o que empiecen por `-`).
Se lanza con `Popen(lista, shell=False)`, en su propia sesión (POSIX) o
grupo de procesos desligado de la consola (Windows: `CREATE_NEW_PROCESS_GROUP
| DETACHED_PROCESS`, y `CREATE_BREAKAWAY_FROM_JOB` cuando el sistema lo
permite), con la salida a `.arboles/.registros/<tarea>.<secuencia>.<generación>.log`
—un registro por lanzamiento— y con `cwd` y `PYTHONPATH` fijados a la raíz
DEL SUPERVISOR que despacha: si la tarea modifica el propio Supervisor en
su árbol, el código que la vigila no es el que está modificando. El
trabajo, en cambio, corre con el entorno del ÁRBOL (`entorno_controlado
(arbol)`, el mismo que usa `verificar`): trabajo y pruebas ven el mismo
código, y el `PYTHONPATH` del Supervisor no se hereda.

Su ciclo:

1. `adoptar`: escribe su PID y un latido, con identidad, generación y
   estado en el WHERE, y además exige que la fila siga con el PID del
   despacho (`--pid-despacho`, obligatorio). Un segundo proceso lanzado
   por accidente con el mismo argv encuentra el PID del primero, su
   UPDATE no casa y sale con código 4 sin haber tocado el árbol. Un
   trabajador rezagado para una ejecución que ya terminó, lo mismo. Un
   `database is locked` NO es «no es mía»: se reintenta unas veces y, si
   persiste, sale como avería sin tocar nada. En la misma transacción
   anota en la cola su PID y la hora de adopción (`cola` marca «sin
   adoptar» la entrada de un despacho que murió antes de lanzar).
2. Con `LatidoAutomatico` latiendo, ejecuta el trabajo encolado dentro del
   árbol: `Popen(lista, cwd=árbol, shell=False)` en su PROPIO grupo de
   procesos, con la salida a un archivo (`<registro>.trabajo.log`, del que
   sólo se lee la cola) y no a tuberías, y con el ejecutable resuelto por
   PATH aquí (`shutil.which`) cuando viene sin ruta. Al agotar el tiempo
   límite, o si el trabajador recibe SIGTERM/SIGINT, se mata el grupo
   ENTERO (`killpg`): un nieto que el trabajo hubiera lanzado no
   sobrevive para seguir escribiendo en el árbol (en Windows sólo muere
   el hijo directo: un Job Object queda para V2). Sin trabajo encolado,
   sólo verifica.
3. Comprueba que TODO lo que la ejecución tocó cae en el ámbito concedido:
   lo confirmado desde el `commit_inicial` de ESTA toma, tanto en HEAD
   como en la punta de la rama de la tarea (confirmar fuera y volver con
   `checkout --detach` no lo esconde), lo que sigue sin confirmar o sin
   versionar, el origen de un renombrado preparado, y lo que
   `update-index --skip-worktree` o `--assume-unchanged` ocultarían a
   `status`. El árbol se vuelve a validar antes de juzgar (un trabajo que
   borrase su `.git` haría que Git respondiera por la raíz). Los
   comodines aquí no cruzan directorios (`*` no vale `/`; `**` sí, con la
   semántica de `.gitignore`): el error cae del lado de «fuera». Y la
   RAÍZ se compara antes y después (su checkout salvo el espejo JSON del
   Supervisor, `.git/config` y los hooks): un `../..` mal calculado o un
   `git config` del trabajo se ven. Un árbol que quedó en otra rama o con
   HEAD separada cuenta como fuera del ámbito.
4. `verificar` en el árbol registrado, con el corredor único.

Cada salida deja la tarea coherente y la entrada cerrada con el resultado,
y el cierre viaja en la MISMA transacción que la transición (gancho
`al_confirmar` de `persistir`, simétrico de `al_conceder`): nadie puede
ver «tarea liberada, entrada aún despachada» y decidir sobre ese estado a
medias. Trabajo en verde y dentro del ámbito → lo que decida `verificar`
(PROPUESTO, REQUIERE_REVISION o BLOQUEADO), entrada TERMINADA con el
veredicto; trabajo fallido (código distinto de 0, no se pudo lanzar,
tiempo agotado) → la tarea se DEVUELVE (REABIERTO) con el motivo y la
entrada queda FALLIDA, sin verificar (un verde con el trabajo a medias
sería un verde falso); escritura fuera del ámbito o del árbol → BLOQUEADO
(decide una persona), entrada FALLIDA con las rutas; el árbol que dejó de
ser válido → devuelta, código 6; la propiedad perdida a mitad → la entrada
se cierra como fallida si aún es suya (quien quitó la tarea decide si la
vuelve a encolar); el propio trabajador averiado o interrumpido → intenta
devolver y cerrar antes de salir (código 2). Códigos: 0 propuesta, 1 no
propuesta, 2 avería, 4 no era suya, 6 el árbol no sirve.

**5. Limpieza: nunca `--force`, nunca fuera de la zona, nunca sin
verificar.**

`limpiar-arboles [tarea]` sólo mira `<raíz>/.arboles/<tarea>`. Se niega
cuando la base registra para esa tarea un árbol FUERA de la zona (una
toma manual con `--worktree ../x`: no se toca nada de esa tarea), cuando
la zona o la ranura son un enlace simbólico, cuando Git no reconoce la
ruta como worktree de este repositorio (`resolver_worktree`: una carpeta
con ese nombre no se borra) o no puede responder por sus cambios, cuando
el árbol no está en la rama de la tarea (con HEAD separada un commit que
sólo viva ahí se perdería), cuando la tarea está EN_EJECUCION o su entrada
sigue despachada, cuando el árbol tiene CUALQUIER cosa sin confirmar,
versionada o no (un nombre con espacios y acentos incluido: `git status
-z`), y cuando tiene archivos IGNORADOS por Git (salidas, modelos, un
`.env`; el bytecode de Python se exceptúa porque se regenera solo). Decide
y borra CON el candado de escritura tomado —la única operación del
paquete que hace Git dentro de una transacción, a propósito: un
`despachar` simultáneo ya no puede reutilizar el árbol entre la decisión
y el `remove`—. Lo que pasa todo eso se retira con `git worktree remove`
a secas; la rama de la tarea conserva sus commits. Lo que hay en la zona
con un nombre que no es de tarea, o que es un enlace, se informa y no se
toca.

**6. Recuperación: la cola sigue a la tarea, y ante la duda no se libera
nada.**

`reanudar` no cambia: clasifica cada ejecución con dos señales y sólo
libera las HUÉRFANAS. Después, `reconciliar_cola` (la consola lo hace al
final de `reanudar`, y `despachar` al empezar) pone la cola de acuerdo
con las tareas, en una transacción: la entrada despachada de una tarea
que volvió a un estado tomable (la recuperación la liberó, alguien la
devolvió), o que está EN_EJECUCION en manos de OTRA ejecución (alguien
la tomó a mano entre medias), vuelve a PENDIENTE con su misma secuencia
—cerrarla perdía un trabajo que nadie había hecho—, y el evento conserva
quién la tenía, con qué generación y PID y en qué árbol; la de una tarea
que terminó por otra vía (propuesta, bloqueada, aprobada, rechazada) se
cierra como TERMINADA con nota; la de una ejecución que sigue viva con el
mismo trabajador y generación —incluido el latido vencido con el proceso
vivo, el trabajador de otra máquina y la fila incompleta— NO se toca, y
`reanudar` la escala a una persona igual que a la tarea. Y SALVO que el
proceso trabajador de la entrada (el PID que adoptó) siga vivo en esta
máquina: entonces tampoco se reencola —un segundo lanzamiento escribiría
en el mismo árbol a la vez que el primero— y se informa como duda
(`CON EL PROCESO TRABAJADOR VIVO Y LA TAREA YA EN OTRAS MANOS`; `reanudar`
devuelve 1). Una entrada pendiente de una tarea APROBADA se retira.

El despacho que muere después de confirmar y antes de lanzar deja la fila
con su propio PID, ya muerto: dentro del margen de cortesía es ACTIVA;
pasado el margen, HUÉRFANA, se recupera y se reencola. El trabajador que
muere de golpe con el trabajo a medias (apagón), lo mismo; el árbol queda
con lo que hubiera, y el siguiente despacho lo reutiliza si está en su
rama. Una base que otra build más nueva ya migró se diagnostica como
`ESQUEMA_MAS_NUEVO` (no «ejecute inicializar-estado», que no puede
retrocederla).

**Órdenes nuevas.**

    python -m orquestacion.ingenieria_supervisor encolar T-0003 --prioridad 5 --trabajo python herramienta.py "un argumento"
    python -m orquestacion.ingenieria_supervisor cola [--json]
    python -m orquestacion.ingenieria_supervisor despachar [T-0003] [--trabajador W]
    python -m orquestacion.ingenieria_supervisor desencolar T-0003 [--motivo "..."]
    python -m orquestacion.ingenieria_supervisor limpiar-arboles [T-0003]

`--trabajo` va la última y se queda con todo lo que la siga, argumento
por argumento; sin ella, el trabajador sólo verifica. `despachar`
devuelve 7 cuando no despacha nada (cola vacía, entrada ya no pendiente,
tarea no tomable, ámbito en conflicto, ficha inválida, árbol que no
sirve, lanzamiento fallido) y dice por qué, entrada por entrada.
`limpiar-arboles` sin tarea devuelve 1 si dejó alguno sin retirar, con el
motivo de cada uno. `reanudar` imprime además lo que hizo con la cola.

**Lo que T-0003 NO hace (queda para Workers V2).**

- Un bucle de despacho: cada `despachar` lanza a lo sumo un trabajador.
  Encadenarlos es de n8n o de un guion.
- Expirar o matar trabajadores: `reanudar` sigue siendo manual y no
  libera ante la duda.
- En Windows, matar al trabajo mata sólo al hijo directo (sin Job
  Object); un nieto puede sobrevivir. `CREATE_BREAKAWAY_FROM_JOB` y
  `DETACHED_PROCESS` no se han ejecutado en Windows: lo comprueba el gate.
- Reutilización de PID: sigue acotada en la dirección segura (A3.3). Un
  PID de trabajador reutilizado por otro programa deja la entrada como
  «proceso vivo» hasta que una persona la retire.
- Acciones desde el tablero web, que sigue sin conocer la cola.
- Un trabajo que modifica el Supervisor de su árbol se verifica con el
  corredor de ese árbol (es lo que se juzga); el trabajador y sus
  escrituras a la base usan siempre el Supervisor de la raíz.
- El trabajo corre como el usuario, con su entorno (salvo `PYTHONPATH` y
  `GIT_*`), sin aislamiento: puede leer la base SQLite o llamar a
  `aprobar`. «APROBADO exige acción humana» es una regla de uso de una
  herramienta local sin autenticación, no una garantía técnica; la
  comparación de la raíz antes y después es una red contra el descuido,
  no contra la mala fe.
- La rama de una tarea nueva nace de `main` cuando existe. Si `main`
  está por detrás de lo que la tarea necesita, se indica la base al
  encolar (`encolar(..., base=...)` por la API; la consola no lo expone
  todavía).
- La migración 3 la aplica cualquier orden de esta build, también las de
  sólo lectura (patrón heredado de A2): una build anterior sobre la misma
  base común queda en `ESQUEMA_MAS_NUEVO` hasta integrar.

**Auditoría adversarial R1 (siete revisores de sólo lectura, en paralelo:
concurrencia y SQLite, Git y worktrees, procesos y despacho, recuperación
y expiración, cola persistente, diseño de pruebas, regresiones
A3.1–A3.3).** Sobre la primera implementación verde (3c57419). Cada
hallazgo se clasificó reproduciéndolo; lo CONFIRMADO se corrigió en esta
misma rama (a6e4f0c) y tiene su comprobación en la batería.

| # | Hallazgo | Clasificación | Comprobación |
|---|---|---|---|
| 1 | La transición de la tarea y el cierre de su entrada iban en dos transacciones; `reconciliar_cola` (que corre en cada `despachar`) reencolaba un trabajo fallido sin tope y su resultado se perdía (reproducido con procesos reales, 4 de 8) | CONFIRMADO | 15 |
| 2 | `adoptar` trataba `database is locked` como «no es mía» y dejaba la tarea colgada con el PID del despacho | CONFIRMADO | 16 |
| 3 | La adopción exclusiva dependía de una opción opcional (`--pid-despacho`) y admitía credencial implícita | CONFIRMADO | 16 |
| 4 | Un `Popen` fallido tras la toma dejaba la tarea EN_EJECUCION sin nadie y la excepción escapaba de `despachar` | CONFIRMADO | 17 |
| 5 | Una ficha ilegible, sin ámbito o con patrones no relativos en cabeza paraba la cola entera; `cola` la mostraba «despachable» | CONFIRMADO | 18 |
| 6 | `reconciliar_cola` cerraba como terminada la entrada de una tarea retomada a mano por otro (trabajo perdido), y reencolaba aunque el proceso trabajador siguiera vivo (dos trabajos en el mismo árbol) | CONFIRMADO | 19 |
| 7 | `limpiar_arbol` decidía sin candado (TOCTOU con `despachar`); `al_conceder` no comprobaba que el árbol siguiera | CONFIRMADO | 20 |
| 8 | Un enlace simbólico en la zona con nombre de otra tarea hacía borrar el árbol de la ejecución viva de esa otra tarea; la zona enlazada dejaba residuos | CONFIRMADO | 21 |
| 9 | Confirmar fuera del ámbito y volver con `checkout --detach`; un `git mv` desde fuera; `skip-worktree`: invisibles a la comprobación de ámbito | CONFIRMADO | 22 |
| 10 | La limpieza borraba archivos ignorados por Git y un árbol con HEAD separada (commits sueltos) | CONFIRMADO | 22 |
| 11 | El tiempo límite y una señal mataban sólo al hijo directo: nietos que seguían escribiendo en el árbol (y bloqueo de las tuberías en Windows); toda la salida del trabajo pasaba por memoria | CONFIRMADO | 23 |
| 12 | El trabajo heredaba el `PYTHONPATH` del Supervisor (importaba el código de la raíz, no el de su rama) | CONFIRMADO | 24 |
| 13 | `.bat/.cmd` pasarían por `cmd.exe` en Windows; un ejecutable sin ruta se buscaba antes en el cwd del padre; una identidad que empieza por `-` rompía el argv del trabajador | CONFIRMADO | 24 |
| 14 | Un trabajo que borraba su `.git` hacía que el ámbito se juzgara contra la raíz; el código 6 del trabajador nunca se devolvía | CONFIRMADO | 25 |
| 15 | `ORDER BY prioridad DESC` sin desempate desordenaba las vivas mezcladas y nadie lo miraba; restos de `worktree add/remove` interrumpidos bloqueaban el despacho para siempre | CONFIRMADO | 26 |
| 16 | La rama nueva nacía del HEAD que la raíz tuviera extraído; `commit_inicial` se heredaba de la ejecución anterior y bloqueaba la segunda vuelta | CONFIRMADO | 27 |
| 17 | El gancho `al_conceder` podía alterar la fila de la toma sin que nadie lo comprobara; el evento de cola se insertaba antes que el de la toma | CONFIRMADO | 6 (orden de eventos), guarda en `tomar` |
| 18 | Escrituras fuera del árbol (raíz, `.git/config`, hooks) invisibles | CONFIRMADO (red contra el descuido) | 22, `huella_de_la_raiz` |
| 19 | `cola.pid` era el del despacho; eventos `cola` sin estado; `ultimo_rechazo` crudo en `--json`; registro compartido entre lanzamientos; prioridad de más de 64 bits; encolar una aprobada; `diagnostico` con base más nueva; docstrings rancios | CONFIRMADO (menores) | 4, 6, 11, 18, 26 |
| 20 | La lectura previa `_por_que_no_se_despacha` sólo miraba el ámbito grabado y `cola` podía decir «despachable» de lo que `tomar` iba a rechazar | CONFIRMADO (informativo) | 19 |
| 21 | Un trabajo puede leer la base o llamar a `aprobar`; el trabajo corre como el usuario | FUERA DE ALCANCE (modelo de confianza de una herramienta local; documentado arriba) | — |
| 22 | La migración 3 la aplican también las órdenes de sólo lectura y una build anterior queda fuera | FUERA DE ALCANCE (patrón heredado de A2; documentado) | — |
| 23 | Reutilización de PID del despacho/trabajador | FUERA DE ALCANCE (deuda de A3.3, acotada en la dirección segura) | — |
| 24 | `listar_cola` lee `cola` y `tareas` en dos instantáneas | FALSO POSITIVO (es informativo; ninguna decisión depende de ello) | — |
| 25 | La rama escribe fuera del ámbito de su ficha (`prueba_estado_global.py`, `prueba_api.py`, `T-0003.json`) | CONFIRMADO, marcado para decisión humana: dos aserciones `== 2` inevitables al subir la versión de esquema; la ficha la escribe el Supervisor | — |

**Mutaciones deliberadas (sobre copias temporales, batería de la copia).**
Las ocho exigidas, y las variantes que salieron de la auditoría; cada una
se reintroduce sola y la batería tiene que fallar:

| Mutación | Detectada por |
|---|---|
| quitar la exclusión por ámbito (`tomar` sin conflictos) | 3 («la transacción concedió dos escritores») |
| quitar la exclusión por misma tarea (las tres capas: `reclamar`, comprobación previa de `tomar`, guarda de la cola) | 1 («se despachó una entrada ya retirada») |
| · sólo la guarda de la cola | 1 |
| · sólo el WHERE de `reclamar` | `prueba_toma_atomica.py` (A3.1: ocho ganadores); la batería de T-0003 no la ve porque las otras capas la tapan |
| · sólo la comprobación previa de `tomar` | ninguna: es explicativa, el WHERE de `reclamar` decide (documentado) |
| despacho a un intérprete de órdenes (`" ".join(argv)` + `shell=True`) | 4 (ningún trabajador adopta), 12 (eco y AST) |
| borrar un árbol no verificable (sin `resolver_worktree`) | 9 |
| · sin poder verificar sus cambios (`cambios is None`) | 25 |
| · con `--force` | 7 («se borró un árbol con trabajo sin confirmar») |
| la recuperación libera ante la duda | 8, y `prueba_ejecucion_segura.py` 13 |
| cola en memoria (no persistente) | 1 (otro proceso ve la cola vacía) |
| orden alterado (`secuencia DESC`) | 1 |
| omitir la limpieza del trabajador terminado (no cierra su entrada) | 4 |
| · no cerrar la del trabajo fallido | 7 |
| · cerrar la entrada en OTRA transacción, después de la transición | 7 |

**Verificación en Windows.** Esta rama sólo se ha ejecutado en Linux
(Python 3.11, git 2.43). `WINDOWS_GATE_REQUIRED = SI`,
`WINDOWS_GATE_EXECUTED = NO`. Lo que puede comportarse distinto en
Windows, y que el gate tiene que mirar: `DETACHED_PROCESS` y
`CREATE_BREAKAWAY_FROM_JOB` al lanzar el trabajador (nunca ejecutados);
la muerte del trabajo mata sólo al hijo directo (la comprobación 23 se
omite y lo dice); los enlaces simbólicos (la 21 se omite si el sistema no
los permite); `shutil.which` con `PATHEXT`; `git worktree add` dentro de
la raíz y `info/exclude`; el manejador de `SIGBREAK`; y la duración: la
batería lanza unos 120 procesos de Python (carreras, trabajadores con su
corredor, consolas) y tarda 29 s aquí frente al límite de 120 s del
corredor por archivo. Un solo bloque, en PowerShell 7, desde un CLON
TEMPORAL del repositorio (la base SQLite vive en `.git/` y el gate crea
tareas de usar y tirar; T-0001 y T-0002 no se ejecutan):

    # Gate Windows de T-0003 (Workers V1). PowerShell 7. Desde un clon temporal en la rama tarea/T-0003.
    $ErrorActionPreference = "Continue"
    $env:PYTHONPATH = "$PWD;$PWD\nucleo;$PWD\orquestacion"
    $env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONDONTWRITEBYTECODE = "1"
    $fallos = @()
    function Paso($nombre, [scriptblock]$bloque) {
        Write-Host "`n=== $nombre ===" -ForegroundColor Cyan
        $t = Measure-Command { & $bloque | Out-Host }
        if ($LASTEXITCODE -ne 0) { $script:fallos += "$nombre (codigo $LASTEXITCODE)"; Write-Host "FALLO: $nombre" -ForegroundColor Red }
        Write-Host ("{0}: {1:N1} s" -f $nombre, $t.TotalSeconds)
    }
    # 1. La batería de Workers V1, sola y cronometrada (27 comprobaciones; 21 y 23 pueden salir OMITIDA en Windows y deben decirlo).
    Paso "1 bateria workers v1" { python .\pruebas\orquestacion\prueba_workers_v1.py }
    # 2. Estrés entre procesos.
    Paso "2 estres" { python .\pruebas\orquestacion\prueba_workers_v1.py --rondas 10 --despachadores 8 }
    # 3. Recursos sin cerrar (lo que decide si Windows puede borrar).
    Paso "3 dev mode" { python -X dev -W error::ResourceWarning .\pruebas\orquestacion\prueba_workers_v1.py }
    # 4. Regresión completa por el corredor único (9 de 9, A3.3 incluida).
    Paso "4 corredor" { python -m orquestacion.ingenieria_supervisor pruebas --detalle }
    # 5. De punta a punta con un trabajador REAL de Windows: encolar, despachar, esperar, cola, limpiar.
    Paso "5a crear" { python -m orquestacion.ingenieria_supervisor --sin-git crear T-9005 --titulo "Gate T-0003" --objetivo "Trabajador real en Windows" --ambito "modulos\gate\*.py" --prueba "pruebas\nucleo\prueba_nucleo.py" }
    Paso "5b encolar" { python -m orquestacion.ingenieria_supervisor --sin-git encolar T-9005 --prioridad 1 --trabajo python -c "import pathlib; pathlib.Path('modulos/gate').mkdir(parents=True, exist_ok=True); pathlib.Path('modulos/gate/hecho.py').write_text('# hecho\n'); import subprocess; subprocess.run(['git','add','-A'],check=True); subprocess.run(['git','-c','user.name=W','-c','user.email=w@x','commit','-q','-m','gate'],check=True)" }
    Paso "5c despachar" { python -m orquestacion.ingenieria_supervisor --sin-git despachar }
    #    A ojo: "Arbol: ...\.arboles\T-9005 (creado ahora)" y "Argumentos (lista, sin interprete)". Se espera al trabajador:
    $limite = (Get-Date).AddMinutes(5)
    do { Start-Sleep -Seconds 3; $cola = python -m orquestacion.ingenieria_supervisor --sin-git cola --json | ConvertFrom-Json; $entrada = $cola | Where-Object { $_.tarea_id -eq "T-9005" } | Select-Object -First 1 } while ($entrada.estado_cola -eq "despachada" -and (Get-Date) -lt $limite)
    Write-Host ("Entrada T-9005: {0} -> {1}" -f $entrada.estado_cola, $entrada.resultado.estado)
    if ($entrada.estado_cola -ne "terminada" -or $entrada.resultado.estado -ne "propuesto") { $fallos += "5 trabajador real (entrada $($entrada.estado_cola))" }
    Paso "5d ver" { python -m orquestacion.ingenieria_supervisor --sin-git ver T-9005 }
    #    A ojo: PROPUESTO, "Verificado en" nombra .arboles\T-9005, y el registro .arboles\.registros\T-9005.1.1.log existe.
    Get-Content .\.arboles\.registros\T-9005.1.1.log | Select-Object -Last 8
    Paso "5e limpiar" { python -m orquestacion.ingenieria_supervisor --sin-git limpiar-arboles T-9005 }
    git worktree list
    # 6. Lo que debe RECHAZARSE en Windows (codigo esperado entre parentesis).
    python -m orquestacion.ingenieria_supervisor --sin-git encolar T-9005 --trabajo tarea.bat x;        Write-Host "6a .bat (esperado 2): $LASTEXITCODE"
    python -m orquestacion.ingenieria_supervisor --sin-git encolar T-9005 --trabajo python -c "print(1)"
    python -m orquestacion.ingenieria_supervisor --sin-git despachar T-9005 --trabajador "-x";           Write-Host "6b identidad con guion (esperado 2): $LASTEXITCODE"
    python -m orquestacion.ingenieria_supervisor --sin-git despachar T-9005;                              Write-Host "6c despachar aprobada/propuesta (esperado 7): $LASTEXITCODE"
    python -m orquestacion.ingenieria_supervisor --sin-git desencolar T-9005;                             Write-Host "6d desencolar (esperado 0): $LASTEXITCODE"
    # 7. Que no quedaron procesos ni temporales (la salida debe estar vacia).
    Get-Process python, git -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -gt (Get-Date).AddMinutes(-10) }
    Get-ChildItem $env:TEMP -Directory | Where-Object { $_.Name -match '^(cola_|misma_|solapadas_|paralelas_|doble_|bien_|falla_|duda_|fuera_|fuera_zona_|ajenos_|apagon_|argv_|consola_|estres_|atomico_|adopcion_|lanzamiento_|envenenada_|reconciliar_|desaparece_|enlaces_|esconder_|nietos_|entorno_|roto_|orden_|base_|senales_)' }
    Get-ChildItem $env:TEMP -File | Where-Object { $_.Name -match '^trabajo_.*\.log$' }
    # 8. Veredicto.
    if ($fallos.Count -eq 0) { Write-Host "`nWINDOWS_GATE_T0003 = OK" -ForegroundColor Green } else { Write-Host "`nWINDOWS_GATE_T0003 = FALLO" -ForegroundColor Red; $fallos }

Criterio para decidir que Windows pasó, todo a la vez: (1) imprime
`PRUEBA_WORKERS_V1=OK`, sale con 0, las cuatro métricas de fallo en 0 y
tarda claramente por debajo de 120 s (anotar el tiempo, y qué
comprobaciones salieron OMITIDA); (2) y (3) salen con 0, la (3) sin
avisos; (4) da 9 de 9; (5) la entrada de T-9005 termina `terminada ->
propuesto`, `ver` muestra el árbol en `.arboles\T-9005` y la limpieza lo
retira; (6) devuelve 2, 2, 7 y 0; (7) no devuelve nada;
`WINDOWS_GATE_T0003 = OK`. Borrar el clon al terminar.

Prueba correspondiente:

    pruebas/orquestacion/prueba_workers_v1.py

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
- (A3.3) Ejecución segura: cada orden escribe sólo sus columnas y ninguna
  decisión humana se pierde; latido automático durante las operaciones
  largas; vitalidad que nunca declara abandono con una sola señal;
  recuperación que no le quita la tarea a quien sigue vivo; verificación
  en el worktree REGISTRADO de la tarea, con evidencia de dónde y sobre
  qué commit corrió y con la corrida descartada si el árbol se movió;
  `crear` atómico. Comprobado con 37 comprobaciones, carreras reales entre
  procesos, tres rondas de auditoría adversarial (sesenta hallazgos
  confirmados en la primera, con los críticos y altos cerrados; la
  revisión final cerró
  además los de worktrees `prunable` o reutilizados, el espejo que
  abortaba la recuperación y la consola que callaba), y 51 mutaciones del
  código que la batería detecta (22 de la ronda R1 y 29 de la revisión
  final).
- (T-0003, Workers V1) Cola persistente con orden determinista y una
  sola entrada viva por tarea; despacho que toma la tarea y marca la
  entrada en una transacción; worktrees automáticos sólo en `.arboles/`;
  proceso trabajador con argv estructurado, adopción exclusiva, trabajo
  en su propio grupo con el entorno de su árbol, comprobación de ámbito
  (también en la punta de la rama y en la raíz) y verificación; cierre
  de la entrada en la misma transacción que la transición; limpieza sin
  `--force` de árboles verificables, limpios y sin ejecución; la cola
  sigue a la recuperación sin liberar ante la duda. Comprobado con 27
  comprobaciones con procesos reales, una ronda R1 de siete auditores y
  una R2 focalizada, y 15 mutaciones detectadas (tabla en su sección).

Pruebas correspondientes:

    pruebas/orquestacion/prueba_supervisor.py
    pruebas/orquestacion/prueba_estado_global.py
    pruebas/orquestacion/prueba_toma_atomica.py
    pruebas/orquestacion/prueba_propiedad_ciclo.py
    pruebas/orquestacion/prueba_ejecucion_segura.py
    pruebas/orquestacion/prueba_workers_v1.py

### Cómo se invoca

Desde la raíz del repositorio:

    python -m orquestacion.ingenieria_supervisor estado
    python -m orquestacion.ingenieria_supervisor diagnostico
    python -m orquestacion.ingenieria_supervisor inicializar-estado
    python -m orquestacion.ingenieria_supervisor sincronizar-definiciones
    python -m orquestacion.ingenieria_supervisor ver T-0001
    python -m orquestacion.ingenieria_supervisor pruebas --detalle
    python -m orquestacion.ingenieria_supervisor tomar T-0001
    python -m orquestacion.ingenieria_supervisor tomar T-0001 --worktree ../wt-T-0001
    python -m orquestacion.ingenieria_supervisor latido T-0001 --trabajador W --generacion 3
    python -m orquestacion.ingenieria_supervisor devolver T-0001 --trabajador W --generacion 3
    python -m orquestacion.ingenieria_supervisor verificar T-0001 --trabajador W --generacion 3
    python -m orquestacion.ingenieria_supervisor reanudar
    python -m orquestacion.ingenieria_supervisor aprobar T-0001
    python -m orquestacion.ingenieria_supervisor rechazar T-0001 --motivo "..."
    python -m orquestacion.ingenieria_supervisor encolar T-0003 --prioridad 5 --trabajo python herramienta.py
    python -m orquestacion.ingenieria_supervisor cola --json
    python -m orquestacion.ingenieria_supervisor despachar
    python -m orquestacion.ingenieria_supervisor desencolar T-0003
    python -m orquestacion.ingenieria_supervisor limpiar-arboles

Los códigos de salida están documentados en la propia ayuda
(`python -m orquestacion.ingenieria_supervisor --ayuda`): 0 hecho, 1 hecho
pero con resultado no deseado, 2 error de uso o avería, 3 toma rechazada,
4 orden rechazada por propiedad, 5 la tarea ya existe, 6 el árbol de
trabajo no sirve, 7 despacho rechazado.

---

## QUÉ NO EXISTE TODAVÍA

### C — lo que queda tras Workers V1 (T-0003)

T-0003 cubre el lanzamiento de trabajadores (uno por `despachar`), los
worktrees automáticos en `.arboles/` y la cola persistente con
prioridad. Queda para Workers V2:

- Un bucle o servicio de despacho (n8n o un guion encadenando
  `despachar`), y varios trabajadores lanzados de una vez.
- Expiración automática de trabajadores: `reanudar` sigue siendo una orden
  manual y no libera ante la duda. A3.3 hace que acierte al juzgar, no
  que se ejecute sola.
- Matar el árbol de procesos del trabajo en Windows (Job Object).
- Acciones desde el tablero web, que sigue siendo de sólo lectura y no
  conoce la cola.
- La deuda anotada al final de la sección T-0003.

### Queda para V2

- Acciones desde el tablero web: hoy es de sólo lectura.
- Aprobación y rechazo desde la interfaz gráfica.
- Priorización AUTOMÁTICA entre tareas pendientes (la cola de T-0003
  tiene prioridad manual por entrada).
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
