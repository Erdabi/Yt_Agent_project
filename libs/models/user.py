from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """An operator account for the internal admin dashboard (not
    customer-facing) — used from Phase 2 onward for approve/reject actions.
    Defined now so the schema and migrations don't have to change shape
    later just to add authentication.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
