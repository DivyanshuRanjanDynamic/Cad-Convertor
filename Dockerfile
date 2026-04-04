# ── Stage 1: Base image ───────────────────────────────────────────────────────
# cadquery/cadquery:latest ships with a pre-configured Conda environment 
# named 'cq' (Python 3.8) that contains all native OCCT dependencies.
FROM cadquery/cadquery:latest

# ── Labels ────────────────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="mechhub-cad-converter"
LABEL org.opencontainers.image.description="STEP → STL conversion API — FastAPI + CadQuery/OCCT"
LABEL org.opencontainers.image.vendor="MechHub"
LABEL org.opencontainers.image.version="1.1.0"

# ── Environment ─────────────────────────────────────────────────────────────
# IMPORTANT: We must use the 'cq' conda environment's bin for everything.
# The base image already has this environment ready at /opt/conda/envs/cq.
ENV CONDA_ENV_PATH=/opt/conda/envs/cq
ENV PATH="${CONDA_ENV_PATH}/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── System / Web Dependencies ────────────────────────────────────────────────
# We switch to root only to perform the installation and fix permissions.
USER root

# Install the web layer directly into the 'cq' environment.
# Using 'python -m pip' ensures we use the interpreter in /opt/conda/envs/cq/bin.
RUN python -m pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    python-multipart==0.0.9

# ── Application Setup ────────────────────────────────────────────────────────
WORKDIR /app

# Copy application code and ensure the 'cq' user owns it.
# The base image provides the 'cq' user for secure execution.
COPY --chown=cq:cq main.py .

# Switch to non-privileged user for runtime security.
USER cq

# ── Runtime Configuration ────────────────────────────────────────────────────
# PORT is set by Railway automatically. We default to 8080 for local development.
ENV PORT=8080

# Expose the API port (documentation only, runtime uses environment).
EXPOSE 8080

# Tessellation quality can be tuned via env without rebuilding the image.
ENV CAD_LINEAR_TOLERANCE=0.1
ENV CAD_ANGULAR_TOLERANCE=0.5

# CORS — comma-separated list of allowed origins.
ENV CORS_ORIGINS="https://mechhub.in,https://www.mechhub.in,https://studio.mechhub.in,http://localhost:3000,http://localhost:9002"

# ── Healthcheck ───────────────────────────────────────────────────────────────
# Verify both process liveness and API readiness.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')" || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
# We use the JSON array form (exec form) to ensure signals (SIGTERM) are 
# correctly propagated to the application.
# The /bin/sh -c wrapper is used to perform environment variable expansion for ${PORT}.
CMD ["/bin/sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 2 --loop uvloop --access-log"]
