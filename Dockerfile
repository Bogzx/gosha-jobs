FROM python:3.11-slim

# openssh-client is needed for SSH SOCKS5 tunnels
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY requirements.txt constraints.txt ./
# Generous timeout/retries: torch (sentence-transformers) is a large wheel.
# constraints.txt pins every transitive version so a rebuild ships what CI
# tested instead of whatever PyPI resolves to that day.
RUN pip install --no-cache-dir --timeout 180 --retries 8 \
      -r requirements.txt -c constraints.txt

COPY . .

# Ensure data + model-cache directories exist and are owned by app user
# (the hf_cache volume inherits this ownership on first mount)
RUN mkdir -p /app/data /home/app/.cache && chown -R app:app /app/data /home/app/.cache

USER app

# Run the bot (main.py shim delegates to gosha.main)
# Override CMD to run just the web: ["uvicorn", "gosha.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
CMD ["python", "main.py"]
