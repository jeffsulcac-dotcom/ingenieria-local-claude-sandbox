from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from .estados import Estado


def nuevo_id() -> str:
    return str(uuid4())


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Proyecto:
    nombre: str
    id: str = field(default_factory=nuevo_id)
    estado: Estado = Estado.NUEVO
    creado_en: datetime = field(default_factory=ahora_utc)


@dataclass(slots=True)
class Modulo:
    nombre: str
    id: str = field(default_factory=nuevo_id)
    estado: Estado = Estado.NUEVO


@dataclass(slots=True)
class Tarea:
    titulo: str
    modulo: str
    id: str = field(default_factory=nuevo_id)
    estado: Estado = Estado.NUEVO
    creado_en: datetime = field(default_factory=ahora_utc)


@dataclass(slots=True)
class Observacion:
    descripcion: str
    severidad: str
    id: str = field(default_factory=nuevo_id)
    estado: Estado = Estado.NUEVO


@dataclass(slots=True)
class Aprobacion:
    objeto_id: str
    aprobado: bool
    comentario: str = ""
    id: str = field(default_factory=nuevo_id)
    creado_en: datetime = field(default_factory=ahora_utc)
