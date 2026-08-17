"""Knowledge base admin APIs (M-15/M-08, TECH 5.5)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import ok
from app.core.exceptions import KnowledgeEmptyError, KnowledgeError, KnowledgeUnsupportedError
from app.db.session import get_db
from app.services.audit import log_action
from app.services.knowledge import MAX_KB_BYTES, KnowledgeService

router = APIRouter(prefix="/api/v1", tags=["knowledge-base"])


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/kb/docs")
async def list_kb_docs(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    docs = KnowledgeService(db).list_docs()
    return ok(
        {
            "items": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "version": d.version,
                    "uploaded_at": _fmt(d.uploaded_at),
                }
                for d in docs
            ]
        }
    )


@router.post("/kb/upload")
async def upload_kb_doc(
    file: UploadFile,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Upload pdf/docx/md (<=20MB); re-uploading the same filename bumps version."""

    data = await file.read(MAX_KB_BYTES + 1)
    if len(data) > MAX_KB_BYTES:
        raise HTTPException(status_code=413, detail="TOO_LARGE")
    try:
        doc = KnowledgeService(db).upload(file.filename or "document", data)
    except KnowledgeUnsupportedError:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_TYPE") from None
    except KnowledgeEmptyError:
        raise HTTPException(status_code=400, detail="EMPTY_CONTENT") from None
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    log_action(
        db,
        "kb_uploaded",
        "kb",
        doc.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"doc_id": doc.id, "version": doc.version})


@router.delete("/kb/docs/{doc_id}")
async def delete_kb_doc(
    doc_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    service = KnowledgeService(db)
    if not service.soft_delete(doc_id):
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    log_action(
        db,
        "kb_deleted",
        "kb",
        doc_id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"doc_id": doc_id})
