import os
import sys
from dotenv import load_dotenv

load_dotenv()

def get_env_variable(var_name: str, default=None, required: bool = True):
    value = os.getenv(var_name, default)
    if required and (value is None or str(value).strip() == ""):
        print(f"[CRITICAL ERROR] Missing required environment variable: {var_name}", file=sys.stderr)
        sys.exit(1)
    return value

BOT_TOKEN = get_env_variable("BOT_TOKEN")
SUPER_OWNER_ID = int(get_env_variable("SUPER_OWNER_ID"))
MONGODB_URI = get_env_variable("MONGODB_URI")
MONGODB_DATABASE = get_env_variable("MONGODB_DATABASE", default="telegram_task_bot")
API_ID = int(get_env_variable("API_ID"))
API_HASH = get_env_variable("API_HASH")
ENCRYPTION_KEY = get_env_variable("ENCRYPTION_KEY")
LOG_LEVEL = get_env_variable("LOG_LEVEL", default="INFO")
