FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install "mistralai>=3.0" "mistralai-workflows>=3.10" "pydantic>=2.0"

COPY . .

CMD ["python", "src/entrypoints/worker.py"]
