FROM python:3.13-alpine

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy scraper source
COPY . .

# Use tini for proper signal handling
RUN apk add --no-cache dumb-init
ENTRYPOINT ["/usr/bin/dumb-init", "--"]

# Prometheus metrics endpoint
EXPOSE 9705

# Entry command -- long-running exporter, not a one-shot script
CMD ["python", "main.py"]
