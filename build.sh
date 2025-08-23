#!/bin/bash

set -e

echo "🐳 Building Oaklycare Backend Docker Image..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Creating from .env.example if it exists..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✅ Created .env from .env.example"
        echo "⚠️  Please update .env with your actual values before running in production"
    else
        echo "❌ No .env.example found. Please create a .env file with your configuration."
        exit 1
    fi
fi

# Build the Docker image
echo "🔨 Building Docker image..."
docker build -t oaklycare-backend .

echo "✅ Build completed successfully!"

echo ""
echo "🚀 To run the application:"
echo "   docker-compose up"
echo ""
echo "🚀 To run in detached mode:"
echo "   docker-compose up -d"
echo ""
echo "🚀 To view logs:"
echo "   docker-compose logs -f"
echo ""
echo "🚀 To stop the application:"
echo "   docker-compose down" 