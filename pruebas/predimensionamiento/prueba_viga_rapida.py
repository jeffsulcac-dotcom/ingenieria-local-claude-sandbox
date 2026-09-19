import sys
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(
    0,
    str(RAIZ),
)


from modulos.predimensionamiento.vigas import (
    CondicionApoyo,
    predimensionar_viga_rapida,
)


def prueba_viga_rapida():

    # Caso 1:
    # Viga simplemente apoyada de 6.00 m
    resultado = predimensionar_viga_rapida(
        luz_m=6.00,
        condicion_apoyo=
            CondicionApoyo.SIMPLEMENTE_APOYADA,
    )

    assert resultado.peralte_minimo_m == 0.375
    assert resultado.peralte_adoptado_m == 0.40


    # Caso 2:
    # Ambos extremos continuos
    resultado = predimensionar_viga_rapida(
        luz_m=6.00,
        condicion_apoyo=
            CondicionApoyo.AMBOS_EXTREMOS_CONTINUOS,
    )

    assert resultado.peralte_minimo_m == 0.286
    assert resultado.peralte_adoptado_m == 0.30


    # Caso 3:
    # Viga del sistema sismorresistente
    resultado = predimensionar_viga_rapida(
        luz_m=6.00,
        condicion_apoyo=
            CondicionApoyo.SIMPLEMENTE_APOYADA,
        sistema_sismorresistente=True,
    )

    assert resultado.ancho_minimo_sismico_m == 0.25
    assert resultado.cumple_relacion_luz_peralte is True


    print("PRUEBA_VIGA_RAPIDA=OK")


if __name__ == "__main__":
    prueba_viga_rapida()
