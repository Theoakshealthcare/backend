# Create a `.env` file from the example:
cp .env.example .env

# Build and start the Docker containers:
docker-compose up -d --build

The API will be available at [http://localhost:8000](http://localhost:8000)
API documentation is available at [http://localhost:8000/docs](http://localhost:8000/docs)

# Run migrations automatically when starting containers
docker-compose up -d

# Or run migrations manually
./scripts/migrate.sh

# Start the development server with hot reload
docker-compose up -d --build

## API Documentation

Once the server is running, you can access the interactive API documentation at:

- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://lo](http://localhost:8000/redoc)

## Testing
# Run tests
docker-compose exec api pytest

!-- If you're using Docker Compose, you can also run migrations directly with: -->
# docker-compose exec api ./scripts/create_migration.sh up