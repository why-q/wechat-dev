FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/rag-poc \
    RAG_INDEX_DIR=/app/rag-poc/index \
    RAG_CORPUS_DIR=/app/rag-poc/corpus \
    RAG_NLTK_DATA_DIR=/tmp/wechat-dev-test/nltk_data \
    PORT=80

WORKDIR /app

COPY rag-poc/pyproject.toml rag-poc/uv.lock /app/rag-poc/
RUN python -m pip install --no-cache-dir --disable-pip-version-check uv==0.10.12 \
    && uv sync --project /app/rag-poc --locked --no-dev \
    && rm -rf /root/.cache /tmp/*

COPY app.py /app/app.py
COPY rag-poc/rag_poc /app/rag-poc/rag_poc
COPY rag-poc/corpus /app/rag-poc/corpus
COPY rag-poc/index /app/rag-poc/index

EXPOSE 80

CMD ["/bin/sh", "-c", "exec /app/rag-poc/.venv/bin/python -m uvicorn app:app --app-dir /app --host 0.0.0.0 --port ${PORT:-80} --no-access-log --no-server-header"]
