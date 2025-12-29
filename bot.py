import os
import logging
from datetime import datetime
from typing import List, Dict
import json
import threading
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
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
PORT = int(os.getenv('PORT', 8080))

# Simple in-memory storage
class Storage:
    def __init__(self):
        self.channels = []  # Format: {'chat_id': str, 'name': str}
        self.users = {}     # Format: {user_id: {user_data}}
        self.referrals = {} # Format: {referred_user_id: referrer_user_id}
        self.load_data()
    
    def load_data(self):
        """Load data from files"""
        try:
            if os.path.exists('channels.json'):
                with open('channels.json', 'r') as f:
                    self.channels = json.load(f)
            
            if os.path.exists('users.json'):
                with open('users.json', 'r') as f:
                    self.users = json.load(f)
            
            if os.path.exists('referrals.json'):
                with open('referrals.json', 'r') as f:
                    self.referrals = json.load(f)
        except:
            pass
    
    def save_data(self):
        """Save data to files"""
        try:
            with open('channels.json', 'w') as f:
                json.dump(self.channels, f)
            
            with open('users.json', 'w') as f:
                json.dump(self.users, f)
            
            with open('referrals.json', 'w') as f:
                json.dump(self.referrals, f)
        except Exception as e:
            logger.error(f"Error saving data: {e}")

# Global storage
storage = Storage()

class ChannelManager:
    """Manage channels"""
    
    @staticmethod
    def get_channels() -> List[Dict]:
        return storage.channels
    
    @staticmethod
    def add_channel(chat_id: str) -> bool:
        try:
            clean_id = chat_id.strip()
            
            # Format chat_id
            if clean_id.startswith('@'):
                chat_id_str = clean_id
            else:
                if clean_id.startswith('-'):
                    chat_id_str = clean_id
                elif clean_id.isdigit() or (clean_id.startswith('100') and len(clean_id) > 9):
                    chat_id_str = f"-{clean_id.lstrip('-')}"
                else:
                    return False
            
            # Check duplicate
            for channel in storage.channels:
                if str(channel.get('chat_id')) == str(chat_id_str):
                    return False
            
            # Add channel
            channel = {
                'chat_id': chat_id_str,
                'name': f"Join Channel {len(storage.channels) + 1}",
                'added_at': datetime.now().isoformat()
            }
            storage.channels.append(channel)
            storage.save_data()
            return True
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            return False
    
    @staticmethod
    def remove_channel(chat_id: str) -> bool:
        try:
            original_count = len(storage.channels)
            storage.channels = [
                c for c in storage.channels 
                if str(c.get('chat_id')) != str(chat_id.strip())
            ]
            
            if len(storage.channels) < original_count:
                storage.save_data()
                return True
            return False
        except Exception as e:
            logger.error(f"Error removing channel: {e}")
            return False

class UserManager:
    """Manage users with fixed referral system"""
    
    @staticmethod
    def get_user(user_id: int) -> Dict:
        user_str = str(user_id)
        
        if user_str in storage.users:
            return storage.users[user_str]
        
        # Create new user
        user_data = {
            'user_id': user_id,
            'balance': 0.0,
            'referral_code': f"REF{user_id}",
            'referral_count': 0,
            'total_earned': 0.0,
            'total_withdrawn': 0.0,
            'joined_at': datetime.now().isoformat(),
            'last_active': datetime.now().isoformat(),
            'transactions': [],
            'has_joined_channels': False
        }
        
        storage.users[user_str] = user_data
        storage.save_data()
        return user_data
    
    @staticmethod
    def update_user(user_id: int, updates: Dict):
        user_str = str(user_id)
        if user_str in storage.users:
            storage.users[user_str].update(updates)
            storage.users[user_str]['last_active'] = datetime.now().isoformat()
            storage.save_data()
    
    @staticmethod
    def add_transaction(user_id: int, amount: float, tx_type: str, description: str):
        user = UserManager.get_user(user_id)
        
        transaction = {
            'id': len(user.get('transactions', [])) + 1,
            'amount': amount,
            'type': tx_type,
            'description': description,
            'date': datetime.now().isoformat()
        }
        
        if 'transactions' not in user:
            user['transactions'] = []
        
        user['transactions'].append(transaction)
        
        if len(user['transactions']) > 50:
            user['transactions'] = user['transactions'][-50:]
        
        UserManager.update_user(user_id, user)
    
    @staticmethod
    def is_referred(user_id: int) -> bool:
        """Check if user was referred by someone"""
        user_str = str(user_id)
        return user_str in storage.referrals
    
    @staticmethod
    def get_referrer(user_id: int) -> int:
        """Get referrer of a user"""
        user_str = str(user_id)
        if user_str in storage.referrals:
            return int(storage.referrals[user_str])
        return None
    
    @staticmethod
    def add_referral(referrer_id: int, referred_id: int) -> bool:
        """Add a referral - returns True if new referral, False if duplicate"""
        if referrer_id == referred_id:
            return False
        
        referred_str = str(referred_id)
        
        # Check if already referred
        if referred_str in storage.referrals:
            logger.info(f"User {referred_id} already referred by {storage.referrals[referred_str]}")
            return False
        
        # Record referral
        storage.referrals[referred_str] = str(referrer_id)
        storage.save_data()
        
        # Update referrer's stats
        referrer = UserManager.get_user(referrer_id)
        new_balance = referrer.get('balance', 0) + 1.0
        
        UserManager.update_user(referrer_id, {
            'balance': new_balance,
            'referral_count': referrer.get('referral_count', 0) + 1,
            'total_earned': referrer.get('total_earned', 0) + 1.0
        })
        
        # Add transaction
        UserManager.add_transaction(
            referrer_id, 
            1.0, 
            'credit', 
            f'Referral bonus for user {referred_id}'
        )
        
        logger.info(f"New referral: {referrer_id} → {referred_id}")
        return True

async def check_channel_membership(bot, user_id: int) -> tuple:
    """Check if user is member of all channels"""
    channels = ChannelManager.get_channels()
    
    if not channels:
        return True, []
    
    not_joined = []
    
    for channel in channels:
        chat_id = channel['chat_id']
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(channel)
        except Exception as e:
            logger.error(f"Error checking {chat_id}: {e}")
            not_joined.append(channel)
    
    return len(not_joined) == 0, not_joined

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with fixed referral system"""
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    # Check for referral parameter
    args = context.args
    if args and args[0].startswith('REF'):
        referral_code = args[0]
        
        # Skip if user was already referred
        if UserManager.is_referred(user.id):
            await update.message.reply_text(
                "⚠️ You have already been referred before. "
                "Referral bonus only works for new users."
            )
        else:
            # Find referrer by code
            referrer_found = None
            for user_id_str, user_data in storage.users.items():
                if user_data.get('referral_code') == referral_code:
                    referrer_found = int(user_id_str)
                    break
            
            if referrer_found and referrer_found != user.id:
                # Add referral (prevents duplicates)
                is_new_referral = UserManager.add_referral(referrer_found, user.id)
                
                if is_new_referral:
                    # Notify referrer
                    try:
                        referrer_name = user.first_name
                        await context.bot.send_message(
                            chat_id=referrer_found,
                            text=f"🎉 **New Referral!**\n\n"
                                 f"You have successfully referred a new user:\n"
                                 f"• Name: {user.first_name}\n"
                                 f"• User ID: {user.id}\n"
                                 f"• Bonus: ₹1.00\n\n"
                                 f"💰 Your new balance: ₹{UserManager.get_user(referrer_found)['balance']:.2f}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify referrer: {e}")
                    
                    await update.message.reply_text(
                        f"✅ **Referral Accepted!**\n\n"
                        f"You were referred by user {referrer_found}.\n"
                        f"They earned ₹1.00 for your join!"
                    )
                else:
                    await update.message.reply_text(
                        "⚠️ This referral link has already been used."
                    )
    
    # Check channel membership
    has_joined, not_joined = await check_channel_membership(
        context.bot, user.id
    )
    
    if not has_joined and not_joined:
        await show_join_buttons(update, context, not_joined)
    else:
        UserManager.update_user(user.id, {'has_joined_channels': True})
        await show_main_menu(update, context)

async def show_join_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE, not_joined: List[Dict]):
    """Show join buttons"""
    user = update.effective_user
    
    keyboard = []
    for channel in not_joined:
        chat_id = channel['chat_id']
        channel_name = channel.get('name', 'Join Channel')
        
        if isinstance(chat_id, str) and chat_id.startswith('-'):
            try:
                chat = await context.bot.get_chat(int(chat_id))
                invite_link = await chat.export_invite_link()
                keyboard.append([
                    InlineKeyboardButton(f"📢 {channel_name}", url=invite_link)
                ])
            except:
                keyboard.append([
                    InlineKeyboardButton(f"📢 {channel_name}", url=f"https://t.me/{chat_id.lstrip('-')}")
                ])
        elif isinstance(chat_id, str) and chat_id.startswith('@'):
            keyboard.append([
                InlineKeyboardButton(f"📢 {channel_name}", url=f"https://t.me/{chat_id.lstrip('@')}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(f"📢 {channel_name}", url=f"https://t.me/c/{chat_id}")
            ])
    
    keyboard.append([
        InlineKeyboardButton("✅ Verify Join", callback_data="verify_join")
    ])
    
    await update.message.reply_text(
        f"👋 Welcome {user.first_name}!\n\n"
        f"Please join the required channel(s) to continue.\n"
        f"After joining, click 'Verify Join' button.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify join"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    has_joined, not_joined = await check_channel_membership(
        context.bot, user.id
    )
    
    if has_joined:
        UserManager.update_user(user.id, {'has_joined_channels': True})
        await query.edit_message_text(
            "✅ **Verified!** You've joined all required channels.\n\n"
            "Now you can access all features."
        )
        await show_main_menu_callback(update, context)
    else:
        keyboard = []
        for channel in not_joined:
            chat_id = channel['chat_id']
            channel_name = channel.get('name', 'Join Channel')
            
            if isinstance(chat_id, str) and chat_id.startswith('-'):
                try:
                    chat = await context.bot.get_chat(int(chat_id))
                    invite_link = await chat.export_invite_link()
                    keyboard.append([
                        InlineKeyboardButton(f"📢 {channel_name}", url=invite_link)
                    ])
                except:
                    keyboard.append([
                        InlineKeyboardButton(f"📢 {channel_name}", url=f"https://t.me/{chat_id.lstrip('-')}")
                    ])
            else:
                keyboard.append([
                    InlineKeyboardButton(f"📢 {channel_name}", url=f"https://t.me/c/{chat_id}")
                ])
        
        keyboard.append([
            InlineKeyboardButton("🔄 Check Again", callback_data="verify_join")
        ])
        
        await query.edit_message_text(
            f"❌ **Not joined yet!**\n\n"
            f"Please join all required channels.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu"""
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    message = (
        f"👤 **Account Overview**\n\n"
        f"🆔 **User ID:** `{user.id}`\n"
        f"👤 **Name:** {user.first_name}\n"
        f"💰 **Balance:** ₹{user_data['balance']:.2f}\n"
        f"👥 **Referrals:** {user_data['referral_count']}\n"
        f"💵 **Total Earned:** ₹{user_data['total_earned']:.2f}\n"
        f"📤 **Total Withdrawn:** ₹{user_data['total_withdrawn']:.2f}"
    )
    
    keyboard = [
        [InlineKeyboardButton("💰 Balance", callback_data="balance"),
         InlineKeyboardButton("📤 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("📜 History", callback_data="history"),
         InlineKeyboardButton("👥 Referrals", callback_data="referrals")],
        [InlineKeyboardButton("🔗 Invite Link", callback_data="invite_link"),
         InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]
    ]
    
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("👑 Admin", callback_data="admin_panel")])
    
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

async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show balance"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    message = (
        f"💰 **Balance Details**\n\n"
        f"💳 **Available:** ₹{user_data['balance']:.2f}\n"
        f"📈 **Total Earned:** ₹{user_data['total_earned']:.2f}\n"
        f"📤 **Total Withdrawn:** ₹{user_data['total_withdrawn']:.2f}\n\n"
        f"👥 **Referral Earnings:** ₹{user_data['referral_count']:.0f}\n\n"
        f"💎 **Earn more:** Share your invite link!"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Withdraw menu"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    message = (
        f"📤 **Withdrawal**\n\n"
        f"💰 **Balance:** ₹{user_data['balance']:.2f}\n"
        f"📦 **Minimum:** ₹10\n\n"
        "**How to withdraw:**\n"
        "Use command: `/withdraw <amount> <method>`\n\n"
        "**Examples:**\n"
        "`/withdraw 50 UPI`\n"
        "`/withdraw 100 Paytm`\n\n"
        "**Methods:** UPI, Paytm, PhonePe, Bank"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal"""
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ **Usage:** `/withdraw <amount> <method>`\n\n"
            "**Example:** `/withdraw 50 UPI`\n"
            "**Methods:** UPI, Paytm, PhonePe, Bank",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        amount = float(context.args[0])
        method = context.args[1].upper()
        
        if amount < 10:
            await update.message.reply_text("❌ Minimum withdrawal is ₹10")
            return
        
        if amount > user_data['balance']:
            await update.message.reply_text("❌ Insufficient balance")
            return
        
        # Process withdrawal
        new_balance = user_data['balance'] - amount
        UserManager.update_user(user.id, {
            'balance': new_balance,
            'total_withdrawn': user_data.get('total_withdrawn', 0) + amount
        })
        
        UserManager.add_transaction(
            user.id,
            -amount,
            'withdrawal',
            f'Withdrawal via {method}'
        )
        
        # Notify admin
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"💰 **New Withdrawal**\n\n"
                         f"👤 User: {user.first_name}\n"
                         f"🆔 ID: {user.id}\n"
                         f"💵 Amount: ₹{amount:.2f}\n"
                         f"📱 Method: {method}"
                )
            except:
                pass
        
        await update.message.reply_text(
            f"✅ **Withdrawal Requested!**\n\n"
            f"💵 **Amount:** ₹{amount:.2f}\n"
            f"📱 **Method:** {method}\n"
            f"⏳ **Status:** Pending\n"
            f"📅 **Processed within:** 24 hours\n\n"
            f"💰 **New Balance:** ₹{new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount")

async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transaction history"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    transactions = user_data.get('transactions', [])
    
    if not transactions:
        message = "📜 **No transactions yet**\n\nShare your invite link to start earning!"
    else:
        message = "📜 **Transaction History**\n\n"
        for tx in reversed(transactions[-10:]):
            amount = tx['amount']
            tx_type = "➕" if tx['type'] == 'credit' else "➖"
            date = datetime.fromisoformat(tx['date']).strftime('%d %b %H:%M')
            message += f"`{date}` {tx_type} ₹{amount:.2f}\n{tx['description']}\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Referral stats"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    # Get list of users referred by this user
    referred_users = []
    for referred_str, referrer_str in storage.referrals.items():
        if referrer_str == str(user.id):
            referred_user_id = int(referred_str)
            referred_user = UserManager.get_user(referred_user_id)
            referred_users.append(referred_user)
    
    message = (
        f"👥 **Referral Program**\n\n"
        f"📊 **Total Referrals:** {user_data['referral_count']}\n"
        f"💰 **Earned from Referrals:** ₹{user_data['referral_count']:.2f}\n"
        f"💵 **Earn per Referral:** ₹1.00\n\n"
    )
    
    if referred_users:
        message += "**Your Referrals:**\n"
        for i, ref_user in enumerate(referred_users[:10], 1):
            message += f"{i}. User ID: {ref_user['user_id']}\n"
        if len(referred_users) > 10:
            message += f"... and {len(referred_users) - 10} more\n\n"
    else:
        message += "**No referrals yet.**\n\n"
    
    message += "**How it works:**\n"
    message += "1. Share your invite link\n"
    message += "2. Friend joins all channels\n"
    message += "3. Friend starts bot with your link\n"
    message += "4. You earn ₹1 immediately!"
    
    keyboard = [
        [InlineKeyboardButton("🔗 Get Invite Link", callback_data="invite_link")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def invite_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Invite link"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = UserManager.get_user(user.id)
    
    referral_code = user_data['referral_code']
    invite_link = f"https://t.me/{context.bot.username}?start={referral_code}"
    
    message = (
        f"🔗 **Your Invite Link**\n\n"
        f"Share this link to earn ₹1 per referral:\n\n"
        f"`{invite_link}`\n\n"
        f"**Your Stats:**\n"
        f"• Referrals: {user_data['referral_count']}\n"
        f"• Earned: ₹{user_data['referral_count'] * 1:.2f}\n\n"
        f"**Important:**\n"
        f"• Each user can use your link only once\n"
        f"• You earn when they complete all steps\n"
        f"• No duplicate earnings from same user\n\n"
        f"**Referral Code:** `{referral_code}`"
    )
    
    keyboard = [
        [InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={invite_link}&text=Join%20this%20bot%20to%20earn%20money%21")],
        [InlineKeyboardButton("👥 Referral Stats", callback_data="referrals")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# Admin Commands
async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add channel"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ **Usage:** `/addchannel <channel_uid>`\n\n"
            "**Examples:**\n"
            "`/addchannel -1001234567890`\n"
            "`/addchannel @username`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    chat_id = context.args[0]
    success = ChannelManager.add_channel(chat_id)
    
    if success:
        await update.message.reply_text(
            f"✅ **Channel Added!**\n\n"
            f"**UID:** {chat_id}\n"
            f"**Total Channels:** {len(storage.channels)}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("❌ Failed to add channel")

async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove channel"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ **Usage:** `/removechannel <channel_uid>`\n\n"
            "**Example:** `/removechannel -1001234567890`"
        )
        return
    
    chat_id = context.args[0]
    success = ChannelManager.remove_channel(chat_id)
    
    if success:
        await update.message.reply_text(
            f"✅ **Channel Removed!**\n\n"
            f"**UID:** {chat_id}\n"
            f"**Remaining:** {len(storage.channels)}"
        )
    else:
        await update.message.reply_text("❌ Channel not found")

async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List channels"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    
    channels = ChannelManager.get_channels()
    
    if not channels:
        await update.message.reply_text("📭 No channels added")
        return
    
    message = "📢 **Required Channels:**\n\n"
    for i, channel in enumerate(channels, 1):
        message += f"{i}. {channel['name']}\n"
        message += f"   `{channel['chat_id']}`\n\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("❌ Admin only", show_alert=True)
        return
    
    total_users = len(storage.users)
    total_channels = len(storage.channels)
    total_balance = sum(u.get('balance', 0) for u in storage.users.values())
    total_referrals = len(storage.referrals)
    
    message = (
        "👑 **Admin Panel**\n\n"
        f"📊 **Statistics:**\n"
        f"• Total Users: {total_users}\n"
        f"• Total Channels: {total_channels}\n"
        f"• Total Balance: ₹{total_balance:.2f}\n"
        f"• Total Referrals: {total_referrals}\n\n"
        "**Commands:**\n"
        "• `/addchannel <uid>` - Add channel\n"
        "• `/removechannel <uid>` - Remove channel\n"
        "• `/listchannels` - List channels\n"
        "• `/broadcast <message>` - Broadcast"
    )
    
    keyboard = [
        [InlineKeyboardButton("📢 Channels", callback_data="admin_channels")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin channels"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("❌ Admin only", show_alert=True)
        return
    
    channels = ChannelManager.get_channels()
    
    message = f"📢 **Channel Management**\n\nTotal: {len(channels)}\n\n"
    
    keyboard = []
    for channel in channels:
        keyboard.append([
            InlineKeyboardButton(
                f"❌ {channel['name'][:20]}",
                callback_data=f"admin_remove_{channel['chat_id']}"
            )
        ])
    
    keyboard.extend([
        [InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_info")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_channels")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ])
    
    await query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("admin_remove_"):
        chat_id = data.replace("admin_remove_", "", 1)
        success = ChannelManager.remove_channel(chat_id)
        
        if success:
            await query.answer("✅ Channel removed", show_alert=True)
            await admin_channels_callback(update, context)
        else:
            await query.answer("❌ Failed", show_alert=True)
    
    elif data == "admin_add_info":
        await query.edit_message_text(
            text="**Add Channel**\n\n"
                 "Use command: `/addchannel <uid>`\n\n"
                 "**Examples:**\n"
                 "`/addchannel -1001234567890`\n"
                 "`/addchannel @username`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_channels")]
            ])
        )
    
    elif data == "admin_stats":
        total_users = len(storage.users)
        total_channels = len(storage.channels)
        total_balance = sum(u.get('balance', 0) for u in storage.users.values())
        total_earned = sum(u.get('total_earned', 0) for u in storage.users.values())
        total_referrals = len(storage.referrals)
        
        await query.edit_message_text(
            text=f"📊 **Statistics**\n\n"
                 f"👥 Users: {total_users}\n"
                 f"📢 Channels: {total_channels}\n"
                 f"💰 Total Balance: ₹{total_balance:.2f}\n"
                 f"💵 Total Earned: ₹{total_earned:.2f}\n"
                 f"👥 Total Referrals: {total_referrals}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ])
        )
    
    elif data == "admin_users":
        total_users = len(storage.users)
        active_users = sum(1 for u in storage.users.values() 
                          if 'last_active' in u and 
                          (datetime.now() - datetime.fromisoformat(u['last_active'])).days < 7)
        
        await query.edit_message_text(
            text=f"👥 **Users**\n\n"
                 f"📊 Total Users: {total_users}\n"
                 f"🎯 Active (7 days): {active_users}\n\n"
                 "Use `/broadcast` to message all users.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ])
        )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Usage: `/broadcast <message>`")
        return
    
    message = " ".join(context.args)
    sent_count = 0
    
    await update.message.reply_text(f"📢 Broadcasting to {len(storage.users)} users...")
    
    for user_id_str in storage.users:
        try:
            await context.bot.send_message(
                chat_id=int(user_id_str),
                text=f"📢 **Announcement:**\n\n{message}"
            )
            sent_count += 1
        except:
            continue
    
    await update.message.reply_text(f"✅ Sent to {sent_count}/{len(storage.users)} users")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help"""
    await update.message.reply_text(
        "❓ **Help**\n\n"
        "**Commands:**\n"
        "• /start - Start bot\n"
        "• /withdraw - Withdraw money\n"
        "• /help - Show this message\n\n"
        "**How to Earn:**\n"
        "• Join required channels\n"
        "• Share your invite link\n"
        "• Earn ₹1 per referral\n\n"
        "**Withdrawal:**\n"
        "• Minimum: ₹10\n"
        "• Methods: UPI, Paytm, PhonePe, Bank\n"
        "• Processing: 24 hours"
    )

# Simple HTTP server for Render
def run_http_server():
    """Run HTTP server for health checks"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Bot is running')
        
        def log_message(self, format, *args):
            pass
    
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
        logger.info(f"✅ HTTP server running on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ HTTP server failed: {e}")

def main():
    """Start bot"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set")
        print("ERROR: Set BOT_TOKEN environment variable")
        return
    
    # Start HTTP server
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Admin commands
    application.add_handler(CommandHandler("addchannel", add_channel_command))
    application.add_handler(CommandHandler("removechannel", remove_channel_command))
    application.add_handler(CommandHandler("listchannels", list_channels_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^back_to_main$"))
    application.add_handler(CallbackQueryHandler(show_main_menu_callback, pattern="^refresh$"))
    application.add_handler(CallbackQueryHandler(balance_callback, pattern="^balance$"))
    application.add_handler(CallbackQueryHandler(withdraw_callback, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(history_callback, pattern="^history$"))
    application.add_handler(CallbackQueryHandler(referrals_callback, pattern="^referrals$"))
    application.add_handler(CallbackQueryHandler(invite_link_callback, pattern="^invite_link$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_channels_callback, pattern="^admin_channels$"))
    application.add_handler(CallbackQueryHandler(admin_handle_callback, pattern="^admin_"))
    
    # Start bot
    logger.info("🤖 Bot is starting...")
    print(f"✅ Bot started!")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    print(f"📢 Channels: {len(storage.channels)}")
    print(f"👥 Users: {len(storage.users)}")
    print(f"🔗 Referrals: {len(storage.referrals)}")
    print(f"🌐 HTTP Server: http://0.0.0.0:{PORT}")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Bot stopped: {e}")

if __name__ == '__main__':
    main()