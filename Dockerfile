FROM python:3.12-slim

# Install ffmpeg and timezone data
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app/ ./app/
COPY static/ ./static/

# Data directory (mount a volume here)
RUN mkdir -p /data
ENV DATA_DIR=/data
# Default timezone — override at runtime: docker run -e TZ=Europe/London ...
ENV TZ=America/New_York

EXPOSE 8088

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
