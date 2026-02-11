import logging
import os
import sys
import time
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
import requests
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum

Base = declarative_base()

class UserRole(enum.Enum):
    PARENT = "parent"
    ATHLETE = "athlete"
    COACH = "coach"
    ADMIN = "admin"

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    role = Column(Enum(UserRole), default=UserRole.ATHLETE)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    favorite_team = Column(String)
    favorite_league = Column(String)
    message_count = Column(Integer, default=0)

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String)
    message_content = Column(Text)
    bot_response = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

def get_database_url():
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return DATABASE_URL

engine = create_engine(get_database_url())
SessionLocal = sessionmaker(bind=engine)

def init_db():
    print("Checking database...")
    Base.metadata.create_all(engine)
    print("Database ready!")

def get_db():
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def check_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_TELEGRAM_ID

def get_or_create_user(telegram_id: str, username: str, first_name: str, last_name: str = None):
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            user = User(telegram_id=telegram_id, username=username, first_name=first_name, last_name=last_name, role=UserRole.ADMIN if check_admin(int(telegram_id)) else UserRole.ATHLETE)
            db.add(user)
            db.commit()
            logger.info(f"New user: {first_name} ({telegram_id})")
        return user
    except Exception as e:
        db.rollback()
        logger.error(f"Error: {e}")
        raise
    finally:
        db.close()

def get_daily_stats():
    db = get_db()
    try:
        today = datetime.utcnow().date()
        total_users = db.query(User).count()
        today_messages = db.query(Conversation).filter(Conversation.timestamp >= today).count()
        new_users_today = db.query(User).filter(User.created_at >= today).count()
        return {"total_users": total_users, "messages_today": today_messages, "new_users_today": new_users_today}
    finally:
        db.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    db_user = get_or_create_user(telegram_id, user.username, user.first_name, user.last_name)
    if db_user.role == UserRole.ADMIN:
        welcome = f"""Welcome Admin {user.first_name}!

Soccer Bot with Analytics

Commands:
/analytics - System stats
/broadcast - Message all users
/leaderboard - Top users"""
    else:
        welcome = f"""Welcome {user.first_name}!

Commands:
/leaderboard - Top users
Start chatting for AI responses!"""
    await update.message.reply_text(welcome)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db()
    try:
        top_users = db.query(User).order_by(User.message_count.desc()).limit(10).all()
        text = "Top Users Leaderboard\n\n"
        for idx, user in enumerate(top_users, 1):
            medal = {1: "1.", 2: "2.", 3: "3."}.get(idx, f"{idx}.")
            text += f"{medal} {user.first_name}: {user.message_count} msgs\n"
        await update.message.reply_text(text)
    finally:
        db.close()

async def analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("Admin only!")
        return
    stats = get_daily_stats()
    text = f"""System Analytics (Today)

Total Users: {stats['total_users']}
Messages Today: {stats['messages_today']}
New Users Today: {stats['new_users_today']}
System Healthy"""
    await update.message.reply_text(text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("Admin only!")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = ' '.join(context.args)
    db = get_db()
    try:
        users = db.query(User).filter_by(is_active=True).all()
        sent = 0
        for user in users:
            try:
                await context.bot.send_message(chat_id=int(user.telegram_id), text=f"Announcement:\n\n{message}")
                sent += 1
            except Exception as e:
                logger.error(f"Failed: {e}")
        await update.message.reply_text(f"Broadcast sent to {sent}/{len(users)} users!")
    finally:
        db.close()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user = update.effective_user
    telegram_id = str(user.id)
    await update.message.chat.send_action(action="typing")
    db_user = get_or_create_user(telegram_id, user.username, user.first_name, user.last_name)
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}, json={"model": "gpt-3.5-turbo", "messages": [{"role": "system", "content": "You are a soccer expert bot!"}, {"role": "user", "content": user_message}], "max_tokens": 500}, timeout=30)
        ai_response = response.json()['choices'][0]['message']['content']
    except Exception as e:
        ai_response = "Sorry, try again!"
        logger.error(f"Error: {e}")
    await update.message.reply_text(ai_response)
    db = get_db()
    try:
        db_user.message_count += 1
        db_user.last_active = datetime.utcnow()
        conv = Conversation(telegram_id=telegram_id, message_content=user_message, bot_response=ai_response)
        db.add(conv)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"DB error: {e}")
    finally:
        db.close()

def main():
    print("Starting Soccer Bot...")
    init_db()
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analytics", analytics))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running!")
    application.run_polling()

if __name__ == "__main__":
    main()
