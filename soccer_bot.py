import logging
import os
import sys
import time
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes, 
    CommandHandler, CallbackQueryHandler, ConversationHandler
)
import requests

# Import our advanced database
from database import (
    init_db, get_db, User, Conversation, UserAnalytics, 
    MatchPrediction, SystemAnalytics, UserRole, ConversationType,
    update_user_activity, get_daily_stats
)

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")

# Conversation states
WAITING_FOR_TEAM = 1
WAITING_FOR_PREDICTION = 2

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============== AUTHENTICATION & AUTHORIZATION ==============

def check_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return str(user_id) == ADMIN_TELEGRAM_ID

def get_or_create_user(telegram_id: str, username: str, first_name: str, last_name: str = None):
    """Get existing user or create new one with analytics"""
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        
        if not user:
            # Create new user
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                role=UserRole.ADMIN if check_admin(int(telegram_id)) else UserRole.USER
            )
            db.add(user)
            db.commit()
            
            # Create analytics record
            analytics = UserAnalytics(user_id=user.id)
            db.add(analytics)
            db.commit()
            
            logger.info(f"New user created: {first_name} ({telegram_id})")
        
        return user
    except Exception as e:
        db.rollback()
        logger.error(f"Error getting/creating user: {e}")
        raise
    finally:
        db.close()

# ============== ANALYTICS FUNCTIONS ==============

def log_conversation(telegram_id: str, user_message: str, bot_response: str, 
                    response_time: float, tokens_used: int = 0, conv_type: ConversationType = ConversationType.GENERAL):
    """Log conversation with analytics"""
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            conv = Conversation(
                user_id=user.id,
                telegram_id=telegram_id,
                message_content=user_message,
                bot_response=bot_response,
                response_time_ms=int(response_time * 1000),
                tokens_used=tokens_used,
                conversation_type=conv_type
            )
            db.add(conv)
            
            # Update user stats
            user.message_count += 1
            user.total_tokens_used += tokens_used
            
            # Update analytics
            if user.analytics:
                user.analytics.last_seen = datetime.utcnow()
            
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error logging conversation: {e}")
    finally:
        db.close()

def get_user_engagement_score(user_id: str) -> dict:
    """Calculate user engagement metrics"""
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return {}
        
        # Calculate streak
        last_active = user.last_active or user.created_at
        days_since_active = (datetime.utcnow() - last_active).days
        
        # Get conversation diversity
        conv_types = db.query(Conversation.conversation_type).filter_by(
            telegram_id=user_id
        ).distinct().count()
        
        return {
            "total_messages": user.message_count,
            "days_active": days_since_active,
            "account_age_days": (datetime.utcnow() - user.created_at).days,
            "conversation_diversity": conv_types,
            "favorite_team": user.favorite_team or "Not set",
            "role": user.role.value
        }
    finally:
        db.close()

# ============== COMMAND HANDLERS ==============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced start command with user setup"""
    user = update.effective_user
    telegram_id = str(user.id)
    
    # Get or create user
    db_user = get_or_create_user(
        telegram_id=telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    # Welcome message based on role
    if db_user.role == UserRole.ADMIN:
        welcome_text = f"""👑 Welcome Admin {user.first_name}!

🤖 Soccer Bot with Advanced Analytics

Available commands:
⚽ /match - Get match predictions
📊 /mystats - Your detailed stats
🏆 /leaderboard - Top users
⚙️ /settings - Configure preferences
📢 /broadcast - Message all users (Admin)
📈 /analytics - System analytics (Admin)
🎯 /predict - Make a match prediction
"""
    else:
        welcome_text = f"""⚽ Welcome {user.first_name}!

I'm your AI Soccer Assistant!

Commands:
⚽ /match - Match analysis & predictions
📊 /mystats - Your stats & engagement
🏆 /leaderboard - Top users
⚙️ /settings - Set favorite team
🎯 /predict - Make predictions

Start by setting your favorite team with /settings!
"""
    
    await update.message.reply_text(welcome_text)

async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed user statistics"""
    telegram_id = str(update.effective_user.id)
    engagement = get_user_engagement_score(telegram_id)
    
    if not engagement:
        await update.message.reply_text("❌ No stats found. Start chatting first!")
        return
    
    stats_text = f"""📊 Your Soccer Bot Stats

👤 Profile:
• Total Messages: {engagement['total_messages']}
• Account Age: {engagement['account_age_days']} days
• Role: {engagement['role'].title()}

⚽ Soccer Profile:
• Favorite Team: {engagement['favorite_team']}
• Conversation Topics: {engagement['conversation_diversity']}

🔥 Engagement:
• Days Since Last Active: {engagement['days_active']}
• Status: {'🔥 Active' if engagement['days_active'] == 0 else '👋 Come back soon!'}

Keep chatting to increase your score!
"""
    await update.message.reply_text(stats_text)

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User settings and preferences"""
    keyboard = [
        [InlineKeyboardButton("⚽ Set Favorite Team", callback_data='set_team')],
        [InlineKeyboardButton("🏆 Set Favorite League", callback_data='set_league')],
        [InlineKeyboardButton("🔔 Toggle Notifications", callback_data='toggle_notif')],
        [InlineKeyboardButton("👤 View Profile", callback_data='view_profile')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚙️ Settings Menu:\nChoose an option:",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings buttons"""
    query = update.callback_query
    await query.answer()
    
    telegram_id = str(update.effective_user.id)
    
    if query.data == 'set_team':
        await query.edit_message_text(
            "⚽ Send me your favorite team name:\n\n"
            "Example: Manchester United, Barcelona, etc."
        )
        context.user_data['waiting_for'] = 'team'
        
    elif query.data == 'view_profile':
        engagement = get_user_engagement_score(telegram_id)
        profile_text = f"""👤 Your Profile

🎫 Role: {engagement['role'].title()}
⚽ Team: {engagement['favorite_team']}
💬 Messages: {engagement['total_messages']}
🎯 Topics: {engagement['conversation_diversity']}

Use /settings to update your preferences!
"""
        await query.edit_message_text(profile_text)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top users"""
    db = get_db()
    try:
        top_users = db.query(User).order_by(User.message_count.desc()).limit(10).all()
        
        leaderboard_text = "🏆 Top Users Leaderboard\n\n"
        
        for idx, user in enumerate(top_users, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
            leaderboard_text += f"{medal} {user.first_name}: {user.message_count} msgs\n"
        
        await update.message.reply_text(leaderboard_text)
    finally:
        db.close()

# ============== ADMIN COMMANDS ==============

async def analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """System-wide analytics (Admin only)"""
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command!")
        return
    
    stats = get_daily_stats()
    
    analytics_text = f"""📈 System Analytics (Today)

👥 Users:
• Daily Active: {stats['daily_active_users']}
• New Today: {stats['new_users_today']}
• Total Users: {stats['total_users']}

💬 Activity:
• Messages Today: {stats['messages_today']}
• Avg per User: {stats['messages_today'] // max(stats['daily_active_users'], 1)}

System Status: 🟢 Healthy
"""
    await update.message.reply_text(analytics_text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (Admin only)"""
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command!")
        return
    
    if not context.args:
        await update.message.reply_text("📢 Usage: /broadcast <message>")
        return
    
    message = ' '.join(context.args)
    db = get_db()
    
    try:
        users = db.query(User).filter_by(is_active=True).all()
        sent_count = 0
        
        for user in users:
            try:
                await context.bot.send_message(
                    chat_id=int(user.telegram_id),
                    text=f"📢 Announcement from Admin:\n\n{message}"
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send to {user.telegram_id}: {e}")
        
        await update.message.reply_text(f"✅ Broadcast sent to {sent_count}/{len(users)} users!")
    finally:
        db.close()

async def predict_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Make a match prediction"""
    if not context.args:
        await update.message.reply_text(
            "🎯 Usage: /predict <match description>\n\n"
            "Example: /predict Manchester United vs Liverpool"
        )
        return
    
    match_desc = ' '.join(context.args)
    telegram_id = str(update.effective_user.id)
    
    await update.message.reply_text(
        f"⚽ Match: {match_desc}\n\n"
        f"What's your prediction? (e.g., 'Man Utd wins 2-1')"
    )
    
    # Store in context for next message
    context.user_data['predicting_match'] = match_desc

# ============== MESSAGE HANDLER ==============

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages with analytics"""
    start_time = time.time()
    
    user_message = update.message.text
    user = update.effective_user
    telegram_id = str(user.id)
    
    # Check if we're waiting for a prediction
    if context.user_data.get('predicting_match'):
        match = context.user_data.pop('predicting_match')
        prediction = user_message
        
        # Save prediction
        db = get_db()
        try:
            db_user = db.query(User).filter_by(telegram_id=telegram_id).first()
            if db_user:
                pred = MatchPrediction(
                    user_id=db_user.id,
                    match_description=match,
                    user_prediction=prediction
                )
                db.add(pred)
                db.commit()
                await update.message.reply_text(
                    f"✅ Prediction saved!\n\n"
                    f"⚽ {match}\n"
                    f"🎯 Your prediction: {prediction}\n\n"
                    f"I'll remind you of the result later!"
                )
        finally:
            db.close()
        return
    
    # Regular message handling with OpenAI
    await update.message.chat.send_action(action="typing")
    
    # Get AI response
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "system", "content": "You are a soccer expert bot. Be enthusiastic about soccer!"},
                    {"role": "user", "content": user_message}
                ],
                "max_tokens": 500
            },
            timeout=30
        )
        
        ai_response = response.json()['choices'][0]['message']['content']
        tokens_used = response.json().get('usage', {}).get('total_tokens', 0)
        
    except Exception as e:
        ai_response = "Sorry, I'm having trouble thinking right now. Try again in a moment! ⚽"
        tokens_used = 0
        logger.error(f"OpenAI error: {e}")
    
    # Send response
    await update.message.reply_text(ai_response)
    
    # Calculate response time
    response_time = time.time() - start_time
    
    # Log everything
    log_conversation(
        telegram_id=telegram_id,
        user_message=user_message,
        bot_response=ai_response,
        response_time=response_time,
        tokens_used=tokens_used
    )

# ============== MAIN ==============

def main():
    print("🚀 Starting Advanced Soccer Bot with Analytics...")
    
    # Initialize database
    init_db()
    
    if not TELEGRAM_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mystats", mystats))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("analytics", analytics))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("predict", predict_match))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Bot is running with Advanced Features!")
    print("📊 Analytics enabled")
    print("🔐 Authentication enabled")
    print("⚽ Soccer features active")
    
    application.run_polling()

if __name__ == "__main__":
    main()
