import os
import logging
import json
import tempfile
import asyncio
from datetime import datetime
from typing import Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Document
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════════════════════
#                              CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8372967401:AAHXy4nGkL7TI3nwSDRFM3ovOpMKrFzmqPg"
DATA_FILE = "users_data.json"

# Conversation states
COMBINE_WAITING = 1
SPLIT_WAITING = 2
SPLIT_METHOD = 3
SPLIT_VALUE = 4
MAKETXT_WAITING = 5
CSVTOTXT_WAITING = 6

# ═══════════════════════════════════════════════════════════════════════════════
#                              LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#                              DATA MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def load_users() -> Dict:
    """Load users data from JSON file."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users: Dict) -> None:
    """Save users data to JSON file."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

def register_user(user) -> None:
    """Register or update user in database."""
    users = load_users()
    user_id = str(user.id)
    users[user_id] = {
        "id": user.id,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "username": user.username or "",
        "last_active": datetime.now().isoformat()
    }
    save_users(users)

# ═══════════════════════════════════════════════════════════════════════════════
#                              UI COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

HEADER = """
╔══════════════════════════════════════╗
║       FILE TOOLKIT BOT               ║
║          Dev: Levetche               ║
╚══════════════════════════════════════╝"""

DIVIDER = "━" * 40

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Create main menu keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("◈ COMBINER ◈", callback_data="combine"),
            InlineKeyboardButton("◈ SPLITTER ◈", callback_data="split"),
        ],
        [
            InlineKeyboardButton("◈ MAKE TXT ◈", callback_data="maketxt"),
            InlineKeyboardButton("◈ CSV→TXT ◈", callback_data="csvtotxt"),
        ],
        [
            InlineKeyboardButton("▣ STATS", callback_data="stats"),
            InlineKeyboardButton("▣ HELP", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def back_keyboard() -> InlineKeyboardMarkup:
    """Create back button keyboard."""
    keyboard = [[InlineKeyboardButton("◄ BACK TO MENU", callback_data="menu")]]
    return InlineKeyboardMarkup(keyboard)

def combine_keyboard() -> InlineKeyboardMarkup:
    """Create combiner action keyboard."""
    keyboard = [
        [InlineKeyboardButton("▶ COMBINE NOW", callback_data="do_combine")],
        [InlineKeyboardButton("✕ CLEAR FILES", callback_data="clear_combine")],
        [InlineKeyboardButton("◄ CANCEL", callback_data="cancel_combine")],
    ]
    return InlineKeyboardMarkup(keyboard)

def split_method_keyboard() -> InlineKeyboardMarkup:
    """Create split method selection keyboard."""
    keyboard = [
        [InlineKeyboardButton("◈ BY MAX SIZE (KB)", callback_data="split_size")],
        [InlineKeyboardButton("◈ BY NUMBER OF FILES", callback_data="split_count")],
        [InlineKeyboardButton("◈ BY MAX LINES", callback_data="split_lines")],
        [InlineKeyboardButton("◄ CANCEL", callback_data="cancel_split")],
    ]
    return InlineKeyboardMarkup(keyboard)

def cancel_keyboard() -> InlineKeyboardMarkup:
    """Create cancel keyboard."""
    keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="menu")]]
    return InlineKeyboardMarkup(keyboard)

def maketxt_keyboard() -> InlineKeyboardMarkup:
    """Create make txt action keyboard."""
    keyboard = [
        [InlineKeyboardButton("▶ CREATE TXT", callback_data="do_maketxt")],
        [InlineKeyboardButton("✕ CLEAR LINES", callback_data="clear_maketxt")],
        [InlineKeyboardButton("◄ CANCEL", callback_data="cancel_maketxt")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ═══════════════════════════════════════════════════════════════════════════════
#                              COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command."""
    user = update.effective_user
    register_user(user)
    
    welcome_text = f"""
{HEADER}

  Welcome, {user.first_name}!

{DIVIDER}

  ◈ COMBINER
    Merge multiple TXT/CSV files
    into a single TXT file

  ◈ SPLITTER
    Split large TXT/CSV files
    by size, count, or lines

  ◈ MAKE TXT
    Convert text messages to TXT

  ◈ CSV→TXT
    Convert CSV file to TXT

{DIVIDER}
      Select an option below
{DIVIDER}"""
    
    # Clear any previous session data
    context.user_data.clear()
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=main_menu_keyboard(),
        parse_mode=None
    )
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    register_user(update.effective_user)
    
    help_text = f"""
{HEADER}

           HELP GUIDE

{DIVIDER}

  ► COMBINER
    1. Select COMBINER
    2. Send multiple TXT/CSV files
    3. Click COMBINE NOW
    4. Receive merged TXT file

  ► SPLITTER
    1. Select SPLITTER
    2. Send a TXT/CSV file
    3. Choose split method
    4. Enter the value
    5. Receive split TXT files

  ► MAKE TXT
    1. Select MAKE TXT
    2. Send text messages
    3. Click CREATE TXT
    4. Receive TXT file

  ► CSV→TXT
    1. Select CSV→TXT
    2. Send a CSV file
    3. Receive TXT file

  ► COMMANDS
    /start  - Main menu
    /help   - This guide
    /stats  - User statistics

  ► NOTE
    Duplicates removed automatically

{DIVIDER}"""
    
    await update.message.reply_text(help_text, reply_markup=back_keyboard())

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats command."""
    register_user(update.effective_user)
    await show_stats(update, context)

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user statistics."""
    users = load_users()
    total_users = len(users)
    
    if total_users == 0:
        stats_text = f"""
{HEADER}

         USER STATISTICS

{DIVIDER}

    No users registered yet

{DIVIDER}"""
    else:
        user_list = ""
        for idx, (uid, data) in enumerate(users.items(), 1):
            name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
            username = data.get('username', '')
            username_display = f"@{username}" if username else "[no username]"
            user_list += f"  {idx:03d}. {name[:15]:<15} {username_display}\n"
        
        stats_text = f"""
{HEADER}

         USER STATISTICS

{DIVIDER}
  Total Users: {total_users}
{DIVIDER}

{user_list}
{DIVIDER}"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            stats_text,
            reply_markup=back_keyboard()
        )
    else:
        await update.message.reply_text(stats_text, reply_markup=back_keyboard())

# ═══════════════════════════════════════════════════════════════════════════════
#                              CALLBACK HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    register_user(update.effective_user)
    data = query.data
    
    # ─────────────────────────────────────────────────────────────────────────
    # MENU NAVIGATION
    # ─────────────────────────────────────────────────────────────────────────
    
    if data == "menu":
        context.user_data.clear()
        menu_text = f"""
{HEADER}

            MAIN MENU

{DIVIDER}

  ◈ COMBINER - Merge files
  ◈ SPLITTER - Split files
  ◈ MAKE TXT - Text to file
  ◈ CSV→TXT  - Convert CSV

{DIVIDER}
      Select an option below
{DIVIDER}"""
        await query.edit_message_text(menu_text, reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    
    elif data == "help":
        help_text = f"""
{HEADER}

           HELP GUIDE

{DIVIDER}

  ► COMBINER
    1. Select COMBINER
    2. Send multiple TXT/CSV files
    3. Click COMBINE NOW
    4. Receive merged TXT file

  ► SPLITTER
    1. Select SPLITTER
    2. Send a TXT/CSV file
    3. Choose split method
    4. Enter the value
    5. Receive split TXT files

  ► MAKE TXT
    1. Select MAKE TXT
    2. Send text messages
    3. Click CREATE TXT
    4. Receive TXT file

  ► NOTE
    Duplicates removed automatically

{DIVIDER}"""
        await query.edit_message_text(help_text, reply_markup=back_keyboard())
        return ConversationHandler.END
    
    elif data == "stats":
        await show_stats(update, context)
        return ConversationHandler.END
    
    # ─────────────────────────────────────────────────────────────────────────
    # COMBINER
    # ─────────────────────────────────────────────────────────────────────────
    
    elif data == "combine":
        context.user_data["combine_files"] = []
        context.user_data["mode"] = "combine"
        
        combine_text = f"""
{HEADER}

            COMBINER

{DIVIDER}

  ► Send TXT or CSV files
  ► Files will be merged by lines
  ► Output: single TXT file

  Files received: 0

{DIVIDER}
      Send your files below
{DIVIDER}"""
        
        await query.edit_message_text(combine_text, reply_markup=combine_keyboard())
        return COMBINE_WAITING
    
    elif data == "do_combine":
        files = context.user_data.get("combine_files", [])
        if len(files) < 2:
            await query.answer("Please send at least 2 files to combine!", show_alert=True)
            return COMBINE_WAITING
        
        await do_combine_files(update, context)
        return ConversationHandler.END
    
    elif data == "clear_combine":
        context.user_data["combine_files"] = []
        combine_text = f"""
{HEADER}

            COMBINER

{DIVIDER}

  ► Files cleared!
  ► Send new TXT or CSV files

  Files received: 0

{DIVIDER}
      Send your files below
{DIVIDER}"""
        await query.edit_message_text(combine_text, reply_markup=combine_keyboard())
        return COMBINE_WAITING
    
    elif data == "cancel_combine":
        context.user_data.clear()
        return await button_callback_menu(update, context)
    
    # ─────────────────────────────────────────────────────────────────────────
    # SPLITTER
    # ─────────────────────────────────────────────────────────────────────────
    
    elif data == "split":
        context.user_data["mode"] = "split"
        context.user_data["split_file"] = None
        
        split_text = f"""
{HEADER}

            SPLITTER

{DIVIDER}

  ► Send a TXT or CSV file
  ► Choose split method
  ► Output: multiple TXT files

  Waiting for file...

{DIVIDER}
       Send your file below
{DIVIDER}"""
        
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_split")]]
        await query.edit_message_text(split_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return SPLIT_WAITING
    
    elif data == "cancel_split":
        context.user_data.clear()
        return await button_callback_menu(update, context)
    
    elif data.startswith("split_"):
        method = data.replace("split_", "")
        context.user_data["split_method"] = method
        
        method_names = {
            "size": "MAX SIZE (KB)",
            "count": "NUMBER OF FILES",
            "lines": "MAX LINES PER FILE"
        }
        
        prompts = {
            "size": "Enter maximum size per file in KB:",
            "count": "Enter number of files to split into:",
            "lines": "Enter maximum lines per file:"
        }
        
        value_text = f"""
{HEADER}

            SPLITTER

{DIVIDER}

  Method: {method_names[method]}

  {prompts[method]}

{DIVIDER}
       Type a number below
{DIVIDER}"""
        
        keyboard = [[InlineKeyboardButton("◄ BACK", callback_data="split_method_back")]]
        await query.edit_message_text(value_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return SPLIT_VALUE
    
    elif data == "split_method_back":
        split_text = f"""
{HEADER}

            SPLITTER

{DIVIDER}

  ► File received!
  ► Choose split method below

{DIVIDER}
      Select split method
{DIVIDER}"""
        await query.edit_message_text(split_text, reply_markup=split_method_keyboard())
        return SPLIT_METHOD
    
    # ─────────────────────────────────────────────────────────────────────────
    # MAKE TXT
    # ─────────────────────────────────────────────────────────────────────────
    
    elif data == "maketxt":
        context.user_data["maketxt_lines"] = []
        context.user_data["mode"] = "maketxt"
        
        maketxt_text = f"""
{HEADER}

            MAKE TXT

{DIVIDER}

  ► Send text messages
  ► Each message = one line
  ► Duplicates auto-removed

  Lines received: 0

{DIVIDER}
       Send your text below
{DIVIDER}"""
        
        await query.edit_message_text(maketxt_text, reply_markup=maketxt_keyboard())
        return MAKETXT_WAITING
    
    elif data == "do_maketxt":
        lines = context.user_data.get("maketxt_lines", [])
        if len(lines) < 1:
            await query.answer("Please send at least 1 line of text!", show_alert=True)
            return MAKETXT_WAITING
        
        await do_maketxt(update, context)
        return ConversationHandler.END
    
    elif data == "clear_maketxt":
        context.user_data["maketxt_lines"] = []
        maketxt_text = f"""
{HEADER}

            MAKE TXT

{DIVIDER}

  ► Lines cleared!
  ► Send new text messages

  Lines received: 0

{DIVIDER}
       Send your text below
{DIVIDER}"""
        await query.edit_message_text(maketxt_text, reply_markup=maketxt_keyboard())
        return MAKETXT_WAITING
    
    elif data == "cancel_maketxt":
        context.user_data.clear()
        return await button_callback_menu(update, context)
    
    # ─────────────────────────────────────────────────────────────────────────
    # CSV TO TXT
    # ─────────────────────────────────────────────────────────────────────────
    
    elif data == "csvtotxt":
        context.user_data["mode"] = "csvtotxt"
        
        csvtotxt_text = f"""
{HEADER}

           CSV → TXT

{DIVIDER}

  ► Send a CSV file
  ► Converts to TXT format
  ► Duplicates auto-removed

  Waiting for file...

{DIVIDER}
     Send your CSV file below
{DIVIDER}"""
        
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_csvtotxt")]]
        await query.edit_message_text(csvtotxt_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return CSVTOTXT_WAITING
    
    elif data == "cancel_csvtotxt":
        context.user_data.clear()
        return await button_callback_menu(update, context)
    
    return ConversationHandler.END

async def button_callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to main menu."""
    query = update.callback_query
    context.user_data.clear()
    
    menu_text = f"""
{HEADER}

            MAIN MENU

{DIVIDER}

  ◈ COMBINER - Merge files
  ◈ SPLITTER - Split files
  ◈ MAKE TXT - Text to file
  ◈ CSV→TXT  - Convert CSV

{DIVIDER}
      Select an option below
{DIVIDER}"""
    
    await query.edit_message_text(menu_text, reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#                              FILE HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_combine_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle file upload for combining."""
    document = update.message.document
    
    if not document:
        await update.message.reply_text("Please send a valid file.")
        return COMBINE_WAITING
    
    file_name = document.file_name.lower()
    if not (file_name.endswith('.txt') or file_name.endswith('.csv')):
        await update.message.reply_text(
            "⚠ Only TXT and CSV files are supported!",
            reply_markup=combine_keyboard()
        )
        return COMBINE_WAITING
    
    # Check file size (Telegram bot API limit is 20MB)
    if document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "⚠ File too large! Maximum size is 20MB.",
            reply_markup=combine_keyboard()
        )
        return COMBINE_WAITING
    
    # Delete previous status message if exists
    if "combine_status_msg" in context.user_data:
        try:
            await context.user_data["combine_status_msg"].delete()
        except Exception:
            pass
    
    # Download file
    try:
        file = await document.get_file()
        file_content = await file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(
            "⚠ Failed to download file. Try a smaller file.",
            reply_markup=combine_keyboard()
        )
        return COMBINE_WAITING
    
    if "combine_files" not in context.user_data:
        context.user_data["combine_files"] = []
    
    context.user_data["combine_files"].append({
        "name": document.file_name,
        "content": file_content.decode("utf-8", errors="ignore")
    })
    
    count = len(context.user_data["combine_files"])
    file_list = "\n".join([f"  {i+1}. {f['name'][:30]}" 
                           for i, f in enumerate(context.user_data["combine_files"])])
    
    status_text = f"""
{HEADER}

            COMBINER

{DIVIDER}
  Files received: {count}
{DIVIDER}

{file_list}

{DIVIDER}
   Send more files or click COMBINE
{DIVIDER}"""
    
    # Send new status and store reference
    status_msg = await update.message.reply_text(status_text, reply_markup=combine_keyboard())
    context.user_data["combine_status_msg"] = status_msg
    return COMBINE_WAITING

async def do_combine_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Combine all uploaded files."""
    query = update.callback_query
    files = context.user_data.get("combine_files", [])
    
    processing_text = f"""
{HEADER}

          PROCESSING

{DIVIDER}

      ▓▓▓▓▓▓░░░░ 50%

   Combining {len(files)} files...

{DIVIDER}"""
    
    await query.edit_message_text(processing_text)
    
    # Combine files and remove duplicates
    all_lines = []
    for f in files:
        content = f["content"]
        lines = content.splitlines()
        all_lines.extend(lines)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_lines = []
    for line in all_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    
    combined_content = "\n".join(unique_lines)
    if combined_content and not combined_content.endswith("\n"):
        combined_content += "\n"
    
    # Create temporary file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp:
        tmp.write(combined_content)
        tmp_path = tmp.name
    
    # Send file
    total_lines = len(unique_lines)
    dupes_removed = len(all_lines) - len(unique_lines)
    
    result_text = f"""
{HEADER}

           COMPLETED

{DIVIDER}

  ► Files combined: {len(files)}
  ► Total lines: {total_lines}
  ► Duplicates removed: {dupes_removed}
  ► Output format: TXT

{DIVIDER}"""
    
    await query.edit_message_text(result_text, reply_markup=back_keyboard())
    
    with open(tmp_path, 'rb') as f:
        await query.message.reply_document(
            document=f,
            filename="combined_output.txt",
            caption="► Combined file ready!"
        )
    
    # Cleanup
    os.unlink(tmp_path)
    context.user_data.clear()

async def handle_split_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle file upload for splitting."""
    document = update.message.document
    
    if not document:
        await update.message.reply_text("Please send a valid file.")
        return SPLIT_WAITING
    
    file_name = document.file_name.lower()
    if not (file_name.endswith('.txt') or file_name.endswith('.csv')):
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_split")]]
        await update.message.reply_text(
            "⚠ Only TXT and CSV files are supported!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SPLIT_WAITING
    
    # Check file size (Telegram bot API limit is 20MB)
    if document.file_size > 20 * 1024 * 1024:
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_split")]]
        await update.message.reply_text(
            "⚠ File too large! Maximum size is 20MB.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SPLIT_WAITING
    
    # Download file
    try:
        file = await document.get_file()
        file_content = await file.download_as_bytearray()
    except Exception as e:
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_split")]]
        await update.message.reply_text(
            "⚠ Failed to download file. Try a smaller file.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SPLIT_WAITING
    
    context.user_data["split_file"] = {
        "name": document.file_name,
        "content": file_content.decode("utf-8", errors="ignore")
    }
    
    lines = context.user_data["split_file"]["content"].count('\n') + 1
    size_kb = len(file_content) / 1024
    
    split_text = f"""
{HEADER}

            SPLITTER

{DIVIDER}

  ► File: {document.file_name[:28]}
  ► Size: {size_kb:.1f} KB
  ► Lines: {lines}

  Choose split method:

{DIVIDER}
       Select method below
{DIVIDER}"""
    
    await update.message.reply_text(split_text, reply_markup=split_method_keyboard())
    return SPLIT_METHOD

async def handle_split_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle split value input."""
    try:
        value = int(update.message.text.strip())
        if value <= 0:
            raise ValueError("Value must be positive")
    except ValueError:
        await update.message.reply_text("⚠ Please enter a valid positive number!")
        return SPLIT_VALUE
    
    method = context.user_data.get("split_method")
    file_data = context.user_data.get("split_file")
    
    if not file_data:
        await update.message.reply_text("⚠ No file found. Please start over.", reply_markup=back_keyboard())
        return ConversationHandler.END
    
    content = file_data["content"]
    
    # Remove duplicates while preserving order
    raw_lines = content.splitlines()
    seen = set()
    unique_lines = []
    for line in raw_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    
    dupes_removed = len(raw_lines) - len(unique_lines)
    lines = [line + "\n" for line in unique_lines]
    
    processing_text = f"""
{HEADER}

          PROCESSING

{DIVIDER}

      ▓▓▓▓▓▓░░░░ 50%

    Splitting file...

{DIVIDER}"""
    
    status_msg = await update.message.reply_text(processing_text)
    
    # Split logic
    chunks = []
    
    if method == "lines":
        # Split by max lines per file
        for i in range(0, len(lines), value):
            chunk = lines[i:i + value]
            chunks.append("".join(chunk))
    
    elif method == "count":
        # Split into N files
        if value > len(lines):
            value = len(lines)
        chunk_size = len(lines) // value
        remainder = len(lines) % value
        
        start = 0
        for i in range(value):
            extra = 1 if i < remainder else 0
            end = start + chunk_size + extra
            chunk = lines[start:end]
            if chunk:
                chunks.append("".join(chunk))
            start = end
    
    elif method == "size":
        # Split by max size in KB
        max_bytes = value * 1024
        current_chunk = ""
        
        for line in lines:
            if len((current_chunk + line).encode('utf-8')) > max_bytes and current_chunk:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        
        if current_chunk:
            chunks.append(current_chunk)
    
    if not chunks:
        chunks = [content]
    
    # Send files
    result_text = f"""
{HEADER}

           COMPLETED

{DIVIDER}

  ► Files created: {len(chunks)}
  ► Duplicates removed: {dupes_removed}
  ► Output format: TXT

{DIVIDER}"""
    
    await status_msg.edit_text(result_text, reply_markup=back_keyboard())
    
    base_name = os.path.splitext(file_data["name"])[0]
    
    for i, chunk in enumerate(chunks, 1):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp:
            tmp.write(chunk)
            tmp_path = tmp.name
        
        with open(tmp_path, 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename=f"{base_name}_part{i:03d}.txt",
                caption=f"► Part {i} of {len(chunks)}"
            )
        
        os.unlink(tmp_path)
        await asyncio.sleep(0.3)  # Prevent rate limiting
    
    context.user_data.clear()
    return ConversationHandler.END

async def handle_maketxt_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle text message for Make TXT."""
    text = update.message.text.strip()
    
    if not text:
        await update.message.reply_text("Please send some text.")
        return MAKETXT_WAITING
    
    if "maketxt_lines" not in context.user_data:
        context.user_data["maketxt_lines"] = []
    
    # Add each line from the message
    new_lines = text.splitlines()
    context.user_data["maketxt_lines"].extend(new_lines)
    
    count = len(context.user_data["maketxt_lines"])
    preview_lines = context.user_data["maketxt_lines"][-5:]  # Show last 5 lines
    preview = "\n".join([f"  {line[:34]}" for line in preview_lines])
    
    status_text = f"""
{HEADER}

            MAKE TXT

{DIVIDER}
  Lines received: {count}
{DIVIDER}
  Last lines:
{preview}
{DIVIDER}
   Send more or click CREATE TXT
{DIVIDER}"""
    
    await update.message.reply_text(status_text, reply_markup=maketxt_keyboard())
    return MAKETXT_WAITING

async def do_maketxt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create TXT file from collected lines."""
    query = update.callback_query
    lines = context.user_data.get("maketxt_lines", [])
    
    processing_text = f"""
{HEADER}

          PROCESSING

{DIVIDER}

      ▓▓▓▓▓▓░░░░ 50%

    Creating TXT file...

{DIVIDER}"""
    
    await query.edit_message_text(processing_text)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_lines = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    
    dupes_removed = len(lines) - len(unique_lines)
    content = "\n".join(unique_lines)
    if content and not content.endswith("\n"):
        content += "\n"
    
    # Create temporary file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    
    result_text = f"""
{HEADER}

           COMPLETED

{DIVIDER}

  ► Total lines: {len(unique_lines)}
  ► Duplicates removed: {dupes_removed}
  ► Output format: TXT

{DIVIDER}"""
    
    await query.edit_message_text(result_text, reply_markup=back_keyboard())
    
    with open(tmp_path, 'rb') as f:
        await query.message.reply_document(
            document=f,
            filename="output.txt",
            caption="► TXT file ready!"
        )
    
    # Cleanup
    os.unlink(tmp_path)
    context.user_data.clear()

async def handle_csvtotxt_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle CSV file upload for conversion to TXT."""
    document = update.message.document
    
    if not document:
        await update.message.reply_text("Please send a valid file.")
        return CSVTOTXT_WAITING
    
    file_name = document.file_name.lower()
    if not file_name.endswith('.csv'):
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_csvtotxt")]]
        await update.message.reply_text(
            "⚠ Only CSV files are supported!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CSVTOTXT_WAITING
    
    # Check file size (Telegram bot API limit is 20MB)
    if document.file_size > 20 * 1024 * 1024:
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_csvtotxt")]]
        await update.message.reply_text(
            "⚠ File too large! Maximum size is 20MB.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CSVTOTXT_WAITING
    
    # Download file
    try:
        file = await document.get_file()
        file_content = await file.download_as_bytearray()
    except Exception as e:
        keyboard = [[InlineKeyboardButton("◄ CANCEL", callback_data="cancel_csvtotxt")]]
        await update.message.reply_text(
            "⚠ Failed to download file. Try a smaller file.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CSVTOTXT_WAITING
    content = file_content.decode("utf-8", errors="ignore")
    
    processing_text = f"""
{HEADER}

          PROCESSING

{DIVIDER}

      ▓▓▓▓▓▓░░░░ 50%

   Converting CSV to TXT...

{DIVIDER}"""
    
    status_msg = await update.message.reply_text(processing_text)
    
    # Remove duplicates while preserving order
    raw_lines = content.splitlines()
    seen = set()
    unique_lines = []
    for line in raw_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    
    dupes_removed = len(raw_lines) - len(unique_lines)
    output_content = "\n".join(unique_lines)
    if output_content and not output_content.endswith("\n"):
        output_content += "\n"
    
    # Create temporary file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp:
        tmp.write(output_content)
        tmp_path = tmp.name
    
    base_name = os.path.splitext(document.file_name)[0]
    
    result_text = f"""
{HEADER}

           COMPLETED

{DIVIDER}

  ► Total lines: {len(unique_lines)}
  ► Duplicates removed: {dupes_removed}
  ► Output format: TXT

{DIVIDER}"""
    
    await status_msg.edit_text(result_text, reply_markup=back_keyboard())
    
    with open(tmp_path, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"{base_name}.txt",
            caption="► TXT file ready!"
        )
    
    # Cleanup
    os.unlink(tmp_path)
    context.user_data.clear()
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#                              FALLBACK HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel current operation."""
    context.user_data.clear()
    await update.message.reply_text(
        "Operation cancelled. Use /start to begin again.",
        reply_markup=back_keyboard()
    )
    return ConversationHandler.END

async def unknown_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unexpected files."""
    register_user(update.effective_user)
    await update.message.reply_text(
        "Use /start to access the menu and select COMBINER or SPLITTER first.",
        reply_markup=back_keyboard()
    )

# ═══════════════════════════════════════════════════════════════════════════════
#                              MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors gracefully."""
    logger.error(f"Exception: {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠ An error occurred. Please try again or use /start to restart.",
            reply_markup=back_keyboard()
        )

def main() -> None:
    """Run the bot."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler for file operations
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(button_callback),
        ],
        states={
            COMBINE_WAITING: [
                MessageHandler(filters.Document.ALL, handle_combine_file),
                CallbackQueryHandler(button_callback),
            ],
            SPLIT_WAITING: [
                MessageHandler(filters.Document.ALL, handle_split_file),
                CallbackQueryHandler(button_callback),
            ],
            SPLIT_METHOD: [
                CallbackQueryHandler(button_callback),
            ],
            SPLIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_split_value),
                CallbackQueryHandler(button_callback),
            ],
            MAKETXT_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_maketxt_text),
                CallbackQueryHandler(button_callback),
            ],
            CSVTOTXT_WAITING: [
                MessageHandler(filters.Document.ALL, handle_csvtotxt_file),
                CallbackQueryHandler(button_callback),
            ],
        },
        fallbacks=[
            CommandHandler("start", start_command),
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(button_callback),
        ],
        per_user=True,
        per_chat=True,
        per_message=False,
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.Document.ALL, unknown_file))
    application.add_error_handler(error_handler)
    
    # Start polling
    print("═" * 50)
    print("  FILE TOOLKIT BOT - RUNNING")
    print("═" * 50)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
