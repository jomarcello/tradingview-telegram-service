import os
import json
import logging
import httpx
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext, Application, CommandHandler, CallbackQueryHandler, ConversationHandler

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Initialize FastAPI app
app = FastAPI(title="Telegram Service", description="Service for sending trading signals via Telegram")

# Initialize Telegram bot
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

# Service URLs
SIGNAL_AI_SERVICE = os.getenv("SIGNAL_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
NEWS_AI_SERVICE = os.getenv("NEWS_AI_SERVICE", "https://tradingview-news-ai-service-production.up.railway.app")

# Initialize bot
bot = Bot(token=BOT_TOKEN)

# Store user states (for back button functionality)
user_states: Dict[int, Dict[str, Any]] = {}

# Command handlers
async def start_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    welcome_message = (
        f"Welcome {user.first_name}! 🚀\n\n"
        "I'm your SigmaPips trading assistant. I'll send you real-time trading signals "
        "with detailed market analysis and news updates.\n\n"
        "Stay tuned for the next trading opportunity! 📈"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

# Setup handlers
application = Application.builder().token(BOT_TOKEN).build()

# Add command handlers
application.add_handler(CommandHandler("start", start_command))

# Start the bot
async def start_bot():
    """Start the bot."""
    await application.initialize()
    await application.start()
    await application.run_polling()

@app.on_event("startup")
async def on_startup():
    """Start the bot when the FastAPI app starts."""
    import asyncio
    asyncio.create_task(start_bot())

class SignalMessage(BaseModel):
    chat_id: int
    signal_data: Dict[str, Any]
    news_data: Optional[Dict[str, Any]] = None

async def format_signal(signal_data: Dict[str, Any]) -> str:
    """Format signal using the Signal AI Service"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info(f"Sending request to Signal AI Service: {SIGNAL_AI_SERVICE}")
            response = await client.post(
                f"{SIGNAL_AI_SERVICE}/format-signal",
                json=signal_data
            )
            if response.status_code != 200:
                logger.error(f"Signal AI Service returned status code {response.status_code}: {response.text}")
                raise HTTPException(status_code=response.status_code, detail=f"Error formatting signal: {response.text}")
            return response.json()["formatted_message"]
    except httpx.TimeoutException:
        logger.error("Timeout while connecting to Signal AI Service")
        raise HTTPException(status_code=504, detail="Signal AI Service timeout")
    except httpx.RequestError as e:
        logger.error(f"Error connecting to Signal AI Service: {str(e)}")
        raise HTTPException(status_code=502, detail=f"Could not connect to Signal AI Service: {str(e)}")

async def get_news_analysis(instrument: str, articles: list) -> Dict[str, Any]:
    """Get news analysis from News AI Service"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info(f"Sending request to News AI Service: {NEWS_AI_SERVICE}")
            response = await client.post(
                f"{NEWS_AI_SERVICE}/analyze-news",
                json={"instrument": instrument, "articles": articles}
            )
            if response.status_code != 200:
                logger.error(f"News AI Service returned status code {response.status_code}: {response.text}")
                raise HTTPException(status_code=response.status_code, detail=f"Error analyzing news: {response.text}")
            return response.json()
    except httpx.TimeoutException:
        logger.error("Timeout while connecting to News AI Service")
        raise HTTPException(status_code=504, detail="News AI Service timeout")
    except httpx.RequestError as e:
        logger.error(f"Error connecting to News AI Service: {str(e)}")
        raise HTTPException(status_code=502, detail=f"Could not connect to News AI Service: {str(e)}")

async def send_initial_message(chat_id: int, signal_text: str) -> None:
    """Send initial signal message with options"""
    keyboard = [
        [
            InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
            InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await bot.send_message(
        chat_id=chat_id,
        text=signal_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        
        if not update.callback_query:
            return {"status": "success"}
            
        query = update.callback_query
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        callback_data = query.data
        
        logger.info(f"Received callback query: {callback_data} from chat_id: {chat_id}")
        
        if callback_data == "sentiment":
            if chat_id not in user_states:
                await query.answer("Session expired. Please request a new signal.")
                return {"status": "error", "message": "Session expired"}
            
            news_data = user_states[chat_id].get("news_data")
            if not news_data:
                await query.answer("No news analysis available.")
                return {"status": "error", "message": "No news analysis available"}
            
            # Create keyboard with back button
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=news_data["analysis"],
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            await query.answer()
            
        elif callback_data == "technical":
            # Create keyboard with back button
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="📊 Technical Analysis feature coming soon!",
                reply_markup=reply_markup
            )
            await query.answer()
            
        elif callback_data == "back":
            if chat_id not in user_states:
                await query.answer("Session expired. Please request a new signal.")
                return {"status": "error", "message": "Session expired"}
            
            # Restore original message with both options
            keyboard = [
                [
                    InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
                    InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            signal_text = user_states[chat_id].get("signal_text", "Signal not available")
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=signal_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            await query.answer()
        
        return {"status": "success"}
            
    except Exception as e:
        logger.error(f"Error handling webhook: {str(e)}")
        logger.exception("Full traceback:")
        return {"status": "error", "detail": str(e)}

@app.post("/send-signal")
async def send_signal(message: SignalMessage):
    """Send a signal message to a specific chat with interactive options"""
    try:
        logger.info(f"Received signal request for chat_id: {message.chat_id}")
        
        # Format signal
        logger.info("Formatting signal...")
        signal_text = await format_signal(message.signal_data)
        logger.info("Signal formatted successfully")
        
        # Get news analysis if provided
        news_analysis = None
        if message.news_data:
            logger.info("Getting news analysis...")
            news_analysis = await get_news_analysis(
                message.news_data["instrument"],
                message.news_data["articles"]
            )
            logger.info("News analysis completed")
        
        # Store in user state
        user_states[message.chat_id] = {
            "signal_text": signal_text,
            "news_data": news_analysis
        }
        logger.info("User state updated")
        
        # Send initial message with options
        logger.info("Sending message to Telegram...")
        await send_initial_message(message.chat_id, signal_text)
        logger.info("Message sent successfully")
        
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        logger.exception("Full traceback:")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check endpoint called")
    return {"status": "ok", "service": "tradingview-telegram-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
