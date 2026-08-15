ARG MUYE_AGENT_BASE_IMAGE
FROM ${MUYE_AGENT_BASE_IMAGE}

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
USER 10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
