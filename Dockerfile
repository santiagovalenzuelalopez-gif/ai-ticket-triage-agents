# ---- Build ----
FROM python:3.11-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Por defecto solo el núcleo (demo). Producción: --build-arg REQUIREMENTS=requirements-gcp.txt
ARG REQUIREMENTS=requirements.txt
COPY requirements.txt requirements-gcp.txt ./
RUN pip install --no-cache-dir --user -r ${REQUIREMENTS}

# ---- Runtime ----
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/home/appuser/.local/bin:$PATH PORT=8080
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app
COPY --from=builder /root/.local /home/appuser/.local
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser data ./data
COPY --chown=appuser:appuser scripts ./scripts
RUN mkdir -p data/logs && chown appuser:appuser data/logs
USER appuser
# Una imagen, tres desplegables: SERVICE_ROLE = all | orchestrator | vision | logs.
# Con LOGS_BACKEND=local se genera un log de demo; con ssh no se toca nada.
# Forma exec envolviendo `sh -c`: el shell expande $PORT y $LOGS_BACKEND, sin la ambigüedad de un CMD que empieza por "[" y no es JSON.
CMD ["sh", "-c", "[ \"$LOGS_BACKEND\" = ssh ] || python scripts/generate_demo_logs.py; exec gunicorn --bind :$PORT --workers 1 --worker-class uvicorn.workers.UvicornWorker --timeout 0 app.main:app"]
