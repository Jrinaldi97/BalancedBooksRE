from typing import Type, TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import Base

T = TypeVar("T", bound=Base)


def clamp_pagination(skip: int, limit: int, max_limit: int = 1000) -> tuple[int, int]:
    """Normalize list-endpoint paging params: skip >= 0, 1 <= limit <= max_limit."""
    return max(0, skip or 0), max(1, min(limit or max_limit, max_limit))


def get_or_404(db: Session, model: Type[T], obj_id: int, name: str = None) -> T:
    obj = db.query(model).filter(model.id == obj_id).first()
    if not obj:
        label = name or model.__name__
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return obj


def reject_null_updates(model: Type[T], update_data: dict) -> dict:
    """Refuse a PUT that explicitly sets a NOT NULL column to null (422).

    The Update schemas are all-optional and the routes dump them with
    exclude_unset=True, so a field the client sent as ``null`` (but a field
    they omitted does not) survives into ``update_data`` as None. Assigning
    that to a NOT NULL column raised an opaque IntegrityError at commit — an
    HTTP 500 (``PUT /api/customers/{id}`` or ``/vendors/{id}`` with
    ``{"name": null}``). This names the field and answers 422 instead. An
    omitted field never reaches ``update_data``, so an empty ``{}`` PUT is
    still a no-op 200. Generic on the model's columns rather than per-schema,
    so it covers every Update schema the same way. Returns ``update_data`` so
    callers can inline it.
    """
    offenders = sorted(
        c.name
        for c in model.__table__.columns
        if not c.nullable
        and not c.primary_key
        and c.name in update_data
        and update_data[c.name] is None
    )
    if offenders:
        raise HTTPException(
            status_code=422,
            detail=f"{', '.join(offenders)} may not be set to null.",
        )
    return update_data
