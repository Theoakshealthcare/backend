# syntax=docker/dockerfile:1.4

ARG PYTHON_VERSION=3.11-slim-bullseye
FROM python:${PYTHON_VERSION}

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    netcat-traditional \
    libpq-dev \
    libjpeg-dev \
    libcairo2 \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ./app /app/app

# Copy alembic configuration
COPY ./alembic.ini /app/alembic.ini

# Copy run script
COPY ./boot/docker-run.sh /opt/run.sh
RUN chmod +x /opt/run.sh

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app
USER app

# Expose port
EXPOSE 8000

CMD ["/opt/run.sh"]
