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
import base64

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
CHART_SERVICE = os.getenv("CHART_SERVICE", "https://tradingview-chart-service-production.up.railway.app")
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

async def get_chart_image(instrument: str, timeframe: str) -> Optional[str]:
    """Get chart screenshot from Chart Service"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{CHART_SERVICE}/capture-chart",
                json={"symbol": instrument, "timeframe": timeframe}
            )
            if response.status_code == 200:
                result = response.json()
                return result["image"]  # Base64 encoded image
            else:
                logger.warning(f"Chart Service failed: {response.text}")
                return None
    except Exception as e:
        logger.warning(f"Error getting chart image: {str(e)}")
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
        update = Update.de_json(data, bot)
        
        if update.callback_query:
            query = update.callback_query
            chat_id = query.message.chat.id
            
            if chat_id not in user_states:
                await query.answer("Session expired. Please request a new signal.")
                return
            
            if query.data == "technical":
                # Get chart screenshot
                instrument = user_states[chat_id]["signal_data"]["instrument"]
                timeframe = user_states[chat_id]["signal_data"]["timeframe"]
                chart_image = await get_chart_image(instrument, timeframe)
                
                if chart_image:
                    # Convert base64 to bytes
                    image_bytes = base64.b64decode(chart_image)
                    
                    # Send chart image
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=image_bytes,
                        caption=f"📈 Technical Analysis for {instrument} ({timeframe})"
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="❌ Sorry, could not generate chart at this time."
                    )
                
            elif query.data == "sentiment":
                if user_states[chat_id].get("news_data"):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=user_states[chat_id]["news_data"]["analysis"],
                        parse_mode='Markdown'
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="❌ No sentiment analysis available at this time."
                    )
            
            await query.answer()
            
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Error handling webhook: {str(e)}")
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

async def get_news_analysis(instrument: str, articles: List[Dict[str, str]]) -> Dict[str, Any]:
    """Get news analysis from News AI Service"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NEWS_AI_SERVICE}/analyze-news",
                json={"instrument": instrument, "articles": articles}
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"News AI Service failed: {response.text}")
                return None
    except Exception as e:
        logger.warning(f"Error getting news analysis: {str(e)}")
        return None

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
        
        # Get news analysis if available
        news_analysis = None
        if message.news_data and message.news_data.articles:
            try:
                articles = [{"title": article.title, "content": article.content} 
                          for article in message.news_data.articles]
                news_analysis = await get_news_analysis(message.signal_data.instrument, articles)
            except Exception as e:
                logger.error(f"Error getting news analysis: {str(e)}")
        
        # Store in user state
        user_states[message.chat_id] = {
            "signal_text": signal_text,
            "signal_data": message.signal_data.dict(),
            "news_data": {
                "analysis": f"📊 *Market Sentiment Analysis*\n\n"
                           f"Based on recent news and market data for {message.signal_data.instrument}:\n\n"
                           f"{news_analysis['analysis'] if news_analysis else 'No market sentiment analysis available at this time.'}"
            } if news_analysis else None
        }
        
        # Send initial message with buttons
        keyboard = [
            [
                InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
                InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await bot.send_message(
            chat_id=message.chat_id,
            text=signal_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        logger.info("Message sent successfully")
        
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Failed to send signal: {str(e)}")
        raise HTTPException(status_code=422, detail=f"Failed to send signal: {str(e)}")

@app.get("/")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check endpoint called")
    return {"status": "ok", "service": "tradingview-telegram-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
