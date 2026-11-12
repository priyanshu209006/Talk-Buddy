# Microsoft's Playwright image: Python + Chromium + every system library Chromium needs.
# Its version MUST match playwright in requirements.txt (1.64.1, the newest image published).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HEADLESS=1 \
    HOST=0.0.0.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects $PORT; server.py reads it.
CMD ["python", "-X", "utf8", "server.py"]
