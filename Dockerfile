FROM node:24-slim AS web-build

WORKDIR /opt/mghands/web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS gateway-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/mghands/src

WORKDIR /opt/mghands

RUN apt-get -o Acquire::AllowInsecureRepositories=true -o Acquire::AllowDowngradeToInsecureRepositories=true update \
    && apt-get install -y --no-install-recommends --allow-unauthenticated ca-certificates curl docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY --from=web-build /opt/mghands/web/dist ./web/dist

RUN python -m pip install --no-cache-dir \
    'bcrypt>=4.1.0' \
    'fastapi>=0.115.0' \
    'httpx>=0.27.0' \
    'pydantic-settings>=2.5.0' \
    'python-multipart>=0.0.9' \
    'uvicorn[standard]>=0.35.0'

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "mghands_gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
