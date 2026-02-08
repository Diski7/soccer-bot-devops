import logging
import requests
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")

# Detect which AI to use
USE_OPENAI = OPENAI_API_KEY is not None and len(OPENAI_API_KEY) > 10

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

def get_openai_response(prompt):
    """Call OpenAI API"""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }
    
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=data,
        timeout=30
    )
    
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']

def get_ollama_response(prompt, model="qwen2.5:1.5b"):
    """Call local Ollama API"""
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": model,
            "prompt": f"{SYSTEM_PROMPT}\n\nUser: {prompt}",
            "stream": False
        }, timeout=60)
        response.raise_for_status()
        return response.json().get('response', 'No response')
    except Exception as e:
        return f"Error connecting to Ollama: {str(e)}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    await update.message.chat.send_action(action="typing")
    
    try:
        if USE_OPENAI:
            ai_response = get_openai_response(user_message)
        else:
            ai_response = get_ollama_response(user_message)
            
        await update.message.reply_text(ai_response)
        
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="typing")
    
    if USE_OPENAI:
        await update.message.reply_text("📸 Image analysis: I can see this is a soccer-related image! For detailed tactical analysis, please describe what you see or use the local version with Ollama.")
    else:
        # Local Ollama vision
        photo_file = await update.message.photo[-1].get_file()
        photo_path = f"/tmp/telegram_{update.message.message_id}.jpg"
        await photo_file.download_to_drive(photo_path)
        
        try:
            response = requests.post(OLLAMA_URL, json={
                "model": "moondream",
                "prompt": f"{SYSTEM_PROMPT}\n\nAnalyze this soccer image.",
                "images": [photo_path],
                "stream": False
            }, timeout=60)
            ai_response = response.json().get('response', 'No analysis')
            await update.message.reply_text(ai_response)
        except Exception as e:
            await update.message.reply_text(f"Vision error: {str(e)}")

def main():
    print(f"🚀 Strategist Bot starting...")
    print(f"🤖 Mode: {'OpenAI Cloud' if USE_OPENAI else 'Ollama Local'}")
    print(f"💰 Cost: {'~$0.0001/msg' if USE_OPENAI else 'FREE (Mac must be on)'}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("✅ Bot is running!")
    application.run_polling()

if __name__ == "__main__":
    main()
