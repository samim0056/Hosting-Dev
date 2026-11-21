#!/usr/bin/env python3
"""
ADVANCED TELEGRAM HOSTING BOT (NO PAYMENT MODULE)
Single-file starter platform for a dedicated Linux VPS.

Install:
    python -m pip install "python-telegram-bot>=21,<23"

Server:
    Install Docker Engine and Git.
    Set BOT_TOKEN and ADMIN_IDS, then run this file.

Example:
    export BOT_TOKEN="123:ABC"
    export ADMIN_IDS="123456789"
    python telegram_hosting_bot.py

Optional environment:
    HOSTING_DATA_DIR=./hosting_data
    MAX_UPLOAD_MB=25
    MAX_EXTRACTED_MB=150
    FREE_PROJECTS=1
    FREE_RAM=512m
    FREE_CPUS=0.50
    LOG_LINES=60

No payment, wallet, UPI, QR-payment, Telegram Stars, or transaction feature
is included. Plans are assigned by admins. GitHub deployment requires a
public HTTPS GitHub repository URL.

SECURITY:
- Run on a dedicated VPS, not a personal workstation.
- Docker socket access is effectively root access to the VPS.
- Containers are resource-limited, but Docker alone is not a perfect sandbox.
- Do not promise public web hosting: this starter does not publish container
  ports or provide a reverse proxy/domain/SSL layer.
- Back up the hosting_data directory regularly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# ================================ CONFIG ==================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DATA_DIR = Path(os.getenv("HOSTING_DATA_DIR", "./hosting_data")).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
DB_FILE = DATA_DIR / "hosting.sqlite3"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_EXTRACTED_MB = int(os.getenv("MAX_EXTRACTED_MB", "150"))
FREE_PROJECTS = int(os.getenv("FREE_PROJECTS", "1"))
LOG_LINES = int(os.getenv("LOG_LINES", "60"))
MAINTENANCE_DEFAULT = "0"

PLANS = {
    "free":    {"ram": "512m", "cpus": "0.50", "slots": FREE_PROJECTS},
    "starter": {"ram": "1g",   "cpus": "1.00", "slots": 2},
    "pro":     {"ram": "2g",   "cpus": "2.00", "slots": 5},
    "ultra":   {"ram": "4g",   "cpus": "3.00", "slots": 10},
    "vip":     {"ram": "4g",   "cpus": "4.00", "slots": 20},
}

RUNTIME_IMAGES = {
    "python": "python:3.12-slim",
    "node": "node:22-alpine",
    "php": "php:8.3-cli-alpine",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hosting-bot")


# ================================ DATABASE ===============================

def connect_db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            joined_at INTEGER NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            plan_until INTEGER,
            referral_by INTEGER,
            banned INTEGER NOT NULL DEFAULT 0,
            last_bonus INTEGER NOT NULL DEFAULT 0,
            bonus_points INTEGER NOT NULL DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            runtime TEXT NOT NULL,
            folder TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'stopped',
            created_at INTEGER NOT NULL,
            restart_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS activity(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            created_at INTEGER NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('maintenance',?)",
            (MAINTENANCE_DEFAULT,),
        )


def setting(key: str, default: str = "") -> str:
    with connect_db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with connect_db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def log_activity(user_id: Optional[int], action: str, detail: str = ""):
    with connect_db() as con:
        con.execute(
            "INSERT INTO activity(user_id,action,detail,created_at) VALUES(?,?,?,?)",
            (user_id, action, detail[:500], int(time.time())),
        )


def ensure_user(user_id: int, referral_by: Optional[int] = None):
    with connect_db() as con:
        exists = con.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not exists:
            if referral_by == user_id:
                referral_by = None
            con.execute(
                "INSERT INTO users(user_id,joined_at,referral_by) VALUES(?,?,?)",
                (user_id, int(time.time()), referral_by),
            )
            if referral_by:
                con.execute(
                    "UPDATE users SET bonus_points=bonus_points+10 WHERE user_id=?",
                    (referral_by,),
                )
                log_activity(referral_by, "referral", f"Invited user {user_id}")
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_user(user_id: int):
    with connect_db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_projects(user_id: int):
    with connect_db() as con:
        return con.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def get_project(project_id: str):
    with connect_db() as con:
        return con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


def save_project(pid: str, uid: int, name: str, runtime: str, folder: Path):
    with connect_db() as con:
        con.execute(
            "INSERT INTO projects(id,user_id,name,runtime,folder,created_at) VALUES(?,?,?,?,?,?)",
            (pid, uid, name, runtime, str(folder), int(time.time())),
        )


def update_project(pid: str, **values):
    allowed = {"name", "status", "restart_count", "last_error"}
    values = {k: v for k, v in values.items() if k in allowed}
    if not values:
        return
    fields = ", ".join(f"{k}=?" for k in values)
    with connect_db() as con:
        con.execute(
            f"UPDATE projects SET {fields} WHERE id=?",
            (*values.values(), pid),
        )


def remove_project(pid: str):
    with connect_db() as con:
        con.execute("DELETE FROM projects WHERE id=?", (pid,))


# ================================ HELPERS ================================

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_banned(uid: int) -> bool:
    user = get_user(uid)
    return bool(user and user["banned"])


def active_plan(user) -> str:
    plan = user["plan"] or "free"
    until = user["plan_until"]
    if plan != "free" and until and until < int(time.time()):
        with connect_db() as con:
            con.execute(
                "UPDATE users SET plan='free',plan_until=NULL WHERE user_id=?",
                (user["user_id"],),
            )
        return "free"
    return plan if plan in PLANS else "free"


def plan_limits(uid: int):
    user = get_user(uid)
    plan = active_plan(user) if user else "free"
    limits = PLANS[plan].copy()
    if is_admin(uid):
        limits["slots"] = 100
        limits["ram"] = "4g"
        limits["cpus"] = "4.00"
    return plan, limits


def clean_name(value: str) -> str:
    return (re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_") or "project")[:32]


def docker(*args: str, timeout: int = 60):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def docker_ok() -> bool:
    try:
        return docker("info", timeout=8).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def container_name(pid: str) -> str:
    return "tg-host-" + re.sub(r"[^a-f0-9]", "", pid.lower())[:16]


def safe_extract(archive: Path, dest: Path):
    total = 0
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError("ZIP contains an unsafe path.")
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_EXTRACTED_MB * 1024 * 1024:
                raise ValueError("Extracted ZIP is too large.")
            target = (root / Path(*member.parts)).resolve()
            if root not in target.parents:
                raise ValueError("ZIP path traversal blocked.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o644)


def detect_runtime(folder: Path):
    if (folder / "package.json").exists():
        return "node"
    if (folder / "index.php").exists():
        return "php"
    if (folder / "main.py").exists() or list(folder.glob("*.py")):
        return "python"
    raise ValueError(
        "Cannot detect project. Include main.py, package.json, index.php, "
        "or a top-level .py file."
    )


def github_url_valid(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"github.com", "www.github.com"}
            and not parsed.username and not parsed.password
        )
    except Exception:
        return False


# ================================ HOSTING ================================

def start_project(project):
    pid = project["id"]
    folder = Path(project["folder"]).resolve()
    if not folder.is_dir():
        return False, "Project directory is missing."

    _, limits = plan_limits(project["user_id"])
    name = container_name(pid)
    try:
        docker("rm", "-f", name, timeout=20)
        runtime = project["runtime"]
        image = RUNTIME_IMAGES[runtime]

        if runtime == "python":
            py = "main.py" if (folder / "main.py").exists() else sorted(folder.glob("*.py"))[0].name
            command = (
                "if [ -f requirements.txt ]; then "
                "pip install --no-cache-dir -r requirements.txt; fi; "
                f"python -u {py}"
            )
        elif runtime == "node":
            command = "npm install --omit=dev && npm start"
        else:
            command = "php index.php"

        # Containers run with bounded CPU/RAM/PIDs, dropped Linux capabilities,
        # no-new-privileges, and a dedicated writable project directory.
        result = docker(
            "run", "-d", "--name", name,
            "--memory", limits["ram"], "--cpus", limits["cpus"],
            "--pids-limit", "128",
            "--restart", "unless-stopped",
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",
            "--label", f"telegram-hosting.project={pid}",
            "--label", f"telegram-hosting.user={project['user_id']}",
            "--workdir", "/app",
            "--mount", f"type=bind,src={folder},dst=/app",
            "--tmpfs", "/tmp:rw,nosuid,size=64m",
            image, "sh", "-lc", command,
            timeout=120,
        )
        if result.returncode:
            err = (result.stderr or result.stdout)[-1800:]
            update_project(pid, status="error", last_error=err)
            return False, err or "Docker failed to start project."
        update_project(pid, status="running", last_error="")
        log_activity(project["user_id"], "project_start", pid)
        return True, "Hosting started."
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError) as exc:
        update_project(pid, status="error", last_error=str(exc))
        return False, str(exc)


def stop_project(project):
    result = docker("rm", "-f", container_name(project["id"]), timeout=30)
    if result.returncode and "No such container" not in (result.stderr + result.stdout):
        return False, (result.stderr or result.stdout)[-1000:]
    update_project(project["id"], status="stopped")
    log_activity(project["user_id"], "project_stop", project["id"])
    return True, "Hosting stopped."


def inspect_status(project):
    result = docker(
        "inspect", "-f", "{{.State.Status}}",
        container_name(project["id"]), timeout=10,
    )
    if result.returncode:
        status = "stopped"
    else:
        status = result.stdout.strip()
    update_project(project["id"], status=status)
    return status


def project_logs(project):
    result = docker(
        "logs", "--tail", str(LOG_LINES),
        container_name(project["id"]), timeout=15,
    )
    return ((result.stdout or "") + (result.stderr or ""))[-3500:] or "No logs available."


def project_stats(project):
    result = docker(
        "stats", "--no-stream", "--format",
        "{{.CPUPerc}} | {{.MemUsage}} | {{.BlockIO}}",
        container_name(project["id"]), timeout=15,
    )
    if result.returncode:
        return "Container is not running."
    return result.stdout.strip() or "Stats unavailable."


def create_backup(project):
    folder = Path(project["folder"]).resolve()
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / f"{project['id']}-{int(time.time())}.zip"
    shutil.make_archive(str(archive.with_suffix("")), "zip", folder)
    return archive


# ================================ TELEGRAM UI =============================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 New Hosting", callback_data="new"),
         InlineKeyboardButton("📂 My Projects", callback_data="projects")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats"),
         InlineKeyboardButton("💎 Plans", callback_data="plans")],
        [InlineKeyboardButton("👥 Referral", callback_data="referral"),
         InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("🆘 Support / Help", callback_data="help")],
    ])


def project_menu(pid: str, status: str):
    rows = []
    if status == "running":
        rows.append([
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{pid}"),
            InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{pid}"),
        ])
    else:
        rows.append([InlineKeyboardButton("▶️ Start", callback_data=f"start:{pid}")])
    rows.extend([
        [InlineKeyboardButton("📜 Logs", callback_data=f"logs:{pid}"),
         InlineKeyboardButton("📈 Resources", callback_data=f"resources:{pid}")],
        [InlineKeyboardButton("💾 Backup", callback_data=f"backup:{pid}"),
         InlineKeyboardButton("✏️ Rename", callback_data=f"rename:{pid}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{pid}")],
        [InlineKeyboardButton("⬅️ My Projects", callback_data="projects")],
    ])
    return InlineKeyboardMarkup(rows)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ref = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            ref = int(context.args[0][4:])
        except ValueError:
            pass
    ensure_user(uid, ref)
    if is_banned(uid):
        await update.message.reply_text("Your account is restricted. Contact support.")
        return
    if setting("maintenance", "0") == "1" and not is_admin(uid):
        await update.message.reply_text("🛠 Bot is under maintenance. Please try later.")
        return
    await update.message.reply_text(
        "🤖 *Advanced Telegram Hosting Bot*\\n\\n"
        "Host Python, Node.js and PHP bot projects from Telegram.\\n"
        "Send a `.py` file or `.zip` project, or use the menu below.",
        parse_mode="Markdown", reply_markup=main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 *Help*\\n"
        "• Upload `.py` or `.zip` to create a project.\\n"
        "• Python ZIP: `main.py` + optional `requirements.txt`.\\n"
        "• Node ZIP: `package.json` with a `start` script.\\n"
        "• PHP ZIP: `index.php`.\\n"
        "• Use My Projects to start, stop, view logs, backup or delete.\\n"
        "• GitHub deploy: `/github https://github.com/owner/repo`\\n\\n"
        "Plans are assigned by the admin. No payment features are included.",
        parse_mode="Markdown",
    )


async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    if is_banned(uid):
        await update.message.reply_text("Account restricted.")
        return
    if setting("maintenance", "0") == "1" and not is_admin(uid):
        await update.message.reply_text("Bot is under maintenance.")
        return

    doc = update.message.document
    filename = doc.file_name or "project"
    ext = Path(filename).suffix.lower()
    if ext not in {".py", ".zip"}:
        await update.message.reply_text("Upload a `.py` file or `.zip` project.", parse_mode="Markdown")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await update.message.reply_text(f"Maximum upload size is {MAX_UPLOAD_MB} MB.")
        return

    plan, limits = plan_limits(uid)
    existing = get_projects(uid)
    if len(existing) >= limits["slots"]:
        await update.message.reply_text(
            f"Your {plan.upper()} plan allows {limits['slots']} project(s)."
        )
        return

    pid = uuid.uuid4().hex[:10]
    folder = (PROJECTS_DIR / str(uid) / pid).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    temp = DATA_DIR / f"upload-{pid}{ext}"
    try:
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=temp)
        if ext == ".zip":
            safe_extract(temp, folder)
        else:
            shutil.copy2(temp, folder / "main.py")
        runtime = detect_runtime(folder)
        name = clean_name(Path(filename).stem)
        save_project(pid, uid, name, runtime, folder)
        log_activity(uid, "project_upload", pid)
        await update.message.reply_text(
            f"✅ Project created\\nName: `{name}`\\nRuntime: `{runtime}`\\nID: `{pid}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📂 Open My Projects", callback_data="projects")
            ]]),
        )
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        await update.message.reply_text(f"❌ Upload failed: {str(exc)[:600]}")
    finally:
        temp.unlink(missing_ok=True)


async def github_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    if is_banned(uid):
        return
    if not context.args:
        await update.message.reply_text("Usage: /github https://github.com/owner/repository")
        return
    url = context.args[0].rstrip("/")
    if not github_url_valid(url):
        await update.message.reply_text("Only public HTTPS GitHub repository URLs are supported.")
        return
    _, limits = plan_limits(uid)
    if len(get_projects(uid)) >= limits["slots"]:
        await update.message.reply_text("You have reached your hosting slot limit.")
        return
    pid = uuid.uuid4().hex[:10]
    folder = (PROJECTS_DIR / str(uid) / pid).resolve()
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = await asyncio.to_thread(
            subprocess.run, ["git", "clone", "--depth", "1", url, str(folder)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or "Git clone failed.")[-700:])
        runtime = detect_runtime(folder)
        name = clean_name(urlparse(url).path.strip("/").split("/")[-1].removesuffix(".git"))
        save_project(pid, uid, name, runtime, folder)
        log_activity(uid, "github_deploy", url)
        await update.message.reply_text(
            f"✅ GitHub project imported: `{name}` ({runtime})",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📂 My Projects", callback_data="projects")
            ]]),
        )
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        await update.message.reply_text(f"❌ GitHub deploy failed: {str(exc)[:700]}")


async def show_projects(query, uid: int):
    projects = get_projects(uid)
    if not projects:
        await query.edit_message_text(
            "No projects yet. Upload a `.py` or `.zip` file.",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
        return
    buttons = []
    for p in projects[:40]:
        status = await asyncio.to_thread(inspect_status, p)
        buttons.append([InlineKeyboardButton(
            f"{'🟢' if status == 'running' else '⚪'} {p['name']} [{p['runtime']}]",
            callback_data=f"open:{p['id']}",
        )])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="home")])
    await query.edit_message_text(
        "📂 *My Projects*", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, data = q.from_user.id, q.data or ""
    ensure_user(uid)
    if is_banned(uid) and not is_admin(uid):
        await q.edit_message_text("Your account is restricted.")
        return
    if setting("maintenance", "0") == "1" and not is_admin(uid) and data != "help":
        await q.edit_message_text("🛠 Bot is under maintenance.")
        return

    if data == "home":
        await q.edit_message_text("🏠 Main Menu", reply_markup=main_menu())
    elif data == "new":
        await q.edit_message_text(
            "📤 Upload a `.py` file or `.zip` project.\\n"
            "Or use `/github https://github.com/owner/repo`.",
            parse_mode="Markdown",
        )
    elif data == "projects":
        await show_projects(q, uid)
    elif data == "help":
        await q.edit_message_text(
            "Upload a `.py` or `.zip` file. GitHub: `/github URL`.",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
    elif data == "profile":
        user = get_user(uid)
        plan = active_plan(user)
        until = user["plan_until"]
        until_text = time.strftime("%Y-%m-%d", time.localtime(until)) if until else "No expiry"
        await q.edit_message_text(
            f"👤 *Profile*\\nID: `{uid}`\\nPlan: `{plan.upper()}`\\n"
            f"Plan expiry: `{until_text}`\\nProjects: {len(get_projects(uid))}\\n"
            f"Referral points: {user['bonus_points']}",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
    elif data == "stats":
        projects = get_projects(uid)
        running = 0
        for p in projects:
            if await asyncio.to_thread(inspect_status, p) == "running":
                running += 1
        await q.edit_message_text(
            f"📊 Your Statistics\\nProjects: {len(projects)}\\nRunning: {running}\\nStopped: {len(projects)-running}",
            reply_markup=main_menu(),
        )
    elif data == "plans":
        lines = ["💎 *Hosting Plans*"]
        for key, value in PLANS.items():
            lines.append(
                f"\\n*{key.upper()}* — RAM {value['ram']}, CPU {value['cpus']}, "
                f"Slots {value['slots']}"
            )
        lines.append("\\nPlans are assigned by an admin. No payment system is included.")
        await q.edit_message_text("\\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())
    elif data == "referral":
        bot = await context.bot.get_me()
        await q.edit_message_text(
            f"👥 *Referral*\\nInvite link:\\n`https://t.me/{bot.username}?start=ref_{uid}`\\n"
            "You receive 10 referral points for each new user.",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
    elif ":" in data:
        action, pid = data.split(":", 1)
        p = get_project(pid)
        if not p or (p["user_id"] != uid and not is_admin(uid)):
            await q.edit_message_text("Project not found or access denied.")
            return
        if action == "open":
            status = await asyncio.to_thread(inspect_status, p)
            await q.edit_message_text(
                f"📦 *{p['name']}*\\nRuntime: `{p['runtime']}`\\n"
                f"Status: `{status}`\\nRestarts: {p['restart_count']}",
                parse_mode="Markdown", reply_markup=project_menu(pid, status),
            )
        elif action in {"start", "restart"}:
            if action == "restart":
                await asyncio.to_thread(stop_project, p)
                with connect_db() as con:
                    con.execute("UPDATE projects SET restart_count=restart_count+1 WHERE id=?", (pid,))
            ok, msg = await asyncio.to_thread(start_project, p)
            await q.edit_message_text(
                ("✅ " if ok else "❌ ") + msg,
                reply_markup=project_menu(pid, "running" if ok else "stopped"),
            )
        elif action == "stop":
            ok, msg = await asyncio.to_thread(stop_project, p)
            await q.edit_message_text(
                ("✅ " if ok else "❌ ") + msg,
                reply_markup=project_menu(pid, "stopped"),
            )
        elif action == "logs":
            logs = await asyncio.to_thread(project_logs, p)
            safe = logs.replace("`", "'")
            await q.edit_message_text(
                f"📜 *Logs — {p['name']}*\\n```\\n{safe[:3200]}\\n```",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data=f"open:{pid}")
                ]]),
            )
        elif action == "resources":
            stats = await asyncio.to_thread(project_stats, p)
            await q.edit_message_text(
                f"📈 *Resources — {p['name']}*\\n`{stats}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data=f"open:{pid}")
                ]]),
            )
        elif action == "backup":
            try:
                archive = await asyncio.to_thread(create_backup, p)
                with open(archive, "rb") as file:
                    await context.bot.send_document(uid, file, filename=archive.name)
                await q.edit_message_text("💾 Backup created and sent.")
            except Exception as exc:
                await q.edit_message_text(f"Backup failed: {str(exc)[:400]}")
        elif action == "rename":
            context.user_data["rename_project"] = pid
            await q.edit_message_text("Send the new project name in your next message.")
        elif action == "delete":
            await asyncio.to_thread(stop_project, p)
            shutil.rmtree(p["folder"], ignore_errors=True)
            remove_project(pid)
            log_activity(uid, "project_delete", pid)
            await q.edit_message_text("🗑 Project deleted.", reply_markup=main_menu())


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = context.user_data.pop("rename_project", None)
    if not pid:
        return
    p = get_project(pid)
    if not p or (p["user_id"] != update.effective_user.id and not is_admin(update.effective_user.id)):
        await update.message.reply_text("Project not found or access denied.")
        return
    name = clean_name(update.message.text or "")
    update_project(pid, name=name)
    log_activity(update.effective_user.id, "project_rename", f"{pid}:{name}")
    await update.message.reply_text(f"Project renamed to: {name}")


# ================================ ADMIN ==================================

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    with connect_db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        projects = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        running = con.execute("SELECT COUNT(*) FROM projects WHERE status='running'").fetchone()[0]
        banned = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
    await update.message.reply_text(
        f"🛡 ADMIN PANEL\\nUsers: {users}\\nProjects: {projects}\\n"
        f"Last-known running: {running}\\nBanned users: {banned}\\n"
        f"Docker: {'Online' if docker_ok() else 'Offline'}\\n"
        f"Maintenance: {setting('maintenance','0')}"
    )


async def admin_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    if len(context.args) < 2:
        return await update.message.reply_text("Usage: /setplan USER_ID free|starter|pro|ultra|vip [days]")
    try:
        uid = int(context.args[0])
        plan = context.args[1].lower()
        days = int(context.args[2]) if len(context.args) > 2 else 30
        if plan not in PLANS:
            raise ValueError("Unknown plan")
        expiry = None if plan == "free" else int(time.time()) + max(1, days) * 86400
        ensure_user(uid)
        with connect_db() as con:
            con.execute("UPDATE users SET plan=?,plan_until=? WHERE user_id=?", (plan, expiry, uid))
        log_activity(update.effective_user.id, "admin_set_plan", f"{uid}:{plan}:{days}")
        await update.message.reply_text(f"Plan {plan.upper()} assigned to {uid}.")
        try:
            await context.bot.send_message(uid, f"Your hosting plan is now {plan.upper()}.")
        except Exception:
            pass
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /ban USER_ID")
    uid = int(context.args[0])
    ensure_user(uid)
    with connect_db() as con:
        con.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await update.message.reply_text(f"User {uid} banned.")


async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /unban USER_ID")
    uid = int(context.args[0])
    with connect_db() as con:
        con.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await update.message.reply_text(f"User {uid} unbanned.")


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    message = " ".join(context.args).strip()
    if not message:
        return await update.message.reply_text("Usage: /broadcast message")
    with connect_db() as con:
        ids = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, message)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    log_activity(update.effective_user.id, "broadcast", f"{sent}/{len(ids)}")
    await update.message.reply_text(f"Broadcast delivered to {sent}/{len(ids)} users.")


async def admin_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        return await update.message.reply_text("Usage: /maintenance on|off")
    value = "1" if context.args[0].lower() == "on" else "0"
    set_setting("maintenance", value)
    await update.message.reply_text(f"Maintenance mode: {context.args[0].upper()}")


async def admin_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    with connect_db() as con:
        rows = con.execute(
            "SELECT user_id,action,detail,created_at FROM activity ORDER BY id DESC LIMIT 20"
        ).fetchall()
    if not rows:
        return await update.message.reply_text("No activity recorded.")
    lines = ["🧾 Recent Activity"]
    for row in rows:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(row["created_at"]))
        lines.append(f"{stamp} | {row['user_id']} | {row['action']} | {row['detail'][:70]}")
    await update.message.reply_text("\\n".join(lines))


async def admin_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Access denied.")
    try:
        disk = shutil.disk_usage(DATA_DIR)
        disk_text = f"{disk.used // (1024**3)} GB / {disk.total // (1024**3)} GB"
    except Exception:
        disk_text = "Unavailable"
    docker_text = "Online" if docker_ok() else "Offline"
    await update.message.reply_text(
        f"🖥 Server Monitor\\nDocker: {docker_text}\\nDisk used: {disk_text}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled Telegram bot error", exc_info=context.error)


# ================================ STARTUP ================================

def main():
    if not BOT_TOKEN:
        raise SystemExit("ERROR: Set BOT_TOKEN environment variable.")
    if not ADMIN_IDS:
        raise SystemExit("ERROR: Set ADMIN_IDS to at least one Telegram numeric user ID.")
    init_db()
    if not docker_ok():
        log.warning("Docker is unavailable. Bot will start, but hosting controls will fail.")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("github", github_cmd))
    app.add_handler(CommandHandler("admin", admin_stats))
    app.add_handler(CommandHandler("setplan", admin_plan))
    app.add_handler(CommandHandler("ban", admin_ban))
    app.add_handler(CommandHandler("unban", admin_unban))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CommandHandler("maintenance", admin_maintenance))
    app.add_handler(CommandHandler("activity", admin_activity))
    app.add_handler(CommandHandler("server", admin_server))
    app.add_handler(MessageHandler(filters.Document.ALL, upload_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    log.info("Advanced Telegram Hosting Bot starting (payment modules disabled).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
