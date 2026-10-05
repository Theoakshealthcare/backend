class NetworkBlocked(RuntimeError):
    """Raised when a test makes a real outbound SMTP or HTTP call."""
