from enum import StrEnum


class Estado(StrEnum):
    NUEVO = "nuevo"
    ANALIZANDO = "analizando"
    PROPUESTO = "propuesto"
    REQUIERE_REVISION = "requiere_revision"
    APROBADO = "aprobado"
    CONGELADO = "congelado"
    RECHAZADO = "rechazado"
    BLOQUEADO = "bloqueado"
    REABIERTO = "reabierto"
    EN_EJECUCION = "en_ejecucion"
    COMPLETADO = "completado"
    ERROR = "error"
    PAUSADO = "pausado"
