"""
Pruebas de A2: estado operativo global del Supervisor en SQLite.

Todas las comprobaciones son herméticas: crean repositorios Git temporales
con su propia base SQLite y nunca tocan el repositorio real ni su base.

Las fichas reales T-0001 y T-0002 se COPIAN a un repositorio temporal para
comprobar el bootstrap; los archivos originales no se leen con intención de
escritura ni se modifican.

Cubre:
 1. creación de la base desde cero;
 2. versión de esquema;
 3. reinicialización idempotente;
 4. ubicación basada en el directorio común de Git;
 5. misma ruta resuelta desde dos worktrees del mismo repositorio;
 6. bootstrap de T-0001;
 7. bootstrap de T-0002;
 8. bootstrap repetido sin duplicados;
 9. modificación de estado operativo persistente;
10. una instancia/proceso nuevo lee el mismo estado;
11. rollback ante operación fallida;
12. historial/evento persistente;
13. CLI `estado` leyendo SQLite;
14. CLI `diagnostico`;
15. separación entre definición y resolución de decisiones humanas;
16. el tablero informa ERROR sin inventar datos cuando no hay base.

No se prueba toma concurrente ni locks: pertenecen a A3/B.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(RAIZ / "orquestacion"))
sys.path.insert(0, str(RAIZ / "nucleo"))


from ingenieria_nucleo.estados import Estado

from ingenieria_supervisor import estado_global
from ingenieria_supervisor import pruebas as corredor
from ingenieria_supervisor import supervisor as nucleo
from ingenieria_supervisor import tarea as fichas


PRUEBA_VERDE = "print('PRUEBA_VERDE=OK')\n"

FICHAS_REALES = ("T-0001", "T-0002")


# ----------------------------------------------------------------------
# Repositorios temporales
# ----------------------------------------------------------------------

def _git(raiz: Path, *argumentos: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argumentos],
        cwd=str(raiz),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def repositorio_temporal(con_commit: bool = False) -> Path:
    """Repositorio Git vacío en una carpeta temporal, con pruebas mínimas."""
    raiz = Path(tempfile.mkdtemp(prefix="estado_global_"))

    inicio = _git(raiz, "init", "-q", "-b", "main")
    assert inicio.returncode == 0, inicio.stderr

    _git(raiz, "config", "user.name", "Prueba A2")
    _git(raiz, "config", "user.email", "prueba@ingenieria.local")
    _git(raiz, "config", "commit.gpgsign", "false")

    carpeta = raiz / "pruebas" / "demostracion"
    carpeta.mkdir(parents=True)
    (carpeta / "prueba_verde.py").write_text(PRUEBA_VERDE, encoding="utf-8")

    fichas.carpeta_tareas(raiz).mkdir(parents=True)

    if con_commit:
        (raiz / ".gitignore").write_text("*.sqlite3*\n", encoding="utf-8")
        _git(raiz, "add", "-A")
        commit = _git(raiz, "commit", "-q", "-m", "base")
        assert commit.returncode == 0, commit.stderr

    return raiz


def _quitar_solo_lectura(funcion, ruta, _excepcion):
    os.chmod(ruta, 0o700)
    funcion(ruta)


def borrar(raiz: Path) -> None:
    # `onexc` existe desde Python 3.12; `onerror` es la vía equivalente en
    # 3.11 y anteriores. La función de limpieza es la misma en ambos casos.
    if sys.version_info >= (3, 12):
        shutil.rmtree(raiz, onexc=_quitar_solo_lectura, ignore_errors=False)
    else:
        shutil.rmtree(raiz, onerror=_quitar_solo_lectura, ignore_errors=False)


def copiar_fichas_reales(raiz: Path) -> None:
    """Copia byte a byte las fichas reales al repositorio temporal."""
    for identificador in FICHAS_REALES:
        origen = fichas.ruta_ficha(RAIZ, identificador)
        destino = fichas.ruta_ficha(raiz, identificador)
        destino.write_bytes(origen.read_bytes())


DEFINICIONES_SINTETICAS = ("T-0901", "T-0902")


def escribir_definiciones(raiz: Path) -> tuple:
    """
    Escribe fichas JSON SIN pasar por el Supervisor.

    Simula definiciones traídas por Git que la base global todavía no
    conoce, que es la situación que el bootstrap debe resolver. A
    diferencia de copiar las fichas reales, su contenido es fijo y no
    cambia cuando el usuario avanza T-0001 o T-0002.
    """
    for identificador in DEFINICIONES_SINTETICAS:
        ficha = fichas.Ficha(
            id=identificador,
            titulo="Definición " + identificador,
            objetivo="Comprobar la CLI contra el estado global.",
            rama="tarea/" + identificador,
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            ambito_archivos=["modulos/demostracion/" + identificador + ".py"],
        )
        fichas.guardar(raiz, ficha)

    return DEFINICIONES_SINTETICAS


def ficha_minima(raiz: Path, identificador="T-0001", **extras):
    parametros = {
        "titulo": "Tarea de comprobación A2",
        "objetivo": "Comprobar el estado global.",
        "criterios_aceptacion": ["La prueba verde pasa."],
        "ambito_archivos": ["modulos/demostracion/algo.py"],
        "pruebas_requeridas": ["pruebas/demostracion/prueba_verde.py"],
    }
    parametros.update(extras)

    return nucleo.crear(raiz, identificador, **parametros)


def _cli(raiz: Path, *argumentos: str) -> subprocess.CompletedProcess:
    """Invoca la CLI canónica del Supervisor en un proceso nuevo."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "orquestacion.ingenieria_supervisor",
            "--raiz",
            str(raiz),
            *argumentos,
        ],
        cwd=str(RAIZ),
        env=corredor.entorno_controlado(RAIZ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


# ----------------------------------------------------------------------
# 1-3. Creación, versión y reinicialización idempotente
# ----------------------------------------------------------------------

def prueba_creacion_desde_cero():
    raiz = repositorio_temporal()

    try:
        ruta = estado_global.ruta_base(raiz)

        assert not ruta.exists()

        informe = estado_global.inicializar_base(raiz)

        assert ruta.is_file(), "No se creó el archivo SQLite."
        assert Path(informe["ruta"]) == ruta

        assert informe["esquema"]["version_anterior"] == 0
        assert informe["esquema"]["version_actual"] == estado_global.VERSION_ESQUEMA
        assert informe["esquema"]["aplicadas"] == list(
            range(1, estado_global.VERSION_ESQUEMA + 1)
        )

        # Pragmas reales, leídos de la base.
        with estado_global.conexion(raiz) as con:
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert con.execute("PRAGMA busy_timeout").fetchone()[0] == (
                estado_global.BUSY_TIMEOUT_MS
            )
            # synchronous: 2 = FULL
            assert con.execute("PRAGMA synchronous").fetchone()[0] == 2

            tablas = {
                fila[0]
                for fila in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        assert {"esquema", "tareas", "eventos"} <= tablas

    finally:
        borrar(raiz)


def prueba_version_de_esquema():
    raiz = repositorio_temporal()

    try:
        with estado_global.conexion(raiz, inicializar_esquema=False) as con:
            assert estado_global.version_esquema(con) == 0

            resultado = estado_global.inicializar(con)

            assert resultado["version_actual"] == estado_global.VERSION_ESQUEMA
            assert estado_global.version_esquema(con) == (
                estado_global.VERSION_ESQUEMA
            )

            filas = con.execute(
                "SELECT version, aplicado_en FROM esquema ORDER BY version"
            ).fetchall()

            assert [fila["version"] for fila in filas] == list(
                range(1, estado_global.VERSION_ESQUEMA + 1)
            )
            assert filas[0]["aplicado_en"]

            # Una base "del futuro" se rechaza de forma explícita.
            con.execute(
                "INSERT INTO esquema (version, aplicado_en) VALUES (99, 'x')"
            )

            try:
                estado_global.inicializar(con)
                raise AssertionError("Se aceptó un esquema más nuevo.")
            except estado_global.ErrorEstadoGlobal as error:
                assert "99" in str(error)

    finally:
        borrar(raiz)


def prueba_reinicializacion_idempotente():
    raiz = repositorio_temporal()

    try:
        primera = estado_global.inicializar_base(raiz)
        segunda = estado_global.inicializar_base(raiz)
        tercera = estado_global.inicializar_base(raiz)

        assert primera["esquema"]["aplicadas"] == list(
            range(1, estado_global.VERSION_ESQUEMA + 1)
        )
        assert segunda["esquema"]["aplicadas"] == []
        assert tercera["esquema"]["aplicadas"] == []

        with estado_global.conexion(raiz) as con:
            assert estado_global.version_esquema(con) == (
                estado_global.VERSION_ESQUEMA
            )
            assert con.execute(
                "SELECT COUNT(*) FROM esquema"
            ).fetchone()[0] == estado_global.VERSION_ESQUEMA

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 4-5. Ubicación global por git common dir y worktrees
# ----------------------------------------------------------------------

def prueba_ubicacion_por_git_common_dir():
    raiz = repositorio_temporal()

    try:
        comun = estado_global.git_common_dir(raiz)

        assert comun == (raiz / ".git").resolve()

        ruta = estado_global.ruta_base(raiz)

        assert ruta == comun / estado_global.NOMBRE_BASE
        assert ruta.name == "ingenieria-supervisor.sqlite3"

        # También se resuelve desde una subcarpeta del mismo repositorio.
        assert estado_global.ruta_base(raiz / "pruebas") == ruta

        # Fuera de un repositorio Git no existe base global: error claro.
        fuera = Path(tempfile.mkdtemp(prefix="sin_git_"))

        try:
            try:
                estado_global.ruta_base(fuera)
                raise AssertionError("Se resolvió una base sin repositorio.")
            except estado_global.ErrorEstadoGlobal as error:
                assert "Git" in str(error)
        finally:
            borrar(fuera)

    finally:
        borrar(raiz)


def prueba_misma_base_desde_dos_worktrees():
    raiz = repositorio_temporal(con_commit=True)
    worktree = Path(tempfile.mkdtemp(prefix="worktree_a2_"))

    # mkdtemp crea la carpeta; git worktree add exige que no exista.
    worktree.rmdir()

    try:
        creado = _git(raiz, "worktree", "add", "-q", "-b", "rama-prueba", str(worktree))
        assert creado.returncode == 0, creado.stderr

        desde_main = estado_global.ruta_base(raiz)
        desde_worktree = estado_global.ruta_base(worktree)

        assert desde_main == desde_worktree, (
            str(desde_main) + " != " + str(desde_worktree)
        )

        # La base está en el .git principal, no dentro del worktree.
        assert desde_main.parent == (raiz / ".git").resolve()
        assert str(worktree) not in str(desde_worktree)

        # Y es la MISMA base: lo que se escribe desde el worktree se lee
        # desde main.
        ficha_minima(worktree, "T-0007", titulo="Creada desde el worktree")

        assert nucleo.cargar(worktree, "T-0007").estado == Estado.NUEVO

        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0007")

        assert fila is not None
        assert fila["titulo"] == "Creada desde el worktree"

        # git status en ambos árboles sigue limpio respecto de la base.
        for arbol in (raiz, worktree):
            estado = _git(arbol, "status", "--porcelain", "--ignored").stdout
            assert "sqlite" not in estado, estado

    finally:
        _git(raiz, "worktree", "remove", "--force", str(worktree))
        borrar(raiz)
        if worktree.exists():
            borrar(worktree)


# ----------------------------------------------------------------------
# 6-8. Bootstrap de las fichas reales
# ----------------------------------------------------------------------

def _comprobar_bootstrap(fila: dict, original: dict, identificador: str) -> None:
    """
    El bootstrap COPIA lo que el JSON traía, sin interpretarlo.

    Todo se compara contra el propio JSON de origen y nunca contra valores
    escritos a mano: así la comprobación sigue siendo válida y determinista
    aunque las fichas reales avancen de estado en el futuro.
    """
    for campo in (
        "estado",
        "rama",
        "worktree",
        "intentos",
        "max_intentos",
        "trabajador_id",
        "pid",
        "iniciado_en",
        "ultimo_latido",
        "ultima_falla",
        "commit_inicial",
        "creado_en",
        "actualizado_en",
        "titulo",
    ):
        assert fila[campo] == original.get(campo), (
            identificador + ": '" + campo + "' se importó como "
            + repr(fila[campo]) + " y el JSON traía "
            + repr(original.get(campo))
        )

    assert fila["definicion_ruta"] == "orquestacion/tareas/" + identificador + ".json"

    # Las decisiones humanas se importan íntegras: mismas claves, mismo
    # orden y misma resolución (o ausencia de ella) que en el JSON.
    declaradas = original.get("requiere_decision_humana", [])

    assert [una["clave"] for una in fila["decisiones"]] == [
        una["clave"] for una in declaradas
    ]

    pendientes = 0

    for importada, declarada in zip(fila["decisiones"], declaradas):
        resuelta = bool(declarada.get("resuelta", False))
        assert importada["resuelta"] is resuelta
        assert importada["resolucion"] == declarada.get("resolucion")
        assert importada["resuelta_en"] == declarada.get("resuelta_en")
        if not resuelta:
            pendientes = pendientes + 1

    assert fila["requiere_decision_humana"] is (pendientes > 0)

    # La última verificación se deriva de las ejecuciones que el JSON traía.
    corridas = [
        una for una in original.get("ejecuciones", [])
        if una.get("tipo") == "corrida"
    ]

    if corridas:
        assert fila["ultima_verificacion"] is not None
        assert fila["ultima_verificacion"]["fecha"] == corridas[-1].get("fecha")
        assert fila["ultima_verificacion"]["resultado"] == corridas[-1].get("resultado")
    else:
        assert fila["ultima_verificacion"] is None


def _comprobar_eventos_importados(eventos: list, original: dict) -> None:
    """
    El historial del JSON se conserva y se le añade el de importación.

    No se supone en qué posición queda el evento de importación: el orden
    lo marca la fecha, y un JSON puede traer marcas de cualquier momento.
    """
    historial = original.get("historial", [])

    tipos = [evento["tipo"] for evento in eventos]

    assert len(eventos) == len(historial) + 1, tipos
    assert tipos.count("importacion") == 1, tipos

    # listar_eventos devuelve del más reciente al más antiguo; al invertir,
    # los eventos importados aparecen en el orden en que los trajo el JSON.
    importados = [
        evento for evento in reversed(eventos)
        if evento["tipo"] != "importacion"
    ]

    assert len(importados) == len(historial)

    for evento, esperado in zip(importados, historial):
        assert evento["fecha"] == esperado.get("fecha")
        assert evento["motivo"] == esperado.get("motivo")
        assert evento["origen"] == esperado.get("origen")
        assert evento["estado_anterior"] == esperado.get("estado_anterior")
        assert evento["estado_nuevo"] == esperado.get("estado_nuevo")


def prueba_bootstrap_t0001():
    raiz = repositorio_temporal()

    try:
        copiar_fichas_reales(raiz)

        antes = fichas.ruta_ficha(raiz, "T-0001").read_bytes()
        original = json.loads(antes.decode("utf-8"))

        informe = estado_global.inicializar_base(raiz)

        assert "T-0001" in informe["sincronizacion"]["importadas"]

        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0001")
            eventos = estado_global.listar_eventos(con, "T-0001")

        assert fila is not None
        _comprobar_bootstrap(fila, original, "T-0001")

        # Historial original conservado + evento de importación.
        _comprobar_eventos_importados(eventos, original)

        # El bootstrap NUNCA escribe el JSON.
        assert fichas.ruta_ficha(raiz, "T-0001").read_bytes() == antes

        # Y la ficha cargada por el Supervisor conserva su definición y
        # sus 3 decisiones pendientes con la misma descripción.
        cargada = nucleo.cargar(raiz, "T-0001")

        assert cargada.estado == Estado(original["estado"])
        assert cargada.titulo == original["titulo"]
        assert cargada.ambito_archivos == original["ambito_archivos"]
        assert len(cargada.decisiones_pendientes()) == len(
            [
                una for una in original["requiere_decision_humana"]
                if not una.get("resuelta", False)
            ]
        )

        for propia, suya in zip(
            cargada.requiere_decision_humana, original["requiere_decision_humana"]
        ):
            assert propia["clave"] == suya["clave"]
            assert propia["descripcion"] == suya["descripcion"]
            assert propia["resuelta"] is bool(suya.get("resuelta", False))

    finally:
        borrar(raiz)


def prueba_bootstrap_t0002():
    raiz = repositorio_temporal()

    try:
        copiar_fichas_reales(raiz)

        antes = fichas.ruta_ficha(raiz, "T-0002").read_bytes()
        original = json.loads(antes.decode("utf-8"))

        estado_global.inicializar_base(raiz)

        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0002")
            eventos = estado_global.listar_eventos(con, "T-0002")

        assert fila is not None
        _comprobar_bootstrap(fila, original, "T-0002")
        _comprobar_eventos_importados(eventos, original)

        assert fichas.ruta_ficha(raiz, "T-0002").read_bytes() == antes

        cargada = nucleo.cargar(raiz, "T-0002")

        assert cargada.estado == Estado(original["estado"])
        assert cargada.pruebas_requeridas == original["pruebas_requeridas"]
        assert cargada.tiene_decisiones_pendientes() is any(
            not una.get("resuelta", False)
            for una in original["requiere_decision_humana"]
        )

    finally:
        borrar(raiz)


def prueba_bootstrap_repetido_sin_duplicados():
    raiz = repositorio_temporal()

    try:
        copiar_fichas_reales(raiz)

        originales = {
            identificador: fichas.ruta_ficha(raiz, identificador).read_bytes()
            for identificador in FICHAS_REALES
        }

        primero = estado_global.inicializar_base(raiz)

        assert sorted(primero["sincronizacion"]["importadas"]) == list(FICHAS_REALES)

        def instantanea() -> tuple:
            with estado_global.conexion(raiz) as con:
                tareas = estado_global.listar_tareas(con)
                eventos = estado_global.listar_eventos(con)
                return (
                    json.dumps(tareas, sort_keys=True),
                    json.dumps(eventos, sort_keys=True),
                    estado_global.contar_tareas(con),
                    estado_global.contar_eventos(con),
                )

        antes = instantanea()

        for _ in range(3):
            repetido = estado_global.sincronizar_definiciones(raiz)

            assert repetido["importadas"] == []
            assert repetido["actualizadas"] == []
            assert sorted(repetido["sin_cambios"]) == list(FICHAS_REALES)

        estado_global.inicializar_base(raiz)

        # Ni el tablero ni la CLI importan de nuevo.
        nucleo.tablero(raiz)
        nucleo.cargar(raiz, "T-0001")

        despues = instantanea()

        assert antes == despues, "El bootstrap repetido alteró la base."
        assert despues[2] == len(FICHAS_REALES)

        # Un evento de importación por ficha, más el historial que traía.
        esperados = len(FICHAS_REALES) + sum(
            len(json.loads(contenido.decode("utf-8")).get("historial", []))
            for contenido in originales.values()
        )

        assert despues[3] == esperados, (
            str(despues[3]) + " eventos en la base, " + str(esperados) + " esperados"
        )

        for identificador, contenido in originales.items():
            assert fichas.ruta_ficha(raiz, identificador).read_bytes() == contenido

        # Ninguna tarea cambió de estado respecto de lo que traía su JSON.
        datos = nucleo.tablero(raiz)

        por_id = {una["id"]: una for una in datos["tareas"]}

        assert sorted(por_id) == list(FICHAS_REALES)

        for identificador, contenido in originales.items():
            traia = json.loads(contenido.decode("utf-8"))
            assert por_id[identificador]["estado"] == traia["estado"]
            assert por_id[identificador]["intentos"] == traia["intentos"]

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 9-12. Persistencia, lectura desde otro proceso, rollback y eventos
# ----------------------------------------------------------------------

def prueba_estado_operativo_persistente():
    raiz = repositorio_temporal()

    try:
        ficha_minima(raiz)

        tomada = nucleo.tomar(raiz, "T-0001")

        # SQLite, con una conexión nueva, refleja la toma.
        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0001")

        assert fila["estado"] == "en_ejecucion"
        assert fila["trabajador_id"] == tomada.trabajador_id
        assert fila["pid"] == tomada.pid
        assert fila["iniciado_en"] == tomada.iniciado_en
        assert fila["ultimo_latido"] == tomada.ultimo_latido
        assert fila["actualizado_en"] == tomada.actualizado_en

        # El espejo JSON coincide con SQLite (sale de él).
        espejo = fichas.leer(raiz, "T-0001")
        assert espejo.estado == Estado.EN_EJECUCION
        assert espejo.actualizado_en == fila["actualizado_en"]

        # Una edición manual del JSON NO cambia la autoridad.
        espejo.estado = Estado.APROBADO
        espejo.intentos = 9
        fichas.guardar(raiz, espejo)

        cargada = nucleo.cargar(raiz, "T-0001")
        assert cargada.estado == Estado.EN_EJECUCION
        assert cargada.intentos == 0

        # Devolver, verificar y aprobar también persisten.
        nucleo.devolver(raiz, "T-0001", "Se devuelve para la prueba.")
        assert nucleo.cargar(raiz, "T-0001").estado == Estado.REABIERTO

        nucleo.tomar(raiz, "T-0001")
        informe = nucleo.verificar(raiz, "T-0001", git=None)
        assert informe["estado"] == "propuesto"

        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0001")

        assert fila["estado"] == "propuesto"
        assert fila["ultima_verificacion"]["resultado"] == "APROBADO"
        assert fila["ultima_verificacion"]["ok"] == 1
        assert fila["ultima_verificacion"]["total"] == 1
        assert fila["ultima_verificacion"]["pruebas"][0]["marca"] == "PRUEBA_VERDE"

        nucleo.aprobar(raiz, "T-0001", "conforme", git=None)

        with estado_global.conexion(raiz) as con:
            assert estado_global.obtener_tarea(con, "T-0001")["estado"] == "aprobado"

        assert nucleo.tablero(raiz)["resumen"]["aprobadas"] == 1

    finally:
        borrar(raiz)


def prueba_proceso_nuevo_lee_el_mismo_estado():
    raiz = repositorio_temporal()

    try:
        ficha_minima(raiz)
        tomada = nucleo.tomar(raiz, "T-0001")

        programa = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from ingenieria_supervisor import estado_global\n"
            "raiz = Path(sys.argv[1])\n"
            "with estado_global.conexion(raiz) as con:\n"
            "    fila = estado_global.obtener_tarea(con, 'T-0001')\n"
            "    eventos = estado_global.listar_eventos(con, 'T-0001')\n"
            "print(json.dumps({'ruta': str(estado_global.ruta_base(raiz)),"
            " 'estado': fila['estado'], 'trabajador': fila['trabajador_id'],"
            " 'eventos': len(eventos)}))\n"
        )

        proceso = subprocess.run(
            [sys.executable, "-c", programa, str(raiz)],
            cwd=str(RAIZ),
            env=corredor.entorno_controlado(RAIZ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

        assert proceso.returncode == 0, proceso.stderr

        leido = json.loads(proceso.stdout.strip().splitlines()[-1])

        assert Path(leido["ruta"]) == estado_global.ruta_base(raiz)
        assert leido["estado"] == "en_ejecucion"
        assert leido["trabajador"] == tomada.trabajador_id
        assert leido["eventos"] == 2  # creación + toma

    finally:
        borrar(raiz)


def prueba_rollback_ante_operacion_fallida():
    raiz = repositorio_temporal()

    try:
        ficha_minima(raiz)

        with estado_global.conexion(raiz) as con:
            antes_tarea = estado_global.obtener_tarea(con, "T-0001")
            antes_eventos = estado_global.contar_eventos(con)

            # 1. Excepción propia a mitad de una transacción.
            try:
                with estado_global.transaccion(con):
                    estado_global.actualizar_tarea(
                        con, "T-0001", {"estado": "bloqueado", "intentos": 7}
                    )
                    estado_global.insertar_evento(
                        con, "T-0001", {"tipo": "transicion", "motivo": "x"}
                    )
                    raise RuntimeError("fallo deliberado")
            except RuntimeError:
                pass

            assert estado_global.obtener_tarea(con, "T-0001") == antes_tarea
            assert estado_global.contar_eventos(con) == antes_eventos

            # 2. Violación de integridad: evento de una tarea inexistente.
            try:
                with estado_global.transaccion(con):
                    estado_global.actualizar_tarea(
                        con, "T-0001", {"estado": "bloqueado"}
                    )
                    estado_global.insertar_evento(
                        con, "T-9999", {"tipo": "transicion", "motivo": "x"}
                    )
                raise AssertionError("Se aceptó un evento sin tarea.")
            except estado_global.ErrorEstadoGlobal as error:
                assert "FOREIGN KEY" in str(error).upper()

            assert estado_global.obtener_tarea(con, "T-0001") == antes_tarea
            assert estado_global.contar_eventos(con) == antes_eventos

            # 3. La conexión sigue utilizable después del rollback.
            with estado_global.transaccion(con):
                estado_global.actualizar_tarea(con, "T-0001", {"intentos": 1})

            assert estado_global.obtener_tarea(con, "T-0001")["intentos"] == 1

        # 4. Una operación del Supervisor que falla no deja rastro parcial.
        try:
            nucleo.verificar(raiz, "T-0001", git=None)
            raise AssertionError("Se verificó una tarea que no está en ejecución.")
        except nucleo.ErrorSupervisor:
            pass

        with estado_global.conexion(raiz) as con:
            assert estado_global.obtener_tarea(con, "T-0001")["estado"] == "nuevo"
            assert estado_global.contar_eventos(con) == antes_eventos

    finally:
        borrar(raiz)


def prueba_historial_de_eventos_persistente():
    raiz = repositorio_temporal()

    try:
        nucleo.crear(
            raiz,
            "T-0001",
            titulo="Con historial",
            ambito_archivos=["modulos/a.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            decisiones=[{"clave": "D-1", "descripcion": "Duda."}],
        )

        nucleo.tomar(raiz, "T-0001")
        nucleo.verificar(raiz, "T-0001", git=None)      # verde + decisión -> requiere_revision
        nucleo.decidir(raiz, "T-0001", "D-1", "Resuelta.")
        nucleo.tomar(raiz, "T-0001")
        nucleo.verificar(raiz, "T-0001", git=None)      # -> propuesto
        nucleo.rechazar(raiz, "T-0001", "No conforme.", git=None)
        nucleo.reabrir(raiz, "T-0001", git=None)
        nucleo.bloquear(raiz, "T-0001", "Bloqueo de prueba.", git=None)

        with estado_global.conexion(raiz) as con:
            eventos = estado_global.listar_eventos(con, "T-0001")

        # Más recientes primero.
        tipos = [evento["tipo"] for evento in eventos]

        assert tipos == [
            "transicion",     # bloqueado
            "transicion",     # reabierto
            "transicion",     # rechazado
            "transicion",     # propuesto
            "verificacion",
            "transicion",     # en_ejecucion
            "decision",
            "transicion",     # requiere_revision
            "verificacion",
            "transicion",     # en_ejecucion
            "creacion",
        ], tipos

        assert eventos[0]["estado_nuevo"] == "bloqueado"
        assert eventos[0]["origen"] == "humano"
        assert eventos[-1]["estado_anterior"] is None
        assert eventos[-1]["estado_nuevo"] == "nuevo"

        decision = [evento for evento in eventos if evento["tipo"] == "decision"][0]
        assert decision["datos"]["clave"] == "D-1"
        assert decision["datos"]["resolucion"] == "Resuelta."

        verificacion = [
            evento for evento in eventos if evento["tipo"] == "verificacion"
        ][0]
        assert verificacion["datos"]["resultado"] == "APROBADO"

        # El historial sobrevive a un proceso nuevo: el tablero lo publica
        # desde SQLite con su tipo y su tarea.
        datos = nucleo.tablero(raiz)
        assert datos["actividad"][0]["tipo"] == "transicion"
        assert datos["actividad"][0]["tarea"] == "T-0001"
        assert datos["resumen"]["ultima_actividad"] == eventos[0]["fecha"]

        # Y el espejo JSON conserva su historial recortado, sin ser fuente.

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 13-14. CLI
# ----------------------------------------------------------------------

def prueba_cli_estado_lee_sqlite():
    raiz = repositorio_temporal()

    try:
        identificadores = escribir_definiciones(raiz)

        # Sin base todavía: la CLI la crea, pero NO importa nada.
        #
        # Desde A3.3 el tablero es de verdad de sólo lectura: antes pedía
        # el candado de escritura de toda la base e insertaba filas, así
        # que una tarea podía nacer con sólo refrescar la página web. Las
        # fichas que la base no conoce se REPORTAN.
        salida = _cli(raiz, "estado", "--json")

        assert salida.returncode == 0, salida.stderr

        datos = json.loads(salida.stdout)

        assert datos["base_global"]["estado"] == "ACTIVA"
        assert Path(datos["base_global"]["ruta"]) == estado_global.ruta_base(raiz)
        assert datos["resumen"]["totales"] == 0, (
            "Una lectura incorporó tareas a la base: " + repr(datos["resumen"])
        )
        assert sorted(datos["resumen"]["sin_importar"]) == sorted(
            identificadores
        ), repr(datos["resumen"].get("sin_importar"))

        # Se incorporan con la orden explícita, que es lo que sí escribe.
        sincronizada = _cli(raiz, "sincronizar-definiciones")

        assert sincronizada.returncode == 0, sincronizada.stderr

        datos = json.loads(_cli(raiz, "estado", "--json").stdout)

        assert datos["resumen"]["totales"] == len(identificadores)
        assert datos["resumen"]["nuevas"] == len(identificadores)
        assert datos["resumen"]["agentes_activos"] == 0
        assert datos["resumen"]["sin_importar"] == []

        # Cambio de estado hecho en ESTE proceso...
        nucleo.tomar(raiz, "T-0902")

        # ...visible desde la CLI en OTRO proceso, leyendo SQLite.
        salida = _cli(raiz, "estado", "--json")
        datos = json.loads(salida.stdout)

        por_id = {tarea["id"]: tarea for tarea in datos["tareas"]}

        assert por_id["T-0902"]["estado"] == "en_ejecucion"
        assert datos["resumen"]["en_ejecucion"] == 1
        assert datos["resumen"]["agentes_activos"] == 1

        # Salida humana en español con el indicador de la base.
        texto = _cli(raiz, "estado").stdout

        assert "Base global SQLite" in texto
        assert "ACTIVA" in texto
        assert "EN_EJECUCION" in texto
        assert "Última actividad global" in texto

        # `ver` también carga desde SQLite.
        detalle = _cli(raiz, "ver", "T-0902").stdout
        assert "EN_EJECUCION" in detalle

    finally:
        borrar(raiz)


def prueba_cli_diagnostico():
    raiz = repositorio_temporal()

    try:
        identificadores = escribir_definiciones(raiz)

        # Antes de inicializar: no crea la base, y lo dice.
        salida = _cli(raiz, "diagnostico", "--json")

        assert salida.returncode == 1
        antes = json.loads(salida.stdout)

        assert antes["estado"] == "NO_INICIALIZADA"
        assert antes["existe"] is False
        assert sorted(antes["sin_importar"]) == list(identificadores)
        assert not estado_global.ruta_base(raiz).exists()

        inicializado = _cli(raiz, "inicializar-estado")
        assert inicializado.returncode == 0, inicializado.stderr

        for identificador in identificadores:
            assert identificador in inicializado.stdout

        salida = _cli(raiz, "diagnostico", "--json")

        assert salida.returncode == 0, salida.stderr
        informe = json.loads(salida.stdout)

        assert informe["estado"] == "ACTIVA"
        assert Path(informe["ruta"]) == estado_global.ruta_base(raiz)
        assert Path(informe["git_common_dir"]) == estado_global.git_common_dir(raiz)
        assert informe["version_esquema"] == estado_global.VERSION_ESQUEMA
        assert informe["journal_mode"] == "wal"
        assert informe["integridad"] == "ok"
        assert informe["tareas"] == len(identificadores)

        # Una ficha escrita fuera del Supervisor no trae historial: el único
        # evento de cada una es su importación.
        assert informe["eventos"] == len(identificadores)
        assert informe["ultima_actualizacion"]
        assert informe["ultimo_evento"]["tipo"] == "importacion"
        assert informe["sin_importar"] == []

        texto = _cli(raiz, "diagnostico").stdout

        for etiqueta in (
            "Ruta real de SQLite",
            "Git common dir",
            "Versión de esquema",
            "journal_mode",
            "busy_timeout",
            "Tareas registradas",
            "Estado de la base",
            "Último evento",
        ):
            assert etiqueta in texto, etiqueta

        # sincronizar-definiciones repetido: sin cambios.
        sincronizado = _cli(raiz, "sincronizar-definiciones")
        assert sincronizado.returncode == 0
        assert ", ".join(identificadores) in sincronizado.stdout

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 15-16. Decisiones humanas y tablero sin base
# ----------------------------------------------------------------------

def prueba_decisiones_definicion_y_resolucion_separadas():
    raiz = repositorio_temporal()

    try:
        nucleo.crear(
            raiz,
            "T-0001",
            titulo="Con decisiones",
            ambito_archivos=["modulos/a.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            decisiones=[
                {"clave": "D-1", "descripcion": "Primera duda."},
                {"clave": "D-2", "descripcion": "Segunda duda."},
            ],
        )

        with estado_global.conexion(raiz) as con:
            huella = estado_global.obtener_tarea(con, "T-0001")["definicion_hash"]

        nucleo.decidir(raiz, "T-0001", "D-1", "Se adopta el criterio A.")

        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0001")

        # La resolución vive en SQLite...
        assert fila["decisiones"][0]["clave"] == "D-1"
        assert fila["decisiones"][0]["resuelta"] is True
        assert fila["decisiones"][0]["resolucion"] == "Se adopta el criterio A."
        assert fila["decisiones"][0]["resuelta_en"]
        assert fila["decisiones"][0]["origen"] == "humano"
        assert fila["decisiones"][1]["resuelta"] is False
        assert fila["requiere_decision_humana"] is True

        # ...sin que la DEFINICIÓN haya cambiado: mismo hash, sin evento
        # de sincronización.
        assert fila["definicion_hash"] == huella

        with estado_global.conexion(raiz) as con:
            tipos = [
                evento["tipo"]
                for evento in estado_global.listar_eventos(con, "T-0001")
            ]
        assert "sincronizacion" not in tipos
        assert tipos[0] == "decision"

        # El estado operativo de SQLite no guarda la descripción: ésa es
        # definición y sigue en el JSON.
        assert "descripcion" not in fila["decisiones"][0]

        # Al cargar, definición (JSON) y resolución (SQLite) se funden.
        cargada = nucleo.cargar(raiz, "T-0001")
        assert cargada.requiere_decision_humana[0]["descripcion"] == "Primera duda."
        assert cargada.requiere_decision_humana[0]["resuelta"] is True
        assert len(cargada.decisiones_pendientes()) == 1

        # Si alguien "desresuelve" la decisión editando el JSON, no cuenta.
        espejo = fichas.leer(raiz, "T-0001")
        espejo.requiere_decision_humana[0]["resuelta"] = False
        espejo.requiere_decision_humana[0]["resolucion"] = None
        fichas.guardar(raiz, espejo)

        assert nucleo.cargar(raiz, "T-0001").requiere_decision_humana[0]["resuelta"] is True

        # Una decisión NUEVA declarada en el JSON sí es definición: entra
        # como pendiente al sincronizar, sin tocar las anteriores.
        espejo = fichas.leer(raiz, "T-0001")
        espejo.requiere_decision_humana.append(
            {"clave": "D-3", "descripcion": "Tercera duda."}
        )
        fichas.guardar(raiz, espejo)

        recargada = nucleo.cargar(raiz, "T-0001")

        assert [una["clave"] for una in recargada.requiere_decision_humana] == [
            "D-1", "D-2", "D-3"
        ]
        assert recargada.requiere_decision_humana[0]["resuelta"] is True
        assert recargada.requiere_decision_humana[2]["resuelta"] is False
        assert len(recargada.decisiones_pendientes()) == 2

        with estado_global.conexion(raiz) as con:
            fila = estado_global.obtener_tarea(con, "T-0001")
            tipos = [
                evento["tipo"]
                for evento in estado_global.listar_eventos(con, "T-0001")
            ]

        assert fila["definicion_hash"] != huella
        assert tipos[0] == "sincronizacion"

    finally:
        borrar(raiz)


def prueba_tablero_sin_base_no_inventa():
    """Fuera de un repositorio Git no hay base global: el tablero lo dice."""
    raiz = Path(tempfile.mkdtemp(prefix="tablero_sin_git_"))

    try:
        fichas.carpeta_tareas(raiz).mkdir(parents=True)

        # Una ficha JSON legible existe, pero SIN base global no se usa
        # como fuente operativa: no se inventa estado.
        ficha = fichas.Ficha(id="T-0001", titulo="Huérfana de base")
        fichas.guardar(raiz, ficha)

        datos = nucleo.tablero(raiz)

        assert datos["base_global"]["estado"] == "ERROR"
        assert "Git" in datos["base_global"]["detalle"]
        assert datos["resumen"]["totales"] == 0
        assert datos["resumen"]["agentes_activos"] == 0
        assert datos["tareas"] == []
        assert datos["actividad"] == []

        # Una tarea registrada cuyo JSON desaparece sigue visible, marcada.
        repositorio = repositorio_temporal()

        try:
            ficha_minima(repositorio)
            fichas.ruta_ficha(repositorio, "T-0001").unlink()

            datos = nucleo.tablero(repositorio)

            assert datos["base_global"]["estado"] == "ACTIVA"
            assert datos["resumen"]["totales"] == 1
            assert datos["tareas"][0]["definicion_legible"] is False
            assert datos["tareas"][0]["estado"] == "nuevo"

        finally:
            borrar(repositorio)

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# Ejecución
# ----------------------------------------------------------------------

COMPROBACIONES = [
    ("creación de SQLite desde cero", prueba_creacion_desde_cero),
    ("versión de esquema", prueba_version_de_esquema),
    ("reinicialización idempotente", prueba_reinicializacion_idempotente),
    ("ubicación basada en git common dir", prueba_ubicacion_por_git_common_dir),
    ("misma base desde dos worktrees", prueba_misma_base_desde_dos_worktrees),
    ("bootstrap de T-0001", prueba_bootstrap_t0001),
    ("bootstrap de T-0002", prueba_bootstrap_t0002),
    ("bootstrap repetido sin duplicados",
     prueba_bootstrap_repetido_sin_duplicados),
    ("estado operativo persistente", prueba_estado_operativo_persistente),
    ("proceso nuevo lee el mismo estado",
     prueba_proceso_nuevo_lee_el_mismo_estado),
    ("rollback ante operación fallida", prueba_rollback_ante_operacion_fallida),
    ("historial de eventos persistente",
     prueba_historial_de_eventos_persistente),
    ("CLI estado lee SQLite", prueba_cli_estado_lee_sqlite),
    ("CLI diagnostico", prueba_cli_diagnostico),
    ("decisiones: definición y resolución separadas",
     prueba_decisiones_definicion_y_resolucion_separadas),
    ("tablero sin base no inventa datos", prueba_tablero_sin_base_no_inventa),
]


def prueba_estado_global():
    for numero, (nombre, comprobacion) in enumerate(COMPROBACIONES, start=1):
        comprobacion()
        print("  " + str(numero).rjust(2) + ". " + nombre + ": OK")

    print("PRUEBA_ESTADO_GLOBAL=OK")


if __name__ == "__main__":
    prueba_estado_global()
