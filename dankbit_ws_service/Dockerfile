FROM python:3.11-slim

# Install system deps
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Work dir
WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code
COPY dankbit_ws_batch.py .

# Run script
CMD ["python", "dankbit_ws_batch.py"]
