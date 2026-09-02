ARG MUYE_RUNTIME_BASE_IMAGE
FROM ${MUYE_RUNTIME_BASE_IMAGE}

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY knowledge_agent_runtime/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home runtime
COPY contracts ./contracts
COPY knowledge_agent_runtime ./knowledge_agent_runtime
USER 10001
ENTRYPOINT ["python", "-m", "knowledge_agent_runtime.main"]
