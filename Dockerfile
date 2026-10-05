FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip \
    && python -m pip install '.[cloud]' \
    && python -c 'import niji.cloud_api; import niji.cloud_worker'

EXPOSE 8080
CMD ["python", "-m", "niji.cloud_api"]
