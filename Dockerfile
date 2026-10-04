FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 tierbot \
    && useradd --uid 10001 --gid tierbot --no-create-home tierbot \
    && mkdir /data \
    && chown tierbot:tierbot /data

COPY tierbot ./tierbot
USER tierbot
CMD ["python", "-m", "tierbot"]
