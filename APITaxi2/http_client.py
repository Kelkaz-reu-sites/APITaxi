from flask import current_app


def request_timeout(prefix):
    """Return a requests timeout tuple for the configured external service."""
    connect_timeout = current_app.config.get(
        f'{prefix}_HTTP_CONNECT_TIMEOUT',
        current_app.config['HTTP_CONNECT_TIMEOUT'],
    )
    read_timeout = current_app.config.get(
        f'{prefix}_HTTP_READ_TIMEOUT',
        current_app.config['HTTP_READ_TIMEOUT'],
    )
    return (connect_timeout, read_timeout)
