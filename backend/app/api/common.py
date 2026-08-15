"""Shared helpers for admin API routers."""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings


async def get_settings_dependency() -> Settings:
    """Async settings dependency (sync deps would run in a thread pool)."""

    return get_settings()


def ok(data: Any = None, msg: str = "ok") -> dict[str, Any]:
    """TECH 5 unified success envelope: {"code":0,"data":...,"msg":...}."""

    return {"code": 0, "data": data, "msg": msg}
