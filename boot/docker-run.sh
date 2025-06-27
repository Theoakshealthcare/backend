#!/bin/bash

source /opt/venv/bin/activate

cd /app
RUN_PORT=${PORT:-8000}
RUN_HOST=${HOST:-0.0.0.0}

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
while ! nc -z ${POSTGRES_SERVER:-db} 5432; do
  sleep 0.2
done
echo "PostgreSQL is up!"

# Run Alembic migrations
echo "Running Alembic migrations..."
cd /app
if [ -f "app/alembic.ini" ]; then
  alembic -c app/alembic.ini upgrade head
else
  echo "❌ alembic.ini not found inside app/. Skipping migrations."
fi

# Start server
if [ "${ENVIRONMENT}" = "development" ]; then
  echo "Starting FastAPI with Uvicorn (dev)..."
  exec uvicorn app.main:app --host $RUN_HOST --port $RUN_PORT --reload
else
  echo "Starting FastAPI with Gunicorn (prod)..."
  exec gunicorn -k uvicorn.workers.UvicornWorker -b $RUN_HOST:$RUN_PORT app.main:app
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