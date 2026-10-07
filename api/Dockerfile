# Pipeline control API. Build from the repo root: docker build -f api/Dockerfile -t pipeline-api .
FROM python:3.12-slim
WORKDIR /srv
COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
COPY api/ api/
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
