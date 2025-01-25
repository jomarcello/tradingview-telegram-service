import os
import json
import logging
import httpx
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI()

# Initialize Telegram bot
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

# Service URLs
SIGNAL_AI_SERVICE = os.getenv("SIGNAL_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
NEWS_AI_SERVICE = os.getenv("NEWS_AI_SERVICE", "https://tradingview-news-ai-service-production.up.railway.app")

# Initialize bot application
application = Application.builder().token(BOT_TOKEN).build()

# Store user states (for back button functionality)
user_states: Dict[int, Dict[str, Any]] = {}

class SignalMessage(BaseModel):
    chat_id: int
    signal_data: Dict[str, Any]
    news_data: Optional[Dict[str, Any]] = None

async def format_signal(signal_data: Dict[str, Any]) -> str:
    """Format signal using the Signal AI Service"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SIGNAL_AI_SERVICE}/format-signal",
            json=signal_data
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Error formatting signal")
        return response.json()["formatted_message"]

async def get_news_analysis(instrument: str, articles: list) -> Dict[str, Any]:
    """Get news analysis from News AI Service"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{NEWS_AI_SERVICE}/analyze-news",
            json={"instrument": instrument, "articles": articles}
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Error analyzing news")
        return response.json()

async def send_initial_message(chat_id: int, signal_text: str) -> None:
    """Send initial signal message with options"""
    keyboard = [
        [
            InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
            InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await application.bot.send_message(
        chat_id=chat_id,
        text=signal_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_sentiment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle sentiment button click"""
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in user_states:
        await query.answer("Session expired. Please request a new signal.")
        return
    
    news_data = user_states[chat_id].get("news_data")
    if not news_data:
        await query.answer("No news analysis available.")
        return
    
    # Create keyboard with back button
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=news_data["analysis"],
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    await query.answer()

async def handle_technical_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle technical analysis button click"""
    query = update.callback_query
    
    # Create keyboard with back button
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="📊 Technical Analysis feature coming soon!",
        reply_markup=reply_markup
    )
    await query.answer()

async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle back button click"""
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in user_states:
        await query.answer("Session expired. Please request a new signal.")
        return
    
    # Restore original message with both options
    keyboard = [
        [
            InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
            InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    signal_text = user_states[chat_id].get("signal_text", "Signal not available")
    await query.edit_message_text(
        text=signal_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    await query.answer()

@app.post("/send-signal")
async def send_signal(message: SignalMessage):
    """Send a signal message to a specific chat with interactive options"""
    try:
        # Format signal
        signal_text = await format_signal(message.signal_data)
        
        # Store in user state
        user_states[message.chat_id] = {
            "signal_text": signal_text,
            "news_data": message.news_data
        }
        
        # Send initial message with options
        await send_initial_message(message.chat_id, signal_text)
        
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Register callback handlers
application.add_handler(CallbackQueryHandler(handle_sentiment_callback, pattern="^sentiment$"))
application.add_handler(CallbackQueryHandler(handle_technical_callback, pattern="^technical$"))
application.add_handler(CallbackQueryHandler(handle_back_callback, pattern="^back$"))

# Start the bot
application.run_polling()
