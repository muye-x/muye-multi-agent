FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY agents/agent-main/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# `contracts` is a repository-owned shared package, not a PyPI dependency.
COPY contracts ./contracts
COPY agents/agent-main ./agent-main

WORKDIR /app/agent-main
CMD ["python", "main.py"]
