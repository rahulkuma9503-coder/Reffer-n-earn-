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
    BotCommand
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode
import pymongo
import redis

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
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []

# Initialize MongoDB
try:
    client = pymongo.MongoClient(MONGODB_URI)
    db = client['telegram_referral_bot']
    
    # Test connection
    client.server_info()
    logger.info("✅ MongoDB connected successfully")
    
    # Create collections if they don't exist
    if 'users' not in db.list_collection_names():
        db.create_collection('users')
    if 'channels' not in db.list_collection_names():
        db.create_collection('channels')
    if 'referrals' not in db.list_collection_names():
        db.create_collection('referrals')
    
    users_collection = db['users']
    channels_collection = db['channels']
    referrals_collection = db['referrals']
    
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    # Create simple in-memory storage for testing
    db = None
    users_collection = None
    channels_collection = None
    referrals_collection = None

# Initialize Redis
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logger.info("✅ Redis connected successfully")
except Exception as e:
    logger.error(f"❌ Redis connection failed: {e}")
    redis_client = None

# In-memory storage for channels (fallback if MongoDB fails)
MEMORY_CHANNELS = []

class ChannelManager:
    @staticmethod
    def get_active_channels() -> List[Dict]:
        """Get all active channels from database or memory"""
        try:
            if channels_collection is not None:
                channels = list(channels_collection.find({'is_active': True}))
                return channels
            else:
                return MEMORY_CHANNELS
        except Exception as e:
            logger.error(f"Error getting channels: {e}")
            return []
    
    @staticmethod
    def add_channel(chat_id: int, invite_link: str, title: str) -> bool:
        """Add a new channel"""
        try:
            if channels_collection is not None:
                channel_data = {
                    'chat_id': chat_id,
                    'invite_link': invite_link,
                    'title': title,
                    'is_active': True,
                    'created_at': datetime.now()
                }
                channels_collection.insert_one(channel_data)
                return True
            else:
                # Add to memory
                MEMORY_CHANNELS.append({
                    'chat_id': chat_id,
                    'invite_link': invite_link,
                    'title': title,
                    'is_active': True
                })
                return True
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            return False
    
    @staticmethod
    def remove_channel(chat_id: int) -> bool:
        """Remove a channel"""
        try:
            if channels_collection is not None:
                result = channels_collection.delete_one({'chat_id': chat_id})
                return result.deleted_count > 0
            else:
                global MEMORY_CHANNELS
                MEMORY_CHANNELS = [c for c in MEMORY_CHANNELS if c['chat_id'] != chat_id]
                return True
        except Exception as e:
            logger.error(f"Error removing channel: {e}")
            return False

class UserManager:
    @staticmethod
    async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> Dict:
        """Get or create user"""
        try:
            if users_collection is not None:
                user = users_collection.find_one({'user_id': user_id})
                
                if not user:
                    referral_code = f"REF{user_id}"
                    user_data = {
                        'user_id': user_id,
                        'username': username,
                        'first_name': first_name,
                        'balance': 0.0,
                        'referral_code': referral_code,
                        'referral_count': 0,
                        'total_earned': 0.0,
                        'created_at': datetime.now(),
                        'last_active': datetime.now(),
                        'has_joined_channels': False,
                        'is_banned': False
                    }
                    users_collection.insert_one(user_data)
                    return user_data
                return user
            else:
                # Simple memory user
                return {
                    'user_id': user_id,
                    'username': username,
                    'first_name': first_name,
                    'balance': 0.0,
                    'referral_code': f"REF{user_id}",
                    'referral_count': 0,
                    'total_earned': 0.0,
                    'has_joined_channels': False,
                    'is_banned': False
                }
        except Exception as e:
            logger.error(f"Error getting user: {e}")
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
    async def update_balance(user_id: int, amount: float, reason: str):
        """Update user balance"""
        try:
            if users_collection is not None:
                user = users_collection.find_one({'user_id': user_id})
                if user:
                    new_balance = user.get('balance', 0) + amount
                    users_collection.update_one(
                        {'user_id': user_id},
                        {
                            '$set': {'balance': new_balance},
                            '$inc': {'total_earned': amount, 'referral_count': 1 if reason == 'referral' else 0}
                        }
                    )
        except Exception as e:
            logger.error(f"Error updating balance: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    
    # Get or create user
    user_data = await UserManager.get_or_create_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name
    )
    
    # Check referral code
    args = context.args
    if args and args[0].startswith('REF'):
        referral_code = args[0]
        if referral_code != user_data['referral_code']:
            # Find referrer
            if users_collection is not None:
                referrer = users_collection.find_one({'referral_code': referral_code})
                if referrer and referrer['user_id'] != user.id:
                    # Record referral
                    if referrals_collection is not None:
                        referrals_collection.insert_one({
                            'referrer_id': referrer['user_id'],
                            'referred_id': user.id,
                            'created_at': datetime.now()
                        })
                    
                    # Add reward to referrer
                    await UserManager.update_balance(referrer['user_id'], 1.0, 'referral')
                    
                    await update.message.reply_text(
                        f"🎉 You were referred by {referrer.get('first_name', 'a friend')}! "
                        f"They earned ₹1 for your join."
                    )
    
    # Check channel membership
    channels = ChannelManager.get_active_channels()
    
    if not channels:
        # No channels required, show main menu directly
        await show_main_menu(update, context)
        return
    
    not_joined = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel['chat_id'],
                user_id=user.id
            )
            if member.status in ['left', 'kicked']:
                not_joined.append(channel)
        except Exception as e:
            logger.error(f"Error checking channel {channel['chat_id']}: {e}")
            not_joined.append(channel)
    
    if not_joined:
        # Show join buttons
        keyboard = []
        for channel in not_joined:
            keyboard.append([
                InlineKeyboardButton(
                    f"📢 Join {channel['title']}",
                    url=channel['invite_link']
                )
            ])
        keyboard.append([
            InlineKeyboardButton("✅ Check Membership", callback_data="check_membership")
        ])
        
        channel_list = "\n".join([f"• {ch['title']}" for ch in not_joined])
        
        await update.message.reply_text(
            f"🔒 **Welcome to the bot!**\n\n"
            f"To continue, please join these channels:\n{channel_list}\n\n"
            f"After joining, click the button below to verify.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        # Already joined all channels
        await show_main_menu(update, context)

async def check_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if user has joined all channels"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    channels = ChannelManager.get_active_channels()
    
    if not channels:
        await query.edit_message_text("No channels required. Welcome!")
        await show_main_menu_callback(update, context)
        return
    
    not_joined = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel['chat_id'],
                user_id=user.id
            )
            if member.status in ['left', 'kicked']:
                not_joined.append(channel)
        except Exception as e:
            logger.error(f"Error checking channel {channel['chat_id']}: {e}")
            not_joined.append(channel)
    
    if not_joined:
        # Still not joined
        keyboard = []
        for channel in not_joined:
            keyboard.append([
                InlineKeyboardButton(
                    f"📢 Join {channel['title']}",
                    url=channel['invite_link']
                )
            ])
        keyboard.append([
            InlineKeyboardButton("✅ Check Again", callback_data="check_membership")
        ])
        
        channel_list = "\n".join([f"• {ch['title']}" for ch in not_joined])
        
        await query.edit_message_text(
            f"❌ **Still not joined!**\n\n"
            f"Please join these channels:\n{channel_list}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        # Successfully joined
        if users_collection is not None:
            users_collection.update_one(
                {'user_id': user.id},
                {'$set': {'has_joined_channels': True}}
            )
        
        await query.edit_message_text("✅ **Great! You've joined all channels.**")
        await show_main_menu_callback(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu"""
    user = update.effective_user
    user_data = await UserManager.get_or_create_user(user.id)
    
    message = (
        f"🎉 **Welcome {user_data['first_name']}!**\n\n"
        f"💰 **Balance:** ₹{user_data.get('balance', 0):.2f}\n"
        f"👥 **Referrals:** {user_data.get('referral_count', 0)}\n"
        f"💵 **Total Earned:** ₹{user_data.get('total_earned', 0):.2f}\n\n"
        f"🔗 **Your Referral Link:**\n"
        f"`https://t.me/{context.bot.username}?start={user_data['referral_code']}`\n\n"
        "Share this link to earn ₹1 for each friend who joins!"
    )
    
    keyboard = [
        [InlineKeyboardButton("👥 My Referrals", callback_data="my_referrals")],
        [InlineKeyboardButton("💰 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("📢 Required Channels", callback_data="view_channels")],
        [InlineKeyboardButton("🆘 Help", callback_data="help")]
    ]
    
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    
    if update.callback_query:
        await update.callback_query.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def show_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu from callback"""
    await show_main_menu(update, context)

async def view_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View required channels"""
    query = update.callback_query
    await query.answer()
    
    channels = ChannelManager.get_active_channels()
    
    if not channels:
        message = "📭 No channels required at the moment."
    else:
        message = "📢 **Required Channels:**\n\n"
        for i, channel in enumerate(channels, 1):
            message += f"{i}. {channel['title']}\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def my_referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's referrals"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if referrals_collection is not None:
        referrals = list(referrals_collection.find({'referrer_id': user.id}))
        count = len(referrals)
    else:
        count = 0
    
    message = f"👥 **Your Referrals:** {count}\n\n"
    message += f"💰 **Earned from referrals:** ₹{count * 1:.2f}\n\n"
    message += f"Your referral link:\n`https://t.me/{context.bot.username}?start=REF{user.id}`"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = await UserManager.get_or_create_user(user.id)
    
    message = (
        f"💰 **Withdrawal**\n\n"
        f"Your balance: ₹{user_data.get('balance', 0):.2f}\n"
        f"Minimum withdrawal: ₹10\n\n"
        "To withdraw, send:\n"
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

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /withdraw command"""
    user = update.effective_user
    user_data = await UserManager.get_or_create_user(user.id)
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /withdraw <amount> <method>\n"
            "Example: /withdraw 50 UPI"
        )
        return
    
    try:
        amount = float(context.args[0])
        method = context.args[1].upper()
        
        if amount < 10:
            await update.message.reply_text("❌ Minimum withdrawal is ₹10")
            return
        
        if amount > user_data.get('balance', 0):
            await update.message.reply_text("❌ Insufficient balance")
            return
        
        # For now, just show confirmation
        await update.message.reply_text(
            f"✅ Withdrawal request submitted!\n\n"
            f"Amount: ₹{amount}\n"
            f"Method: {method}\n\n"
            f"Your request will be processed within 24 hours."
        )
        
        # Notify admin
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"💰 New withdrawal request:\n"
                         f"User: {user_data['first_name']}\n"
                         f"Amount: ₹{amount}\n"
                         f"Method: {method}\n"
                         f"User ID: {user.id}"
                )
            except:
                pass
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount")

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help"""
    query = update.callback_query
    await query.answer()
    
    message = (
        "🆘 **Help**\n\n"
        "📢 **How it works:**\n"
        "1. Join required channels\n"
        "2. Get your referral link\n"
        "3. Share with friends\n"
        "4. Earn ₹1 per referral\n\n"
        "💰 **Withdrawal:**\n"
        "• Minimum: ₹10\n"
        "• Methods: UPI, Paytm, PhonePe\n"
        "• Use: `/withdraw <amount> <method>`\n\n"
        "📝 **Commands:**\n"
        "• /start - Start bot\n"
        "• /balance - Check balance\n"
        "• /referral - Get referral link\n"
        "• /withdraw - Withdraw money\n"
        "• /help - Show this message"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /balance command"""
    user = update.effective_user
    user_data = await UserManager.get_or_create_user(user.id)
    
    await update.message.reply_text(
        f"💰 **Your Balance:** ₹{user_data.get('balance', 0):.2f}\n"
        f"👥 **Referrals:** {user_data.get('referral_count', 0)}\n"
        f"💵 **Total Earned:** ₹{user_data.get('total_earned', 0):.2f}"
    )

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /referral command"""
    user = update.effective_user
    user_data = await UserManager.get_or_create_user(user.id)
    
    referral_link = f"https://t.me/{context.bot.username}?start={user_data['referral_code']}"
    
    await update.message.reply_text(
        f"🔗 **Your Referral Link:**\n`{referral_link}`\n\n"
        f"Share this link to earn ₹1 for each friend who joins!"
    )

# Admin Commands
async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new channel (Admin only)"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /addchannel <chat_id> <invite_link> <title>\n"
            "Example: /addchannel -1001234567890 https://t.me/channelname Channel Name"
        )
        return
    
    try:
        chat_id = int(context.args[0])
        invite_link = context.args[1]
        title = " ".join(context.args[2:])
        
        success = ChannelManager.add_channel(chat_id, invite_link, title)
        
        if success:
            await update.message.reply_text(f"✅ Channel added: {title}")
        else:
            await update.message.reply_text("❌ Failed to add channel")
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Must be a number.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a channel (Admin only)"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
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
        
        success = ChannelManager.remove_channel(chat_id)
        
        if success:
            await update.message.reply_text(f"✅ Channel removed: {chat_id}")
        else:
            await update.message.reply_text("❌ Channel not found")
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all channels (Admin only)"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    channels = ChannelManager.get_active_channels()
    
    if not channels:
        await update.message.reply_text("📭 No channels added yet.")
        return
    
    message = "📢 **Active Channels:**\n\n"
    for i, channel in enumerate(channels, 1):
        message += f"{i}. {channel['title']}\n"
        message += f"   ID: {channel['chat_id']}\n"
        message += f"   Link: {channel['invite_link']}\n\n"
    
    await update.message.reply_text(message)

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("❌ Admin only", show_alert=True)
        return
    
    channels = ChannelManager.get_active_channels()
    
    message = (
        "👑 **Admin Panel**\n\n"
        f"📢 Active Channels: {len(channels)}\n"
        f"🤖 Bot Status: ✅ Running\n\n"
        "**Commands:**\n"
        "• /addchannel - Add new channel\n"
        "• /removechannel - Remove channel\n"
        "• /listchannels - List all channels\n"
        "• /broadcast - Send message to all users"
    )
    
    keyboard = [
        [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
        [InlineKeyboardButton("👥 View Users", callback_data="view_users")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def manage_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage channels interface"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("❌ Admin only", show_alert=True)
        return
    
    channels = ChannelManager.get_active_channels()
    
    message = "📢 **Manage Channels**\n\n"
    
    keyboard = []
    for channel in channels[:10]:  # Show first 10
        keyboard.append([
            InlineKeyboardButton(
                f"❌ {channel['title'][:20]}",
                callback_data=f"remove_channel_{channel['chat_id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Add New", callback_data="add_channel_info")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("remove_channel_"):
        try:
            chat_id = int(data.split("_")[-1])
            success = ChannelManager.remove_channel(chat_id)
            
            if success:
                await query.answer("✅ Channel removed", show_alert=True)
                await manage_channels_callback(update, context)
            else:
                await query.answer("❌ Failed to remove", show_alert=True)
        except:
            await query.answer("❌ Error", show_alert=True)
    
    elif data == "add_channel_info":
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
    
    elif data == "view_users":
        if users_collection is not None:
            user_count = users_collection.count_documents({})
            await query.edit_message_text(
                text=f"👥 **Total Users:** {user_count}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
                ])
            )
        else:
            await query.edit_message_text(
                text="👥 **Users:** Database not connected",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
                ])
            )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (Admin only)"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = " ".join(context.args)
    
    if users_collection is not None:
        users = users_collection.find({})
        count = 0
        
        for user_doc in users:
            try:
                await context.bot.send_message(
                    chat_id=user_doc['user_id'],
                    text=f"📢 **Announcement:**\n\n{message}"
                )
                count += 1
            except:
                pass
        
        await update.message.reply_text(f"✅ Broadcast sent to {count} users")
    else:
        await update.message.reply_text("❌ User database not available")

# Clean message handler
async def clean_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean command messages"""
    try:
        if update.message:
            await update.message.delete()
    except:
        pass

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set")
        print("Please set BOT_TOKEN environment variable")
        return
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
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
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^back_to_main$"))
    application.add_handler(CallbackQueryHandler(view_channels_callback, pattern="^view_channels$"))
    application.add_handler(CallbackQueryHandler(my_referrals_callback, pattern="^my_referrals$"))
    application.add_handler(CallbackQueryHandler(withdraw_callback, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(help_callback, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(manage_channels_callback, pattern="^manage_channels$"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^(remove_channel_|add_channel_info|view_users)$"))
    
    # Message handler for cleanup
    application.add_handler(MessageHandler(filters.COMMAND, clean_message))
    
    # Start bot
    logger.info("🤖 Bot is starting...")
    print("✅ Bot is running!")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()