import os
import sys
import logging

logger = logging.getLogger("config")

def get_env_var(var_name: str, required: bool = True) -> str:
    value = os.getenv(var_name)
    if not value and required:
        logger.error(f"Missing required environment variable: {var_name}")
        print(f"[FATAL ERROR] Missing required environment variable: {var_name}", file=sys.stderr)
        sys.exit(1)
    return value or ""

BOT_TOKEN = get_env_var("BOT_TOKEN")
SUPER_OWNER_ID = int(get_env_var("SUPER_OWNER_ID"))
MONGODB_URI = get_env_var("MONGODB_URI")
MONGODB_DATABASE = get_env_var("MONGODB_DATABASE")
API_ID = int(get_env_var("API_ID"))
API_HASH = get_env_var("API_HASH")
#ENCRYPTION_KEY = get_env_var("ENCRYPTION_KEY")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
