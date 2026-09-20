"""
Pruebas del Supervisor de Desarrollo V1.

Todas las comprobaciones son herméticas: trabajan sobre repositorios
temporales creados al vuelo, nunca sobre el repositorio real.

Desde A2 cada raíz temporal es además un repositorio Git propio, porque el
estado operativo vive en la base SQLite ubicada en el directorio común de
Git: cada comprobación usa así su propia base, aislada de las demás y de
la del repositorio real.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(RAIZ / "orquestacion"))
sys.path.insert(0, str(RAIZ / "nucleo"))


from ingenieria_nucleo.estados import Estado

from ingenieria_supervisor import estado_global
from ingenieria_supervisor import pruebas as corredor
from ingenieria_supervisor import supervisor as nucleo
from ingenieria_supervisor import tarea as fichas


# ----------------------------------------------------------------------
# Repositorio temporal de juguete
# ----------------------------------------------------------------------

PRUEBA_VERDE = (
    "print('PRUEBA_VERDE=OK')\n"
)

PRUEBA_ROJA = (
    "valor = 1\n"
    "assert valor == 2, 'fallo deliberado para comprobar el corredor'\n"
    "print('PRUEBA_ROJA=OK')\n"
)

PRUEBA_MUDA = (
    "print('terminé bien pero no declaro ninguna marca')\n"
)


def crear_repositorio(con_roja=False, con_muda=False) -> Path:
    """
    Crea una raíz temporal con pruebas sintéticas controladas.

    Desde A2 la raíz es un repositorio Git mínimo: el estado operativo vive
    en la base SQLite global ubicada en el directorio común de Git, así que
    sin repositorio no hay Supervisor. Cada raíz temporal tiene su propio
    `.git`, por lo que cada comprobación usa una base aislada.
    """
    raiz = Path(tempfile.mkdtemp(prefix="supervisor_"))

    inicio = subprocess.run(
        ["git", "init", "-q", "-b", "main", str(raiz)],
        capture_output=True,
        text=True,
    )

    assert inicio.returncode == 0, (
        "No se pudo crear el repositorio temporal: " + inicio.stderr
    )

    carpeta = raiz / "pruebas" / "demostracion"
    carpeta.mkdir(parents=True)

    (carpeta / "prueba_verde.py").write_text(PRUEBA_VERDE, encoding="utf-8")

    if con_roja:
        (carpeta / "prueba_roja.py").write_text(PRUEBA_ROJA, encoding="utf-8")

    if con_muda:
        (carpeta / "prueba_muda.py").write_text(PRUEBA_MUDA, encoding="utf-8")

    fichas.carpeta_tareas(raiz).mkdir(parents=True)

    return raiz


def _quitar_solo_lectura(funcion, ruta, _excepcion):
    """Git marca sus objetos como sólo lectura; se limpian igual."""
    os.chmod(ruta, 0o700)
    funcion(ruta)


def borrar(raiz: Path) -> None:
    # `onexc` existe desde Python 3.12; `onerror` es la vía equivalente en
    # 3.11 y anteriores. La función de limpieza es la misma en ambos casos.
    if sys.version_info >= (3, 12):
        shutil.rmtree(raiz, onexc=_quitar_solo_lectura, ignore_errors=False)
    else:
        shutil.rmtree(raiz, onerror=_quitar_solo_lectura, ignore_errors=False)


def ficha_minima(raiz: Path, identificador="T-0001", **extras):
    parametros = {
        "titulo": "Tarea de comprobación",
        "objetivo": "Comprobar el Supervisor.",
        "criterios_aceptacion": ["La prueba verde pasa."],
        "ambito_archivos": ["modulos/demostracion/algo.py"],
        "pruebas_requeridas": ["pruebas/demostracion/prueba_verde.py"],
    }
    parametros.update(extras)

    return nucleo.crear(raiz, identificador, **parametros)


# ----------------------------------------------------------------------
# 1. Creación y lectura de ficha
# ----------------------------------------------------------------------

def prueba_creacion_y_lectura():
    raiz = crear_repositorio()

    try:
        creada = ficha_minima(raiz)

        assert creada.estado == Estado.NUEVO
        assert creada.rama == "tarea/T-0001"
        assert creada.intentos == 0
        assert creada.creado_en
        assert creada.actualizado_en

        # Debe existir el archivo esperado y ser JSON legible.
        ruta = fichas.ruta_ficha(raiz, "T-0001")
        assert ruta.is_file()

        crudo = json.loads(ruta.read_text(encoding="utf-8"))
        assert crudo["id"] == "T-0001"
        assert crudo["estado"] == "nuevo"

        # Y debe reconstruirse igual al leerla.
        leida = fichas.leer(raiz, "T-0001")
        assert leida.id == creada.id
        assert leida.titulo == creada.titulo
        assert leida.estado == creada.estado
        assert leida.ambito_archivos == creada.ambito_archivos

        # No se permite duplicar.
        try:
            ficha_minima(raiz)
            raise AssertionError("Se permitió crear dos veces la misma ficha.")
        except nucleo.ErrorSupervisor:
            pass

        # Identificadores fuera de formato no pueden construir rutas.
        for malo in ["../../fuera", "T-1", "tarea", ""]:
            try:
                fichas.ruta_ficha(raiz, malo)
                raise AssertionError("Identificador aceptado: " + repr(malo))
            except fichas.ErrorFicha:
                pass

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 2. Escritura atómica
# ----------------------------------------------------------------------

def prueba_escritura_atomica():
    raiz = crear_repositorio()

    try:
        ficha = ficha_minima(raiz)
        ruta = fichas.ruta_ficha(raiz, "T-0001")

        original = ruta.read_text(encoding="utf-8")

        # Tras una escritura correcta no debe quedar ningún temporal.
        assert fichas.temporales_huerfanos(raiz) == []

        # Una ficha inválida no puede sustituir a la anterior.
        ficha.titulo = ""

        try:
            fichas.guardar(raiz, ficha)
            raise AssertionError("Se guardó una ficha inválida.")
        except fichas.ErrorFicha:
            pass

        # El archivo definitivo sigue intacto...
        assert ruta.read_text(encoding="utf-8") == original

        # ...y el temporal fallido no quedó abandonado.
        assert fichas.temporales_huerfanos(raiz) == []

        # La ficha de disco sigue siendo legible y coherente.
        recuperada = fichas.leer(raiz, "T-0001")
        assert recuperada.titulo == "Tarea de comprobación"

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 3. Transiciones válidas e inválidas
# ----------------------------------------------------------------------

def prueba_transicion_valida():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)

        tomada = nucleo.tomar(raiz, "T-0001")

        assert tomada.estado == Estado.EN_EJECUCION
        assert tomada.trabajador_id
        assert tomada.pid
        assert tomada.iniciado_en
        assert tomada.ultimo_latido

        # El historial conserva la transición.
        ultimo = tomada.historial[-1]
        assert ultimo["estado_anterior"] == "nuevo"
        assert ultimo["estado_nuevo"] == "en_ejecucion"

        # Y quedó persistida.
        assert fichas.leer(raiz, "T-0001").estado == Estado.EN_EJECUCION

    finally:
        borrar(raiz)


def prueba_transicion_invalida():
    raiz = crear_repositorio()

    try:
        ficha = ficha_minima(raiz)

        # NUEVO -> PROPUESTO no existe en la máquina de estados.
        assert not nucleo.transicion_permitida(Estado.NUEVO, Estado.PROPUESTO)

        try:
            nucleo.transicionar(
                ficha, Estado.PROPUESTO, "salto ilegal",
                nucleo.ORIGEN_AUTOMATICO,
            )
            raise AssertionError("Se aceptó una transición inválida.")
        except nucleo.ErrorTransicion as error:
            assert "inválida" in str(error)

        # La ficha no se movió.
        assert ficha.estado == Estado.NUEVO

        # Tampoco se puede verificar una tarea que no está en ejecución.
        try:
            nucleo.verificar(raiz, "T-0001", git=None)
            raise AssertionError("Se verificó una tarea que no está en ejecución.")
        except nucleo.ErrorSupervisor:
            pass

        # Ni aprobar una tarea que no está propuesta.
        try:
            nucleo.aprobar(raiz, "T-0001", git=None)
            raise AssertionError("Se aprobó una tarea que no estaba propuesta.")
        except nucleo.ErrorSupervisor:
            pass

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 4. Un solo escritor: solapamiento de ámbitos
# ----------------------------------------------------------------------

def prueba_solapamiento_de_ambitos():
    raiz = crear_repositorio()

    try:
        # Comparación de patrones, caso por caso.
        assert nucleo.patrones_solapan("a/b.py", "a/b.py")
        assert nucleo.patrones_solapan("a/b.py", "./a/b.py")
        assert nucleo.patrones_solapan("a", "a/b.py")
        assert nucleo.patrones_solapan("a/plantillas/*", "a/plantillas/x.html")
        assert nucleo.patrones_solapan("a/*.py", "a/vigas.py")

        assert not nucleo.patrones_solapan("a/b.py", "a/c.py")
        assert not nucleo.patrones_solapan("a/uno/*", "a/dos/*")
        assert not nucleo.patrones_solapan("modulos/vigas.py", "aplicacion/x.py")

        ficha_minima(
            raiz,
            "T-0001",
            ambito_archivos=["aplicacion/plantillas/*"],
        )

        ficha_minima(
            raiz,
            "T-0002",
            ambito_archivos=["aplicacion/plantillas/inicio.html"],
        )

        ficha_minima(
            raiz,
            "T-0003",
            ambito_archivos=["modulos/aparte.py"],
        )

        nucleo.tomar(raiz, "T-0001")

        # T-0002 pisa el ámbito de T-0001: no puede tomarse.
        try:
            nucleo.tomar(raiz, "T-0002")
            raise AssertionError("Se permitieron dos escritores sobre el mismo ámbito.")
        except nucleo.ErrorSolapamiento as error:
            assert "T-0001" in str(error)

        assert fichas.leer(raiz, "T-0002").estado == Estado.NUEVO

        # T-0003 no se solapa: sí puede trabajarse en paralelo.
        tercera = nucleo.tomar(raiz, "T-0003")
        assert tercera.estado == Estado.EN_EJECUCION

        # Al soltar T-0001, T-0002 ya puede tomarse.
        nucleo.devolver(raiz, "T-0001", "liberada")
        segunda = nucleo.tomar(raiz, "T-0002")
        assert segunda.estado == Estado.EN_EJECUCION

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 5. El corredor detecta FALLO real y prueba sin marca
# ----------------------------------------------------------------------

def prueba_corredor_detecta_fallo_real():
    raiz = crear_repositorio(con_roja=True)

    try:
        corrida = corredor.ejecutar_todas(raiz)

        assert corrida["resultado"] == corredor.RESULTADO_FALLO
        assert corrida["total"] == 2
        assert corrida["ok"] == 1
        assert corrida["fallidas"] == 1

        por_nombre = {uno["prueba"]: uno for uno in corrida["detalle"]}

        roja = por_nombre["pruebas/demostracion/prueba_roja.py"]

        assert roja["veredicto"] == corredor.VEREDICTO_FALLO
        assert roja["codigo"] != 0
        assert roja["marca"] is None
        # La salida real queda registrada, no un simple PASS/FAIL.
        assert "AssertionError" in roja["salida"]
        assert "fallo deliberado" in roja["salida"]

        verde = por_nombre["pruebas/demostracion/prueba_verde.py"]
        assert verde["veredicto"] == corredor.VEREDICTO_OK
        assert verde["marca"] == "PRUEBA_VERDE"

    finally:
        borrar(raiz)


def prueba_corredor_detecta_prueba_sin_marca():
    raiz = crear_repositorio(con_muda=True)

    try:
        corrida = corredor.ejecutar_todas(raiz)

        por_nombre = {uno["prueba"]: uno for uno in corrida["detalle"]}
        muda = por_nombre["pruebas/demostracion/prueba_muda.py"]

        # Terminó bien, pero no declaró marca: no puede contarse como aprobada.
        assert muda["codigo"] == 0
        assert muda["marca"] is None
        assert muda["veredicto"] == corredor.VEREDICTO_INDETERMINADO

        assert corrida["indeterminadas"] == 1
        assert corrida["ok"] == 1
        assert corrida["resultado"] == corredor.RESULTADO_FALLO

    finally:
        borrar(raiz)


def prueba_corredor_no_depende_de_la_carpeta_actual():
    raiz = crear_repositorio()

    try:
        anterior = os.getcwd()
        os.chdir(tempfile.gettempdir())

        try:
            corrida = corredor.ejecutar_todas(raiz)
        finally:
            os.chdir(anterior)

        assert corrida["resultado"] == corredor.RESULTADO_APROBADO
        assert corrida["ok"] == 1

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 6. Pruebas requeridas por la tarea
# ----------------------------------------------------------------------

def prueba_pruebas_requeridas_ausentes():
    raiz = crear_repositorio()

    try:
        ficha_minima(
            raiz,
            "T-0001",
            pruebas_requeridas=["pruebas/demostracion/prueba_inexistente.py"],
        )

        nucleo.tomar(raiz, "T-0001")

        informe = nucleo.verificar(raiz, "T-0001", git=None)

        # La corrida global está en verde...
        assert informe["corrida"]["resultado"] == corredor.RESULTADO_APROBADO

        # ...pero falta la prueba que la ficha exige.
        assert informe["estado"] == str(Estado.REQUIERE_REVISION)
        assert any("prueba_inexistente" in uno for uno in informe["problemas"])

    finally:
        borrar(raiz)


def prueba_sin_prueba_propia_no_se_propone():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0001", pruebas_requeridas=[])

        nucleo.tomar(raiz, "T-0001")

        informe = nucleo.verificar(raiz, "T-0001", git=None)

        assert informe["estado"] == str(Estado.REQUIERE_REVISION)
        assert any("prueba propia" in uno for uno in informe["problemas"])

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 7. Camino verde completo y aprobación exclusivamente humana
# ----------------------------------------------------------------------

def prueba_camino_verde_llega_a_propuesto():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        informe = nucleo.verificar(raiz, "T-0001", git=None)

        assert informe["problemas"] == []
        assert informe["estado"] == str(Estado.PROPUESTO)

        ficha = fichas.leer(raiz, "T-0001")

        assert ficha.estado == Estado.PROPUESTO
        assert ficha.intentos == 0
        assert ficha.ultima_falla is None

        # La evidencia real quedó guardada dentro de la ficha.
        corrida = ficha.ejecuciones[-1]
        assert corrida["tipo"] == "corrida"
        assert corrida["resultado"] == corredor.RESULTADO_APROBADO
        assert corrida["detalle"][0]["marca"] == "PRUEBA_VERDE"

    finally:
        borrar(raiz)


def prueba_imposible_autoaprobar():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        informe = nucleo.verificar(raiz, "T-0001", git=None)

        # Lo máximo que alcanza el Supervisor por su cuenta es PROPUESTO.
        assert informe["estado"] == str(Estado.PROPUESTO)

        ficha = fichas.leer(raiz, "T-0001")

        # Un intento automático de aprobar es rechazado por la máquina.
        try:
            nucleo.transicionar(
                ficha, Estado.APROBADO, "intento automático",
                nucleo.ORIGEN_AUTOMATICO,
            )
            raise AssertionError("El Supervisor se autoaprobó.")
        except nucleo.ErrorTransicion as error:
            assert "acción humana" in str(error)

        # Lo mismo para RECHAZADO.
        try:
            nucleo.transicionar(
                ficha, Estado.RECHAZADO, "intento automático",
                nucleo.ORIGEN_AUTOMATICO,
            )
            raise AssertionError("El Supervisor rechazó por su cuenta.")
        except nucleo.ErrorTransicion:
            pass

        assert ficha.estado == Estado.PROPUESTO

        # Ninguna transición automática puede alcanzar APROBADO.
        for origen in nucleo.TRANSICIONES:
            assert Estado.APROBADO in nucleo.ESTADOS_SOLO_HUMANOS

        # Con acción humana explícita, sí.
        aprobada = nucleo.aprobar(raiz, "T-0001", "revisado por el usuario",
                                  git=None)

        assert aprobada.estado == Estado.APROBADO
        assert aprobada.historial[-1]["origen"] == nucleo.ORIGEN_HUMANO

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 8. Límite de intentos y bloqueo
# ----------------------------------------------------------------------

def prueba_limite_de_intentos_y_bloqueo():
    raiz = crear_repositorio(con_roja=True)

    try:
        ficha_minima(raiz, "T-0001", max_intentos=2)

        nucleo.tomar(raiz, "T-0001")
        primero = nucleo.verificar(raiz, "T-0001", git=None)

        assert primero["estado"] == str(Estado.REQUIERE_REVISION)
        assert fichas.leer(raiz, "T-0001").intentos == 1

        nucleo.tomar(raiz, "T-0001")
        segundo = nucleo.verificar(raiz, "T-0001", git=None)

        assert segundo["estado"] == str(Estado.BLOQUEADO)

        ficha = fichas.leer(raiz, "T-0001")

        assert ficha.intentos == 2
        assert ficha.ultima_falla is not None
        assert ficha.ultima_falla["intento"] == 2

        # Una tarea bloqueada no puede volver a tomarse sin intervención.
        try:
            nucleo.tomar(raiz, "T-0001")
            raise AssertionError("Se tomó una tarea bloqueada.")
        except nucleo.ErrorSupervisor:
            pass

        # La intervención humana la devuelve al circuito.
        reabierta = nucleo.reabrir(raiz, "T-0001", "corregido a mano", git=None)
        assert reabierta.estado == Estado.REABIERTO

        nucleo.tomar(raiz, "T-0001")

    finally:
        borrar(raiz)


def prueba_bloqueo_explicito():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        bloqueada = nucleo.bloquear(
            raiz, "T-0001", "incertidumbre normativa real", git=None
        )

        assert bloqueada.estado == Estado.BLOQUEADO
        assert bloqueada.trabajador_id is None
        assert bloqueada.pid is None

        # El bloqueo exige motivo.
        nucleo.reabrir(raiz, "T-0001", "vuelve", git=None)

        try:
            nucleo.bloquear(raiz, "T-0001", "   ", git=None)
            raise AssertionError("Se aceptó un bloqueo sin motivo.")
        except nucleo.ErrorSupervisor:
            pass

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 9. Decisiones humanas pendientes
# ----------------------------------------------------------------------

def prueba_decision_humana_frena_la_propuesta():
    raiz = crear_repositorio()

    try:
        nucleo.crear(
            raiz,
            "T-0001",
            titulo="Auditoría con dudas abiertas",
            objetivo="Comprobar el freno por decisión humana.",
            ambito_archivos=["modulos/demostracion/algo.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            decisiones=[
                {"clave": "D-1", "descripcion": "Duda normativa sin resolver."}
            ],
        )

        ficha = fichas.leer(raiz, "T-0001")
        assert ficha.tiene_decisiones_pendientes()

        nucleo.tomar(raiz, "T-0001")
        informe = nucleo.verificar(raiz, "T-0001", git=None)

        # Las pruebas están en verde...
        assert informe["corrida"]["resultado"] == corredor.RESULTADO_APROBADO
        assert informe["problemas"] == []

        # ...pero la tarea NO llega a propuesto.
        assert informe["estado"] == str(Estado.REQUIERE_REVISION)
        assert len(informe["decisiones_pendientes"]) == 1

        # Y no se consume un intento: no es un fallo del trabajo.
        assert fichas.leer(raiz, "T-0001").intentos == 0

        # Tampoco puede aprobarse a la fuerza.
        try:
            nucleo.aprobar(raiz, "T-0001", git=None)
            raise AssertionError("Se aprobó con decisiones pendientes.")
        except nucleo.ErrorSupervisor:
            pass

        # Resuelta explícitamente la duda, el camino se abre.
        nucleo.decidir(
            raiz, "T-0001", "D-1", "Se confirmó el criterio con la norma."
        )

        resuelta = fichas.leer(raiz, "T-0001")
        assert not resuelta.tiene_decisiones_pendientes()
        assert resuelta.requiere_decision_humana[0]["resuelta"] is True
        assert resuelta.requiere_decision_humana[0]["resuelta_en"]

        nucleo.tomar(raiz, "T-0001")
        segundo = nucleo.verificar(raiz, "T-0001", git=None)

        assert segundo["estado"] == str(Estado.PROPUESTO)

        aprobada = nucleo.aprobar(raiz, "T-0001", "conforme", git=None)
        assert aprobada.estado == Estado.APROBADO

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 10. Recuperación tras cierre o apagón
# ----------------------------------------------------------------------

def prueba_reanudar_ejecucion_huerfana():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        antes = fichas.leer(raiz, "T-0001")
        eventos_antes = len(antes.historial)

        # El trabajador desapareció: su proceso ya no existe y, además,
        # el latido dejó de ser reciente (pasó el margen de cortesía).
        mas_tarde = datetime.now(timezone.utc) + timedelta(minutes=5)

        informe = nucleo.reanudar(
            raiz,
            ahora=mas_tarde,
            comprobar_proceso=lambda pid: False,
        )

        assert informe["revisadas"] == 1
        assert len(informe["huerfanas"]) == 1
        assert informe["huerfanas"][0]["id"] == "T-0001"

        ficha = fichas.leer(raiz, "T-0001")

        # La tarea no se perdió: volvió al circuito.
        assert ficha.estado == Estado.REABIERTO

        # El trabajador quedó liberado.
        assert ficha.trabajador_id is None
        assert ficha.pid is None
        assert ficha.ultimo_latido is None

        # La ejecución anterior quedó marcada como interrumpida...
        interrupcion = ficha.ejecuciones[-1]
        assert interrupcion["tipo"] == "interrupcion"
        assert interrupcion["resultado"] == "INTERRUMPIDA"
        assert interrupcion["trabajador_id"] == antes.trabajador_id
        assert interrupcion["pid"] == antes.pid
        assert "ya no existe" in interrupcion["motivo"]
        assert interrupcion["iniciado_en"] == antes.iniciado_en
        assert interrupcion["ultimo_latido"] == antes.ultimo_latido

        # ...el motivo quedó registrado...
        assert ficha.ultima_falla is not None
        assert "interrumpida" in ficha.ultima_falla["problemas"][0].lower()

        # ...y el historial se conservó y creció.
        assert len(ficha.historial) > eventos_antes

        # La tarea puede retomarse sin perder nada.
        retomada = nucleo.tomar(raiz, "T-0001")
        assert retomada.estado == Estado.EN_EJECUCION

    finally:
        borrar(raiz)


def prueba_reanudar_respeta_mandato_breve():
    """
    Un 'tomar' desde la línea de comandos deja siempre un PID muerto: el
    proceso del mandato termina en cuanto escribe la ficha. Eso, por sí
    solo, no puede declarar la tarea abandonada.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        # Proceso inexistente, pero latido de hace un instante.
        informe = nucleo.reanudar(
            raiz,
            comprobar_proceso=lambda pid: False,
        )

        assert informe["huerfanas"] == []
        assert len(informe["activas"]) == 1
        assert "margen de cortesía" in informe["activas"][0]["motivo"]

        assert fichas.leer(raiz, "T-0001").estado == Estado.EN_EJECUCION

        # Pasado el margen de cortesía, sí se declara abandonada.
        tarde = datetime.now(timezone.utc) + timedelta(minutes=5)

        informe = nucleo.reanudar(
            raiz,
            ahora=tarde,
            comprobar_proceso=lambda pid: False,
        )

        assert len(informe["huerfanas"]) == 1
        assert fichas.leer(raiz, "T-0001").estado == Estado.REABIERTO

    finally:
        borrar(raiz)


def prueba_reanudar_respeta_tarea_viva():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        informe = nucleo.reanudar(
            raiz,
            comprobar_proceso=lambda pid: True,
        )

        assert len(informe["activas"]) == 1
        assert informe["huerfanas"] == []

        assert fichas.leer(raiz, "T-0001").estado == Estado.EN_EJECUCION

    finally:
        borrar(raiz)


def prueba_reanudar_detecta_latido_vencido():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)
        nucleo.tomar(raiz, "T-0001")

        # El proceso sigue vivo, pero hace CINCO HORAS que no da señales.
        #
        # Un latido vencido no basta para declarar abandono, y el umbral de
        # abandono TAMPOCO decide solo. Un proceso vivo y comprobable es una
        # señal fuerte que contradice al latido: mientras exista, no hay
        # abandono demostrado, dure lo que dure el silencio. Lo más probable
        # aquí es que el hilo del latido haya muerto y el trabajador siga
        # trabajando; quitarle la tarea sería tirar su trabajo.
        #
        # Se informa para que lo mire una persona, y no se toca nada.
        futuro = datetime.now(timezone.utc) + timedelta(hours=5)

        informe = nucleo.reanudar(
            raiz,
            ahora=futuro,
            comprobar_proceso=lambda pid: True,
        )

        assert informe["huerfanas"] == [], (
            "Se recuperó una ejecución con el proceso vivo: "
            + repr(informe["huerfanas"])
        )
        assert len(informe["latido_vencido"]) == 1, (
            "No se informó del latido vencido: " + repr(informe)
        )
        assert "sigue vivo" in informe["latido_vencido"][0]["motivo"], (
            informe["latido_vencido"][0]["motivo"]
        )
        assert "umbral de abandono" in informe["latido_vencido"][0]["motivo"], (
            "El aviso no menciona cuánto lleva sin latir: "
            + informe["latido_vencido"][0]["motivo"]
        )

        # La tarea sigue siendo de su dueño.
        viva = fichas.leer(raiz, "T-0001")

        assert viva.estado == Estado.EN_EJECUCION
        assert viva.trabajador_id is not None

        # Con el proceso muerto SÍ hay dos señales, y entonces se recupera.
        segundo = nucleo.reanudar(
            raiz,
            ahora=futuro,
            comprobar_proceso=lambda pid: False,
        )

        assert len(segundo["huerfanas"]) == 1, (
            "Con el proceso muerto y el latido vencido debía recuperarse: "
            + repr(segundo)
        )
        assert "dos señales" in segundo["huerfanas"][0]["motivo"], (
            segundo["huerfanas"][0]["motivo"]
        )

        assert fichas.leer(raiz, "T-0001").estado == Estado.REABIERTO

    finally:
        borrar(raiz)


def prueba_reanudar_detecta_ficha_inconsistente():
    raiz = crear_repositorio()

    try:
        ficha = ficha_minima(raiz)

        # Ficha en ejecución sin identidad de trabajador: estado imposible,
        # propio de una escritura interrumpida a medias.
        #
        # Desde A2 el estado operativo vive en SQLite: el JSON operativo
        # no es entrada, así que la inconsistencia se inyecta en la base.
        # De paso se comprueba que un JSON alterado a mano NO manda.
        ficha.estado = Estado.EN_EJECUCION
        fichas.guardar(raiz, ficha)

        assert nucleo.cargar(raiz, "T-0001").estado == Estado.NUEVO, (
            "Un JSON editado a mano cambió el estado operativo."
        )

        with estado_global.conexion(raiz) as con:
            with estado_global.transaccion(con):
                estado_global.actualizar_tarea(
                    con, "T-0001", {"estado": str(Estado.EN_EJECUCION)}
                )

        informe = nucleo.reanudar(raiz)

        # Desde A3.3 una fila incompleta se INFORMA y no se toca. No
        # demuestra que el trabajador esté muerto —puede estar vivo y
        # latiendo—, y liberarla se la quitaba. La salida es `reabrir`,
        # que es una orden humana.
        assert informe["inconsistentes"] == [], (
            "Se liberó una fila incompleta sin comprobar nada: "
            + repr(informe["inconsistentes"])
        )
        assert len(informe["inconsistentes_sin_tocar"]) == 1, (
            "No se informó de la fila incompleta: " + repr(informe)
        )
        assert (
            "sin identidad completa"
            in informe["inconsistentes_sin_tocar"][0]["motivo"]
        )

        assert nucleo.cargar(raiz, "T-0001").estado == Estado.EN_EJECUCION, (
            "La recuperación cambió el estado de una fila que no debía tocar."
        )

        # Y una persona la desbloquea con `reabrir`.
        nucleo.reabrir(raiz, "T-0001", "Fila incompleta revisada a mano.")

        assert nucleo.cargar(raiz, "T-0001").estado == Estado.REABIERTO
        assert fichas.leer(raiz, "T-0001").estado == Estado.REABIERTO

    finally:
        borrar(raiz)


def prueba_reanudar_limpia_temporales_abandonados():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)

        # Un corte durante una escritura deja un temporal suelto.
        basura = (
            fichas.carpeta_tareas(raiz)
            / (fichas.PREFIJO_TEMPORAL + "roto" + fichas.SUFIJO_TEMPORAL)
        )
        basura.write_text("{ esto no es json", encoding="utf-8")

        # Un temporal recién escrito NO está abandonado: es una escritura en
        # vuelo, probablemente de otro proceso. Borrarlo rompía su
        # `os.replace` y abortaba la pasada de recuperación a medias.
        assert fichas.temporales_huerfanos(raiz) == [], (
            "Un temporal recién creado se consideró abandonado."
        )

        en_vuelo = nucleo.reanudar(raiz)

        assert en_vuelo["temporales_eliminados"] == [], (
            "La recuperación borró un temporal que podía estar en uso."
        )
        assert basura.exists()

        # Envejecido, sí es basura de un corte.
        viejo_ts = time.time() - fichas.EDAD_TEMPORAL_HUERFANO_S - 60
        os.utime(basura, (viejo_ts, viejo_ts))

        assert len(fichas.temporales_huerfanos(raiz)) == 1

        informe = nucleo.reanudar(raiz)

        assert len(informe["temporales_eliminados"]) == 1
        assert fichas.temporales_huerfanos(raiz) == []
        assert not basura.exists()

        # La ficha buena sigue intacta.
        assert fichas.leer(raiz, "T-0001").estado == Estado.NUEVO

    finally:
        borrar(raiz)


def prueba_ficha_corrupta_no_derriba_el_tablero():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz)

        rota = fichas.carpeta_tareas(raiz) / "T-0002.json"
        rota.write_text("{ roto", encoding="utf-8")

        datos = nucleo.tablero(raiz)

        assert datos["resumen"]["totales"] == 1
        assert len(datos["fichas_ilegibles"]) == 1
        assert datos["fichas_ilegibles"][0]["archivo"] == "T-0002.json"

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 11. Integridad del estado persistente
# ----------------------------------------------------------------------

def prueba_ficha_con_identificador_cruzado():
    """
    Copiar una ficha con otro nombre no puede destruir la original: si el
    campo 'id' no coincide con el archivo, un guardado posterior escribiría
    sobre la ficha equivocada.
    """
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0001")

        original = fichas.ruta_ficha(raiz, "T-0001")
        copia = fichas.ruta_ficha(raiz, "T-0003")

        copia.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

        try:
            fichas.leer(raiz, "T-0003")
            raise AssertionError("Se aceptó una ficha con el id cruzado.")
        except fichas.ErrorFicha as error:
            assert "T-0001" in str(error)

        # La original sigue intacta y el tablero reporta el problema.
        assert fichas.leer(raiz, "T-0001").titulo == "Tarea de comprobación"

        datos = nucleo.tablero(raiz)
        assert datos["resumen"]["totales"] == 1
        assert len(datos["fichas_ilegibles"]) == 1

    finally:
        borrar(raiz)


def prueba_ficha_mal_codificada_no_derriba_el_tablero():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0001")

        # Bytes que no son UTF-8 válido.
        rota = fichas.carpeta_tareas(raiz) / "T-0002.json"
        rota.write_bytes(b'{"id": "T-0002", "titulo": "\xff\xfe roto"}')

        datos = nucleo.tablero(raiz)

        assert datos["resumen"]["totales"] == 1
        assert len(datos["fichas_ilegibles"]) == 1
        assert "UTF-8" in datos["fichas_ilegibles"][0]["motivo"]

        # Y reanudar tampoco se cae.
        informe = nucleo.reanudar(raiz)
        assert len(informe["fichas_ilegibles"]) == 1

    finally:
        borrar(raiz)


def prueba_json_ajeno_no_paraliza_el_supervisor():
    raiz = crear_repositorio()

    try:
        ficha_minima(raiz, "T-0001")

        ajeno = fichas.carpeta_tareas(raiz) / "notas.json"
        ajeno.write_text('{"cualquier": "cosa"}', encoding="utf-8")

        # listar() lo ignora: no es una ficha.
        assert [f.id for f in fichas.listar(raiz)] == ["T-0001"]

        # Y por tanto tomar() sigue funcionando.
        tomada = nucleo.tomar(raiz, "T-0001")
        assert tomada.estado == Estado.EN_EJECUCION

        # Pero el tablero lo reporta, no lo esconde.
        datos = nucleo.tablero(raiz)
        assert any(
            e["archivo"] == "notas.json" for e in datos["fichas_ilegibles"]
        )

    finally:
        borrar(raiz)


def prueba_ambito_invalido_se_rechaza():
    raiz = crear_repositorio()

    try:
        # Sin ámbito no hay forma de garantizar un solo escritor.
        ficha_minima(raiz, "T-0001", ambito_archivos=[])

        try:
            nucleo.tomar(raiz, "T-0001")
            raise AssertionError("Se tomó una tarea sin ámbito declarado.")
        except nucleo.ErrorSupervisor as error:
            assert "ámbito" in str(error)

        # Rutas absolutas o con '..' no son comparables de forma fiable.
        #
        # Desde A2 el estado global recuerda cada identificador aunque su
        # JSON se borre, así que cada patrón usa una tarea distinta.
        patrones = [
            "C:/INGENIERIA_LOCAL/motor/modulos/vigas.py",
            "/modulos/vigas.py",
            "../fuera/archivo.py",
        ]

        for numero, patron in enumerate(patrones, start=2):
            identificador = "T-" + str(numero).zfill(4)

            ficha_minima(raiz, identificador, ambito_archivos=[patron])

            try:
                nucleo.tomar(raiz, identificador)
                raise AssertionError("Se aceptó el patrón: " + patron)
            except nucleo.ErrorSupervisor as error:
                assert "relativas" in str(error)

        # Borrar el JSON no borra la tarea del estado global: el
        # identificador queda reservado y no puede volver a crearse.
        fichas.ruta_ficha(raiz, "T-0002").unlink()

        try:
            ficha_minima(raiz, "T-0002", ambito_archivos=["modulos/x.py"])
            raise AssertionError("Se recreó una tarea que el estado global ya conocía.")
        except nucleo.ErrorSupervisor as error:
            assert "estado global" in str(error)

        # Un patrón que abarca todo el repositorio choca con cualquiera.
        assert nucleo.patrones_solapan(".", "modulos/vigas.py")
        assert nucleo.patrones_solapan("", "modulos/vigas.py")
        assert nucleo.patrones_solapan("*", "modulos/vigas.py")

    finally:
        borrar(raiz)


def prueba_ambito_retenido_tras_requerir_revision():
    """
    Una tarea que quedó en requiere_revision conserva cambios sin confirmar
    en el árbol de trabajo: sigue siendo la dueña de sus archivos.
    """
    raiz = crear_repositorio(con_roja=True)

    try:
        ficha_minima(
            raiz, "T-0001",
            ambito_archivos=["aplicacion/servidor.py"],
        )
        ficha_minima(
            raiz, "T-0002",
            ambito_archivos=["aplicacion/servidor.py"],
        )

        nucleo.tomar(raiz, "T-0001")
        informe = nucleo.verificar(raiz, "T-0001", git=None)

        assert informe["estado"] == str(Estado.REQUIERE_REVISION)

        # El trabajador quedó liberado: no hay PID fantasma en el tablero.
        pendiente = fichas.leer(raiz, "T-0001")
        assert pendiente.trabajador_id is None
        assert pendiente.pid is None

        # Pero el ámbito sigue retenido por el ESTADO de la tarea.
        try:
            nucleo.tomar(raiz, "T-0002")
            raise AssertionError(
                "Se permitió un segundo escritor sobre un ámbito retenido."
            )
        except nucleo.ErrorSolapamiento as error:
            assert "T-0001" in str(error)

        # La propia tarea sí puede retomarse.
        assert nucleo.tomar(raiz, "T-0001").estado == Estado.EN_EJECUCION

    finally:
        borrar(raiz)


def prueba_reabrir_reinicia_los_intentos():
    raiz = crear_repositorio(con_roja=True)

    try:
        ficha_minima(raiz, "T-0001", max_intentos=2)

        nucleo.tomar(raiz, "T-0001")
        nucleo.verificar(raiz, "T-0001", git=None)
        nucleo.tomar(raiz, "T-0001")
        nucleo.verificar(raiz, "T-0001", git=None)

        bloqueada = fichas.leer(raiz, "T-0001")
        assert bloqueada.estado == Estado.BLOQUEADO
        assert bloqueada.intentos == 2

        # La intervención humana devuelve el presupuesto completo.
        reabierta = nucleo.reabrir(raiz, "T-0001", "corregido a mano", git=None)

        assert reabierta.estado == Estado.REABIERTO
        assert reabierta.intentos == 0

        # Si no se reiniciara, esta verificación volvería a bloquear.
        nucleo.tomar(raiz, "T-0001")
        informe = nucleo.verificar(raiz, "T-0001", git=None)

        assert informe["estado"] == str(Estado.REQUIERE_REVISION)
        assert fichas.leer(raiz, "T-0001").intentos == 1

    finally:
        borrar(raiz)


def prueba_corredor_detecta_tiempo_agotado():
    raiz = crear_repositorio()

    try:
        carpeta = raiz / "pruebas" / "demostracion"

        (carpeta / "prueba_colgada.py").write_text(
            "import time\n"
            "time.sleep(30)\n"
            "print('PRUEBA_COLGADA=OK')\n",
            encoding="utf-8",
        )

        corrida = corredor.ejecutar_todas(raiz, tiempo_limite_s=2)

        por_nombre = {uno["prueba"]: uno for uno in corrida["detalle"]}
        colgada = por_nombre["pruebas/demostracion/prueba_colgada.py"]

        assert colgada["veredicto"] == corredor.VEREDICTO_TIEMPO_AGOTADO
        assert colgada["codigo"] is None
        assert colgada["marca"] is None

        assert corrida["agotadas"] == 1
        assert corrida["resultado"] == corredor.RESULTADO_FALLO

        # Una prueba colgada tampoco puede llevar la tarea a propuesto.
        ficha_minima(
            raiz, "T-0001",
            pruebas_requeridas=["pruebas/demostracion/prueba_colgada.py"],
        )

        nucleo.tomar(raiz, "T-0001")
        informe = nucleo.verificar(raiz, "T-0001", tiempo_limite_s=2, git=None)

        assert informe["estado"] == str(Estado.REQUIERE_REVISION)
        assert any("TIEMPO_AGOTADO" in uno for uno in informe["problemas"])

    finally:
        borrar(raiz)


def prueba_tablero_publica_valores_reales():
    """
    El tablero no puede limitarse a tener las claves correctas: sus valores
    deben coincidir con lo que dice la ficha.
    """
    raiz = crear_repositorio(con_roja=True)

    try:
        nucleo.crear(
            raiz,
            "T-0001",
            titulo="Tarea observada",
            objetivo="Comprobar el tablero.",
            ambito_archivos=["modulos/observado.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            max_intentos=4,
            decisiones=[{"clave": "D-9", "descripcion": "Duda abierta."}],
        )

        vacio = nucleo.tablero(raiz)

        assert vacio["resumen"]["totales"] == 1
        assert vacio["resumen"]["nuevas"] == 1
        assert vacio["resumen"]["agentes_activos"] == 0

        uno = vacio["tareas"][0]

        assert uno["id"] == "T-0001"
        assert uno["titulo"] == "Tarea observada"
        assert uno["estado"] == "nuevo"
        assert uno["rama"] == "tarea/T-0001"
        assert uno["worktree"] is None
        assert uno["intentos"] == 0
        assert uno["max_intentos"] == 4
        assert uno["pruebas_ok"] == 0
        assert uno["pruebas_total"] == 0
        assert uno["ultima_falla"] is None
        assert len(uno["decisiones_pendientes"]) == 1
        assert uno["decisiones_pendientes"][0]["clave"] == "D-9"
        assert uno["trabajador_id"] is None
        assert uno["pid"] is None

        tomada = nucleo.tomar(raiz, "T-0001")

        activo = nucleo.tablero(raiz)

        assert activo["resumen"]["en_ejecucion"] == 1
        assert activo["resumen"]["agentes_activos"] == 1
        assert activo["tareas"][0]["trabajador_id"] == tomada.trabajador_id
        assert activo["tareas"][0]["pid"] == tomada.pid
        assert activo["tareas"][0]["ultimo_latido"] == tomada.ultimo_latido

        nucleo.verificar(raiz, "T-0001", git=None)

        tras_fallo = nucleo.tablero(raiz)
        revisada = tras_fallo["tareas"][0]

        assert tras_fallo["resumen"]["requieren_revision"] == 1
        assert tras_fallo["resumen"]["agentes_activos"] == 0
        assert revisada["intentos"] == 1
        # 2 pruebas descubiertas, 1 en OK: los números deben ser reales.
        assert revisada["pruebas_total"] == 2
        assert revisada["pruebas_ok"] == 1
        assert revisada["ultima_falla"] is not None
        assert revisada["ultima_falla"]["intento"] == 1

        # La actividad reciente conserva el origen de cada transición.
        assert tras_fallo["actividad"][0]["tarea"] == "T-0001"
        assert tras_fallo["actividad"][0]["origen"] == nucleo.ORIGEN_AUTOMATICO
        assert tras_fallo["actividad"][-1]["origen"] == nucleo.ORIGEN_HUMANO

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# 12. Límites del commit automático
# ----------------------------------------------------------------------

def _git(raiz: Path, *argumentos):
    return subprocess.run(
        ["git", *argumentos],
        cwd=str(raiz),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def prueba_commit_automatico_limitado():
    raiz = crear_repositorio()

    try:
        # La raíz ya es un repositorio: crear_repositorio() lo inicializa
        # desde A2, porque sin Git no hay base global que ubicar.
        _git(raiz, "config", "user.name", "Prueba Supervisor")
        _git(raiz, "config", "user.email", "prueba@ingenieria.local")
        _git(raiz, "config", "commit.gpgsign", "false")

        (raiz / "semilla.txt").write_text("inicio\n", encoding="utf-8")
        _git(raiz, "add", "-A")
        _git(raiz, "commit", "-m", "inicio")

        ficha_minima(raiz)

        git = nucleo.Git(raiz)

        # 1. En la rama principal el commit automático está prohibido.
        resultado = git.commit_ficha("T-0001", "Tarea T-0001: nuevo")

        assert resultado["realizado"] is False
        assert "main" in resultado["motivo"]

        # 2. En una rama que no es la de la tarea, tampoco.
        _git(raiz, "checkout", "-b", "otra-rama")

        resultado = git.commit_ficha("T-0001", "Tarea T-0001: nuevo")

        assert resultado["realizado"] is False
        assert "tarea/T-0001" in resultado["motivo"]

        # 3. En la rama de la tarea sí, y sólo con la ficha.
        _git(raiz, "checkout", "-b", "tarea/T-0001")

        (raiz / "codigo_suelto.py").write_text("x = 1\n", encoding="utf-8")

        resultado = git.commit_ficha(
            "T-0001", "Tarea T-0001: requiere revision"
        )

        assert resultado["realizado"] is True
        assert resultado["rama"] == "tarea/T-0001"

        archivos = _git(
            raiz, "show", "--name-only", "--format=", "HEAD"
        ).stdout.split()

        assert archivos == ["orquestacion/tareas/T-0001.json"]

        # El código suelto NO fue arrastrado por el commit automático.
        pendiente = _git(raiz, "status", "--porcelain", "-uall").stdout

        assert "codigo_suelto.py" in pendiente

        # 4. Resolver una decisión NO es una transición: no genera commit.
        nucleo.crear(
            raiz, "T-0002",
            titulo="Tarea con duda",
            ambito_archivos=["modulos/otro.py"],
            pruebas_requeridas=["pruebas/demostracion/prueba_verde.py"],
            decisiones=[{"clave": "D-1", "descripcion": "Duda."}],
        )

        _git(raiz, "checkout", "-b", "tarea/T-0002")
        _git(raiz, "add", "--", "orquestacion/tareas/T-0002.json")
        _git(raiz, "commit", "-m", "ficha inicial")

        antes = _git(raiz, "rev-parse", "HEAD").stdout.strip()

        nucleo.decidir(raiz, "T-0002", "D-1", "Resuelto a mano.")

        despues = _git(raiz, "rev-parse", "HEAD").stdout.strip()

        assert antes == despues, (
            "decidir() creó un commit automático sin haber transicionado."
        )

        # El cambio está en el disco, esperando decisión del humano.
        assert "T-0002.json" in _git(
            raiz, "status", "--porcelain"
        ).stdout

    finally:
        borrar(raiz)


# ----------------------------------------------------------------------
# Ejecución
# ----------------------------------------------------------------------

COMPROBACIONES = [
    ("creación y lectura de ficha", prueba_creacion_y_lectura),
    ("escritura atómica", prueba_escritura_atomica),
    ("transición válida", prueba_transicion_valida),
    ("transición inválida", prueba_transicion_invalida),
    ("solapamiento de ámbitos", prueba_solapamiento_de_ambitos),
    ("corredor detecta FALLO real", prueba_corredor_detecta_fallo_real),
    ("corredor detecta prueba sin marca",
     prueba_corredor_detecta_prueba_sin_marca),
    ("corredor detecta tiempo agotado",
     prueba_corredor_detecta_tiempo_agotado),
    ("corredor no depende de la carpeta actual",
     prueba_corredor_no_depende_de_la_carpeta_actual),
    ("pruebas requeridas ausentes", prueba_pruebas_requeridas_ausentes),
    ("sin prueba propia no se propone", prueba_sin_prueba_propia_no_se_propone),
    ("camino verde llega a propuesto", prueba_camino_verde_llega_a_propuesto),
    ("imposibilidad de autoaprobar", prueba_imposible_autoaprobar),
    ("límite de intentos y bloqueo", prueba_limite_de_intentos_y_bloqueo),
    ("reabrir reinicia los intentos", prueba_reabrir_reinicia_los_intentos),
    ("bloqueo explícito", prueba_bloqueo_explicito),
    ("ámbito inválido se rechaza", prueba_ambito_invalido_se_rechaza),
    ("ámbito retenido tras requerir revisión",
     prueba_ambito_retenido_tras_requerir_revision),
    ("decisión humana frena la propuesta",
     prueba_decision_humana_frena_la_propuesta),
    ("reanudar ejecución huérfana", prueba_reanudar_ejecucion_huerfana),
    ("reanudar respeta tarea viva", prueba_reanudar_respeta_tarea_viva),
    ("reanudar respeta un mandato breve",
     prueba_reanudar_respeta_mandato_breve),
    ("reanudar detecta latido vencido",
     prueba_reanudar_detecta_latido_vencido),
    ("reanudar detecta ficha inconsistente",
     prueba_reanudar_detecta_ficha_inconsistente),
    ("reanudar limpia temporales abandonados",
     prueba_reanudar_limpia_temporales_abandonados),
    ("ficha corrupta no derriba el tablero",
     prueba_ficha_corrupta_no_derriba_el_tablero),
    ("ficha mal codificada no derriba el tablero",
     prueba_ficha_mal_codificada_no_derriba_el_tablero),
    ("ficha con identificador cruzado",
     prueba_ficha_con_identificador_cruzado),
    ("json ajeno no paraliza el Supervisor",
     prueba_json_ajeno_no_paraliza_el_supervisor),
    ("el tablero publica valores reales",
     prueba_tablero_publica_valores_reales),
    ("commit automático limitado", prueba_commit_automatico_limitado),
]


def prueba_supervisor():
    for numero, (nombre, comprobacion) in enumerate(COMPROBACIONES, start=1):
        comprobacion()
        print("  " + str(numero).rjust(2) + ". " + nombre + ": OK")

    print("PRUEBA_SUPERVISOR=OK")


if __name__ == "__main__":
    prueba_supervisor()
