#!/bin/bash
set -e

# Start the FastAPI application
uvicorn app.main:app --reload