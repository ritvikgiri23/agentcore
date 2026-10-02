from pydantic import BaseModel

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
# Postgres OFFSET is a bigint.
MAX_PAGE_OFFSET = 2**63 - 1


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
