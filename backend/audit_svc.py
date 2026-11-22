"""审计只读面。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def list_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/rejections")
def list_rejections(authorization: AuthHeader = None):
    """出金拒写专面：缺票/废票拒写留痕，载荷含提交票号原值，供五页同源核对。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM audit_trail
               WHERE action IN ('release_reject_no_ticket','release_reject_bad_ticket')
               ORDER BY id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
