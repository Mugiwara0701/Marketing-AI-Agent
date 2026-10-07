# Pipeline control API. Build from the repo root: docker build -f api/Dockerfile -t pipeline-api .
FROM python:3.12-slim
WORKDIR /srv
COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
COPY api/ api/
RUN useradd --system --uid 10001 app
USER app
ENV PORT=8000
EXPOSE 8000
# PORT: set by Render (and most hosts). Proxy headers: TLS ends at the host's proxy, so trust its X-Forwarded-*.
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
