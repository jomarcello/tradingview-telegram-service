import os
import json
import logging
import httpx
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackContext, 
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ConversationHandler, 
    MessageHandler,
    ContextTypes
)
from dotenv import load_dotenv
import base64
import asyncio
import time

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for more detailed logs
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.FileHandler('telegram_service.log')  # Also log to file
    ]
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Telegram Service",
    description="Service for sending trading signals via Telegram",
    version="1.0.0"
)

# Initialize Telegram bot
BOT_TOKEN = "7583525993:AAFp90r7UqCY2KdGufKgHHjjslBy7AnY_Sg"  # Using same token as subscriber matcher
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

# Service URLs
SIGNAL_AI_SERVICE = os.getenv("SIGNAL_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
NEWS_AI_SERVICE = os.getenv("NEWS_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
CHART_SERVICE = os.getenv("CHART_SERVICE", "https://tradingview-chart-service-production.up.railway.app")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://tradingview-telegram-service-production.up.railway.app/webhook")
SUPABASE_URL = "https://utigkgjcyqnrhpndzqhs.supabase.co/rest/v1/subscribers"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV0aWdrZ2pjeXFucmhwbmR6cWhzIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTczNjMyMzA1NiwiZXhwIjoyMDUxODk5MDU2fQ.8JovzmGQofC4oC2016P7aa6FZQESF3UNSjUTruIYWbg"

logger.info(f"Initialized with services: SIGNAL={SIGNAL_AI_SERVICE}, NEWS={NEWS_AI_SERVICE}, CHART={CHART_SERVICE}")

# Initialize bot
bot = Bot(token=BOT_TOKEN)

# Store user states
user_states = {}

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

class MessageRegistry:
    def __init__(self):
        self.last_signal_time = {}  # chat_id -> last signal time
        self.cooldown = 300  # 5 minutes in seconds
        
    def can_send_signal(self, chat_id: str) -> bool:
        now = time.time()
        if chat_id in self.last_signal_time:
            if now - self.last_signal_time[chat_id] < self.cooldown:
                return False
        self.last_signal_time[chat_id] = now
        return True

# Initialize registry
message_registry = MessageRegistry()

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

async def get_tradingview_url(symbol: str, timeframe: str) -> str:
    """Generate TradingView chart URL"""
    # Map common symbols to TradingView format
    tv_symbol_map = {
        "EURUSD": "FX:EURUSD",
        "GBPUSD": "FX:GBPUSD",
        "USDJPY": "FX:USDJPY",
        "BTCUSD": "BINANCE:BTCUSDT",
        "ETHUSD": "BINANCE:ETHUSDT",
        "US30": "DJ:DJI",
        "SPX500": "SP:SPX",
        "NAS100": "NASDAQ:NDX",
        "XAUUSD": "OANDA:XAUUSD"
    }
    
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
    
    tv_symbol = tv_symbol_map.get(symbol.upper(), symbol)
    tv_timeframe = tv_timeframe_map.get(timeframe.lower(), "60")
    
    # Generate TradingView chart URL
    url = f"https://www.tradingview.com/chart/?symbol={tv_symbol}&interval={tv_timeframe}"
    logger.info(f"Generated TradingView URL: {url}")
    return url

async def get_tradingview_widget_html(symbol: str, timeframe: str) -> str:
    """Generate TradingView widget HTML"""
    # Map common symbols to TradingView format
    tv_symbol_map = {
        "EURUSD": "FX:EURUSD",
        "GBPUSD": "FX:GBPUSD",
        "USDJPY": "FX:USDJPY",
        "BTCUSD": "BINANCE:BTCUSDT",
        "ETHUSD": "BINANCE:ETHUSDT",
        "US30": "DJ:DJI",
        "SPX500": "SP:SPX",
        "NAS100": "NASDAQ:NDX",
        "XAUUSD": "OANDA:XAUUSD"
    }
    
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
    
    tv_symbol = tv_symbol_map.get(symbol.upper(), symbol)
    tv_timeframe = tv_timeframe_map.get(timeframe.lower(), "60")
    
    # Generate TradingView widget HTML
    widget_html = f'''
    <!-- TradingView Widget BEGIN -->
    <div class="tradingview-widget-container">
      <div id="tradingview_chart"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget(
      {{
        "width": "100%",
        "height": 500,
        "symbol": "{tv_symbol}",
        "interval": "{tv_timeframe}",
        "timezone": "Etc/UTC",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "toolbar_bg": "#f1f3f6",
        "enable_publishing": false,
        "hide_side_toolbar": false,
        "allow_symbol_change": true,
        "container_id": "tradingview_chart"
      }});
      </script>
    </div>
    <!-- TradingView Widget END -->
    '''
    return widget_html

async def get_chart_image(instrument: str, timeframe: str) -> str:
    """Get chart image from TradingView chart service"""
    try:
        url = f"{os.getenv('CHART_SERVICE')}/screenshot"
        params = {
            "symbol": instrument,
            "interval": timeframe,
            "studies": ["RSI", "MACD", "BB"],  # Add default technical indicators
            "theme": "dark"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            if response.status_code != 200:
                raise Exception(f"Chart service returned status code {response.status_code}")
                
            return response.content
            
    except Exception as e:
        logger.error(f"Error getting chart image: {str(e)}")
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

async def init_webhook():
    """Initialize bot and set webhook"""
    try:
        # Create application and register handlers
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CallbackQueryHandler(button_handler))
        
        # Remove webhook, then set it up
        await bot.delete_webhook()
        await asyncio.sleep(0.1)  # Small delay
        webhook_info = await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook setup completed: {webhook_info}")
        return webhook_info
    except Exception as e:
        logger.error(f"Error setting webhook: {str(e)}")
        raise

@app.on_event("startup")
async def startup():
    """Run startup tasks"""
    await init_webhook()

class SignalRequest(BaseModel):
    signal_data: Dict[str, Any]
    chat_id: Optional[str] = None
    news_data: Optional[Dict[str, Any]] = None

@app.post("/send-signal")
async def send_signal(signal_request: SignalRequest) -> dict:
    """Send a trading signal to Telegram"""
    try:
        signal_data = signal_request.signal_data
        chat_id = signal_request.chat_id
        
        # Format message if not already formatted
        message = signal_data.get("formatted_message", "")
        if not message:
            message = format_signal_message(signal_data)
            
        # Create keyboard markup with sentiment and chart buttons
        keyboard = [
            [
                InlineKeyboardButton("📊 Technical Analysis", callback_data="technical_analysis"),
                InlineKeyboardButton("📰 Market Sentiment", callback_data="market_sentiment")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send to all subscribers or specific chat_id
        if chat_id == "all":
            subscribers = await get_subscribers()
            for subscriber in subscribers:
                # Clean the chat_id (remove any whitespace)
                sub_chat_id = str(subscriber["chat_id"]).strip()
                
                # Check cooldown
                if not message_registry.can_send_signal(sub_chat_id):
                    logger.info(f"Skipping signal for {sub_chat_id} due to cooldown")
                    continue
                
                # Store in user state
                user_states[sub_chat_id] = {
                    "instrument": signal_data["instrument"],
                    "timeframe": signal_data["timeframe"],
                    "original_message": message
                }
                
                try:
                    # Send message
                    await bot.send_message(
                        chat_id=sub_chat_id,
                        text=message,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    logger.info(f"Sent signal to subscriber {sub_chat_id}")
                except Exception as e:
                    logger.error(f"Failed to send to {sub_chat_id}: {str(e)}")
                    continue
        else:
            # Clean the chat_id (remove any whitespace)
            chat_id = str(chat_id).strip()
            
            # Check cooldown
            if not message_registry.can_send_signal(chat_id):
                return {"status": "error", "message": "Please wait before sending another signal"}
            
            # Store in user state
            user_states[chat_id] = {
                "instrument": signal_data["instrument"],
                "timeframe": signal_data["timeframe"],
                "original_message": message
            }
            
            # Send message
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            logger.info(f"Sent signal to chat_id {chat_id}")
            
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error sending signal: {str(e)}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button presses"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Get user state
        chat_id = str(query.message.chat_id)
        user_state = user_states.get(chat_id, {})
        
        if query.data == "technical_analysis":
            if not all(k in user_state for k in ["instrument", "timeframe"]):
                await query.edit_message_text(
                    text="Sorry, I couldn't find the trading pair information. Please try again with a new signal.",
                    reply_markup=None
                )
                return
                
            try:
                # Show loading state
                await query.edit_message_text(
                    text="📊 Generating technical analysis chart...",
                    parse_mode=ParseMode.MARKDOWN
                )
                
                # Get chart image from TradingView service
                chart_image = await get_chart_image(user_state["instrument"], user_state["timeframe"])
                if not chart_image:
                    raise Exception("Failed to get chart image")
                    
                # Create back button
                keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                
                # Send chart as new message
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=chart_image,
                    caption=f"📊 Technical Analysis for {user_state['instrument']} ({user_state['timeframe']})\n\nShowing RSI, MACD and Bollinger Bands",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
            except Exception as e:
                logger.error(f"Error getting technical analysis: {str(e)}")
                await bot.send_message(
                    chat_id=chat_id,
                    text="Sorry, I couldn't generate the technical analysis chart. Please try again later."
                )
                
        elif query.data == "market_sentiment":
            if not all(k in user_state for k in ["instrument", "timeframe"]):
                await query.edit_message_text(
                    text="Sorry, I couldn't find the trading pair information. Please try again with a new signal.",
                    reply_markup=None
                )
                return
                
            try:
                # Show loading state
                loading_text = f"📰 Analyzing market sentiment..."
                await query.edit_message_text(
                    text=loading_text,
                    parse_mode=ParseMode.MARKDOWN
                )
                
                # Get sentiment data
                sentiment_data = await get_news_analysis(user_state["instrument"], [])
                if not sentiment_data:
                    raise Exception("Failed to get market sentiment")
                    
                # Create back button
                keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                
                # Send sentiment as new message
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📰 Market Sentiment Analysis for {user_state['instrument']}\n\n{sentiment_data}\n\n_Based on recent market news and events._",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
                
                # Restore original message
                original_message = user_state.get("original_message", "")
                if original_message:
                    keyboard = [
                        [
                            InlineKeyboardButton("📊 Technical Analysis", callback_data="technical_analysis"),
                            InlineKeyboardButton("📰 Market Sentiment", callback_data="market_sentiment")
                        ]
                    ]
                    await query.edit_message_text(
                        text=original_message,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.MARKDOWN
                    )
                
            except Exception as e:
                logger.error(f"Error getting market sentiment: {str(e)}")
                await bot.send_message(
                    chat_id=chat_id,
                    text="Sorry, I couldn't analyze the market sentiment. Please try again later."
                )
                
        elif query.data == "back_to_signal":
            # Delete the current message (analysis)
            await bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            
    except Exception as e:
        logger.error(f"Error in button handler: {str(e)}")
        try:
            await query.edit_message_text(
                text="Sorry, something went wrong. Please try again with a new signal.",
                reply_markup=None
            )
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data == "technical_analysis")
async def handle_technical_analysis(call):
    try:
        # Get the symbol from the original message
        symbol = get_symbol_from_message(call.message.text)
        if not symbol:
            await bot.answer_callback_query(call.id, "Could not find symbol in message")
            return

        # Call TradingView chart service
        chart_url = f"https://tradingview-chart-service-production.up.railway.app/screenshot?symbol={symbol}&interval=15m"
        async with httpx.AsyncClient() as client:
            response = await client.get(chart_url)
            if response.status_code != 200:
                logger.error(f"Failed to get chart: {response.status}")
                await bot.answer_callback_query(call.id, "Failed to get chart")
                return
                
            data = response.json()
            if data.get("status") != "success":
                logger.error(f"Chart service error: {data}")
                await bot.answer_callback_query(call.id, "Chart service error")
                return
                
            # Convert base64 image
            image_data = base64.b64decode(data["image"])
            
            # Send photo
            await bot.send_photo(
                chat_id=call.message.chat.id,
                photo=image_data,
                reply_to_message_id=call.message.message_id,
                caption=f"TradingView Chart for {symbol} (15m timeframe)"
            )
            
            await bot.answer_callback_query(call.id)
            
    except Exception as e:
        logger.error(f"Error in technical analysis: {str(e)}")
        await bot.answer_callback_query(call.id, "An error occurred")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        logger.info(f"Received webhook data: {json.dumps(data, indent=2)}")
        update = Update.de_json(data, bot)
        
        if update.callback_query:
            await button_handler(update, None)
            
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}")
        return {"status": "error", "detail": str(e)}

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

def format_signal_message(signal_data: Dict[str, Any]) -> str:
    """Format signal message"""
    try:
        # If we already have a formatted message from the AI service, use that
        if "formatted_message" in signal_data:
            return signal_data["formatted_message"]
            
        # Otherwise, create basic signal format with HTML tags
        direction_emoji = "📈" if signal_data.get("direction", "").lower() == "buy" else "📉"
        
        message = f"""<b>🚨 New Trading Signal 🚨</b>

<b>Instrument:</b> {signal_data.get('instrument', 'Unknown')}
<b>Action:</b> {signal_data.get('direction', 'Unknown').upper()} {direction_emoji}

<b>Entry Price:</b> {signal_data.get('entry_price', 'Unknown')}
<b>Stop Loss:</b> {signal_data.get('stop_loss', 'Unknown')} 🛑
<b>Take Profit:</b> {signal_data.get('take_profit', 'Unknown')} 🎯

<b>Timeframe:</b> {signal_data.get('timeframe', 'Unknown')}
<b>Strategy:</b> {signal_data.get('strategy', 'Unknown')}

--------------------

<b>Risk Management:</b>
• Position size: 1-2% max
• Use proper stop loss
• Follow your trading plan

--------------------

<b>🤖 SigmaPips AI Verdict:</b>
{signal_data.get('ai_verdict', 'AI verdict not available.')}\n"""
        
        return message
    except Exception as e:
        logger.exception("Error formatting signal message")
        return "Error formatting signal message"

@app.get("/")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check endpoint called")
    return {"status": "ok", "service": "tradingview-telegram-service"}

async def get_subscribers() -> List[dict]:
    """Get all subscribers from Supabase."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}?select=*",
                headers={
                    'apikey': SUPABASE_KEY,
                    'Authorization': f'Bearer {SUPABASE_KEY}',
                    'Content-Type': 'application/json'
                }
            )
            response.raise_for_status()
            subscribers = response.json()
            logger.info(f"Found {len(subscribers)} subscribers")
            return subscribers
    except Exception as e:
        logger.error(f"Error getting subscribers: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting subscribers: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
