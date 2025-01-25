import os
import json
import logging
import httpx
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext, Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler
from dotenv import load_dotenv

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
app = FastAPI(
    title="Telegram Service",
    description="Service for sending trading signals via Telegram",
    version="1.0.0"
)

# Initialize Telegram bot
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

# Service URLs
SIGNAL_AI_SERVICE = os.getenv("SIGNAL_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
NEWS_AI_SERVICE = os.getenv("NEWS_AI_SERVICE", "https://tradingview-news-ai-service-production.up.railway.app")

# Initialize bot and application
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()

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

async def setup_bot():
    """Setup bot handlers and start polling in the background"""
    try:
        logger.info("Setting up bot handlers...")
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start_command))
        
        # Add webhook handler
        application.add_handler(
            MessageHandler(filters.ALL, lambda u, c: None)
        )
        
        logger.info("Bot handlers setup complete")
        return True
    except Exception as e:
        logger.error(f"Error setting up bot: {e}")
        return False

@app.on_event("startup")
async def startup_event():
    """Start the bot when the FastAPI app starts"""
    try:
        logger.info("Starting bot setup...")
        success = await setup_bot()
        if success:
            logger.info("Bot setup completed successfully")
        else:
            logger.error("Bot setup failed")
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        raise

class SignalMessage(BaseModel):
    """Signal message model"""
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
            
            result = response.json()
            # Remove the 'Remember:' section and everything after it
            formatted_message = result["formatted_message"]
            if "Remember:" in formatted_message:
                formatted_message = formatted_message.split("Remember:")[0].strip()
            return formatted_message
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
            logger.info(f"Request data: instrument={instrument}, articles={articles}")
            
            response = await client.post(
                f"{NEWS_AI_SERVICE}/analyze-news",
                json={"instrument": instrument, "articles": articles}
            )
            
            if response.status_code != 200:
                logger.error(f"News AI Service returned status code {response.status_code}: {response.text}")
                raise HTTPException(status_code=response.status_code, detail=f"Error analyzing news: {response.text}")
            
            result = response.json()
            logger.info(f"News analysis result: {result}")
            return result
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
    
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=signal_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        logger.info(f"Initial message sent to chat_id: {chat_id}")
    except Exception as e:
        logger.error(f"Error sending initial message: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to send message: {str(e)}")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        logger.info(f"Received webhook data: {data}")
        
        update = Update.de_json(data, bot)
        if not update or not update.callback_query:
            logger.info("No callback query in update")
            return {"status": "success"}
            
        query = update.callback_query
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        callback_data = query.data
        
        logger.info(f"Processing callback query: {callback_data} from chat_id: {chat_id}")
        logger.info(f"Current user states: {user_states}")
        
        if callback_data == "sentiment":
            logger.info(f"Handling sentiment callback for chat_id: {chat_id}")
            
            if chat_id not in user_states:
                logger.error(f"Chat ID {chat_id} not found in user_states")
                await query.answer("Session expired. Please request a new signal.")
                return {"status": "error", "message": "Session expired"}
            
            news_data = user_states[chat_id].get("news_data")
            logger.info(f"News data for chat_id {chat_id}: {news_data}")
            
            if not news_data:
                logger.error(f"No news data available for chat_id {chat_id}")
                await query.answer("No news analysis available.")
                return {"status": "error", "message": "No news analysis available"}
            
            try:
                keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                sentiment_text = news_data.get("analysis", "No analysis available")
                logger.info(f"Sending sentiment analysis: {sentiment_text}")
                
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=sentiment_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                await query.answer("Market sentiment analysis loaded")
            except Exception as e:
                logger.error(f"Error sending sentiment analysis: {str(e)}")
                logger.exception("Full traceback:")
                await query.answer("Error displaying sentiment analysis")
                return {"status": "error", "message": str(e)}
            
        elif callback_data == "technical":
            logger.info("Handling technical analysis callback")
            try:
                keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="📊 *Technical Analysis*\n\nDetailed technical analysis feature coming soon! Our team is working on integrating advanced indicators and chart patterns.",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                await query.answer("Technical analysis info loaded")
            except Exception as e:
                logger.error(f"Error sending technical analysis: {str(e)}")
                logger.exception("Full traceback:")
                await query.answer("Error displaying technical analysis")
                return {"status": "error", "message": str(e)}
            
        elif callback_data == "back":
            logger.info("Handling back button callback")
            try:
                if chat_id not in user_states:
                    logger.error(f"Chat ID {chat_id} not found in user_states for back action")
                    await query.answer("Session expired. Please request a new signal.")
                    return {"status": "error", "message": "Session expired"}
                
                signal_text = user_states[chat_id].get("signal_text", "Signal not available")
                keyboard = [
                    [
                        InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
                        InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=signal_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                await query.answer("Returned to main menu")
            except Exception as e:
                logger.error(f"Error handling back action: {str(e)}")
                logger.exception("Full traceback:")
                await query.answer("Error returning to main menu")
                return {"status": "error", "message": str(e)}
        
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
        logger.info(f"Signal data: {message.signal_data}")
        
        # Format signal
        logger.info("Formatting signal...")
        signal_text = await format_signal(message.signal_data)
        logger.info("Signal formatted successfully")
        
        # Get news analysis
        news_analysis = None
        if message.signal_data.get("news"):
            logger.info("Getting news analysis...")
            news_analysis = await get_news_analysis(
                message.signal_data["instrument"],
                message.signal_data["news"]
            )
            logger.info(f"News analysis completed: {news_analysis}")
        
        # Store in user state
        user_states[message.chat_id] = {
            "signal_text": signal_text,
            "news_data": {
                "analysis": f"📊 *Market Sentiment Analysis*\n\n"
                           f"Based on recent news and market data for {message.signal_data['instrument']}:\n\n"
                           f"{news_analysis['analysis'] if news_analysis else 'No market sentiment analysis available at this time.'}"
            }
        }
        logger.info(f"User state updated for chat_id {message.chat_id}: {user_states[message.chat_id]}")
        
        # Send initial message with options
        logger.info("Sending message to Telegram...")
        await send_initial_message(message.chat_id, signal_text)
        logger.info("Message sent successfully")
        
        return {"status": "success", "message": "Signal sent successfully"}
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        logger.exception("Full traceback:")
        raise HTTPException(status_code=500, detail=f"Failed to send signal: {str(e)}")

@app.get("/")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check endpoint called")
    return {"status": "ok", "service": "tradingview-telegram-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
