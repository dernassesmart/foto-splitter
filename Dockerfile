FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev libpng-dev libtiff-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

# Default data dirs (override via volumes)
RUN mkdir -p /data/input /data/output

ENV INPUT_DIR=/data/input \
    OUTPUT_DIR=/data/output \
    PROMINENCE=46 \
    PADDING=0 \
    ROTATION=0

EXPOSE 8080

CMD ["python", "app.py"]
