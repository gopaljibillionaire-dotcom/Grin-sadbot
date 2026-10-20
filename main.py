import asyncio
import base64
import datetime
import logging
import os
import sys
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from motor.motor_asyncio import AsyncIOMotorClient
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

# In-memory store for active login clients
temp_clients = {}

# --- Security: Encryption Utilities for Telethon Sessions ---
def get_aesgcm():
    key_bytes = config.ENCRYPTION_KEY.encode("utf-8")
    if len(key_bytes) < 32:
        key_bytes = key_bytes.ljust(32, b"\0")
    else:
        key_bytes = key_bytes[:32]
    return AESGCM(key_bytes)

def encrypt_session(session_string: str) -> str:
    aesgcm = get_aesgcm()
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, session_string.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")

def decrypt_session(encrypted_data: str) -> str:
    aesgcm = get_aesgcm()
    raw = base64.b64decode(encrypted_data.encode("utf-8"))
    nonce, ciphertext = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")

# --- FSM States ---
class AddUserStates(StatesGroup):
    waiting_for_user_id = State()

class RemoveUserStates(StatesGroup):
    waiting_for_user_id = State()

class AccountLoginStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()

class AddTaskStates(StatesGroup):
    selecting_account = State()
    waiting_for_destination = State()
    waiting_for_message = State()
    selecting_interval = State()
    confirming_task = State()

# --- Helper Checks & Database Initialization ---
async def ensure_indexes():
    await users_col.create_index("user_id", unique=True)
    await accounts_col.create_index("account_id", unique=True)
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
        "1. **Add Account**: Securely log into your Telegram user account via Telethon.\n"
        "   *Note: Telegram login codes are delivered directly to your official Telegram app notifications chat (Service Notifications), NOT via SMS.*\n"
        "2. **Add Task**: Automate posting messages to authorized chats.\n"
        "3. **My Tasks**: View, pause, resume, or delete your background tasks.\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
    await callback.message.edit_text(help_text, reply_markup=keyboard, parse_mode="Markdown")
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
    await message.answer(f"✅ Successfully authorized user `{target_id}`.", parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "admin_remove_user")
async def cb_admin_remove_user(callback: CallbackQuery, state: FSMContext):
    if not is_super_owner(callback.from_user.id):
        return
    await state.set_state(RemoveUserStates.waiting_for_user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="admin_panel")]])
    await callback.message.edit_text("🗑 Enter the numeric Telegram user ID to revoke access:", reply_markup=keyboard)
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
    await message.answer(f"✅ Revoked access for user `{target_id}` and paused associated tasks.", parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not is_super_owner(callback.from_user.id):
        return
    
    total_users = await users_col.count_documents({"authorized": True})
    total_accounts = await accounts_col.count_documents({})
    active_accounts = await accounts_col.count_documents({"status": "connected"})
    total_tasks = await tasks_col.count_documents({})
    running_tasks = await tasks_col.count_documents({"status": "running"})

    stats_text = (
        f"📊 **System Statistics**\n\n"
        f"• Authorized Users: `{total_users}`\n"
        f"• Total Accounts: `{total_accounts}` (Active: `{active_accounts}`)\n"
        f"• Total Tasks: `{total_tasks}`\n"
        f"• Running Tasks: `{running_tasks}`\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Back to Admin", callback_data="admin_panel")]])
    await callback.message.edit_text(stats_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# --- Telethon Account Login Flow ---
@router.callback_query(F.data == "add_account")
async def cb_add_account(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_user(callback.from_user.id):
        return
    await state.set_state(AccountLoginStates.waiting_for_phone)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="main_menu")]])
    await callback.message.edit_text(
        "📱 **Add Telegram Account**\n\n"
        "Please enter your phone number with country code (e.g., `+918368682624`):\n\n"
        "⚠️ *Important*: The code will be sent to your **official Telegram app** (Service Notifications chat), NOT via SMS.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AccountLoginStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.from_user.id
    logger.info(f"[LOGIN] User {user_id} initiated login with phone number: {phone}")
    
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    try:
        logger.info(f"[LOGIN] Connecting Telethon client for user {user_id}...")
        await client.connect()
        
        logger.info(f"[LOGIN] Requesting sign-in code for {phone}...")
        sent_code = await client.send_code_request(phone)
        temp_clients[user_id] = client
        
        logger.info(f"[LOGIN] Successfully dispatched code request for {phone}. Hash type: {type(sent_code)}")
        await state.update_data(phone=phone)
        await state.set_state(AccountLoginStates.waiting_for_code)
        await message.answer(
            "📩 **Verification Code Dispatched!**\n\n"
            "Please check your **official Telegram application** (under **Service Notifications**) for the login code. Enter it below:"
        , parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[LOGIN ERROR] Failed sending code for phone {phone}: {e}", exc_info=True)
        try:
            await client.disconnect()
        except Exception:
            pass
        await message.answer(f"❌ Error sending confirmation code: {str(e)}\nMake sure the format is correct (e.g. +91...) and try again.")
        await state.clear()

@router.message(AccountLoginStates.waiting_for_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    data = await state.get_data()
    phone = data.get("phone")

    client = temp_clients.get(user_id)
    if not client:
        await message.answer("⚠️ Session expired or bot restarted. Please click 'Add Account' again.")
        await state.clear()
        return

    try:
        await client.sign_in(phone=phone, code=code)
        await finalize_account_connection(message, client, state)
    except SessionPasswordNeededError:
        await state.set_state(AccountLoginStates.waiting_for_password)
        await message.answer("🔐 Two-step verification is enabled. Please enter your account password:")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await message.answer(f"❌ Invalid or expired code: {e}. Start over via /start.")
        if user_id in temp_clients:
            try:
                await temp_clients[user_id].disconnect()
            except Exception:
                pass
            del temp_clients[user_id]
        await state.clear()
    except Exception as e:
        logger.error(f"[LOGIN ERROR] Code sign-in error: {e}", exc_info=True)
        await message.answer(f"❌ Authentication failed: {str(e)}")
        if user_id in temp_clients:
            try:
                await temp_clients[user_id].disconnect()
            except Exception:
                pass
            del temp_clients[user_id]
        await state.clear()

@router.message(AccountLoginStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()
    user_id = message.from_user.id
    client = temp_clients.get(user_id)
    if not client:
        await message.answer("⚠️ Session expired. Please click 'Add Account' again.")
        await state.clear()
        return

    try:
        await client.sign_in(password=password)
        await finalize_account_connection(message, client, state)
    except PasswordHashInvalidError:
        await message.answer("❌ Invalid password. Start over via /start.")
        if user_id in temp_clients:
            try:
                await temp_clients[user_id].disconnect()
            except Exception:
                pass
            del temp_clients[user_id]
        await state.clear()
    except Exception as e:
        logger.error(f"[LOGIN ERROR] Password sign-in error: {e}", exc_info=True)
        await message.answer(f"❌ Authentication error: {str(e)}")
        if user_id in temp_clients:
            try:
                await temp_clients[user_id].disconnect()
            except Exception:
                pass
            del temp_clients[user_id]
        await state.clear()

async def finalize_account_connection(message: Message, client: TelegramClient, state: FSMContext):
    user_id = message.from_user.id
    me = await client.get_me()
    session_string = client.session.save()
    await client.disconnect()

    if user_id in temp_clients:
        del temp_clients[user_id]

    encrypted_ref = encrypt_session(session_string)
    account_id = f"acc_{me.id}_{int(datetime.datetime.utcnow().timestamp())}"

    await accounts_col.update_one(
        {"telegram_user_id": me.id, "owner_id": user_id},
        {
            "$set": {
                "account_id": account_id,
                "owner_id": user_id,
                "telegram_user_id": me.id,
                "encrypted_session_reference": encrypted_ref,
                "status": "connected",
                "created_at": datetime.datetime.utcnow()
            }
        },
        upsert=True
    )
    await state.clear()
    await message.answer(f"✅ Successfully connected account for **{me.first_name}** (`{me.id}`)", parse_mode="Markdown", reply_markup=get_start_keyboard(user_id))

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
        text += f"• ID: `{acc['account_id']}` | TG ID: `{acc['telegram_user_id']}` | Status: **{acc['status']}**\n"
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"Disconnect {acc['telegram_user_id']}", callback_data=f"disc_acc_{acc['account_id']}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton(text="« Back", callback_data="main_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("disc_acc_"))
async def cb_disconnect_account(callback: CallbackQuery):
    account_id = callback.data.replace("disc_acc_", "")
    await accounts_col.delete_one({"account_id": account_id, "owner_id": callback.from_user.id})
    await tasks_col.update_many({"account_id": account_id}, {"$set": {"status": "paused"}})
    await callback.answer("Account disconnected.", show_alert=True)
    await cb_my_accounts(callback)

# --- Add Task Workflow ---
@router.callback_query(F.data == "add_task")
async def cb_add_task(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    accounts = await accounts_col.find({"owner_id": user_id, "status": "connected"}).to_list(length=10)
    if not accounts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Add Account First", callback_data="add_account")],[InlineKeyboardButton(text="« Back", callback_data="main_menu")]])
        await callback.message.edit_text("⚠️ You need at least one connected account to create a task.", reply_markup=keyboard)
        return

    keyboard_buttons = []
    for acc in accounts:
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"Account: {acc['telegram_user_id']}", callback_data=f"task_acc_{acc['account_id']}")
        ])
    keyboard_buttons.append([InlineKeyboardButton(text="Cancel", callback_data="main_menu")])

    await state.set_state(AddTaskStates.selecting_account)
    await callback.message.edit_text("📌 Select the Telegram account to run this task with:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons))
    await callback.answer()

@router.callback_query(AddTaskStates.selecting_account, F.data.startswith("task_acc_"))
async def process_task_account(callback: CallbackQuery, state: FSMContext):
    account_id = callback.data.replace("task_acc_", "")
    await state.update_data(account_id=account_id)
    await state.set_state(AddTaskStates.waiting_for_destination)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="main_menu")]])
    await callback.message.edit_text(
        "🎯 Enter the destination public group link or channel username (e.g., `@mychannel`):",
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
    await message.answer("✍️ Now send the text message you want to schedule and broadcast:", reply_markup=keyboard)

@router.message(AddTaskStates.waiting_for_message)
async def process_task_message(message: Message, state: FSMContext):
    text_content = message.text or message.caption or "[Media / Non-text Content]"
    await state.update_data(message_reference=text_content)
    await state.set_state(AddTaskStates.selecting_interval)

    keyboard = [
        [InlineKeyboardButton(text="10 Minutes", callback_data="int_600")],
        [InlineKeyboardButton(text="30 Minutes", callback_data="int_1800")],
        [InlineKeyboardButton(text="1 Hour", callback_data="int_3600")],
        [InlineKeyboardButton(text="2 Hours", callback_data="int_7200")],
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
        f"• Destination: `{data['destination']}`\n"
        f"• Interval: `{interval_seconds} seconds`\n\n"
        "Do you want to confirm and activate this automated task?"
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
        "account_id": data["account_id"],
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
        f"• Destination: `{task['destination_id']}`\n"
        f"• Interval: `{task['interval_seconds']}s`\n"
        f"• Status: **{task['status']}**"
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
        account = await accounts_col.find_one({"account_id": task["account_id"]})
        if not account or account.get("status") != "connected":
            raise Exception("Associated account not found or disconnected.")

        session_string = decrypt_session(account["encrypted_session_reference"])
        client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await accounts_col.update_one({"account_id": account["account_id"]}, {"$set": {"status": "expired"}})
            await tasks_col.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})
            await bot.send_message(task["owner_id"], f"⚠️ Session expired for account `{account['telegram_user_id']}`.")
            await client.disconnect()
            return

        await client.send_message(task["destination_id"], task["message_reference"])
        await client.disconnect()

        next_run = datetime.datetime.utcnow() + datetime.timedelta(seconds=task["interval_seconds"])
        await tasks_col.update_one(
            {"task_id": task_id},
            {"$set": {"status": "running", "next_run_at": next_run}}
        )
    except FloodWaitError as e:
        logger.warning(f"FloodWait encountered on task {task_id}: {e.seconds}s")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Task execution failure for {task_id}: {e}")
        await tasks_col.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})

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
