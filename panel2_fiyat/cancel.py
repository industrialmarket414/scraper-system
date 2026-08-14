class ScrapeCancelled(Exception):
    """Raised when a user requests stopping an active price scrape."""


_cancel_requested: set[str] = set()


def request_cancel(site_id: str) -> None:
    _cancel_requested.add(site_id)


def clear_cancel(site_id: str) -> None:
    _cancel_requested.discard(site_id)


def is_cancel_requested(site_id: str) -> bool:
    return site_id in _cancel_requested
