FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
# Keep referenced module requirement files in the image before resolving the
# aggregate requirements file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY control_server/requirements.txt ./control_server/requirements.txt
COPY muye-llm/requirements.txt ./muye-llm/requirements.txt
COPY muye-data/requirements.txt ./muye-data/requirements.txt
COPY agents/agent-main/requirements.txt ./agents/agent-main/requirements.txt
COPY muye-gateway/requirements.txt ./muye-gateway/requirements.txt
COPY muye-channels/requirements.txt ./muye-channels/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
