import os
import logging
import json
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    BotCommand,
    InputFile
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode, ChatMemberStatus
import pymongo
from bson import ObjectId
import redis
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import asyncio

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/telegram_bot')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
PORT = int(os.getenv('PORT', 8080))

# Initialize MongoDB
try:
    client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  # Test connection
    db = client['telegram_bot']
    logger.info("✅ MongoDB connected successfully")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    # Create in-memory fallback
    db = None

# Collections
if db:
    users_collection = db['users']
    channels_collection = db['channels']
    referrals_collection = db['referrals']
    transactions_collection = db['transactions']
    withdrawals_collection = db['withdrawals']
else:
    # Fallback to memory (for testing only)
    users_collection = None
    channels_collection = None
    referrals_collection = None
    transactions_collection = None
    withdrawals_collection = None
    logger.warning("⚠️ Using in-memory storage. Data will be lost on restart!")

# Initialize Redis
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
    redis_client.ping()
    logger.info("✅ Redis connected successfully")
except Exception as e:
    logger.error(f"❌ Redis connection failed: {e}")
    redis_client = None

# In-memory storage for testing
if not db:
    class MemoryStorage:
        def __init__(self):
            self.users = {}
            self.channels = {}
            self.referrals = []
            self.transactions = []
            self.withdrawals = []
        
        def find_one(self, query):
            collection = getattr(self, query.get('_collection', 'users'))
            if '_id' in query:
                return collection.get(query['_id'])
            return None
        
        def find(self, query=None):
            collection = getattr(self, query.get('_collection', 'users') if query else 'users')
            return list(collection.values()) if isinstance(collection, dict) else collection
    
    memory_db = MemoryStorage()
    users_collection = memory_db
    channels_collection = memory_db
    referrals_collection = memory_db
    transactions_collection = memory_db
    withdrawals_collection = memory_db

class ChannelManager:
    @staticmethod
    def get_active_channels() -> List[Dict]:
        """Get all active channels from database"""
        try:
            if channels_collection and hasattr(channels_collection, 'find'):
                channels = list(channels_collection.find(
                    {'is_active': True},
                    {'_id': 0, 'chat_id': 1, 'invite_link': 1, 'title': 1, 'type': 1}
                ))
                return channels
            else:
                # Fallback to default channels
                return [
                    {
                        'chat_id': -1001234567890,
                        'invite_link': 'https://t.me/example_channel',
                        'title': 'Example Channel',
                        'type': 'channel'
                    }
                ]
        except Exception as e:
            logger.error(f"Error getting channels: {e}")
            return []

    @staticmethod
    def add_channel(chat_id: int, invite_link: str, title: str, channel_type: str = "channel") -> bool:
        """Add a new channel to database"""
        try:
            if channels_collection and hasattr(channels_collection, 'insert_one'):
                existing = channels_collection.find_one({'chat_id': chat_id})
                if existing:
                    channels_collection.update_one(
                        {'chat_id': chat_id},
                        {'$set': {
                            'invite_link': invite_link,
                            'title': title,
                            'type': channel_type,
                            'is_active': True,
                            'updated_at': datetime.now()
                        }}
                    )
                else:
                    channels_collection.insert_one({
                        'chat_id': chat_id,
                        'invite_link': invite_link,
                        'title': title,
                        'type': channel_type,
                        'is_active': True,
                        'created_at': datetime.now(),
                        'updated_at': datetime.now()
                    })
                return True
            return False
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            return False

    @staticmethod
    def remove_channel(chat_id: int) -> bool:
        """Deactivate a channel"""
        try:
            if channels_collection and hasattr(channels_collection, 'update_one'):
                result = channels_collection.update_one(
                    {'chat_id': chat_id},
                    {'$set': {'is_active': False, 'updated_at': datetime.now()}}
                )
                return result.modified_count > 0
            return False
        except Exception as e:
            logger.error(f"Error removing channel: {e}")
            return False

class UserManager:
    @staticmethod
    async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> Dict:
        """Get existing user or create new user"""
        try:
            if users_collection and hasattr(users_collection, 'find_one'):
                user = users_collection.find_one({'user_id': user_id})
                
                if not user:
                    referral_code = f"REF{user_id}{datetime.now().strftime('%m%d')}"
                    user_data = {
                        'user_id': user_id,
                        'username': username,
                        'first_name': first_name,
                        'balance': 0.0,
                        'referral_code': referral_code,
                        'referral_count': 0,
                        'total_earned': 0.0,
                        'total_withdrawn': 0.0,
                        'created_at': datetime.now(),
                        'last_active': datetime.now(),
                        'has_joined_channels': False,
                        'is_banned': False,
                        'language': 'en'
                    }
                    users_collection.insert_one(user_data)
                    user = user_data
                    logger.info(f"Created new user: {user_id}")
                
                return user
            else:
                # Fallback
                return {
                    'user_id': user_id,
                    'username': username,
                    'first_name': first_name,
                    'balance': 0.0,
                    'referral_code': f"REF{user_id}{datetime.now().strftime('%m%d')}",
                    'referral_count': 0,
                    'total_earned': 0.0,
                    'has_joined_channels': False,
                    'is_banned': False
                }
        except Exception as e:
            logger.error(f"Error getting user: {e}")
            # Return minimal user data
            return {
                'user_id': user_id,
                'first_name': first_name or 'User',
                'balance': 0.0,
                'referral_code': f"REF{user_id}",
                'referral_count': 0,
                'total_earned': 0.0,
                'has_joined_channels': False
            }

    @staticmethod
    async def update_user_activity(user_id: int):
        """Update user's last active timestamp"""
        try:
            if users_collection and hasattr(users_collection, 'update_one'):
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {'last_active': datetime.now()}}
                )
        except Exception as e:
            logger.error(f"Error updating user activity: {e}")

    @staticmethod
    async def add_balance(user_id: int, amount: float, reason: str):
        """Add balance to user"""
        try:
            if users_collection and hasattr(users_collection, 'find_one'):
                user = users_collection.find_one({'user_id': user_id})
                if user:
                    new_balance = user['balance'] + amount
                    users_collection.update_one(
                        {'user_id': user_id},
                        {
                            '$set': {'balance': new_balance},
                            '$inc': {'total_earned': amount}
                        }
                    )
                    
                    # Record transaction
                    if transactions_collection and hasattr(transactions_collection, 'insert_one'):
                        transaction = {
                            'user_id': user_id,
                            'amount': amount,
                            'type': 'credit',
                            'reason': reason,
                            'status': 'completed',
                            'created_at': datetime.now()
                        }
                        transactions_collection.insert_one(transaction)
                    
                    logger.info(f"Added {amount} to user {user_id} for {reason}")
        except Exception as e:
            logger.error(f"Error adding balance: {e}")

class ReferralManager:
    @staticmethod
    async def record_referral(referrer_id: int, referred_id: int):
        """Record a referral"""
        try:
            if referrals_collection and hasattr(referrals_collection, 'find_one'):
                referral = referrals_collection.find_one({
                    'referrer_id': referrer_id,
                    'referred_id': referred_id
                })
                
                if not referral:
                    referral_data = {
                        'referrer_id': referrer_id,
                        'referred_id': referred_id,
                        'status': 'completed',
                        'created_at': datetime.now(),
                        'completed_at': datetime.now(),
                        'reward': 1.0
                    }
                    referrals_collection.insert_one(referral_data)
                    
                    # Add referral reward (1 RS)
                    await UserManager.add_balance(referrer_id, 1.0, 'referral')
                    
                    # Increment referral count
                    if users_collection and hasattr(users_collection, 'update_one'):
                        users_collection.update_one(
                            {'user_id': referrer_id},
                            {'$inc': {'referral_count': 1}}
                        )
                    
                    logger.info(f"Recorded referral: {referrer_id} -> {referred_id}")
        except Exception as e:
            logger.error(f"Error recording referral: {e}")

class ChannelVerification:
    @staticmethod
    async def check_channel_membership(bot, user_id: int) -> Tuple[bool, List[Dict]]:
        """Check if user is member of all required channels"""
        channels = ChannelManager.get_active_channels()
        not_joined = []
        
        for channel in channels:
            try:
                member = await bot.get_chat_member(
                    chat_id=channel['chat_id'],
                    user_id=user_id
                )
                if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, 'left', 'kicked']:
                    not_joined.append(channel)
            except Exception as e:
                logger.error(f"Error checking membership for {channel['chat_id']}: {e}")
                # Assume not joined if error
                not_joined.append(channel)
        
        return len(not_joined) == 0, not_joined
    
    @staticmethod
    def create_join_buttons(not_joined_channels: List[Dict]) -> InlineKeyboardMarkup:
        """Create inline buttons for joining channels"""
        keyboard = []
        
        for channel in not_joined_channels:
            emoji = "📢" if channel.get('type') == 'channel' else "👥"
            keyboard.append([
                InlineKeyboardButton(
                    f"{emoji} Join {channel['title']}",
                    url=channel['invite_link']
                )
            ])
        
        # Add check button
        keyboard.append([
            InlineKeyboardButton("✅ I've Joined - Check Now", callback_data="check_join")
        ])
        
        return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with referral parameter"""
    try:
        user = update.effective_user
        message = update.message or (update.callback_query.message if update.callback_query else None)
        
        if not message:
            return
            
        # Check if user is banned
        user_data = await UserManager.get_or_create_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name
        )
        
        if user_data.get('is_banned', False):
            await message.reply_text("🚫 Your account has been banned.")
            return
        
        # Check for referral parameter
        args = context.args
        if args and len(args) > 0:
            referral_code = args[0]
            if referral_code.startswith('REF'):
                # Find referrer
                if users_collection and hasattr(users_collection, 'find_one'):
                    referrer = users_collection.find_one({'referral_code': referral_code})
                    if referrer and referrer['user_id'] != user.id:
                        await ReferralManager.record_referral(referrer['user_id'], user.id)
                        await message.reply_text(
                            f"🎉 Referral accepted! You were referred by {referrer.get('first_name', 'a user')}."
                        )
        
        # Check channel membership
        has_joined, not_joined = await ChannelVerification.check_channel_membership(
            context.bot, user.id
        )
        
        if not has_joined:
            # Send join message with buttons
            channel_list = "\n".join([f"• {ch['title']}" for ch in not_joined])
            message_text = (
                "🔒 **Welcome!**\n\n"
                "To use this bot, you need to join our official channels/groups:\n"
                f"{channel_list}\n\n"
                "Please join all required channels and then click the check button below."
            )
            
            keyboard = ChannelVerification.create_join_buttons(not_joined)
            sent_msg = await message.reply_text(
                message_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Store message ID for cleanup
            if context.user_data is not None:
                context.user_data['join_message_id'] = sent_msg.message_id
        else:
            # User has joined all channels
            if users_collection and hasattr(users_collection, 'update_one'):
                users_collection.update_one(
                    {'user_id': user.id},
                    {'$set': {'has_joined_channels': True}}
                )
            
            # Clean up join message if exists
            if context.user_data and 'join_message_id' in context.user_data:
                try:
                    await context.bot.delete_message(
                        chat_id=user.id,
                        message_id=context.user_data['join_message_id']
                    )
                except:
                    pass
            
            # Show main menu
            await show_main_menu(update, context)
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text("An error occurred. Please try again.")

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle check join callback"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        # Check membership again
        has_joined, not_joined = await ChannelVerification.check_channel_membership(
            context.bot, user_id
        )
        
        if not has_joined:
            # Still not joined
            channel_list = "\n".join([f"• {ch['title']}" for ch in not_joined])
            message_text = (
                "❌ **Not Joined Yet!**\n\n"
                "You still need to join these channels:\n"
                f"{channel_list}\n\n"
                "Please join all channels and click check again."
            )
            
            keyboard = ChannelVerification.create_join_buttons(not_joined)
            await query.edit_message_text(
                text=message_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            # Successfully joined
            if users_collection and hasattr(users_collection, 'update_one'):
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {'has_joined_channels': True}}
                )
            
            # Clean up join message
            try:
                await query.delete_message()
            except:
                pass
            
            # Show main menu
            await show_main_menu_callback(update, context)
    except Exception as e:
        logger.error(f"Error in check_join_callback: {e}")
        try:
            await query.answer("An error occurred. Please try again.", show_alert=True)
        except:
            pass

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu"""
    try:
        user_id = update.effective_user.id
        message = update.message or (update.callback_query.message if update.callback_query else None)
        
        if not message:
            return
        
        user = await UserManager.get_or_create_user(user_id)
        
        # Update activity
        await UserManager.update_user_activity(user_id)
        
        message_text = (
            f"🎉 **Welcome {user['first_name']}!**\n\n"
            f"💰 **Balance:** ₹{user.get('balance', 0):.2f}\n"
            f"👥 **Referrals:** {user.get('referral_count', 0)}\n"
            f"💵 **Total Earned:** ₹{user.get('total_earned', 0):.2f}\n\n"
            "🔗 **Your Referral Link:**\n"
            f"`https://t.me/{context.bot.username}?start={user['referral_code']}`\n\n"
            "Share this link with friends and earn ₹1 for each successful referral!"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("👥 My Referrals", callback_data="my_referrals"),
                InlineKeyboardButton("💰 Withdraw", callback_data="withdraw")
            ],
            [
                InlineKeyboardButton("📊 Statistics", callback_data="stats"),
                InlineKeyboardButton("🆘 Help", callback_data="help")
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
                InlineKeyboardButton("📢 Channels", callback_data="view_channels")
            ]
        ]
        
        if user_id in ADMIN_IDS:
            keyboard.append([
                InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")
            ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Error in show_main_menu: {e}")
        await update.message.reply_text("An error occurred. Please try again.")

async def show_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu from callback"""
    await show_main_menu(update, context)

async def my_referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's referrals"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        # For now, just show a placeholder message
        message = "📭 You haven't referred anyone yet.\nShare your referral link to start earning!"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in my_referrals_callback: {e}")

async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal request"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        user = await UserManager.get_or_create_user(user_id)
        
        message = (
            f"💰 **Withdrawal Information**\n\n"
            f"Available Balance: ₹{user.get('balance', 0):.2f}\n"
            f"Minimum Withdrawal: ₹10\n\n"
            "To withdraw, use the command:\n"
            "`/withdraw <amount> <method>`\n\n"
            "Example:\n"
            "`/withdraw 50 UPI`\n\n"
            "Supported methods: UPI, Paytm, PhonePe"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in withdraw_callback: {e}")

async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show statistics"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Get channel count
        channels = ChannelManager.get_active_channels()
        
        message = (
            "📊 **Bot Statistics**\n\n"
            f"📢 Active Channels: {len(channels)}\n"
            "💰 Reward per Referral: ₹1\n"
            "💵 Minimum Withdrawal: ₹10\n\n"
            "🎯 **How to Earn:**\n"
            "1. Share your referral link\n"
            "2. Friends join channels\n"
            "3. You earn ₹1 per referral"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in stats_callback: {e}")

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help"""
    try:
        query = update.callback_query
        await query.answer()
        
        message = (
            "🆘 **Help & Instructions**\n\n"
            "📢 **How to Earn:**\n"
            "1. Join all required channels\n"
            "2. Get your referral link\n"
            "3. Share with friends\n"
            "4. Earn ₹1 per referral\n\n"
            "💰 **Withdrawal:**\n"
            "• Minimum: ₹10\n"
            "• Methods: UPI, Paytm, PhonePe\n"
            "• Use: `/withdraw <amount> <method>`\n\n"
            "📝 **Commands:**\n"
            "• /start - Start the bot\n"
            "• /balance - Check balance\n"
            "• /referral - Get referral link\n"
            "• /help - Show this help\n\n"
            "⚠️ **Important:**\n"
            "• You must join all channels\n"
            "• No spam or fake accounts\n"
            "• Follow Telegram rules"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in help_callback: {e}")

async def view_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View all required channels"""
    try:
        query = update.callback_query
        await query.answer()
        
        channels = ChannelManager.get_active_channels()
        
        if not channels:
            message_text = "📭 No channels required at the moment."
        else:
            message_text = "📢 **Required Channels/Groups:**\n\n"
            for i, channel in enumerate(channels, 1):
                emoji = "📢" if channel.get('type') == 'channel' else "👥"
                message_text += f"{i}. {emoji} {channel['title']}\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in view_channels_callback: {e}")

# Admin Commands
async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new channel (Admin only)"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /addchannel <chat_id> <invite_link> <title>\n"
            "Example: /addchannel -1001234567890 https://t.me/channelname Channel Name\n\n"
            "For private groups: Use invite link like https://t.me/+invitecode"
        )
        return
    
    try:
        chat_id = int(context.args[0])
        invite_link = context.args[1]
        title = " ".join(context.args[2:])
        
        # Validate invite link
        if not invite_link.startswith("https://t.me/"):
            await update.message.reply_text("❌ Invalid invite link. Must start with https://t.me/")
            return
        
        # Determine channel type
        channel_type = "group" if "+" in invite_link else "channel"
        
        # Add channel
        success = ChannelManager.add_channel(chat_id, invite_link, title, channel_type)
        
        if success:
            # Test bot access
            try:
                await context.bot.get_chat(chat_id)
                await update.message.reply_text(
                    f"✅ Channel added successfully!\n"
                    f"Title: {title}\n"
                    f"Chat ID: {chat_id}\n"
                    f"Type: {channel_type}\n"
                    f"Link: {invite_link}\n\n"
                    f"✅ Bot has access to this channel."
                )
            except Exception as e:
                await update.message.reply_text(
                    f"✅ Channel added but bot may not have access.\n"
                    f"Error: {str(e)[:100]}\n\n"
                    f"Make sure bot is added as admin to the channel."
                )
        else:
            await update.message.reply_text("❌ Failed to add channel. Check logs.")
        
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Must be an integer (negative for channels/groups).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a channel (Admin only)"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /removechannel <chat_id>\n"
            "Example: /removechannel -1001234567890"
        )
        return
    
    try:
        chat_id = int(context.args[0])
        
        # Remove channel
        success = ChannelManager.remove_channel(chat_id)
        
        if success:
            await update.message.reply_text(f"✅ Channel {chat_id} removed successfully!")
        else:
            await update.message.reply_text("❌ Channel not found or already removed.")
        
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Must be an integer.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all channels (Admin only)"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    channels = ChannelManager.get_active_channels()
    
    if not channels:
        await update.message.reply_text("📭 No active channels.")
        return
    
    message_text = "📋 **Active Channels:**\n\n"
    for i, channel in enumerate(channels, 1):
        emoji = "📢" if channel.get('type') == 'channel' else "👥"
        message_text += (
            f"{i}. {emoji} {channel['title']}\n"
            f"   ID: {channel['chat_id']}\n"
            f"   Link: {channel['invite_link']}\n\n"
        )
    
    await update.message.reply_text(message_text)

async def test_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test bot's access to a channel"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /testchannel <chat_id>\n"
            "Example: /testchannel -1001234567890"
        )
        return
    
    try:
        chat_id = int(context.args[0])
        
        # Try to get chat info
        try:
            chat = await context.bot.get_chat(chat_id)
            chat_type = chat.type
            title = chat.title
            
            # Check if bot is admin
            try:
                member = await context.bot.get_chat_member(chat_id, context.bot.id)
                is_admin = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
                admin_status = "✅ Bot is admin" if is_admin else "❌ Bot is not admin"
            except:
                admin_status = "❌ Cannot check admin status"
            
            await update.message.reply_text(
                f"✅ **Channel Test**\n\n"
                f"Title: {title}\n"
                f"Chat ID: {chat_id}\n"
                f"Type: {chat_type}\n"
                f"{admin_status}"
            )
            
        except Exception as e:
            await update.message.reply_text(
                f"❌ **Access Failed**\n\n"
                f"Chat ID: {chat_id}\n"
                f"Error: {str(e)[:100]}\n\n"
                "Make sure:\n"
                "1. Bot is added to the channel/group\n"
                "2. Bot has admin privileges\n"
                "3. Channel ID is correct"
            )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Must be an integer.")

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        if user_id not in ADMIN_IDS:
            await query.answer("You are not an admin!", show_alert=True)
            return
        
        channels = ChannelManager.get_active_channels()
        
        message_text = (
            "👑 **Admin Panel**\n\n"
            f"📢 Active Channels: {len(channels)}\n\n"
            "**Channel Management:**\n"
            "• /addchannel - Add new channel\n"
            "• /removechannel - Remove channel\n"
            "• /listchannels - List all channels\n"
            "• /testchannel - Test channel access\n\n"
            "**Bot Status:**\n"
            f"• MongoDB: {'✅ Connected' if db else '❌ Disconnected'}\n"
            f"• Redis: {'✅ Connected' if redis_client else '❌ Disconnected'}"
        )
        
        keyboard = [
            [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
            [InlineKeyboardButton("📊 Bot Stats", callback_data="admin_stats")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
        ]
        
        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in admin_panel_callback: {e}")

async def manage_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel management interface"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        if user_id not in ADMIN_IDS:
            await query.answer("You are not an admin!", show_alert=True)
            return
        
        channels = ChannelManager.get_active_channels()
        
        message_text = "📢 **Channel Management**\n\n"
        
        keyboard = []
        for channel in channels:
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Remove {channel['title'][:15]}",
                    callback_data=f"remove_{channel['chat_id']}"
                )
            ])
        
        keyboard.extend([
            [InlineKeyboardButton("➕ Add New Channel", callback_data="add_channel_info")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="manage_channels")],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
        ])
        
        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in manage_channels_callback: {e}")

async def handle_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel management callbacks"""
    try:
        query = update.callback_query
        await query.answer()
        
        callback_data = query.data
        
        if callback_data.startswith("remove_"):
            chat_id = int(callback_data.split("_")[1])
            success = ChannelManager.remove_channel(chat_id)
            
            if success:
                await query.answer(f"Channel removed!", show_alert=True)
                await manage_channels_callback(update, context)
            else:
                await query.answer("Failed to remove channel", show_alert=True)
        
        elif callback_data == "add_channel_info":
            await query.edit_message_text(
                text="To add a channel:\n\n"
                     "Use command:\n"
                     "`/addchannel <chat_id> <invite_link> <title>`\n\n"
                     "Example:\n"
                     "`/addchannel -1001234567890 https://t.me/mychannel My Channel`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="manage_channels")]
                ])
            )
        
        elif callback_data == "admin_stats":
            # Simple stats
            channels = ChannelManager.get_active_channels()
            await query.edit_message_text(
                text=f"📊 **Bot Statistics**\n\n"
                     f"Active Channels: {len(channels)}\n"
                     f"MongoDB: {'✅' if db else '❌'}\n"
                     f"Redis: {'✅' if redis_client else '❌'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
                ])
            )
    except Exception as e:
        logger.error(f"Error in handle_channel_callback: {e}")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /balance command"""
    try:
        user_id = update.effective_user.id
        user = await UserManager.get_or_create_user(user_id)
        
        message = (
            f"💰 **Your Balance**\n\n"
            f"Available: ₹{user.get('balance', 0):.2f}\n"
            f"Total Earned: ₹{user.get('total_earned', 0):.2f}\n"
            f"Referrals: {user.get('referral_count', 0)}\n\n"
            f"Your Referral Code: `{user['referral_code']}`"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in balance_command: {e}")
        await update.message.reply_text("Error getting balance. Please try again.")

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /referral command"""
    try:
        user_id = update.effective_user.id
        user = await UserManager.get_or_create_user(user_id)
        
        referral_link = f"https://t.me/{context.bot.username}?start={user['referral_code']}"
        message = (
            "🔗 **Your Referral Link**\n\n"
            f"`{referral_link}`\n\n"
            "Share this link with friends and earn ₹1 for each friend who:\n"
            "1. Joins all required channels\n"
            "2. Starts the bot using your link\n\n"
            f"**Your Code:** `{user['referral_code']}`\n"
            f"**Total Referrals:** {user.get('referral_count', 0)}\n"
            f"**Earned from Referrals:** ₹{user.get('total_earned', 0):.2f}"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in referral_command: {e}")
        await update.message.reply_text("Error getting referral link. Please try again.")

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /withdraw command"""
    try:
        user_id = update.effective_user.id
        user = await UserManager.get_or_create_user(user_id)
        
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /withdraw <amount> <payment_method>\n"
                "Example: /withdraw 50 UPI\n\n"
                f"Your balance: ₹{user.get('balance', 0):.2f}"
            )
            return
        
        try:
            amount = float(context.args[0])
            payment_method = context.args[1].upper()
            
            if amount < 10:
                await update.message.reply_text("❌ Minimum withdrawal amount is ₹10")
                return
            
            if amount > user.get('balance', 0):
                await update.message.reply_text("❌ Insufficient balance")
                return
            
            # For now, just confirm
            await update.message.reply_text(
                f"✅ Withdrawal request submitted!\n\n"
                f"Amount: ₹{amount}\n"
                f"Method: {payment_method}\n"
                f"Status: Pending approval\n\n"
                f"Admin will process your request within 24 hours."
            )
            
            # Notify admin
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"💰 New Withdrawal Request\n\n"
                             f"User: {user['first_name']} (@{user.get('username', 'N/A')})\n"
                             f"Amount: ₹{amount}\n"
                             f"Method: {payment_method}\n"
                             f"User ID: {user_id}"
                    )
                except:
                    pass
            
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Please enter a valid number.")
            
    except Exception as e:
        logger.error(f"Error in withdraw_command: {e}")
        await update.message.reply_text("Error processing withdrawal. Please try again.")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (Admin only)"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = " ".join(context.args)
    
    # In a real implementation, you would get users from database
    # For now, just confirm
    await update.message.reply_text(
        f"📢 Broadcast message prepared:\n\n{message}\n\n"
        f"⚠️ Note: User broadcast not implemented in demo."
    )

# Simple scheduler setup without errors
def setup_scheduler():
    """Setup background scheduler"""
    try:
        scheduler = BackgroundScheduler()
        
        # Simple job that doesn't require arguments
        def simple_cleanup():
            logger.info("🔄 Cleanup job running")
        
        scheduler.add_job(
            simple_cleanup,
            'interval',
            minutes=30,
            id='cleanup_job'
        )
        
        scheduler.start()
        logger.info("✅ Scheduler started successfully")
    except Exception as e:
        logger.error(f"❌ Failed to start scheduler: {e}")

# Clean message handler
async def clean_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean command messages"""
    try:
        if update.message:
            await update.message.delete()
    except:
        pass

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Sorry, an error occurred. Please try again."
            )
    except:
        pass

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set in environment variables")
        print("Please set BOT_TOKEN environment variable")
        return
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CommandHandler("help", help_callback))
    
    # Admin commands
    application.add_handler(CommandHandler("addchannel", add_channel_command))
    application.add_handler(CommandHandler("removechannel", remove_channel_command))
    application.add_handler(CommandHandler("listchannels", list_channels_command))
    application.add_handler(CommandHandler("testchannel", test_channel_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^back_to_main$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^refresh$"))
    application.add_handler(CallbackQueryHandler(my_referrals_callback, pattern="^my_referrals$"))
    application.add_handler(CallbackQueryHandler(withdraw_callback, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(stats_callback, pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(help_callback, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(view_channels_callback, pattern="^view_channels$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(manage_channels_callback, pattern="^manage_channels$"))
    application.add_handler(CallbackQueryHandler(handle_channel_callback, pattern="^(remove_|add_channel_info|admin_stats)"))
    
    # Message handler for cleanup
    application.add_handler(MessageHandler(filters.COMMAND, clean_message))
    
    # Setup scheduler
    setup_scheduler()
    
    # Start bot
    logger.info("🤖 Bot is starting...")
    print("✅ Bot is running!")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    
    # Run with webhook for Render compatibility
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--webhook':
        from flask import Flask, request
        import threading
        
        app = Flask(__name__)
        
        @app.route('/')
        def home():
            return "✅ Bot is running"
        
        @app.route('/health')
        def health():
            return "OK", 200
        
        @app.route(f'/{BOT_TOKEN}', methods=['POST'])
        def webhook():
            update = Update.de_json(request.get_json(force=True), application.bot)
            application.update_queue.put(update)
            return 'OK'
        
        # Set webhook
        webhook_url = os.getenv('WEBHOOK_URL', f'https://{os.getenv("RENDER_SERVICE_NAME", "localhost")}.onrender.com')
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}"
        )
        
        # Start Flask in background
        threading.Thread(target=lambda: app.run(
            host='0.0.0.0',
            port=PORT,
            debug=False
        )).start()
    else:
        # Polling mode (for development)
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()