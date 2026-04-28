FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tools used by healthchecks (curl) and process detection (procps -> pgrep)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl procps \
 && rm -rf /var/lib/apt/lists/*

# uv via PyPI (ghcr.io unreachable from some networks)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml ./
RUN uv pip install --system --no-cache --requirement pyproject.toml

# Source (overridden by bind mount in dev compose)
COPY . .

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
