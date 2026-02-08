import logging
import requests
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# Load environment variables from .env file
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OLLAMA_URL = os.getenv("OLLAMA_URL")

SYSTEM_PROMPT = """You are now functioning as my athletic strategist, college soccer recruitment specialist, creative athletic development director, and positioning expert; for every response:
• Think critically
• Speak like a seasoned operator (if you use acronyms, share in full in brackets)
• Challenge assumptions
• Offer structured feedback, not just answers
• Teach after each output in a short paragraph so I learn with you
• If you are not sure about something, do not hallucinate. Find help from other agents and critically think for the best answer.
• Do not ever use the word 'delve', or dashes in sentences "—"
• Be direct, concise, and human. No corporate fluff."""

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    await update.message.chat.send_action(action="typing")
    
    full_prompt = f"{SYSTEM_PROMPT}\n\nUser request: {user_message}"
    model = "qwen2.5:1.5b"
    
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": model,
            "prompt": full_prompt,
            "stream": False
        })
        
        ai_response = response.json().get('response', 'No response from AI')
        await update.message.reply_text(ai_response)
        
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}. Make sure Ollama is running.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="typing")
    model = "moondream"
    
    photo_file = await update.message.photo[-1].get_file()
    photo_path = f"/tmp/telegram_{update.message.message_id}.jpg"
    await photo_file.download_to_drive(photo_path)
    
    vision_prompt = f"{SYSTEM_PROMPT}\n\nAnalyze this soccer image in detail."
    
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": model,
            "prompt": vision_prompt,
            "images": [photo_path],
            "stream": False
        })
        
        ai_response = response.json().get('response', 'No analysis')
        await update.message.reply_text(ai_response)
        
    except Exception as e:
        await update.message.reply_text(f"Vision error: {str(e)}")

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Strategist Bot running in Docker...")
    application.run_polling()

if __name__ == "__main__":
    main()
