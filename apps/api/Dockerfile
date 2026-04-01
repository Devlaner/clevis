FROM python:3.12-slim
WORKDIR /app
COPY apps/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/api/src ./src
COPY packages/checks /app/packages/checks
ENV PYTHONPATH=/app/src:/app/packages/checks/src
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
