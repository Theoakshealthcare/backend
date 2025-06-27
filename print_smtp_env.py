import os
from pathlib import Path

# Print raw environment variables
print("=== Raw Environment Variables ===")
for key in os.environ:
    if 'SMTP' in key or 'EMAIL' in key:
        print(f"{key} = {os.environ[key]}")

# Try to load from .env as well
if Path('.env').exists():
    print("\n=== .env File Contents ===")
    with open('.env') as f:
        for line in f:
            if 'SMTP' in line or 'EMAIL' in line:
                print(line.strip())

# Print what the app config sees
try:
    from app.core.config import settings
    print("\n=== Settings from app.core.config ===")
    print(f"SMTP_HOST: {settings.SMTP_HOST}")
    print(f"SMTP_PORT: {settings.SMTP_PORT}")
    print(f"SMTP_USER: {settings.SMTP_USER}")
    print(f"SMTP_PASSWORD: {settings.SMTP_PASSWORD}")
    print(f"EMAILS_FROM_EMAIL: {settings.EMAILS_FROM_EMAIL}")
except Exception as e:
    print(f"\nCould not import app.core.config: {e}") 