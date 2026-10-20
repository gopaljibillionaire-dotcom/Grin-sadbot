import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError

import config

# Configure Structured Logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")

# Initialize Bot & Dispatcher
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Initialize MongoDB & Encryption
db_client = AsyncIOMotorClient(config.MONGODB_URI)
db = db_client[config.MONGODB_DATABASE]

try:
    fernet = Fernet(config.ENCRYPTION_KEY.encode() if not isinstance(config.ENCRYPTION_KEY, bytes) else config.ENCRYPTION_KEY)
except Exception as e:
    logger.error(f"Invalid ENCRYPTION_KEY provided: {e}")
    sys.exit(1)

scheduler = AsyncIOScheduler()

# FSM States
class AddAccountStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()

class AddTaskStates(StatesGroup):
    selecting_account = State()
    waiting_for_destination = State()
    waiting_for_message = State()
    selecting_interval = State()
    confirming_task = State()

class AdminStates(StatesGroup):
    waiting_for_user_id = State()

# --- Security & DB Helpers ---

async def is_authorized_user(user_id: int) -> bool:
    if user_id == config.SUPER_OWNER_ID:
        return True
    user = await db.users.find_one({"user_id": user_id, "authorized": True})
    return bool(user)

async def log_admin_action(admin_id: int, action: str, target_id: int):
    await db.admin_audit.insert_one({
        "admin_id": admin_id,
        "action": action,
        "target_id": target_id,
        "timestamp": datetime.now(timezone.utc)
    })

async def log_task_event(task_id: str, owner_id: int, event: str, safe_error: str = None):
    await db.task_logs.insert_one({
        "task_id": task_id,
        "owner_id": owner_id,
        "event": event,
        "timestamp": datetime.now(timezone.utc),
        "safe_error_message": safe_error
    })

def encrypt_session(session_str: str) -> str:
    return fernet.encrypt(session_str.encode()).decode()

def decrypt_session(encrypted_str: str) -> str:
    return fernet.decrypt(encrypted_str.encode()).decode()

# --- Keyboards ---

def get_main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Add Account", callback_data="add_account", style="success"),
            InlineKeyboardButton(text="My Accounts", callback_data="my_accounts", style="primary")
        ],
        [
            InlineKeyboardButton(text="Add Task", callback_data="add_task", style="success"),
            InlineKeyboardButton(text="My Tasks", callback_data="my_tasks", style="primary")
        ],
        [
            InlineKeyboardButton(text="Task History", callback_data="task_history", style="primary"),
            InlineKeyboardButton(text="Help", callback_data="help", style="primary")
        ]
    ]
    if user_id == config.SUPER_OWNER_ID:
        rows.append([InlineKeyboardButton(text="Admin Panel", callback_data="admin_panel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add User", callback_data="admin_add_user", style="success"),
         InlineKeyboardButton(text="Remove User", callback_data="admin_remove_user", style="danger")],
        [InlineKeyboardButton(text="List Users", callback_data="admin_list_users", style="primary"),
         InlineKeyboardButton(text="User Details", callback_data="admin_user_details", style="primary")],
        [InlineKeyboardButton(text="Account Overview", callback_data="admin_accounts", style="primary"),
         InlineKeyboardButton(text="Task Overview", callback_data="admin_tasks", style="primary")],
        [InlineKeyboardButton(text="Pause All Tasks", callback_data="admin_pause_all", style="danger"),
         InlineKeyboardButton(text="Resume Eligible", callback_data="admin_resume_all", style="success")],
        [InlineKeyboardButton(text="System Statistics", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton(text="« Back to Menu", callback_data="main_menu", style="primary")]
    ])

# --- Handlers: Start & Menu ---

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_authorized_user(message.from_user.id):
        await message.answer("⛔ You are not authorized to use this bot. Contact the administrator.")
        return
    await message.answer(
        "👋 **Welcome to Telegram Account Task Manager Bot**\n\n"
        "Manage your Telegram user accounts and automate permitted messaging securely.",
        reply_markup=get_main_menu(message.from_user.id)
    )

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_authorized_user(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "👋 **Main Menu**\n\nSelect an option below:",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ **Help & Guidelines**\n\n"
        "• **Add Account**: Connect your Telegram account securely using Telethon.\n"
        "• **Add Task**: Schedule automated posts to authorized public/private groups.\n"
        "• **Intervals**: Choose between 10m, 30m, 1h, or 2h.\n"
        "• **Security**: Credentials and session strings are fully encrypted at rest.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")]
        ])
    )
    await callback.answer()

# --- Super-Owner Admin Panel Handlers ---

@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if callback.from_user.id != config.SUPER_OWNER_ID:
        await callback.answer("Access denied.", show_alert=True)
        return
    await callback.message.edit_text("👑 **Super-Owner Admin Panel**", reply_markup=get_admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_add_user")
async def cb_admin_add_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.SUPER_OWNER_ID:
        return
    await state.set_state(AdminStates.waiting_for_user_id)
    await state.update_data(admin_action="add_user")
    await callback.message.edit_text(
        "👤 **Add User**\n\nPlease enter the numeric Telegram User ID to authorize:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Cancel", callback_data="admin_panel", style="danger")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_remove_user")
async def cb_admin_remove_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.SUPER_OWNER_ID:
        return
    await state.set_state(AdminStates.waiting_for_user_id)
    await state.update_data(admin_action="remove_user")
    await callback.message.edit_text(
        "🚫 **Remove User**\n\nPlease enter the numeric Telegram User ID to revoke access:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Cancel", callback_data="admin_panel", style="danger")]])
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_id)
async def process_admin_user_id(message: Message, state: FSMContext):
    if message.from_user.id != config.SUPER_OWNER_ID:
        return
    data = await state.get_data()
    action = data.get("admin_action")
    await state.clear()

    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Invalid User ID. Must be numeric.", reply_markup=get_admin_menu())
        return

    if target_id == config.SUPER_OWNER_ID and action == "remove_user":
        await message.answer("❌ Cannot remove the Super Owner.", reply_markup=get_admin_menu())
        return

    if action == "add_user":
        existing = await db.users.find_one({"user_id": target_id})
        if existing:
            await db.users.update_one({"user_id": target_id}, {"$set": {"authorized":True}})
        else:
            await db.users.insert_one({
                "user_id": target_id,
                "authorized": True,
                "role": "user",
                "created_at": datetime.now(timezone.utc)
            })
        await log_admin_action(message.from_user.id, "ADD_USER", target_id)
        await message.answer(f"✅ User `{target_id}` authorized successfully.", reply_markup=get_admin_menu())

    elif action == "remove_user":
        await db.users.update_one({"user_id": target_id}, {"$set": {"authorized": False}})
        # Pause their tasks safely
        await db.tasks.update_many({"owner_id": target_id}, {"$set": {"status": "paused"}})
        await log_admin_action(message.from_user.id, "REMOVE_USER_AND_PAUSE_TASKS", target_id)
        await message.answer(f"✅ User `{target_id}` access revoked and tasks paused.", reply_markup=get_admin_menu())

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != config.SUPER_OWNER_ID:
        return
    auth_users = await db.users.count_documents({"authorized": True})
    total_accounts = await db.accounts.count_documents({})
    active_accounts = await db.accounts.count_documents({"status": "active"})
    total_tasks = await db.tasks.count_documents({})
    running_tasks = await db.tasks.count_documents({"status": "running"})
    paused_tasks = await db.tasks.count_documents({"status": "paused"})
    failed_tasks = await db.tasks.count_documents({"status": "failed"})

    text = (
        "📊 **System Statistics**\n\n"
        f"• Authorized Users: `{auth_users}`\n"
        f"• Active Accounts: `{active_accounts}` / `{total_accounts}`\n"
        f"• Total Tasks: `{total_tasks}`\n"
        f"• Running Tasks: `{running_tasks}`\n"
        f"• Paused Tasks: `{paused_tasks}`\n"
        f"• Failed Tasks: `{failed_tasks}`"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back", callback_data="admin_panel", style="primary")]
    ]))
    await callback.answer()

# --- Add Account Workflow (Telethon) ---

@dp.callback_query(F.data == "add_account")
async def cb_add_account(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_user(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    await state.set_state(AddAccountStates.waiting_for_phone)
    await callback.message.edit_text(
        "📱 **Add Telegram Account**\n\nPlease enter your phone number with country code (e.g. `+1234567890`):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Cancel", callback_data="main_menu", style="danger")]]))
    await callback.answer()

@dp.message(AddAccountStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash, session=client.session.save())
        await client.disconnect()
        await state.set_state(AddAccountStates.waiting_for_code)
        await message.answer("📩 Verification code sent to your Telegram app. Please enter the code:")
    except Exception as e:
        await client.disconnect()
        logger.error(f"Telethon phone error: {e}")
        await message.answer(f"❌ Error sending code: {e}. Try again /start", reply_markup=get_main_menu(message.from_user.id))
        await state.clear()

@dp.message(AddAccountStates.waiting_for_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]

    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(data["session"]), config.API_ID, config.API_HASH)
    await client.connect()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        session_str = client.session.save()
        me = await client.get_me()
        await client.disconnect()

        account_id = f"acc_{me.id}_{int(datetime.now(timezone.utc).timestamp())}"
        encrypted_session = encrypt_session(session_str)

        await db.accounts.insert_one({
            "account_id": account_id,
            "owner_id": message.from_user.id,
            "telegram_user_id": me.id,
            "encrypted_session_reference": encrypted_session,
            "status": "active",
            "created_at": datetime.now(timezone.utc)
        })

        await state.clear()
        await message.answer(f"✅ Account connected successfully!\nUsername: @{me.username or 'None'}", reply_markup=get_main_menu(message.from_user.id))
    except SessionPasswordNeededError:
        await state.update_data(session=client.session.save())
        await client.disconnect()
        await state.set_state(AddAccountStates.waiting_for_password)
        await message.answer("🔒 Two-step verification is enabled. Please enter your account password:")
    except Exception as e:
        await client.disconnect()
        await state.clear()
        await message.answer(f"❌ Authentication failed: {e}", reply_markup=get_main_menu(message.from_user.id))

@dp.message(AddAccountStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()

    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(data["session"]), config.API_ID, config.API_HASH)
    await client.connect()

    try:
        await client.sign_in(password=password)
        session_str = client.session.save()
        me = await client.get_me()
        await client.disconnect()

        account_id = f"acc_{me.id}_{int(datetime.now(timezone.utc).timestamp())}"
        encrypted_session = encrypt_session(session_str)

        await db.accounts.insert_one({
            "account_id": account_id,
            "owner_id": message.from_user.id,
            "telegram_user_id": me.id,
            "encrypted_session_reference": encrypted_session,
            "status": "active",
            "created_at": datetime.now(timezone.utc)
        })

        await state.clear()
        await message.answer(f"✅ Account connected successfully!\nUsername: @{me.username or 'None'}", reply_markup=get_main_menu(message.from_user.id))
    except Exception as e:
        await client.disconnect()
        await state.clear()
        await message.answer(f"❌ Password verification failed: {e}", reply_markup=get_main_menu(message.from_user.id))

# --- My Accounts & Disconnect ---

@dp.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: CallbackQuery):
    accounts = await db.accounts.find({"owner_id": callback.from_user.id, "status": "active"}).to_list(length=10)
    if not accounts:
        await callback.message.edit_text("📂 You have no active connected accounts.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")]
        ]))
        await callback.answer()
        return

    text = "📂 **Your Connected Accounts**\n\n"
    keyboard = []
    for acc in accounts:
        text += f"• ID: `{acc['account_id']}` (TG ID: `{acc['telegram_user_id']}`)\n"
        keyboard.append([InlineKeyboardButton(text=f"Disconnect {acc['account_id']}", callback_data=f"disconnect_{acc['account_id']}", style="danger")])
    
    keyboard.append([InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("disconnect_"))
async def cb_disconnect_account(callback: CallbackQuery):
    account_id = callback.data.split("_", 1)[1]
    await db.accounts.update_one({"account_id": account_id, "owner_id": callback.from_user.id}, {"$set": {"status": "disconnected"}})
    await db.tasks.update_many({"account_id": account_id}, {"$set": {"status": "paused"}})
    await callback.message.edit_text("✅ Account disconnected and associated tasks paused.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")]
    ]))
    await callback.answer()

# --- Add Task Workflow ---

@dp.callback_query(F.data == "add_task")
async def cb_add_task(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_user(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    accounts = await db.accounts.find({"owner_id": callback.from_user.id, "status": "active"}).to_list(length=10)
    if not accounts:
        await callback.message.edit_text("❌ You need an active connected account first.", reply_markup=get_main_menu(callback.from_user.id))
        await callback.answer()
        return

    keyboard = [[InlineKeyboardButton(text=acc['account_id'], callback_data=f"task_acc_{acc['account_id']}", style="primary")] for acc in accounts]
    keyboard.append([InlineKeyboardButton(text="« Cancel", callback_data="main_menu", style="danger")])
    
    await state.set_state(AddTaskStates.selecting_account)
    await callback.message.edit_text("📋 **Add Task**\n\nSelect the connected account for this task:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("task_acc_"), AddTaskStates.selecting_account)
async def cb_select_account(callback: CallbackQuery, state: FSMContext):
    account_id = callback.data.split("_", 2)[2]
    await state.update_data(account_id=account_id)
    await state.set_state(AddTaskStates.waiting_for_destination)
    await callback.message.edit_text("🔗 Enter the public/private group link or username (e.g., `@mygroup` or `https://t.me/mygroup`):")
    await callback.answer()

@dp.message(AddTaskStates.waiting_for_destination)
async def process_destination(message: Message, state: FSMContext):
    dest = message.text.strip()
    await state.update_data(destination_id=dest)
    await state.set_state(AddTaskStates.waiting_for_message)
    await message.answer("✍️ Please forward or send the exact message you want to schedule and distribute:")

@dp.message(AddTaskStates.waiting_for_message)
async def process_message(message: Message, state: FSMContext):
    # Store message reference securely (text or caption)
    msg_content = message.text or message.caption or "[Media Message]"
    await state.update_data(message_reference=msg_content)
    await state.set_state(AddTaskStates.selecting_interval)

    keyboard = [
        [InlineKeyboardButton(text="10 Minutes", callback_data="interval_600", style="primary")],
        [InlineKeyboardButton(text="30 Minutes", callback_data="interval_1800", style="primary")],
        [InlineKeyboardButton(text="1 Hour", callback_data="interval_3600", style="primary")],
        [InlineKeyboardButton(text="2 Hours", callback_data="interval_7200", style="primary")],
        [InlineKeyboardButton(text="« Cancel", callback_data="main_menu", style="danger")]
    ]
    await message.answer("⏱ Select the permitted scheduling interval:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("interval_"), AddTaskStates.selecting_interval)
async def cb_select_interval(callback: CallbackQuery, state: FSMContext):
    interval = int(callback.data.split("_")[1])
    await state.update_data(interval_seconds=interval)
    data = await state.get_data()

    await state.set_state(AddTaskStates.confirming_task)
    summary = (
        "📌 **Task Summary**\n\n"
        f"• Account: `{data['account_id']}`\n"
        f"• Destination: `{data['destination_id']}`\n"
        f"• Interval: `{interval // 60} minutes`\n"
        f"• Message Preview: {data['message_reference'][:100]}...\n\n"
        "Confirm task creation?"
    )
    keyboard = [
        [InlineKeyboardButton(text="✅ Confirm & Activate", callback_data="confirm_task_yes", style="success")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu", style="danger")]
    ]
    await callback.message.edit_text(summary, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data == "confirm_task_yes", AddTaskStates.confirming_task)
async def cb_confirm_task(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = f"task_{callback.from_user.id}_{int(datetime.now(timezone.utc).timestamp())}"

    task_doc = {
        "task_id": task_id,
        "owner_id": callback.from_user.id,
        "account_id": data["account_id"],
        "destination_id": data["destination_id"],
        "message_reference": data["message_reference"],
        "interval_seconds": data["interval_seconds"],
        "status": "running",
        "last_run_at": None,
        "next_run_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc)
    }

    await db.tasks.insert_one(task_doc)
    await log_task_event(task_id, callback.from_user.id, "CREATED")

    # Add job to scheduler
    scheduler.add_job(
        execute_task,
        'interval',
        seconds=data["interval_seconds"],
        args=[task_id],
        id=task_id,
        replace_existing=True
    )

    await state.clear()
    await callback.message.edit_text("✅ Task created and activated successfully!", reply_markup=get_main_menu(callback.from_user.id))
    await callback.answer()

# --- Task Management & Execution Engine ---

async def execute_task(task_id: str):
    task = await db.tasks.find_one({"task_id": task_id, "status": "running"})
    if not task:
        return

    account = await db.accounts.find_one({"account_id": task["account_id"], "status": "active"})
    if not account:
        await db.tasks.update_one({"task_id": task_id}, {"$set": {"status": "paused"}})
        await log_task_event(task_id, task["owner_id"], "PAUSED_NO_ACTIVE_ACCOUNT", "Account inactive or disconnected")
        return

    from telethon.sessions import StringSession
    session_str = decrypt_session(account["encrypted_session_reference"])
    client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await db.accounts.update_one({"account_id": account["account_id"]}, {"$set": {"status": "expired"}})
            await db.tasks.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})
            await log_task_event(task_id, task["owner_id"], "SESSION_EXPIRED", "Telegram session expired or revoked")
            await client.disconnect()
            return

        await client.send_message(task["destination_id"], task["message_reference"])
        await client.disconnect()

        now = datetime.now(timezone.utc)
        await db.tasks.update_one(
            {"task_id": task_id},
            {"$set": {"last_run_at": now, "status": "running"}}
        )
        await log_task_event(task_id, task["owner_id"], "SUCCESS")
    except FloodWaitError as e:
        logger.warning(f"FloodWait encountered on task {task_id}: {e.seconds}s")
        await log_task_event(task_id, task["owner_id"], "FLOOD_WAIT", f"FloodWait: {e.seconds}s")
        await client.disconnect()
    except Exception as e:
        logger.error(f"Execution error on task {task_id}: {e}")
        await db.tasks.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})
        await log_task_event(task_id, task["owner_id"], "ERROR", str(e))
        try:
            await client.disconnect()
        except:
            pass

@dp.callback_query(F.data == "my_tasks")
async def cb_my_tasks(callback: CallbackQuery):
    tasks = await db.tasks.find({"owner_id": callback.from_user.id}).to_list(length=10)
    if not tasks:
        await callback.message.edit_text("📋 You have no scheduled tasks.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")]
        ]))
        await callback.answer()
        return

    text = "📋 **Your Tasks**\n\n"
    keyboard = []
    for t in tasks:
        text += f"• `{t['task_id']}` | Status: `{t['status']}` | Dest: `{t['destination_id']}`\n"
        keyboard.append([
            InlineKeyboardButton(text=f"Pause {t['task_id']}", callback_data=f"pause_{t['task_id']}", style="danger"),
            InlineKeyboardButton(text=f"Delete {t['task_id']}", callback_data=f"del_{t['task_id']}", style="danger")
        ])

    keyboard.append([InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("pause_"))
async def cb_pause_task(callback: CallbackQuery):
    task_id = callback.data.split("_", 1)[1]
    await db.tasks.update_one({"task_id": task_id, "owner_id": callback.from_user.id}, {"$set": {"status": "paused"}})
    if scheduler.get_job(task_id):
        scheduler.remove_job(task_id)
    await callback.message.edit_text(f"✅ Task `{task_id}` paused.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")]
    ]))
    await callback.answer()

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete_task(callback: CallbackQuery):
    task_id = callback.data.split("_", 1)[1]
    await db.tasks.deleteOne({"task_id": task_id, "owner_id": callback.from_user.id})
    if scheduler.get_job(task_id):
        scheduler.remove_job(task_id)
    await callback.message.edit_text(f"✅ Task `{task_id}` deleted.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back", callback_data="main_menu", style="primary")]
    ]))
    await callback.answer()

# --- Startup & Recovery ---

async def restore_tasks():
    cursor = db.tasks.find({"status": "running"})
    async for task in cursor:
        scheduler.add_job(
            execute_task,
            'interval',
            seconds=task["interval_seconds"],
            args=[task["task_id"]],
            id=task["task_id"],
            replace_existing=True
        )
    logger.info("Restored running tasks from MongoDB into scheduler.")

async def main():
    scheduler.start()
    await restore_tasks()
    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await db_client.close()

if __name__ == "__main__":
    asyncio.run(main())
