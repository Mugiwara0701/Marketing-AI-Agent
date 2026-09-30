FROM python:3.14-slim
WORKDIR /srv
COPY libs/agentkit /tmp/agentkit
RUN pip install --no-cache-dir /tmp/agentkit
COPY services/outreach-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/outreach-service/app ./app
COPY config ./config
RUN useradd -m app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
