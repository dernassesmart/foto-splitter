FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev libpng-dev libtiff-dev libgl1 libglib2.0-0 git wget \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch CPU first (smaller than GPU version)
RUN pip install --no-cache-dir \
    torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install other requirements
COPY requirements.txt .
RUN grep -v torch requirements.txt | pip install --no-cache-dir -r /dev/stdin

# Install MobileSAM
RUN pip install --no-cache-dir timm && \
    pip install --no-cache-dir git+https://github.com/ChaoningZhang/MobileSAM.git

# Download MobileSAM weights (~40MB)
RUN mkdir -p /app/models && \
    wget -q -O /app/models/mobile_sam.pt \
    https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt && \
    echo "Model downloaded: $(du -sh /app/models/mobile_sam.pt)"

COPY src/ .

RUN mkdir -p /data/input /data/output /data/pending

ENV INPUT_DIR=/data/input \
    OUTPUT_DIR=/data/output \
    PENDING_DIR=/data/pending \
    SAM_MODEL=/app/models/mobile_sam.pt \
    PROMINENCE=20 \
    PADDING=0 \
    ROTATION=0

EXPOSE 8080

CMD ["python", "app.py"]
