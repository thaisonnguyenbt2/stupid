FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Run unbuffered so logs stream instantly
ENV PYTHONUNBUFFERED=1

CMD ["python", "binance_ingest.py"]
