FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -c requirements.lock -r requirements.txt
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY scripts ./scripts
COPY deploy/healthcheck.py ./deploy/healthcheck.py
RUN useradd --uid 10001 --create-home forge && mkdir -p /srv/artifacts /srv/data && chown -R forge:forge /srv
USER forge
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers","--forwarded-allow-ips=*"]

FROM backend AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN playwright install --with-deps chromium && chmod -R a+rX /opt/browsers
USER forge
CMD ["celery","-A","app.worker:celery","worker","-Q","browser","--concurrency=1","--loglevel=warning"]
