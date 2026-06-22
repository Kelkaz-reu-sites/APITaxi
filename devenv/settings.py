DEBUG = True
TESTING = False

SECRET_KEY = 'local-dev-only'
SECURITY_PASSWORD_SALT = 'local-dev-only'

SQLALCHEMY_DATABASE_URI = 'postgresql+psycopg2://apitaxi:apitaxi@taxi-postgres:5432/apitaxi'
REDIS_URL = 'redis://taxi-redis:6379/0'
CELERY_BROKER_URL = 'redis://taxi-redis:6379/1'
CELERY_RESULT_BACKEND = 'redis://taxi-redis:6379/2'

SECURITY_PASSWORD_HASH = 'bcrypt'
CONSOLE_URL = 'http://localhost:3000'
SWAGGER_URL = 'http://localhost:5000'
NEUTRAL_OPERATOR = True
