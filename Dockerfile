# A small Linux image with Python 3.13
FROM python:3.13-slim

# LightGBM needs this system library on Linux (it's the Linux version of libomp)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install packages first, so Docker can reuse this layer when only code changes
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

# Copy only what the API needs
COPY src/ src/
COPY api/ api/
COPY models/ models/

EXPOSE 8000

# 0.0.0.0 = accept connections from outside the container.
# Hosting services tell the app which port to use through $PORT; locally it falls back to 8000.
CMD uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}