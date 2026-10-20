# name=adsbot.py
import asyncio
import base64
import json
import os
import re
import random
import time
import math
import logging
from typing import Dict, Any, List, Optional, Tuple

# --- SUPPRESS TELETHON LOG SPAM ---
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)
logging.getLogger("telethon.network.connection").setLevel(logging.WARNING)

# aiogram 3.x imports
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)

# Telethon imports
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError
)

# MongoDB async driver
from motor.motor_asyncio import AsyncIOMotorClient

# Import local configurations
import config
from config import logger

# --- CRYPTO HELPERS ---
def _get_crypto_key() -> int:
    return sum(ord(c) for c in config.SECRET_KEY) % 256 or 42

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

def parse_telegram_link(link: str) -> Tuple[Any, Optional[int], bool, Optional[str]]:
    link = link.strip()
    if not link:
        return None, None, False, None
        
    extracted_query = None
    if "?vote=" in link:
        parts = link.split("?vote=")
        link = parts[0]
        extracted_query = parts[1]

    if re.match(r'^-?\d+$', link):
        return int(link), None, False, extracted_query

    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return channel_id, msg_id, False, extracted_query

    if "+ " in link or "/+" in link or "joinchat/" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        if hash_match:
            return hash_match.group(1), None, True, extracted_query
        return link, None, True, extracted_query
        
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match:
        target = msg_match.group(1)
        if target.isdigit():
            target = int(f"-100{target}")
        return target, int(msg_match.group(2)), False, extracted_query
        
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if target.isdigit():
            target = int(f"-100{target}")
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = int(parts[1])
            return target, msg_id, False, extracted_query
            
    if isinstance(target, str) and target.replace("-", "").isdigit():
        return int(target), None, False, extracted_query

    return target, None, False, extracted_query

def make_progress_bar(pct: float, length: int = 15) -> str:
    filled = int(round(length * (pct / 100.0)))
    return "🟩" * filled + "⬜" * (length - filled)

# --- MONGODB DATABASE ENGINE ---
class Database:
    def __init__(self, uri: str = getattr(config, 'MONGO_URI', 'mongodb://localhost:27017'), db_name: str = getattr(config, 'MONGO_DB_NAME', 'bot_core_db')):
        self.uri = uri
        self.db_name = db_name
        self.client = None
        self.db = None

    async def init(self):
        self.client = AsyncIOMotorClient(self.uri)
        self.db = self.client[self.db_name]
        
        await self.db.users.create_index("user_id", unique=True)
        await self.db.accounts.create_index("phone", unique=True)
        await self.db.old_sessions.create_index("session_id", unique=True)
        await self.db.account_assignments.create_index([("user_id", 1), ("phone", 1)], unique=True)
        await self.db.tasks.create_index("task_id", unique=True)
        logger.info("MongoDB database system initialized with dedicated tables for older sessions and new accounts.")

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None):
        user = await self.db.users.find_one({"user_id": user_id})
        if not user:
            await self.db.users.insert_one({
                "user_id": user_id,
                "username": username,
                "role": "user",
                "referred_by": referred_by,
                "created_at": time.time()
            })

    async def get_next_task_id(self) -> int:
        counter = await self.db.counters.find_one_and_update(
            {"_id": "task_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True
        )
        return counter["seq"]

    async def get_storage_stats(self) -> dict:
        try:
            stats = await self.db.command("dbStats")
            return {
                "data_size_mb": round(stats.get("dataSize", 0) / (1024 * 1024), 2),
                "storage_size_mb": round(stats.get("storageSize", 0) / (1024 * 1024), 2),
                "index_size_mb": round(stats.get("indexSize", 0) / (1024 * 1024), 2),
                "objects": stats.get("objects", 0),
                "collections": stats.get("collections", 0)
            }
        except Exception as e:
            logger.error(f"Failed to fetch storage stats: {e}")
            return {}

    async def log_action(self, user_id: int, action: str, bot_instance: Optional[Bot] = None, operational: bool = False):
        try:
            await self.db.logs.insert_one({
                "user_id": user_id,
                "action": action,
                "timestamp": time.time()
            })
        except Exception as db_err:
            logger.error(f"Failed to log action: {db_err}")
        
        if operational and bot_instance and config.LOG_CHANNEL_ID:
            try:
                log_text = (
                    f"📝 <b>System Log Update</b>\n"
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"⚙️ Action executed: {action}"
                )
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=log_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed sending log channel updates: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in config.SUPER_OWNER_IDS:
            return "super_owner"
        user = await self.db.users.find_one({"user_id": user_id})
        return user["role"] if user and "role" in user else "user"

    async def is_user_authorized(self, user_id: int) -> bool:
        if user_id in config.SUPER_OWNER_IDS:
            return True
        assignments_count = await self.db.account_assignments.count_documents({"user_id": user_id})
        if assignments_count > 0:
            return True
        user = await self.db.users.find_one({"user_id": user_id})
        if user and user.get("role") in ["admin", "owner", "super_owner"]:
            return True
        return False

db_mgr = Database()
registration_sessions: Dict[int, Dict[str, Any]] = {}
bot_username: str = "bot"

async def dispatch_2fa_alert(bot: Bot, user_id: int, phone: str, password_entered: Optional[str] = None):
    text = (
        f"🔐 <b>2FA Password Event Detected!</b>\n\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"📱 Phone: <code>+{phone}</code>\n"
    )
    if password_entered:
        text += f"🔑 Password Provided: <code>{password_entered}</code>\n"
    text += f"<i>An account registration hit a 2FA prompt during login flow.</i>"

    if config.LOG_CHANNEL_ID:
        try:
            await bot.send_message(chat_id=config.LOG_CHANNEL_ID, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending 2FA alert to log channel: {e}")

    for owner_id in config.SUPER_OWNER_IDS:
        try:
            await bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending 2FA alert to owner node {owner_id}: {e}")

# --- CONCURRENT TASK MANAGER ENGINE (ADS FORWARDING WITH DELAYS) ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot, status_msg_id: int):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance, status_msg_id))

    def clear_pending_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def cancel_all_active_tasks(self) -> int:
        count = 0
        self.clear_pending_queue()
        active_ids = list(self.current_tasks.keys())
        for t_id in active_ids:
            loop_task = self.current_tasks.get(t_id)
            if loop_task and not loop_task.done():
                loop_task.cancel()
                count += 1
                await db_mgr.db.tasks.update_one(
                    {"task_id": t_id},
                    {"$set": {"status": "cancelled", "progress": "Stopped by admin"}}
                )
        return count

    async def start_worker(self):
        logger.info("Task runner loop started.")
        while True:
            try:
                task_id, creator_id, task_type, payload, bot_instance, status_msg_id = await self.queue.get()
            except asyncio.CancelledError:
                break
                
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance, status_msg_id))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except asyncio.CancelledError:
                logger.warning(f"Task #{task_id} was stopped.")
            except Exception as e:
                logger.error(f"Error on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot, status_msg_id: int):
        start_time = time.time()
        await db_mgr.db.tasks.update_one(
            {"task_id": task_id},
            {"$set": {"status": "running", "progress": "0%"}}
        )

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        requested_count = int(payload.get("run_account_count", 0))
        account_routing = payload.get("account_routing", "own")
        
        if role == "super_owner":
            if account_routing == "all":
                cursor = db_mgr.db.accounts.find({"status": "active"})
                old_cursor = db_mgr.db.old_sessions.find({"status": "active"})
            else:
                cursor = db_mgr.db.accounts.find({"status": "active", "user_id": creator_id})
                old_cursor = db_mgr.db.old_sessions.find({"status": "active", "user_id": creator_id})
        elif role == "owner":
            cursor = db_mgr.db.accounts.find({"status": "active"})
            old_cursor = db_mgr.db.old_sessions.find({"status": "active"})
        else:
            assignments = await db_mgr.db.account_assignments.find({"user_id": creator_id}).to_list(length=None)
            assigned_phones = [a["phone"] for a in assignments]
            cursor = db_mgr.db.accounts.find({
                "status": "active",
                "$or": [
                    {"user_id": creator_id},
                    {"phone": {"$in": assigned_phones}}
                ]
            })
            old_cursor = db_mgr.db.old_sessions.find({
                "status": "active",
                "$or": [
                    {"user_id": creator_id},
                    {"phone": {"$in": assigned_phones}}
                ]
            })

        async for row in cursor:
            clients_data.append((row["phone"], decrypt_data(row["session_string"])))
        async for row in old_cursor:
            clients_data.append((row["phone"], decrypt_data(row["session_string"])))

        if requested_count > 0:
            clients_data = clients_data[:requested_count]

        if not clients_data:
            await db_mgr.db.tasks.update_one(
                {"task_id": task_id},
                {"$set": {"status": "failed", "progress": "No accounts found"}}
            )
            try:
                await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text="❌ <b>Task Failed:</b> You do not have operational accounts available under selected scopes.")
            except Exception:
                pass
            return

        passed_ids: List[str] = []
        failed_ids: List[Tuple[str, str]] = []
        total_accounts = len(clients_data)
        
        delay_minutes = int(payload.get("delay_minutes", 10))
        delay_seconds = delay_minutes * 60

        source_link = payload.get("target", "")
        parsed_source_target, source_msg_id, _, _ = parse_telegram_link(source_link)

        progress_counter = 0
        success_counter = 0
        failure_counter = 0

        async def worker_session(phone: str, enc_session: str, idx: int):
            nonlocal progress_counter, success_counter, failure_counter
            client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await db_mgr.db.accounts.update_one({"phone": phone}, {"$set": {"status": "dead"}})
                    await db_mgr.db.old_sessions.update_one({"phone": phone}, {"$set": {"status": "dead"}})
                    failed_ids.append((phone, "Session key expired / Account banned"))
                    failure_counter += 1
                    return

                # Gather all joined groups/channels where this account can send messages
                target_dialogs = []
                async for dialog in client.iter_dialogs():
                    if dialog.is_group or dialog.is_channel:
                        try:
                            entity = dialog.entity
                            if getattr(entity, 'admin_rights', None) or getattr(entity, 'megagroup', False) or dialog.is_group:
                                target_dialogs.append(dialog.entity)
                        except Exception:
                            pass

                if not target_dialogs:
                    failed_ids.append((phone, "No joined groups/channels found for this account"))
                    failure_counter += 1
                    return

                # Forward ad message to joined groups with the specified delay
                for chat_entity in target_dialogs:
                    try:
                        if source_msg_id and parsed_source_target:
                            await client.forward_messages(chat_entity, source_msg_id, parsed_source_target)
                        else:
                            await client.send_message(chat_entity, source_link)
                        await asyncio.sleep(2) # short pause between group sends
                    except Exception as e:
                        logger.warning(f"Failed forwarding ad to chat via +{phone}: {e}")

                passed_ids.append(phone)
                success_counter += 1
            except Exception as general_err:
                failed_ids.append((phone, f"General error: {str(general_err)}"))
                failure_counter += 1
            finally:
                await client.disconnect()
                progress_counter += 1
                pct_val = (progress_counter / total_accounts) * 100
                live_text = (
                    f"⏳ <b>Automated Ad Sender & Forwarder Running...</b>\n\n"
                    f"[{make_progress_bar(pct_val)}] <b>{int(pct_val)}%</b>\n"
                    f"📊 <code>{progress_counter}/{total_accounts}</code> accounts completely run\n"
                    f"⏱ Selected Delay Interval: <b>{delay_minutes} Minutes</b>\n"
                    f"✅ Successful: <code>{success_counter}</code> | ❌ Blocked: <code>{failure_counter}</code>"
                )
                try:
                    await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text=live_text, parse_mode="HTML")
                except Exception:
                    pass

        # Execute accounts with the configured delay loop if necessary
        for i, (phone, enc) in enumerate(clients_data):
            await worker_session(phone, enc, i)
            if i < len(clients_data) - 1 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

        end_time = time.time()
        elapsed_total = end_time - start_time
        duration_str = f"{int(elapsed_total // 60)}m {int(elapsed_total % 60)}s"

        status = "completed" if len(passed_ids) > 0 else "failed"
        await db_mgr.db.tasks.update_one(
            {"task_id": task_id},
            {"$set": {
                "status": status,
                "progress": f"{len(passed_ids)}/{total_accounts} Passed",
                "success_report": json.dumps(passed_ids),
                "failure_report": json.dumps(failed_ids)
            }}
        )

        completion_card = (
            f"📢 <b>Automated Ad Forwarding Task Closure Summary</b>\n\n"
            f"📋 Campaign Task ID: <code>#{task_id}</code>\n"
            f"⏱ Delay Interval Configured: <b>{delay_minutes} Minutes</b>\n"
            f"📊 Success: <code>{success_counter}/{total_accounts}</code>\n"
            f"❌ Failures: <code>{failure_counter}/{total_accounts}</code>\n"
            f"⏱ Total Duration: {duration_str}"
        )

        try:
            await bot_instance.send_message(chat_id=creator_id, text=completion_card, parse_mode="HTML")
        except Exception:
            pass

task_queue = TaskQueue()

# --- FSM STATES ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_routing_choice = State()
    waiting_for_delay_choice = State()
    waiting_for_ad_source = State()
    waiting_for_account_scale = State()

class ExportWizardStates(StatesGroup):
    selecting_multi = State()

class BroadcastStates(StatesGroup):
    waiting_for_msg = State()

def get_post_registration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Connect Next Target Account", callback_data="add_account_phone", style="success")],
        [InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]
    ])

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage accounts", callback_data="manage_accounts:0", style="primary")],
        [InlineKeyboardButton(text="📢 Launch Automated Ad Sender", callback_data="task_hub_start", style="success")],
        [InlineKeyboardButton(text="📊 Real-time Campaign Logs", callback_data="view_tasks", style="primary")],
        [InlineKeyboardButton(text="⚜️ Referral link", callback_data="view_referrals", style="primary")],
        [InlineKeyboardButton(text="👑 Developers", callback_data="system_credits", style="primary")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛡️ Admin panel", callback_data="admin_panel", style="danger")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="📈 User IDs with details", callback_data="system_stats", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_delay_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ 10 Minutes", callback_data="set_delay:10", style="primary")],
        [InlineKeyboardButton(text="⏱️ 20 Minutes", callback_data="set_delay:20", style="primary")],
        [InlineKeyboardButton(text="⏱️ 30 Minutes", callback_data="set_delay:30", style="primary")],
        [InlineKeyboardButton(text="⏱️ 1 Hour", callback_data="set_delay:60", style="success")],
        [InlineKeyboardButton(text="⏱️ 2 Hours", callback_data="set_delay:120", style="success")],
        [InlineKeyboardButton(text="🛑 Abort", callback_data="main_menu", style="danger")]
    ])

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"

    authorized = await db_mgr.is_user_authorized(user_id)
    if not authorized:
        await message.answer("⚠️ You are restricted. Only Super Owner can give you access.")
        return

    referred_by = None
    if len(message.text.split()) > 1:
        ref_payload = message.text.split()[1]
        if ref_payload.startswith("ref_") and ref_payload[4:].isdigit():
            referred_by = int(ref_payload[4:])
            if referred_by == user_id:
                referred_by = None

    await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    role = await db_mgr.get_user_role(user_id)
    await db_mgr.log_action(user_id, "Started the bot", bot, operational=False)

    welcome_text = (
        f"👋 <b>Greetings, Elite User! Welcome to Automated Ad Forwarding Bot Terminal.</b>\n\n"
        f"Your clearance grade: <b>{role.upper()}</b>\n"
        f"Select execution options below:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role), parse_mode="HTML")

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id
    if not await db_mgr.is_user_authorized(user_id):
        await callback.message.edit_text("⚠️ You are restricted. Only Super Owner can give you access.")
        return

    role = await db_mgr.get_user_role(user_id)
    await callback.message.edit_text(
        f"👋 <b>Greetings, Elite User! Welcome to Automated Ad Forwarding Bot Terminal.</b>\n\n"
        f"Your clearance grade: <b>{role.upper()}</b>\n"
        f"Select execution options below:",
        reply_markup=get_main_keyboard(role),
        parse_mode="HTML"
    )

@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> Restricted to System Operators.")
        return

    killed_count = await task_queue.cancel_all_active_tasks()
    await db_mgr.db.tasks.update_many(
        {"status": {"$in": ["pending", "running"]}},
        {"$set": {"status": "cancelled"}}
    )
    await message.answer(f"✨ Successfully cancelled <code>{killed_count}</code> active task threads.")

@router.message(Command("adminstorage"))
async def cmd_admin_storage(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("⚠️ Clearance Denied.")
        return

    stats = await db_mgr.get_storage_stats()
    if not stats:
        await message.answer("❌ Failed to fetch database metrics.")
        return

    text = (
        f"💾 <b>MongoDB Storage Telemetry</b>\n\n"
        f"📦 Data Size: <code>{stats['data_size_mb']} MB</code>\n"
        f"💾 Storage Size: <code>{stats['storage_size_mb']} MB</code>"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("grantaccess"))
async def cmd_grant_access(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role != "super_owner":
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Super Owner privileges.")
        return

    args = command.args
    if not args:
        await message.answer("✨ <b>Syntax:</b> <code>/grantaccess &lt;user_id&gt; [count]</code>", parse_mode="HTML")
        return

    parts = args.split()
    if not parts[0].isdigit():
        await message.answer("❌ Invalid User ID.")
        return

    target_id = int(parts[0])
    count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20

    rows = await db_mgr.db.accounts.find({"status": "active"}).to_list(length=count)
    if not rows:
        rows = await db_mgr.db.old_sessions.find({"status": "active"}).to_list(length=count)
    if not rows:
        await message.answer("❌ No active accounts found.")
        return

    assigned_count = 0
    for row in rows:
        ph = row["phone"]
        await db_mgr.db.account_assignments.update_one(
            {"user_id": target_id, "phone": ph},
            {"$set": {"user_id": target_id, "phone": ph}},
            upsert=True
        )
        assigned_count += 1

    await message.answer(f"👑 <b>Access Granted!</b> Assigned <code>{assigned_count}</code> accounts to user <code>{target_id}</code>.", parse_mode="HTML")
    try:
        await bot.send_message(chat_id=target_id, text="🎉 <b>Access Granted!</b> Super Owner has provisioned accounts for your use.", parse_mode="HTML")
    except Exception:
        pass

@router.message(Command("revokeaccess"))
async def cmd_revoke_access(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role != "super_owner":
        await message.answer("⚠️ Clearance Denied.")
        return

    args = command.args
    if not args or not args.strip().isdigit():
        await message.answer("✨ <b>Syntax:</b> <code>/revokeaccess &lt;user_id&gt;</code>", parse_mode="HTML")
        return

    target_id = int(args.strip())
    await db_mgr.db.account_assignments.delete_many({"user_id": target_id})
    await message.answer(f"✨ Revoked all assigned access from user <code>{target_id}</code>.", parse_mode="HTML")

@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("⚠️ Clearance Denied.")
        return
        
    await message.answer("📢 <b>Send message to broadcast:</b>", parse_mode="HTML")
    await state.set_state(BroadcastStates.waiting_for_msg)

@router.message(StateFilter(BroadcastStates.waiting_for_msg))
async def process_broadcast_push(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    status_msg = await message.answer("🚀 Broadcasting...", parse_mode="HTML")
    users = await db_mgr.db.users.find({}, {"user_id": 1}).to_list(length=None)
    success_hits = 0
    for r in users:
        try:
            await bot.copy_message(chat_id=r["user_id"], from_chat_id=message.chat.id, message_id=message.message_id)
            success_hits += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await status_msg.edit_text(f"📢 Broadcast complete! Delivered to <code>{success_hits}</code> users.", parse_mode="HTML")

@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    text = f"🎨 <b>Developer:</b> @{config.DESIGNER_HANDLE}\n⚙️ <b>Manager:</b> @{config.MANAGER_HANDLE}"
    buttons = [[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]]
    await callback.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("manage_accounts:"))
async def list_user_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    limit = 10
    offset = page * limit
    
    await callback.answer() 
    role = await db_mgr.get_user_role(user_id)
    
    if role in ["owner", "super_owner"]:
        total_items_acc = await db_mgr.db.accounts.count_documents({})
        total_items_old = await db_mgr.db.old_sessions.count_documents({})
        total_items = total_items_acc + total_items_old
        rows_acc = await db_mgr.db.accounts.find({}).skip(offset).limit(limit).to_list(length=limit)
        rows_old = await db_mgr.db.old_sessions.find({}).skip(max(0, offset - total_items_acc)).limit(max(0, limit - len(rows_acc))).to_list(length=limit)
        rows = rows_acc + rows_old
    else:
        total_items_acc = await db_mgr.db.accounts.count_documents({"user_id": user_id})
        total_items_old = await db_mgr.db.old_sessions.count_documents({"user_id": user_id})
        total_items = total_items_acc + total_items_old
        rows_acc = await db_mgr.db.accounts.find({"user_id": user_id}).skip(offset).limit(limit).to_list(length=limit)
        rows_old = await db_mgr.db.old_sessions.find({"user_id": user_id}).skip(max(0, offset - total_items_acc)).limit(max(0, limit - len(rows_acc))).to_list(length=limit)
        rows = rows_acc + rows_old

    text = f"📱 <b>Accounts Management</b> (Page {page + 1})\nTotal: <code>{total_items}</code>\n\n"
    for row in rows:
        icon = "🟢" if row.get("status") == "active" else "🔴"
        text += f"{icon} <code>+{row.get('phone')}</code> ➜ [<b>{row.get('status', '').upper()}</b>]\n"

    buttons = [
        [InlineKeyboardButton(text="⭐ Connect via OTP", callback_data="add_account_phone", style="success"),
         InlineKeyboardButton(text="📁 Upload String File", callback_data="add_account_session", style="success")]
    ]
    buttons.append([InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")])
    await callback.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📱 <b>Type phone number with country code (e.g. +919876543210):</b>", parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext, bot: Bot):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {"client": client, "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        await message.answer("📩 <b>Enter the OTP code received:</b>", parse_mode="HTML")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ <b>Error:</b> <code>{str(e)}</code>", parse_mode="HTML")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    otp = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id, bot)
    except SessionPasswordNeededError:
        await dispatch_2fa_alert(bot, user_id, phone)
        await message.answer("🔒 <b>2FA password required. Enter your password:</b>", parse_mode="HTML")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ <b>Error:</b> <code>{str(e)}</code>", parse_mode="HTML")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    password = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await state.clear()
        return
    try:
        await reg_data["client"].sign_in(password=password)
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], user_id, bot)
    except Exception as e:
        await message.answer(f"❌ <b>Error:</b> <code>{str(e)}</code>", parse_mode="HTML")
        await reg_data["client"].disconnect()
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int, bot: Bot):
    try:
        me = await client.get_me()
        raw_session_str = client.session.save()
        encrypted_session = encrypt_data(raw_session_str)
        clean_phone = phone.replace("+", "")
        await db_mgr.db.accounts.update_one(
            {"phone": clean_phone},
            {"$set": {
                "phone": clean_phone,
                "user_id": user_id,
                "username": me.username or "None",
                "session_string": encrypted_session,
                "status": "active",
                "last_active": time.time()
            }},
            upsert=True
        )
        await message.answer(f"🎉 <b>Success!</b> Account <code>+{phone}</code> is connected.", reply_markup=get_post_registration_keyboard(), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ <b>Error:</b> <code>{str(e)}</code>", parse_mode="HTML")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📁 <b>Send your Telethon String Session:</b>", parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    session_str = message.text.strip()
    try:
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        me = await client.get_me()
        phone = me.phone or f"custom_{me.id}"
        encrypted_session = encrypt_data(session_str)
        clean_phone = phone.replace("+", "")
        await db_mgr.db.accounts.update_one(
            {"phone": clean_phone},
            {"$set": {
                "phone": clean_phone,
                "user_id": user_id,
                "username": me.username or "None",
                "session_string": encrypted_session,
                "status": "active",
                "last_active": time.time()
            }},
            upsert=True
        )
        await client.disconnect()
        await message.answer("✨ Session added successfully!", reply_markup=get_post_registration_keyboard(), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Error: {e}")
    await state.clear()

# --- AUTOMATED AD SENDER TASK WIZARD ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    if not await db_mgr.is_user_authorized(user_id):
        await callback.message.edit_text("⚠️ You are restricted. Only Super Owner can give you access.")
        return

    role = await db_mgr.get_user_role(user_id)
    if role == "super_owner":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Use Own Accounts", callback_data="set_routing:own", style="primary")],
            [InlineKeyboardButton(text="👑 Use All Accounts", callback_data="set_routing:all", style="success")]
        ])
        await callback.message.edit_text("<b>👑 Super Owner Options:</b> Select account routing:", reply_markup=kb, parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_routing_choice)
    else:
        await state.update_data(account_routing="own")
        await prompt_delay_selection(callback.message, state)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_routing_choice), F.data.startswith("set_routing:"))
async def task_hub_process_routing(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    routing = callback.data.split(":")[1]
    await state.update_data(account_routing=routing)
    await prompt_delay_selection(callback.message, state)

async def prompt_delay_selection(message: Message, state: FSMContext):
    await message.edit_text(
        "<b>Step 1: Select message delay interval between sends:</b>\n"
        "<i>Options: 10 mins, 20 mins, 30 mins, 1 hr, 2 hrs</i>",
        reply_markup=get_delay_selection_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(TaskWizardStates.waiting_for_delay_choice)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_delay_choice), F.data.startswith("set_delay:"))
async def task_hub_process_delay(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    delay_min = int(callback.data.split(":")[1])
    await state.update_data(delay_minutes=delay_min)
    await callback.message.edit_text(
        "<b>Step 2: Send the ad message text or source post link you want to forward to joined groups:</b>",
        parse_mode="HTML"
    )
    await state.set_state(TaskWizardStates.waiting_for_ad_source)

@router.message(StateFilter(TaskWizardStates.waiting_for_ad_source))
async def task_hub_process_ad_source(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    await message.answer(
        "<b>Step 3: Enter the number of accounts to use (Type <code>0</code> for ALL available accounts):</b>",
        parse_mode="HTML"
    )
    await state.set_state(TaskWizardStates.waiting_for_account_scale)

@router.message(StateFilter(TaskWizardStates.waiting_for_account_scale))
async def process_account_scale(message: Message, state: FSMContext, bot: Bot):
    scale_text = message.text.strip()
    if not scale_text.isdigit():
        await message.answer("❌ Enter a valid number.")
        return
        
    requested_count = int(scale_text)
    await state.update_data(run_account_count=requested_count)
    await finalize_ad_task(message, state, bot)

async def finalize_ad_task(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    
    init_msg = await bot.send_message(
        chat_id=user_id, 
        text="⏳ <b>Initializing Automated Ad Forwarding Task...</b>",
        parse_mode="HTML"
    )

    task_id = await db_mgr.get_next_task_id()
    await db_mgr.db.tasks.insert_one({
        "task_id": task_id,
        "creator_id": user_id,
        "type": "ad_forward",
        "payload": json.dumps(data),
        "status": "pending",
        "progress": "0%",
        "created_at": time.time()
    })

    await task_queue.add_task(task_id, user_id, "ad_forward", data, bot, init_msg.message_id)
    await state.clear()

@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    rows = await db_mgr.db.tasks.find({"creator_id": user_id}).sort("task_id", -1).limit(10).to_list(length=10)

    text = "📊 <b>Campaign Task Logs</b>\n\n"
    for r in rows:
        text += f"🔹 <b>Task ID:</b> <code>#{r['task_id']}</code> | Status: <b>{r['status']}</b> | Progress: <code>{r['progress']}</code>\n"
    await callback.message.edit_text(text if rows else "No active campaign tasks.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return Back", callback_data="main_menu", style="primary")]]), parse_mode="HTML")

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    count = await db_mgr.db.users.count_documents({"referred_by": user_id})
    await callback.message.edit_text(f"👥 <b>Referral Link:</b>\n<code>https://t.me/{bot_username}?start=ref_{user_id}</code>\n\nTotal referrals: <code>{count}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return Back", callback_data="main_menu", style="primary")]]), parse_mode="HTML")

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="💾 Check Database Storage", callback_data="check_db_storage", style="primary")],
        [InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]
    ]
    await callback.message.edit_text(
        "🛡️ <b>Administrative Console</b>\n\n"
        "Commands:\n"
        "🔹 <code>/grantaccess &lt;user_id&gt; [count]</code>\n"
        "🔹 <code>/revokeaccess &lt;user_id&gt;</code>\n"
        "🔹 <code>/broadcast</code>\n"
        "🔹 <code>/canceltasks</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "check_db_storage")
async def handle_check_db_storage(callback: CallbackQuery, bot: Bot):
    stats = await db_mgr.get_storage_stats()
    text = f"💾 <b>DB Storage</b>\nData Size: <code>{stats.get('data_size_mb', 0)} MB</code>"
    buttons = [[InlineKeyboardButton(text="🔙 Return Back", callback_data="admin_panel", style="primary")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

# Verification routine disabled per request
async def verify_saved_sessions():
    pass

async def main():
    global bot_username
    await db_mgr.init()
    # verify_saved_sessions() is bypassed so startup remains uninterrupted
    if not config.BOT_TOKEN:
        logger.error("Bot token missing!")
        return
    bot = Bot(token=config.BOT_TOKEN)
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    worker_task = asyncio.create_task(task_queue.start_worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot execution successfully stopped.")
