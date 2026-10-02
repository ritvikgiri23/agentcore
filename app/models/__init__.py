"""ORM models. Importing this package registers every table on `Base.metadata`."""

from app.models.user import User

__all__ = ["User"]
