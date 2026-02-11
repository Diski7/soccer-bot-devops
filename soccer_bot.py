import logging
import os
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
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
    role = Column(Enum(UserRole), default=UserRole.ATHLETE)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    message_count = Column(Integer, default=0)

def get_database_url():
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return DATABASE_URL

engine = create_engine(get_database_url())
SessionLocal = sessionmaker(bind=engine)

def init_db():
    Base.metadata.create_all(engine)

def get_db():
    return SessionLocal()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")

def check_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_TELEGRAM_ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /analytics for stats or just say hi!")

async def analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("Admin only!")
        return
    await update.message.reply_text("System is running!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # SIMPLE GREETING - No AI
    await update.message.reply_text("Hi, how may I assist you today?")
    
    # Still track the message (optional)
    telegram_id = str(update.effective_user.id)
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.message_count += 1
            user.last_active = datetime.utcnow()
            db.commit()
    except:
        pass
    finally:
        db.close()

def main():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analytics", analytics))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()

if __name__ == "__main__":
    main()
