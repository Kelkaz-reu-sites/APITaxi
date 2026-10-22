##### DEV API IMAGE #####

# Official release archive, hash verified before any uv network operation.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS uv-tools
ADD --checksum=sha256:2b428396f12572d4429cf210ca234dd599a33b3af0958a4d5ed569d63e3f344f https://github.com/astral-sh/uv/releases/download/0.13.4/uv-x86_64-unknown-linux-gnu.tar.gz /tmp/uv.tar.gz
RUN tar -xzf /tmp/uv.tar.gz --strip-components=1 -C /usr/local/bin \
 && rm /tmp/uv.tar.gz && uv --version

# This Timescale image uses Ubuntu 22.04. Install Python 3.12 explicitly below;
# its distribution Python 3.10 has reached end of support.
FROM timescale/timescaledb-ha:pg17.11-ts2.31.0-all@sha256:26e355e055c97d0a51afc415b71fa728dd85664fb24165d595cc8cd0c485b2fc AS base-devenv

ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true

USER root
RUN apt-get update && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates
RUN apt-get install -y gpgv
RUN apt-get install -y \
  git \
  less \
  gcc \
  libffi-dev \
  libgeos-dev \
  libpq-dev \
  python3-dev \
  python3-pip \
  redis-server \
  sudo \
  vim

COPY --from=uv-tools /usr/local/bin/uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
ENV UV_PYTHON_BIN_DIR=/usr/local/bin
COPY deploy/check_ca_trust.py deploy/refresh_ensurepip.py /opt/rezo-ca/
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/pip-26.2.2-py3-none-any.whl
RUN uv python install --default 3.12 \
 && python3 -c "import sys, tomllib; assert sys.version_info[:2] == (3, 12)" \
 && python3 -m pip install --break-system-packages --no-index --no-deps /tmp/pip-26.2.2-py3-none-any.whl \
 && python3 -B /opt/rezo-ca/refresh_ensurepip.py /tmp/pip-26.2.2-py3-none-any.whl \
 && rm /tmp/pip-26.2.2-py3-none-any.whl \
 && python3 -B /opt/rezo-ca/check_ca_trust.py
# tox/virtualenv must not seed their independently cached older pip wheels.
ENV VIRTUALENV_PIP=26.2.2



##### DEV TEST IMAGE #####

FROM base-devenv AS test-devenv

RUN uv venv --python 3.12 /opt/test-tools \
 && uv pip install --python /opt/test-tools/bin/python 'tox==4.65.0'
ENV PATH=/opt/test-tools/bin:$PATH

USER postgres

ENTRYPOINT ["/usr/bin/bash"]


#### DEV API IMAGE #####

FROM base-devenv AS devenv

RUN apt-get install -y python3-venv

WORKDIR /git/APITaxi

ADD devenv/entrypoint.sh /
ENTRYPOINT ["/entrypoint.sh"]

# needed to run "sudo -E /venv/bin/pip install"
RUN echo "postgres ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# timescaledb-ha already creates a user with uid 1000, and the development environment
# assumes you are also uid 1000 so both can read and write code
# we used to create a user "api", now it's "postgres"
USER postgres
ENV VIRTUAL_ENV=/venv
ENV PATH=/venv/bin/:$PATH
ENV HOME=/tmp

ENV APITAXI_CONFIG_FILE=/settings.py
# `flask shell` and flask commands like `flask create_user` need FLASK_APP to be set.
ENV FLASK_APP=APITaxi

CMD ["gunicorn", "--config", "deploy/conf/gunicorn.conf.py", "--reload", "APITaxi:create_app()"]


##### DEV WORKER IMAGE #####

FROM devenv AS worker-devenv

USER root
RUN useradd worker
RUN echo "worker ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER worker
ENV VIRTUAL_ENV=/venv
ENV PATH=/venv/bin/:$PATH
ENV APITAXI_CONFIG_FILE=/settings.py

# If we use the square bracket CMD style, process doesn't auto-reload on code change.
# The simple CMD format is used on purpose, until we understand why CMD [...] doesn't work.
#
# --debug-force-polling with an interval would be required for Mac M1, see https://github.com/gorakhargosh/watchdog/issues/838
CMD watchmedo auto-restart --directory=/git/ --pattern='*.py' --recursive -- celery --app=APITaxi2.celery_worker worker -E -c 1


##### DEV WORKER BEAT IMAGE #####

FROM worker-devenv AS worker-beat-devenv

# Same comment as above for Mac M1
CMD watchmedo auto-restart --directory=/git/ --pattern='*.py' --recursive -- celery --app=APITaxi2.celery_worker beat -s /tmp/celerybeat-schedule --pidfile /tmp/celerybeat.pid


##### PROD DEPENDENCY BUILD IMAGE #####
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS runtime-dependencies

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates \
  libpq-dev \
  python3-pip \
  python3-venv \
  libgeos-dev \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/pip-26.2.2-py3-none-any.whl
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-index --no-deps /tmp/pip-26.2.2-py3-none-any.whl \
 && rm /tmp/pip-26.2.2-py3-none-any.whl \
 && /venv/bin/pip install --no-cache-dir 'setuptools==80.10.2'
COPY requirements.txt requirements.lock /build/
RUN /venv/bin/pip install --no-cache-dir --require-hashes -r /build/requirements.lock \
 && /venv/bin/pip check \
 && /venv/bin/pip uninstall -y pip setuptools \
 && /venv/bin/python -c "import importlib.util; assert importlib.util.find_spec('pip') is None; assert importlib.util.find_spec('setuptools') is None; import celery, flask_security, passlib.context, psycopg2, shapely"


##### PROD RUNTIME IMAGE #####
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true
ENV PYTHONUNBUFFERED=1

# Keep only the interpreter and shared libraries in the deployed image. The
# build stage owns pip and development headers, including linux-libc-dev.
RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates \
  libpq5 \
  python3 \
  libgeos-c1t64 \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=runtime-dependencies /venv /venv

RUN useradd --system --create-home --home-dir /home/api --shell /usr/sbin/nologin api

# Required by click with Python3, cf https://click.palletsprojects.com/python3/
ENV LC_ALL=C.UTF-8

# `flask shell` and flask commands like `flask create_user` need FLASK_APP to be set.
ENV FLASK_APP=APITaxi
ENV VIRTUAL_ENV=/venv
ENV PATH=/venv/bin/:$PATH

RUN mkdir -p /var/run/api-taxi
RUN chown api:api /var/run/api-taxi

WORKDIR /app

# Application source code
COPY . /app
RUN chown -R api:api /app /home/api \
 && /venv/bin/python -B deploy/check_ca_trust.py \
 && /venv/bin/python -B deploy/check_runtime.py

USER api


##### PROD WORKER IMAGE #####
FROM runtime AS worker

CMD ["celery", "--app=APITaxi2.celery_worker", "worker", "--pidfile=/tmp/celery.pid", "-n", "api_taxi_worker@%h", "-E"]


##### PROD WORKER BEAT IMAGE #####
FROM runtime AS beat

CMD ["celery", "--app=APITaxi2.celery_worker", "beat", "--pidfile=/tmp/celery-beat.pid", "-s", "/tmp/celerybeat-schedule"]


##### PROD WEB IMAGE #####
FROM runtime AS web

EXPOSE 5000
CMD ["gunicorn", "--config", "/app/deploy/conf/gunicorn.conf.py", "APITaxi:create_app()"]
