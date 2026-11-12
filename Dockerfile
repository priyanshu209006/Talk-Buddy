# Microsoft's Playwright image: Python + Chromium + every system library Chromium needs.
# Its version MUST match playwright in requirements.txt (1.64.1, the newest image published).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HEADLESS=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    HOME=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data /app/logs && chmod -R a+rX /app && chmod -R 777 /app/data /app/logs

EXPOSE 7860

# Render injects $PORT (overriding 7860); Hugging Face Spaces defaults to 7860.
CMD ["python", "-X", "utf8", "server.py"]
