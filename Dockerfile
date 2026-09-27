FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-tur \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY docflow ./docflow

ENV DOCFLOW_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "docflow.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
