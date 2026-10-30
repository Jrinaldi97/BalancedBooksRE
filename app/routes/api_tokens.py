# ============================================================================
# API token management — admin sessions only. The middleware additionally
# blocks token principals from this whole prefix (no self-escalation), so
# only a human admin in a browser can mint or revoke tokens.
#
# The full secret appears exactly once, in the create response. No DELETE:
# tokens deactivate, keeping their label meaningful in the audit trail.
# ============================================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from app.schemas.common import StrictModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_tokens import ApiToken
from app.models.users import ROLE_ADMIN, VALID_ROLES
from app.services.api_token_service import generate_token, hash_token

router = APIRouter(prefix="/api/tokens", tags=["api_tokens"])


def _require_admin(request: Request) -> None:
    if (request.session.get("role") or "admin") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")


def _out(t: ApiToken) -> dict:
    return {
        "id": t.id,
        "label": t.label,
        "token_hint": t.token_hint,
        "role": t.role,
        "is_active": t.is_active,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
    }


class TokenCreate(StrictModel):
    label: str = Field(..., min_length=1, max_length=100)
    role: str = Field(...)


class TokenUpdate(StrictModel):
    is_active: Optional[bool] = None
    label: Optional[str] = Field(None, min_length=1, max_length=100)


@router.get("")
def list_tokens(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    return [_out(t) for t in db.query(ApiToken).order_by(ApiToken.id).all()]


@router.post("", status_code=201)
def create_token(payload: TokenCreate, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    label = payload.label.strip()
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    if db.query(ApiToken).filter(ApiToken.label == label).first():
        raise HTTPException(status_code=409, detail="Label already exists")
    secret = generate_token()
    row = ApiToken(
        label=label,
        token_hash=hash_token(secret),
        token_hint=secret[:10],
        role=payload.role,
        is_active=True,
        created_by=request.session.get("username") or "operator",
    )
    db.add(row)
    db.commit()
    out = _out(row)
    # The one and only time the secret leaves the server.
    out["token"] = secret
    return out


@router.put("/{token_id}")
def update_token(
    token_id: int, payload: TokenUpdate, request: Request, db: Session = Depends(get_db)
):
    _require_admin(request)
    row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    if payload.is_active is True and not row.is_active:
        raise HTTPException(
            status_code=409,
            detail="A revoked token cannot be reactivated; create a new token",
        )
    if payload.label is not None:
        label = payload.label.strip()
        clash = (
            db.query(ApiToken)
            .filter(ApiToken.label == label, ApiToken.id != token_id)
            .first()
        )
        if clash:
            raise HTTPException(status_code=409, detail="Label already exists")
        row.label = label
    if payload.is_active is not None:
        row.is_active = payload.is_active
    db.commit()
    return _out(row)
