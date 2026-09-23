FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Cloud Run injects PORT; default to 8080 for local runs.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
