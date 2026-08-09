FROM python:3.12-slim

WORKDIR /app

# Install librosa dependencies (for spectrum analysis)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 ffmpeg && \
    pip install --no-cache-dir librosa numpy matplotlib && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY . .
RUN mkdir -p server/data

EXPOSE 9090
CMD ["python3", "server/eryu.py"]
