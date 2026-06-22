import os


def _int_env(name, default):
    return int(os.getenv(name, default))


def _bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ('1', 'true', 'yes', 'on')


bind = os.getenv('GUNICORN_BIND', '0.0.0.0:5000')
workers = _int_env('GUNICORN_WORKERS', 2)
worker_class = os.getenv('GUNICORN_WORKER_CLASS', 'gthread')
threads = _int_env('GUNICORN_THREADS', 4)

timeout = _int_env('GUNICORN_TIMEOUT', 30)
graceful_timeout = _int_env('GUNICORN_GRACEFUL_TIMEOUT', 30)
keepalive = _int_env('GUNICORN_KEEPALIVE', 5)

reload = _bool_env('GUNICORN_RELOAD', False)
preload_app = _bool_env('GUNICORN_PRELOAD_APP', False)

accesslog = '-'
errorlog = '-'
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')
capture_output = True

# Keep query strings and headers out of access logs. API keys must stay in
# headers, but this also avoids accidentally logging future query credentials.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(D)s'

forwarded_allow_ips = os.getenv('GUNICORN_FORWARDED_ALLOW_IPS', '127.0.0.1')
worker_tmp_dir = os.getenv('GUNICORN_WORKER_TMP_DIR', '/tmp')

# Preserve the historical uWSGI ability to accept long API URLs.
limit_request_line = _int_env('GUNICORN_LIMIT_REQUEST_LINE', 32768)
