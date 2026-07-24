"""
Aktif B2B çekim görevlerini takip eder.
Her site_id için en fazla 1 görev aynı anda çalışabilir.
"""
import asyncio

_running: dict[str, asyncio.Task] = {}


def is_running(site_id: str) -> bool:
    t = _running.get(site_id)
    return bool(t and not t.done())


def register(site_id: str, task: asyncio.Task):
    _running[site_id] = task


def unregister(site_id: str):
    _running.pop(site_id, None)


def cancel(site_id: str) -> bool:
    t = _running.get(site_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


def running_ids() -> list[str]:
    """Şu an aktif çekim yapılan site ID'leri."""
    return [sid for sid, t in _running.items() if not t.done()]
