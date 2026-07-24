"""Shared declarative base and column mixins for every ORM model.

`Base.metadata` is what `migrations/env.py` points Alembic at for
autogeneration — every model module must be imported (see
`libs/models/__init__.py`) before that metadata is complete.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    """Every table uses a randomly generated UUID primary key, not a serial
    integer — this lets any agent generate an id up front (e.g. to name an
    object-storage key) before the row is ever inserted.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
