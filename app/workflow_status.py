AUTOMATIC_PAYMENT_STATUSES = frozenset({"ready", "creating", "unknown"})


def payment_is_automatic(status):
    """Вернуть True, если платёж безопасно обрабатывается без участия человека."""
    return status in AUTOMATIC_PAYMENT_STATUSES
