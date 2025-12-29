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

# Initialize MongoDB
client = pymongo.MongoClient(MONGODB_URI)
db = client['telegram_bot']
users_collection = db['users']
channels_collection = db['channels']
referrals_collection = db['referrals']
transactions_collection = db['transactions']
withdrawals_collection = db['withdrawals']

# Initialize Redis
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Scheduler
scheduler = BackgroundScheduler()

class ChannelManager:
    @staticmethod
    def get_active_channels() -> List[Dict]:
        """Get all active channels from database"""
        channels = list(channels_collection.find(
            {'is_active': True},
            {'_id': 0, 'chat_id': 1, 'invite_link': 1, 'title': 1, 'type': 1}
        ))
        return channels
    
    @staticmethod
    def add_channel(chat_id: int, invite_link: str, title: str, channel_type: str = "channel") -> bool:
        """Add a new channel to database"""
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
    
    @staticmethod
    def remove_channel(chat_id: int) -> bool:
        """Deactivate a channel"""
        result = channels_collection.update_one(
            {'chat_id': chat_id},
            {'$set': {'is_active': False, 'updated_at': datetime.now()}}
        )
        return result.modified_count > 0
    
    @staticmethod
    def update_channel(chat_id: int, **kwargs) -> bool:
        """Update channel information"""
        result = channels_collection.update_one(
            {'chat_id': chat_id},
            {'$set': {**kwargs, 'updated_at': datetime.now()}}
        )
        return result.modified_count > 0

class UserManager:
    @staticmethod
    async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> Dict:
        """Get existing user or create new user"""
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
    
    @staticmethod
    async def update_user_activity(user_id: int):
        """Update user's last active timestamp"""
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'last_active': datetime.now()}}
        )
    
    @staticmethod
    async def add_balance(user_id: int, amount: float, reason: str):
        """Add balance to user"""
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

class ReferralManager:
    @staticmethod
    async def record_referral(referrer_id: int, referred_id: int):
        """Record a referral"""
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
            users_collection.update_one(
                {'user_id': referrer_id},
                {'$inc': {'referral_count': 1}}
            )
            
            logger.info(f"Recorded referral: {referrer_id} -> {referred_id}")

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
                if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
                    not_joined.append(channel)
            except Exception as e:
                logger.error(f"Error checking membership for {channel['chat_id']}: {e}")
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
    user = update.effective_user
    message = update.message or update.callback_query.message
    
    # Clean previous messages if callback
    if update.callback_query:
        try:
            await update.callback_query.delete_message()
        except:
            pass
    
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
        context.user_data['join_message_id'] = sent_msg.message_id
    else:
        # User has joined all channels
        users_collection.update_one(
            {'user_id': user.id},
            {'$set': {'has_joined_channels': True}}
        )
        
        # Clean up join message if exists
        if 'join_message_id' in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=user.id,
                    message_id=context.user_data['join_message_id']
                )
            except:
                pass
        
        # Show main menu
        await show_main_menu(update, context)

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle check join callback"""
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

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu"""
    user_id = update.effective_user.id
    message = update.message or update.callback_query.message if update.callback_query else None
    
    user = users_collection.find_one({'user_id': user_id})
    
    if not user:
        await message.reply_text("User not found!")
        return
    
    # Update activity
    await UserManager.update_user_activity(user_id)
    
    message_text = (
        f"🎉 **Welcome {user['first_name']}!**\n\n"
        f"💰 **Balance:** ₹{user['balance']:.2f}\n"
        f"👥 **Referrals:** {user['referral_count']}\n"
        f"💵 **Total Earned:** ₹{user['total_earned']:.2f}\n"
        f"📤 **Total Withdrawn:** ₹{user.get('total_withdrawn', 0):.2f}\n\n"
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

async def show_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu from callback"""
    await show_main_menu(update, context)

async def view_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View all required channels"""
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
            message_text += f"   Link: {channel['invite_link']}\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Admin Commands for Channel Management
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
            "For groups: Use -100 for supergroups, - for private groups"
        )
        return
    
    try:
        chat_id = int(context.args[0])
        invite_link = context.args[1]
        title = " ".join(context.args[2:])
        
        # Determine channel type
        channel_type = "channel" if "t.me/+" not in invite_link else "group"
        
        # Add channel
        ChannelManager.add_channel(chat_id, invite_link, title, channel_type)
        
        # Test bot access
        try:
            await context.bot.get_chat(chat_id)
            access_status = "✅ Bot has access"
        except Exception as e:
            access_status = f"⚠️ Bot may not have access: {str(e)[:50]}"
        
        await update.message.reply_text(
            f"✅ Channel added successfully!\n"
            f"Title: {title}\n"
            f"Chat ID: {chat_id}\n"
            f"Type: {channel_type}\n"
            f"Link: {invite_link}\n"
            f"{access_status}"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Must be an integer.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

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
        
        # Find channel
        channel = channels_collection.find_one({'chat_id': chat_id})
        if not channel:
            await update.message.reply_text("❌ Channel not found")
            return
        
        # Remove channel
        ChannelManager.remove_channel(chat_id)
        
        await update.message.reply_text(
            f"✅ Channel removed successfully!\n"
            f"Title: {channel['title']}\n"
            f"Chat ID: {chat_id}"
        )
        
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
    
    channels = list(channels_collection.find({}))
    
    if not channels:
        await update.message.reply_text("📭 No channels in database.")
        return
    
    message_text = "📋 **All Channels in Database:**\n\n"
    for i, channel in enumerate(channels, 1):
        status = "✅ Active" if channel.get('is_active', True) else "❌ Inactive"
        emoji = "📢" if channel.get('type') == 'channel' else "👥"
        message_text += (
            f"{i}. {emoji} {channel['title']}\n"
            f"   ID: {channel['chat_id']}\n"
            f"   Status: {status}\n"
            f"   Link: {channel['invite_link']}\n"
            f"   Added: {channel.get('created_at', 'N/A').strftime('%Y-%m-%d')}\n\n"
        )
    
    await update.message.reply_text(message_text)

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel with channel management"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.answer("You are not an admin!", show_alert=True)
        return
    
    total_channels = channels_collection.count_documents({'is_active': True})
    total_users = users_collection.count_documents({})
    today_users = users_collection.count_documents({
        'created_at': {'$gte': datetime.now().replace(hour=0, minute=0, second=0)}
    })
    
    message_text = (
        "👑 **Admin Panel**\n\n"
        f"📊 Total Users: {total_users}\n"
        f"📈 New Today: {today_users}\n"
        f"📢 Active Channels: {total_channels}\n\n"
        "**Channel Management:**\n"
        "• /addchannel - Add new channel\n"
        "• /removechannel - Remove channel\n"
        "• /listchannels - List all channels\n"
        "• /testchannel - Test channel access\n\n"
        "**User Management:**\n"
        "• /broadcast - Send message to all users\n"
        "• /user <id> - View user details\n"
        "• /ban <id> - Ban user\n"
        "• /unban <id> - Unban user\n"
        "• /stats - Detailed statistics"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels"),
            InlineKeyboardButton("👥 User Management", callback_data="admin_users")
        ],
        [
            InlineKeyboardButton("💰 Payouts", callback_data="admin_payouts"),
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    await query.edit_message_text(
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def manage_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel management interface"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.answer("You are not an admin!", show_alert=True)
        return
    
    channels = ChannelManager.get_active_channels()
    
    message_text = "📢 **Channel Management**\n\n"
    message_text += f"Active Channels: {len(channels)}\n\n"
    
    keyboard = []
    for channel in channels[:10]:  # Show first 10 channels
        keyboard.append([
            InlineKeyboardButton(
                f"❌ {channel['title'][:20]}",
                callback_data=f"remove_channel_{channel['chat_id']}"
            )
        ])
    
    keyboard.extend([
        [InlineKeyboardButton("➕ Add New Channel", callback_data="add_channel_dialog")],
        [InlineKeyboardButton("🔄 Refresh List", callback_data="refresh_channels")],
        [InlineKeyboardButton("📋 Export Channels", callback_data="export_channels")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
    ])
    
    await query.edit_message_text(
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
                admin_status = "✅ Bot is admin" if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER] else "❌ Bot is not admin"
            except:
                admin_status = "❌ Cannot check admin status"
            
            await update.message.reply_text(
                f"✅ **Channel Access Test**\n\n"
                f"Title: {title}\n"
                f"Chat ID: {chat_id}\n"
                f"Type: {chat_type}\n"
                f"{admin_status}\n\n"
                f"Bot can see members: {'✅' if admin_status.startswith('✅') else '❌'}"
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

async def export_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export channels to JSON file"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    channels = list(channels_collection.find({}, {'_id': 0}))
    
    if not channels:
        await update.message.reply_text("📭 No channels to export.")
        return
    
    # Convert ObjectId to string and datetime to string
    for channel in channels:
        if 'created_at' in channel and isinstance(channel['created_at'], datetime):
            channel['created_at'] = channel['created_at'].isoformat()
        if 'updated_at' in channel and isinstance(channel['updated_at'], datetime):
            channel['updated_at'] = channel['updated_at'].isoformat()
    
    # Create JSON file
    import tempfile
    import json
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(channels, f, indent=2, ensure_ascii=False)
        temp_file = f.name
    
    # Send file
    try:
        with open(temp_file, 'rb') as file:
            await update.message.reply_document(
                document=InputFile(file, filename='channels_export.json'),
                caption=f"Exported {len(channels)} channels"
            )
    finally:
        import os
        os.unlink(temp_file)

async def import_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Import channels from JSON file"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command")
        return
    
    if not update.message.document:
        await update.message.reply_text(
            "Please send a JSON file with channel data.\n"
            "Use /exportchannels to get the format."
        )
        return
    
    try:
        # Download file
        file = await update.message.document.get_file()
        import tempfile
        import json
        
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as f:
            await file.download_to_drive(f.name)
            temp_file = f.name
        
        # Read and import
        with open(temp_file, 'r') as f:
            channels = json.load(f)
        
        imported = 0
        for channel in channels:
            try:
                ChannelManager.add_channel(
                    channel['chat_id'],
                    channel['invite_link'],
                    channel['title'],
                    channel.get('type', 'channel')
                )
                imported += 1
            except:
                continue
        
        await update.message.reply_text(
            f"✅ Imported {imported} channels successfully!"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed: {str(e)}")
    finally:
        import os
        if 'temp_file' in locals():
            os.unlink(temp_file)

# Additional callback handlers
async def handle_channel_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel removal from callback"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.answer("You are not an admin!", show_alert=True)
        return
    
    # Extract chat_id from callback data
    callback_data = query.data
    if callback_data.startswith("remove_channel_"):
        try:
            chat_id = int(callback_data.split("_")[-1])
            channel = channels_collection.find_one({'chat_id': chat_id})
            
            if channel:
                ChannelManager.remove_channel(chat_id)
                await query.answer(f"Channel {channel['title']} removed!", show_alert=True)
                await manage_channels_callback(update, context)
            else:
                await query.answer("Channel not found!", show_alert=True)
        except:
            await query.answer("Error removing channel!", show_alert=True)

async def add_channel_dialog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show add channel dialog"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.answer("You are not an admin!", show_alert=True)
        return
    
    message_text = (
        "➕ **Add New Channel**\n\n"
        "To add a channel, use the command:\n"
        "`/addchannel <chat_id> <invite_link> <title>`\n\n"
        "**How to get Chat ID:**\n"
        "1. Add @username_to_id_bot to your channel\n"
        "2. Forward a message from channel to the bot\n"
        "3. It will show the Chat ID (negative number)\n\n"
        "**Example:**\n"
        "`/addchannel -1001234567890 https://t.me/my_channel My Channel`"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="manage_channels")]]
    await query.edit_message_text(
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# Cleanup function
async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE):
    """Clean up old messages"""
    try:
        # Implementation depends on your storage strategy
        pass
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

def setup_scheduler():
    """Setup background scheduler"""
    scheduler.add_job(
        cleanup_old_messages,
        IntervalTrigger(minutes=30),
        id='cleanup_job',
        replace_existing=True
    )
    scheduler.start()

# Register handlers
def setup_handlers(application):
    """Setup all bot handlers"""
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", lambda u, c: show_main_menu(u, c)))
    application.add_handler(CommandHandler("referral", lambda u, c: show_main_menu(u, c)))
    application.add_handler(CommandHandler("withdraw", lambda u, c: withdraw_callback(u, c)))
    application.add_handler(CommandHandler("help", lambda u, c: help_callback(u, c)))
    
    # Admin commands
    application.add_handler(CommandHandler("addchannel", add_channel_command))
    application.add_handler(CommandHandler("removechannel", remove_channel_command))
    application.add_handler(CommandHandler("listchannels", list_channels_command))
    application.add_handler(CommandHandler("testchannel", test_channel_command))
    application.add_handler(CommandHandler("exportchannels", export_channels_command))
    application.add_handler(CommandHandler("importchannels", import_channels_command))
    application.add_handler(CommandHandler("broadcast", lambda u, c: admin_panel_callback(u, c)))
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^back_to_main$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^refresh$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: my_referrals_callback(u, c), pattern="^my_referrals$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: withdraw_callback(u, c), pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: stats_callback(u, c), pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: help_callback(u, c), pattern="^help$"))
    application.add_handler(CallbackQueryHandler(view_channels_callback, pattern="^view_channels$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(manage_channels_callback, pattern="^manage_channels$"))
    application.add_handler(CallbackQueryHandler(handle_channel_removal, pattern="^remove_channel_"))
    application.add_handler(CallbackQueryHandler(add_channel_dialog_callback, pattern="^add_channel_dialog$"))
    
    # Message handler for cleanup
    application.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))  # Silent handler
    
    # Set bot commands
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("balance", "Check your balance"),
        BotCommand("referral", "Get your referral link"),
        BotCommand("withdraw", "Withdraw earnings"),
        BotCommand("help", "Show help")
    ]
    
    # Admin commands
    admin_commands = commands + [
        BotCommand("addchannel", "Add new channel (Admin)"),
        BotCommand("removechannel", "Remove channel (Admin)"),
        BotCommand("listchannels", "List all channels (Admin)"),
        BotCommand("testchannel", "Test channel access (Admin)"),
        BotCommand("broadcast", "Broadcast message (Admin)")
    ]
    
    async def set_commands():
        await application.bot.set_my_commands(commands)
        # Set admin commands separately if needed
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set in environment variables")
        return
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Setup scheduler
    setup_scheduler()
    
    # Setup handlers
    setup_handlers(application)
    
    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()