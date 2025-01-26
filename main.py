import os
import json
import logging
import httpx
from typing import Optional, Dict, Any, List
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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Add file handler for debugging
file_handler = logging.FileHandler('telegram_bot.log')
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://tradingview-telegram-service-production.up.railway.app/webhook")

# Initialize bot
bot = Bot(token=BOT_TOKEN)

# Store user states (for back button functionality)
user_states: Dict[int, Dict[str, Any]] = {}

# Market specific settings
MARKET_SETTINGS = {
    # Forex pairs (1 pip = 0.0001)
    "EURUSD": {"pip_value": 0.0001, "decimals": 4},
    "GBPUSD": {"pip_value": 0.0001, "decimals": 4},
    "USDJPY": {"pip_value": 0.01, "decimals": 3},    # JPY pairs use 2 decimals
    "AUDUSD": {"pip_value": 0.0001, "decimals": 4},
    "USDCAD": {"pip_value": 0.0001, "decimals": 4},
    
    # Crypto (different point values)
    "BTCUSD": {"pip_value": 1, "decimals": 1},      # 1 point = $1
    "ETHUSD": {"pip_value": 0.1, "decimals": 2},    # 0.1 point = $0.1
    "XRPUSD": {"pip_value": 0.0001, "decimals": 4}, # More precise for lower value coins
    
    # Indices
    "US30": {"pip_value": 1, "decimals": 0},        # 1 point = $1
    "SPX500": {"pip_value": 0.25, "decimals": 2},   # 0.25 points
    "NAS100": {"pip_value": 0.25, "decimals": 2},   # 0.25 points
    
    # Commodities
    "XAUUSD": {"pip_value": 0.1, "decimals": 2},    # Gold (0.1 point = $0.1)
    "XAGUSD": {"pip_value": 0.01, "decimals": 3},   # Silver (0.01 point)
    "WTIUSD": {"pip_value": 0.01, "decimals": 2},   # Oil (0.01 point)
}

def calculate_rr_levels(instrument: str, entry_price: float, direction: str, risk_pips: float = None, risk_points: float = None) -> dict:
    """
    Calculate take profit level based on 1:1 risk-reward ratio.
    
    Args:
        instrument: Trading instrument (e.g., 'EURUSD', 'BTCUSD')
        entry_price: Entry price for the trade
        direction: Trade direction ('buy' or 'sell')
        risk_pips: Risk in pips (for forex)
        risk_points: Risk in points (for other markets)
    
    Returns:
        dict: Contains calculated stop loss and take profit levels
    """
    try:
        # Get market settings
        settings = MARKET_SETTINGS.get(instrument.upper())
        if not settings:
            logger.warning(f"No settings found for {instrument}, using default forex settings")
            settings = {"pip_value": 0.0001, "decimals": 4}
        
        pip_value = settings["pip_value"]
        decimals = settings["decimals"]
        
        # Calculate risk in points if given in pips
        if risk_pips is not None:
            risk_points = risk_pips * pip_value
        
        # Calculate stop loss and take profit
        if direction.lower() == "buy":
            stop_loss = round(entry_price - risk_points, decimals)
            take_profit = round(entry_price + risk_points, decimals)
        else:  # sell
            stop_loss = round(entry_price + risk_points, decimals)
            take_profit = round(entry_price - risk_points, decimals)
        
        return {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_points": risk_points,
            "risk_pips": risk_points / pip_value if pip_value else None,
            "pip_value": pip_value
        }
    except Exception as e:
        logger.error(f"Error calculating RR levels: {str(e)}")
        return None

@app.post("/calculate-rr")
async def calculate_risk_reward(
    instrument: str,
    entry_price: float,
    direction: str,
    risk_pips: float = None,
    risk_points: float = None
):
    """Calculate 1:1 risk-reward levels for a given trade setup"""
    try:
        if not risk_pips and not risk_points:
            raise HTTPException(status_code=400, detail="Either risk_pips or risk_points must be provided")
            
        result = calculate_rr_levels(
            instrument=instrument,
            entry_price=entry_price,
            direction=direction,
            risk_pips=risk_pips,
            risk_points=risk_points
        )
        
        if not result:
            raise HTTPException(status_code=500, detail="Error calculating RR levels")
            
        return {
            "status": "success",
            "data": {
                "instrument": instrument,
                "direction": direction,
                "levels": result,
                "message": f"Calculated 1:1 RR levels for {instrument} {direction} trade"
            }
        }
    except Exception as e:
        logger.error(f"Error in calculate_risk_reward endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("startup")
async def startup_event():
    """Set webhook on startup"""
    try:
        webhook_url = "https://tradingview-telegram-service-production.up.railway.app/webhook"
        logger.info(f"Setting webhook to: {webhook_url}")
        webhook_info = await bot.get_webhook_info()
        current_url = webhook_info.url if webhook_info else None
        logger.info(f"Current webhook URL: {current_url}")
        
        if current_url != webhook_url:
            logger.info("Deleting old webhook...")
            await bot.delete_webhook()
            logger.info("Setting new webhook...")
            success = await bot.set_webhook(url=webhook_url)
            if success:
                logger.info("Webhook set successfully")
            else:
                logger.error("Failed to set webhook")
        else:
            logger.info("Webhook already set correctly")
            
        # Verify webhook is set
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Final webhook URL: {webhook_info.url}")
        logger.info(f"Webhook info: {webhook_info.to_dict()}")
    except Exception as e:
        logger.error(f"Error setting webhook: {str(e)}")
        logger.exception("Full traceback:")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Remove webhook on shutdown"""
    try:
        logger.info("Removing webhook")
        await bot.delete_webhook()
        logger.info("Webhook removed successfully")
    except Exception as e:
        logger.error(f"Error removing webhook: {e}")

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
                logger.info("Sentiment analysis sent successfully")
                return {"status": "success", "message": "Sentiment analysis sent"}
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
                logger.info("Technical analysis sent successfully")
                return {"status": "success", "message": "Technical analysis sent"}
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
                logger.info("Returned to main menu successfully")
                return {"status": "success", "message": "Returned to main menu"}
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

class SignalData(BaseModel):
    instrument: str
    direction: str
    entry_price: float
    timeframe: str
    stop_loss: float
    take_profit: Optional[float] = None
    strategy: str

class NewsArticle(BaseModel):
    title: str
    content: str

class NewsData(BaseModel):
    articles: List[NewsArticle]

class SignalMessage(BaseModel):
    chat_id: int
    signal_data: SignalData
    news_data: Optional[NewsData] = None

async def format_signal(signal_data: Dict[str, Any]) -> str:
    """Format signal using the Signal AI Service"""
    try:
        # Generate TradingView chart URL
        instrument = signal_data.get("instrument", "").upper()
        # Map common symbols to TradingView format
        tv_symbol_map = {
            "EURUSD": "FX:EURUSD",
            "GBPUSD": "FX:GBPUSD",
            "USDJPY": "FX:USDJPY",
            "BTCUSD": "BINANCE:BTCUSDT",  # Using Binance as source
            "ETHUSD": "BINANCE:ETHUSDT",
            "US30": "DJ:DJI",
            "SPX500": "SP:SPX",
            "NAS100": "NASDAQ:NDX",
            "XAUUSD": "OANDA:XAUUSD"
        }
        tv_symbol = tv_symbol_map.get(instrument, instrument)
        timeframe = signal_data.get("timeframe", "1h")
        # Map timeframes to TradingView format
        tv_timeframe_map = {
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "4h": "240",
            "1d": "D",
            "1w": "W"
        }
        tv_timeframe = tv_timeframe_map.get(timeframe, "60")
        
        chart_url = f"https://www.tradingview.com/chart/?symbol={tv_symbol}&interval={tv_timeframe}"
        
        # Create basic signal format if Signal AI Service fails
        direction_emoji = "📈" if signal_data.get("direction", "").lower() == "buy" else "📉"
        basic_signal = f"""🚨 New Trading Signal 🚨

Instrument: {signal_data.get("instrument", "")}
Action: {signal_data.get("direction", "").upper()} {direction_emoji}

Entry Price: {signal_data.get("entry_price", "")}
Stop Loss: {signal_data.get("stop_loss", "")} 🛑
Take Profit: {signal_data.get("take_profit", "")} 🎯

Timeframe: {signal_data.get("timeframe", "")}
Strategy: {signal_data.get("strategy", "")}

-------------------

Risk Management:
• Position size: 1-2% max
• Use proper stop loss
• Follow your trading plan

-------------------

📊 View Chart:
{chart_url}"""
        
        # Try to get AI formatted signal, fallback to basic if fails
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{SIGNAL_AI_SERVICE}/format-signal",
                    json=signal_data
                )
                if response.status_code == 200:
                    formatted_signal = response.json()["formatted_signal"]
                    formatted_signal += f"\n\n📊 View Chart:\n{chart_url}"
                    return formatted_signal
                else:
                    logger.warning(f"Signal AI Service failed, using basic format. Error: {response.text}")
                    return basic_signal
        except Exception as e:
            logger.warning(f"Signal AI Service failed, using basic format. Error: {str(e)}")
            return basic_signal
            
    except Exception as e:
        logger.error(f"Error formatting signal: {str(e)}")
        raise HTTPException(status_code=422, detail=f"Error formatting signal: {str(e)}")

@app.post("/send-signal")
async def send_signal(message: SignalMessage):
    """Send a signal message to a specific chat with interactive options"""
    try:
        logger.info(f"Received signal request for chat_id: {message.chat_id}")
        
        # Calculate proper RR levels
        entry_price = message.signal_data.entry_price
        stop_loss = message.signal_data.stop_loss
        instrument = message.signal_data.instrument
        direction = message.signal_data.direction
        
        if all([entry_price, stop_loss, instrument, direction]):
            # Calculate risk in points
            settings = MARKET_SETTINGS.get(instrument.upper(), {"pip_value": 0.0001, "decimals": 4})
            risk_points = abs(entry_price - stop_loss)
            
            # Recalculate levels to ensure 1:1 RR
            levels = calculate_rr_levels(
                instrument=instrument,
                entry_price=entry_price,
                direction=direction,
                risk_points=risk_points
            )
            
            if levels:
                message.signal_data.stop_loss = levels["stop_loss"]
                message.signal_data.take_profit = levels["take_profit"]
                logger.info(f"Adjusted levels for 1:1 RR: {levels}")
        
        # Format signal
        logger.info("Formatting signal...")
        signal_text = await format_signal(message.signal_data.dict())
        logger.info(f"Signal formatted successfully: {signal_text}")
        
        # Get news analysis
        news_analysis = None
        if message.news_data and message.news_data.get("articles"):
            logger.info("Getting news analysis...")
            news_analysis = await get_news_analysis(
                message.signal_data["instrument"],
                message.news_data["articles"]
            )
            logger.info(f"News analysis completed: {news_analysis}")
        else:
            logger.warning("No news data or articles provided")
        
        # Store in user state
        user_states[message.chat_id] = {
            "signal_text": signal_text,
            "news_data": {
                "analysis": f"📊 *Market Sentiment Analysis*\n\n"
                           f"Based on recent news and market data for {message.signal_data['instrument']}:\n\n"
                           f"{news_analysis['analysis'] if news_analysis else 'No market sentiment analysis available at this time.'}"
            } if news_analysis else None
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
