# Match the project's Python minor version
FROM python:3.14-slim

WORKDIR /app

# Non-sensitive settings
ENV PYTHONUNBUFFERED=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	PORT=8000

# Git for GitPython/DagsHub and certificates for HTTPS
RUN apt-get update \
	&& apt-get install -y --no-install-recommends git ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

# Copy application files according to .dockerignore
# Copying first also supports "-e ." in requirements.txt
COPY . .

# Install project dependencies
RUN python -m pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

# Start FastAPI using the configured port
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
