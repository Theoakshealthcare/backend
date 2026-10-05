#!/bin/bash

set -e  # Exit on any error

source /app/.venv/bin/activate

cd /app
RUN_PORT=${PORT:-8000}
RUN_HOST=${HOST:-0.0.0.0}

# Function to check database connectivity
check_database() {
    echo "Checking database connectivity..."
    
    # Extract database host from DATABASE_URL
    if [[ -n "$DATABASE_URL" ]]; then
        # Parse DATABASE_URL to get host and port
        if [[ $DATABASE_URL =~ postgresql://[^:]+:[^@]+@([^:]+):([^/]+) ]]; then
            DB_HOST=${BASH_REMATCH[1]}
            DB_PORT=${BASH_REMATCH[2]}
        else
            echo "❌ Could not parse DATABASE_URL"
            return 1
        fi
    else
        DB_HOST=${POSTGRES_SERVER:-db}
        DB_PORT=${POSTGRES_PORT:-5432}
    fi
    
    echo "Testing connection to $DB_HOST:$DB_PORT..."
    
    # Wait for database to be ready
    local max_attempts=30
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if nc -z "$DB_HOST" "$DB_PORT" 2>/dev/null; then
            echo "✅ Database is ready!"
            return 0
        else
            echo "⏳ Waiting for database... (attempt $attempt/$max_attempts)"
            sleep 2
            attempt=$((attempt + 1))
        fi
    done
    
    echo "❌ Database connection failed after $max_attempts attempts"
    return 1
}

# Check database connectivity
if ! check_database; then
    echo "❌ Database health check failed. Exiting..."
    exit 1
fi

# Run Alembic migrations
echo "Running Alembic migrations..."
cd /app

# Check if alembic.ini exists in the correct location
if [ -f "alembic.ini" ]; then
    if [ "${ENVIRONMENT}" = "development" ]; then
        # Dev only: bootstrap an empty DB from the models (no-op if tables exist).
        python -m scripts.init_local_db
    fi
    echo "✅ Found alembic.ini, running migrations..."
    alembic upgrade head
    echo "✅ Migrations completed successfully"
else
    echo "❌ alembic.ini not found. Skipping migrations."
    echo "Current directory: $(pwd)"
    echo "Directory contents:"
    ls -la
fi

# Start server
echo "Starting FastAPI server..."
if [ "${ENVIRONMENT}" = "development" ]; then
    echo "🚀 Starting FastAPI with Uvicorn (development mode)..."
    exec uvicorn app.main:app --host $RUN_HOST --port $RUN_PORT --reload --log-level info
else
    echo "🚀 Starting FastAPI with Gunicorn (production mode)..."
    exec gunicorn \
        --worker-class uvicorn.workers.UvicornWorker \
        --bind $RUN_HOST:$RUN_PORT \
        --workers 4 \
        --timeout 120 \
        --keep-alive 2 \
        --max-requests 1000 \
        --max-requests-jitter 100 \
        app.main:app
fi

# uuncomment below to se in production
# exec gunicorn -k uvicorn.workers.UvicornWorker -b $RUN_HOST:$RUN_PORT app.main:app


# For debugging purpose. If needed, should be placed before the `gunicorn` command
# Debug information
# echo "==== DEBUGGING INFORMATION ===="
# echo "Current directory: $(pwd)"
# echo "Directory contents:"
# ls -la
# echo "Python path:"
# python -c "import sys; print(sys.path)"
# echo "Looking for 'app' module:"
# python -c "import pkgutil; print([p for p in pkgutil.iter_modules() if p.name == 'app'])"
# echo "==== END DEBUGGING ===="