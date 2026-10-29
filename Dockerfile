# Carbon-Aware Kubernetes Batch Scheduler
# Multi-stage build for minimal production image

# Build stage
FROM python:3.12-slim as builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir build

# Copy project files
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY config/ config/

# Build wheel
RUN python -m build --wheel

# Production stage
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Carbon-Aware Kubernetes Batch Scheduler"
LABEL org.opencontainers.image.description="Kubernetes controller that holds back flexible batch workloads and releases them when the grid is greenest"
LABEL org.opencontainers.image.source="https://github.com/mahmudul-islam32/carbon-aware-k8s-scheduler"
LABEL org.opencontainers.image.licenses="MIT"

# Create non-root user
RUN groupadd -r carbon && useradd -r -g carbon carbon

WORKDIR /app

# Copy wheel from builder
COPY --from=builder /app/dist/*.whl ./

# Install the package
RUN pip install --no-cache-dir ./*.whl && rm ./*.whl

# Copy config files
COPY config/ /app/config/

# Set environment variables
ENV CARBON_EMISSION_FACTORS_FILE=/app/config/emission_factors.yaml
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER carbon

# Expose metrics port
EXPOSE 9090

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9090/healthz')" || exit 1

# Run the controller
ENTRYPOINT ["carbon-scheduler"]
