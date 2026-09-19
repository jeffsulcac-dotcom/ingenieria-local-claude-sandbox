from dataclasses import dataclass
from enum import StrEnum
from math import ceil


NORMA = "RNE E.060 Concreto Armado - DS N.° 010-2009"
REFERENCIA_PERALTE = "E.060 9.6.2 - Tabla 9.1"
REFERENCIA_SISMICA = "E.060 21.5.1"


class CondicionApoyo(StrEnum):
    SIMPLEMENTE_APOYADA = "simplemente_apoyada"
    UN_EXTREMO_CONTINUO = "un_extremo_continuo"
    AMBOS_EXTREMOS_CONTINUOS = "ambos_extremos_continuos"
    VOLADIZO = "voladizo"


DIVISORES_PERALTE = {
    CondicionApoyo.SIMPLEMENTE_APOYADA: 16.0,
    CondicionApoyo.UN_EXTREMO_CONTINUO: 18.5,
    CondicionApoyo.AMBOS_EXTREMOS_CONTINUOS: 21.0,
    CondicionApoyo.VOLADIZO: 8.0,
}


@dataclass(slots=True)
class ResultadoVigaRapida:
    luz_m: float
    condicion_apoyo: CondicionApoyo
    fy_mpa: float

    peralte_minimo_m: float
    peralte_adoptado_m: float

    sistema_sismorresistente: bool

    ancho_minimo_sismico_m: float | None
    cumple_relacion_luz_peralte: bool | None

    norma: str
    referencia_peralte: str
    referencia_sismica: str | None

    advertencia: str


def redondear_hacia_arriba(
    valor: float,
    paso: float = 0.05,
) -> float:
    if valor <= 0:
        raise ValueError("El valor debe ser mayor que cero.")

    return round(
        ceil((valor - 1e-12) / paso) * paso,
        3,
    )


def predimensionar_viga_rapida(
    luz_m: float,
    condicion_apoyo: CondicionApoyo,
    fy_mpa: float = 420.0,
    sistema_sismorresistente: bool = False,
) -> ResultadoVigaRapida:

    if luz_m <= 0:
        raise ValueError(
            "La luz debe ser mayor que cero."
        )

    if fy_mpa <= 0:
        raise ValueError(
            "fy debe ser mayor que cero."
        )

    divisor = DIVISORES_PERALTE[condicion_apoyo]

    # E.060 Tabla 9.1:
    # Para fy diferente de 420 MPa,
    # los valores se multiplican por:
    # (0.4 + fy / 700)
    factor_fy = 0.4 + fy_mpa / 700.0

    peralte_minimo = (
        luz_m / divisor
    ) * factor_fy

    peralte_adoptado = redondear_hacia_arriba(
        peralte_minimo,
        0.05,
    )

    ancho_minimo_sismico = None
    cumple_relacion = None
    referencia_sismica = None

    if sistema_sismorresistente:

        # E.060 21.5.1.3
        ancho_minimo_sismico = max(
            0.25,
            0.25 * peralte_adoptado,
        )

        ancho_minimo_sismico = redondear_hacia_arriba(
            ancho_minimo_sismico,
            0.05,
        )

        # E.060 21.5.1.2
        cumple_relacion = (
            luz_m >= 4.0 * peralte_adoptado
        )

        referencia_sismica = REFERENCIA_SISMICA

    return ResultadoVigaRapida(
        luz_m=round(luz_m, 3),
        condicion_apoyo=condicion_apoyo,
        fy_mpa=round(fy_mpa, 1),

        peralte_minimo_m=round(
            peralte_minimo,
            3,
        ),

        peralte_adoptado_m=peralte_adoptado,

        sistema_sismorresistente=
            sistema_sismorresistente,

        ancho_minimo_sismico_m=
            ancho_minimo_sismico,

        cumple_relacion_luz_peralte=
            cumple_relacion,

        norma=NORMA,
        referencia_peralte=
            REFERENCIA_PERALTE,

        referencia_sismica=
            referencia_sismica,

        advertencia=(
            "Predimensionamiento preliminar. "
            "La sección definitiva requiere análisis "
            "y diseño estructural."
        ),
    )
