FROM python:3.11-slim

# Build deps for prophet (compiles cmdstan headers) and healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so the layer caches across code-only changes
COPY FEWS_Price_data/dashboard/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy only the FEWS price subproject (other subprojects are excluded via .dockerignore)
COPY FEWS_Price_data /app/FEWS_Price_data

# Cloud Run injects PORT; default to 8080 for local docker run
ENV PORT=8080 \
    FEWS_DB_PATH=/tmp/fews_haiti.duckdb \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD streamlit run /app/FEWS_Price_data/dashboard/app.py \
    --server.port=${PORT} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --server.enableCORS=false \
    --server.enableXsrfProtection=false
