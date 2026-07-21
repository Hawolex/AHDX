FROM python:3.12-slim

WORKDIR /app

# Install deps first so this layer caches when only the code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All state lives here, mounted as a volume from the host.
ENV AHDX_DATA=/data
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "app.py"]
