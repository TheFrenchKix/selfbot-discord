import discord
from discord.ext import commands
from discord.state import ConnectionState
import os
import sys
import aiohttp
import traceback
from datetime import datetime
import time
import random
import asyncio
import re
from bs4 import BeautifulSoup
import qrcode
import io
import hmac
import json
import logging
from web3 import Web3

# Force UTF-8 encoding for entire script
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

from litecoin import LitecoinPriceManager
from selfbot import automation as automation_mod
from selfbot import shop as shop_api
from selfbot import swap as swap_mod
from selfbot import swapcog
from selfbot import toolkit
from selfbot import ui
from selfbot.config import ConfigError, load_settings, mask_token
from selfbot.helpers import (
    calcul, getUrlInMessage, getPriceInMessage, getQuantityInMessage,
    encode_code, decode_code, pluralize, is_error_result,
)
from selfbot.instance import InstanceState
from selfbot.logging_setup import register_secret, setup_logging
from selfbot.storage.db import Database

log = logging.getLogger("selfbot.bot")

# Réglages: environnement (.env) + config.ini (non secret). Le token est validé au démarrage.
SETTINGS = load_settings(require_token=False)
register_secret(SETTINGS.token)
for _k in (SETTINGS.api.swapzone, SETTINGS.api.fixedfloat_key, SETTINGS.api.fixedfloat_secret):
    if _k:
        register_secret(_k)
if SETTINGS.proxy_url:
    register_secret(SETTINGS.proxy_url)
STATE = InstanceState(settings=SETTINGS)
spawn = STATE.spawn

def _db() -> Database:
    """Base SQLite de l'instance (ouverte à la demande)."""
    if STATE.db is None:
        STATE.db = Database(SETTINGS.data_dir / "selfbot.db")
    return STATE.db

STATS = toolkit.Stats(lambda: _db())

# Variables
stockSniper             = True
noMove                  = False
autoBuy                 = False
autoMode                = "ticket"
autoRedeem              = False
maxprice                = SETTINGS.filters.max_price
addtosnipe              = False
nomoveChannel           = None
proxies_enabled         = False
price_manager           = None
callback_price          = 0.0
deleteTime              = 2
deleteTimeLong          = 5
ltcconvert              = 100000000
snipeDelay              = None
waitingTx               = False
guessing                = False

stockAuthor             = None
stockGuild              = None

startTime               = 0
ticketTime              = 0

validNitro              = set()
checkedCodes            = set()

TOKEN                   = SETTINGS.token
CUSTOM_TOKEN            = SETTINGS.token
exchangerateAPIkey      = SETTINGS.api.exchangerate
tatumAPIkey             = SETTINGS.api.tatum

ltcprivate              = ''
usdtprivate             = ''

stocksChannels          = set()
snipeChannels           = set()
ticketChannels          = set()
button_cache            = {}
guild_ticket_map        = {}

guildsId                = set()

deletedStockChannels    = set()
deletedCategory         = set()
deleteList              = set()

attachment_cache        = {}

sleepUsers              = set()
sleepStatus             = False
sleepMessage            = "I'm sleeping."

DEBUG                   = False
PREFIX                  = SETTINGS.command_prefix

codeRegex               = re.compile("(discord.com/gifts/|discordapp.com/gifts/|discord.gift/)([a-zA-Z0-9]+)")

ltc_address             = SETTINGS.ltc_address
usdt_address            = SETTINGS.usdt_address
privatepass             = ''

api_token               = SETTINGS.api.blockcypher

msgChannel              = SETTINGS.channels.message
vouchChannel            = SETTINGS.channels.vouch
transactionChannel      = SETTINGS.channels.transaction

deletedStockChannels    = set()
deletedCategory         = set()

# Initialize an empty set to store keywords
keywords                = set()
excludeKeywords         = set()
labelsCheck             = set()

bingo_guessed_numbers   = set()
bingo_channel           = None
bingo_max_number        = 1

PROXY_URL              = SETTINGS.proxy_url


def mask_proxy(url):
    """Masque user:pass d'une URL de proxy."""
    return re.sub(r"//[^/@]*@", "//***:***@", url) if url else "Not set"

def get_user_agent():
    """User-Agent configurable (USER_AGENT), sinon celui de la lib Discord."""
    if SETTINGS.user_agent:
        return SETTINGS.user_agent
    try:
        ua = getattr(client.http, "user_agent", None)
        if ua:
            return ua
    except NameError:
        pass
    return "Mozilla/5.0"

# Add this patch before creating the client
def patch_parse_ready_supplemental():
    try:
        original_parse = ConnectionState.parse_ready_supplemental

        def patched_parse_ready_supplemental(self, data):
            try:
                # Ensure pending_payments exists and is a list
                if 'pending_payments' not in data:
                    data['pending_payments'] = []
                print(f'Patched parse_ready_supplemental')
                return original_parse(self, data)
            except Exception as e:
                print(f"Error in parse_ready_supplemental: {e}")
                # Set empty pending payments
                self.pending_payments = {}
                # Continue with rest of ready process
                del self._ready_data
                self.call_handlers('connect')
                self.dispatch('connect')
                self._ready_task = asyncio.create_task(self._delay_ready())

        ConnectionState.parse_ready_supplemental = patched_parse_ready_supplemental
    except AttributeError:
        # If the method doesn't exist, create a dummy method or skip patching
        print("parse_ready_supplemental method not found in ConnectionState, skipping patch")
        pass

# Apply the patch before creating the client
patch_parse_ready_supplemental()

# Create the bot object with self_bot=True
class SelfBot(commands.Bot):
    async def setup_hook(self):
        """Charge le cog de commandes utilitaires (selfbot/extras.py)."""
        from selfbot import extras
        await extras.setup(self, lambda: STATE.db, PREFIX)
        await automation_mod.setup(self, _automation_bridge(), lambda: _db())
        await swapcog.setup(self, _swap_bridge(), lambda: _db(), PREFIX)

    async def process_commands(self, message):
        """Expanse les alias personnels (`alias add`) pour nos propres messages."""
        if message.content and self.user is not None and message.author.id == self.user.id:
            cog = self.get_cog("Automation")
            if cog is not None:
                message.content = cog.expand(message.content)
        await super().process_commands(message)

    async def close(self):
        """Arrêt propre: annule les tâches de fond suivies, ferme sessions HTTP/DB."""
        try:
            if getattr(self, "_loop_price", None):
                self._loop_price.stop()
            await STATE.shutdown()
        finally:
            await super().close()

try:
    client = SelfBot(command_prefix=SETTINGS.command_prefix, self_bot=True)
    STATE.client = client
except Exception as e:
    log.error("Error creating client: %s", e)
    sys.exit(1)

async def setup_proxy():
    """Configure proxy for all aiohttp sessions including Discord gateway"""

    connector = aiohttp.TCPConnector(enable_cleanup_closed=True)

    session = aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": get_user_agent()}
    )

    client.http.connector = connector
    client.http._session = session

    printdebug(f"✅ Proxy configured: {mask_proxy(PROXY_URL)}", "SUCCESS")
    printdebug(f"Connector type: {type(connector).__name__}", "INFO")

    # Try to get session ID
    try:
        session_id = client.ws.session_id if hasattr(client.ws, 'session_id') else "Not available yet"
        printdebug(f"Session ID: {session_id}", "INFO")
    except Exception as e:
        printdebug(f"Could not get session ID: {str(e)}", "WARNING")

    return session

@client.event
async def on_ready():
    global stocksChannels, ticketChannels, price_manager
    log.info('Logged in as %s - %s', client.user.name, client.user.id)

    file_to_table("stocks.txt", stocksChannels)
    file_to_table("tickets.txt", ticketChannels)
    getGuilds()
    await build_guild_ticket_map()
    await cache_all_buttons()
    start_price_manager()

    # Check proxy configuration
    if PROXY_URL:
        printdebug(f"Bot is configured to use proxy: {mask_proxy(PROXY_URL)}", "SUCCESS")
    else:
        printdebug("No proxy URL configured", "WARNING")

    # Fix: Changed condition from "if hasattr" to "if not hasattr"
    if not hasattr(client.ws, '_proxy_configured'):
        await setup_proxy()
        client._proxy_configured = True
        printdebug("Proxy setup completed", "SUCCESS")

        # Get session ID from client connection
        try:
            client.session_id = client.ws.session_id
            print(f"✅ Stored session ID: {client.session_id}")
        except AttributeError:
            print("⚠️ Could not retrieve session ID from ws")
    else:
        print("ℹ️ Proxy already configured")

# Check if new channel made in the server
@client.event
async def on_guild_channel_create(channel):
    global guildsId
    if channel.guild.id in guildsId:
        global stockAuthor, stockGuild, startTime, ticketTime
        try:
            # Check if we have a valid stockGuild and permissions
            if (stockGuild and
                channel.guild.id == stockGuild and
                channel.permissions_for(channel.guild.me).send_messages and
                autoBuy):

                stockAuthor = None
                stockGuild  = None

                elapsed_time = ticketTime - startTime

                sended = await send_message(
                    msgChannel,
                    ui.card("Auto ticket", icon="🎫", fields=[("Channel", channel.mention), ("Time", f"`{round(elapsed_time, 2)}s`")], footer=client.user.mention),
                    'stock'
                )

                if sended:
                    printdebug(f"Auto Ticket sent for channel {channel.id}", "SUCCESS")
                    if await ackMessage(sended):
                        printdebug(f"Acknowledged message for channel {channel.id}", "SUCCESS")
                    else:
                        printdebug(f"Failed to acknowledge message for channel {channel.id}", "ERROR")

        except (AttributeError, discord.Forbidden):
            # Handle cases where we can't access the channel or permissions
            print(f"Failed to process channel creation: {channel.id}\nError: {traceback.format_exc()}")
            return

# Check if stock channel is deleted
@client.event
async def on_guild_channel_delete(channel):
    global addtosnipe
    if int(channel.id) in stocksChannels:
        deletedCategory.add(channel.category)
        stocksChannels.remove(int(channel.id))
        try:
            snipeChannels.remove(int(channel.id))
            addtosnipe = True
        except KeyError:
            pass
        await asyncio.sleep(1)
        foundNC, newNC = await foundNukedChannel(channel.guild.id, channel.name)
        # if foundNC:
        #     await send_message(msgChannel, f'**<#{newNC}>** has been nuked.')
        # else:
        #     await send_message(msgChannel, f'**{channel.name}** has been deleted in `{channel.guild.name}`.')
        table_to_file("stocks.txt", stocksChannels)

@client.event
async def on_message(message):
    global stockAuthor, stockGuild, startTime, maxprice

    # Quick ignore self and system messages
    if message.author.id == client.user.id or (message.guild is not None and message.guild.id in SETTINGS.ignored_guild_ids):
        await client.process_commands(message)
        return

    # 7. STOCK SNIPER - OPTIMIZED FOR SPEED
    if stockSniper and message.channel.id in stocksChannels:
        content = message.content

        # Quick empty/question check
        if not content or '?' in content.lower():
            await client.process_commands(message)
            return

        content_lower = content.lower()

        # Early exit: check keywords first (faster than other checks)
        has_keyword = keywords and any(k in content_lower for k in keywords)
        has_price_symbol = '€' in content_lower and message.attachments

        if not (has_keyword or has_price_symbol):
            await client.process_commands(message)
            return

        # Check exclude keywords
        if any(exc in content_lower for exc in excludeKeywords):
            await client.process_commands(message)
            return

        # Extract and validate price
        price = getPriceInMessage(content_lower)
        if not price:
            await client.process_commands(message)
            return

        # FAST PATH: Only extract what we need
        quantity        = getQuantityInMessage(content_lower)
        url             = getUrlInMessage(content_lower)
        price_value     = float(price.replace('€', '').replace('$', '').replace(',', '.'))

        # Log once with all info
        printdebug(f"[STOCK] Price: {price_value}€ | Qty: {quantity} | Channel: {message.channel.id}", "SUCCESS")

        # Ignore if quantity is invalid
        if quantity is None:
            await client.process_commands(message)
            return

        # Process in background (don't block)
        spawn(_process_stock_async(message, price, quantity, url, price_value))

    # Auto redeem discord gift codes
    if autoRedeem:
        matches = codeRegex.finditer(message.content)
        if matches and message.author.id != client.user.id and not isinstance(message.channel, discord.DMChannel):
            for match in matches:
                code = match.group(2)
                printdebug(f"AutoRedeem: scheduling redeem for code={code} channel={message.channel.id}", "INFO")
                spawn(redeem_code(code, message))

    # Extract numbers from bingo channel to store guessed numbers
    if bingo_channel and message.channel.id == bingo_channel.id and message.author.id != client.user.id:
        try:
            if message.content.strip().isdigit():
                guessed_number = int(message.content.strip())
                if guessed_number not in bingo_guessed_numbers:
                    if guessed_number > 0 and guessed_number <= bingo_max_number:
                        bingo_guessed_numbers.add(guessed_number)
                        printdebug(f"Added {guessed_number} to bingo_guessed_numbers from user {message.author.id}", "INFO")
        except (ValueError, AttributeError):
            pass

    # 4. CACHE ATTACHMENTS - Only in monitored guilds
    if message.attachments and message.guild and message.guild.id in guildsId and message.author.id != client.user.id:
        spawn(_cache_attachment_async(message))

    # Vouch channel reactions
    if message.channel.id == vouchChannel:
        printdebug(f"Adding vouch reactions to message {message.id} in vouchChannel", "INFO")
        spawn(message.add_reaction('💕'))
        spawn(message.add_reaction('✅'))

    # Sleep mode auto-reply
    if sleepStatus and message.author.id not in sleepUsers and isinstance(message.channel, discord.DMChannel):
        printdebug(f"Sleep auto-reply to {message.author.id}", "INFO")
        spawn(send_message(message.channel.id, f"{message.author.mention} {sleepMessage}\n-# 💤 Automatic reply"))
        sleepUsers.add(message.author.id)

    # Process commands
    await client.process_commands(message)

# Extract these as separate functions to reduce main on_message complexity
async def _cache_attachment_async(message):
    """Cache attachments without blocking"""
    try:
        for attachment in message.attachments:
            file_data = await attachment.read()
            attachment_cache[message.id] = {
                'data': file_data,
                'filename': attachment.filename,
                'author': message.author,
                'channel': message.channel
            }
            printdebug(f"Attachment cached: {attachment.filename}", "INFO")
    except Exception as e:
        printdebug(f"Error caching attachment: {e}", "ERROR")

def channel_list(title, ids, icon, empty):
    """Liste de salons façon embed (mentions)."""
    return ui.list_card(title, [f"<#{i}>" for i in ids], icon=icon, empty=empty)

def added_card(kind, ids):
    """Résultat d'un ajout multiple de salons."""
    valid = [i for i in ids if str(i).isdigit()]
    bad = [i for i in ids if not str(i).isdigit()]
    detail = ui.bullets([f"<#{i}>" for i in valid], limit=15) if valid else ""
    out = ui.ok(f"**{len(valid)}** channel(s) added to the **{kind}** list", detail)
    if bad:
        out += "\n" + ui.warn(f"Ignored {len(bad)} invalid ID(s)", ui.bullets([f"`{b}`" for b in bad], limit=5))
    return out

def stock_card(title, message, price, quantity, ticket=None, url=None):
    """Notification de stock façon embed."""
    fields = [("💳 Price", f"`{price}`"), ("📊 Quantity", f"`{quantity}`"),
              ("💬 Channel", message.channel.mention), ("🧑‍💻 Author", message.author.mention)]
    if ticket:
        fields.append(("🎫 Ticket", f"<#{ticket}>"))
    if url:
        fields.append(("🛒 Link", f"[autobuy]({url})"))
    content = message.content.replace("`", "")[:600]
    return ui.card(title, fields, icon="📦", desc=f"```{content}```", footer=client.user.mention)

async def _process_stock_async(message, price, quantity, url, price_value):
    """Process stock in background without blocking main handler"""
    global stockAuthor, stockGuild, startTime, ticketTime

    try:
        STATS.record("snipes", message.channel.id, time.time() - message.created_at.timestamp())
        if not url:
            # Find ticket using the guild_ticket_map
            ticketChannel = await foundticket(message.guild.id)

            if ticketChannel and client.get_channel(int(ticketChannel)):
                # TICKET FOUND - AUTO BUY LOGIC
                if autoBuy and message.channel.id in snipeChannels and price_value <= float(maxprice):
                    startTime = time.time()
                    STATS.record("autobuys", message.channel.id)

                    if snipeDelay:
                        await asyncio.sleep(snipeDelay)

                    if autoMode == "dm" or 'dm' in message.content.lower().split():
                        spawn(send_dm(message.author, f"{message.author.mention}"))
                    elif autoMode == "ticket":
                        stockGuild = message.guild.id
                        stockAuthor = message.author
                        ticketTime = time.time()
                        # FIRE AND FORGET - openticket is async
                        spawn(openticket(ticketChannel))

                # Send notification (fire and forget)
                spawn(send_message(
                    msgChannel,
                    stock_card("New stock · ticket", message, price, quantity, ticket=ticketChannel),
                    'stock'
                ))
            else:
                # NO TICKET FOUND
                if autoBuy and message.channel.id in snipeChannels and price_value <= float(maxprice):
                    startTime = time.time()
                    STATS.record("autobuys", message.channel.id)
                    if snipeDelay:
                        await asyncio.sleep(snipeDelay)
                    spawn(send_dm(message.author, f"{message.author.mention}"))

                spawn(send_message(
                    msgChannel,
                    stock_card("New stock", message, price, quantity)
                ))
        else:
            # URL FOUND - AUTOBUY CANDIDATE
            spawn(send_message(
                msgChannel,
                stock_card("New stock · autobuy", message, price, quantity, url=url)
            ))
    except Exception as e:
        printdebug(f"Error processing stock: {e}", "ERROR")
        traceback.print_exc()

@client.event
async def on_message_delete(message):
    # Check if we have cached attachment data
    if message.id in attachment_cache:
        try:
            cached = attachment_cache[message.id]

            # Create Discord file from cached data
            discord_file = discord.File(
                io.BytesIO(cached['data']),
                filename=cached['filename']
            )

            # Send the actual file
            target = client.get_channel(msgChannel)
            if target is None:
                log.warning("msgChannel %s introuvable: pièce jointe supprimée non relayée", msgChannel)
                return
            await target.send(
                ui.card("Deleted attachment", [("Author", cached['author'].mention), ("Channel", cached['channel'].mention), ("File", f"`{cached['filename']}`")], icon="🗑️"),
                file=discord_file
            )

            # Clean up cache
            del attachment_cache[message.id]

        except Exception as e:
            await send_message(msgChannel, ui.err(f"Could not relay the deleted attachment: {e}"))

@client.event
async def on_voice_state_update(member, before, after):
    """
    This event is called when a member changed their voice state.

    This can be:

     * A member joins a voice channel.
     * A member leaves a voice channel.
     * A member becomes muted or deafened a voice channel.

    Args:
        before:
            The original Member object before getting changed.

        after:
            The new Member object after getting changed.

    """
    global noMove, nomoveChannel
    if noMove:
        if member.id == client.user.id:
            if member.voice:
                if after.channel != nomoveChannel:
                    await member.move_to(nomoveChannel)
            else:
                nomoveChannel = 0
                noMove = False

@client.event
async def on_error(event, *args, **kwargs):
    """Handle errors in event handlers"""
    error = traceback.format_exc()
    log.error('An error occurred in %s:\n%s', event, error)

    # Specifically handle the NoneType iteration error
    if "TypeError: 'NoneType' object is not iterable" in error:
        print("Handling NoneType error in pending_payments...")
        # Continue execution
        pass

@client.event
async def on_command_error(ctx, error):
    """Gestion centralisée des erreurs de commandes (plus de silence ni de traceback brut)."""
    if isinstance(error, commands.CommandNotFound):
        return
    original = getattr(error, "original", error)
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        msg = ui.err("Invalid arguments", f"{error}")
    elif isinstance(error, commands.CheckFailure):
        msg = ui.err("You are not allowed to run this command")
    else:
        msg = ui.err(f"Command failed: `{type(original).__name__}`", "Details are in the logs")
        log.error("Command %s failed: %r", getattr(ctx.command, "name", "?"), original, exc_info=original)
    try:
        await ctx.message.edit(content=msg[:1900])
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    except Exception:
        pass

# Litecoin price monitoring
def start_price_manager():
    """Démarre le flux de prix LTC (thread websocket), une seule fois."""
    global price_manager
    if price_manager is not None:
        return
    try:
        price_manager = LitecoinPriceManager(on_price_update=on_price_update)
        price_manager.start()
    except Exception as e:
        log.warning("Price manager indisponible: %s", e)
        price_manager = None

def on_price_update(price: float):
    """Callback function for price updates"""
    global callback_price
    callback_price = round(price, 2)
    return round(price, 2)

@client.command()
async def proxytest(ctx):
    """Test if bot is using proxy and get IP address."""
    try:
        await ctx.message.edit(ui.wait("Testing proxy connection…"))

        test_url = "https://api.ipify.org?format=json"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                test_url,
                proxy=PROXY_URL or None,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    ip = data.get('ip', 'Unknown')
                    await ctx.message.edit(ui.card("Proxy test", [("IP", f"`{ip}`"), ("Proxy", f"`{mask_proxy(PROXY_URL)}`"), ("Status", "🟢 Connected")], icon="✅"))
                    printdebug(f"Proxy test successful - IP: {ip}", "SUCCESS")
                else:
                    await ctx.message.edit(ui.err(f"Proxy test failed: {response.status}"))

    except asyncio.TimeoutError:
        await ctx.message.edit(ui.err("Proxy connection timeout (10s)"))
        printdebug("Proxy test timed out", "ERROR")
    except Exception as e:
        await ctx.message.edit(ui.err(f"Proxy test error: {str(e)}"))
        printdebug(f"Proxy test error: {str(e)}", "ERROR")

    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def proxyinfo(ctx):
    """Display current proxy configuration."""

    info = ui.card("Proxy", icon="🔌", fields=[
        ("Proxy", "🟢 Configured" if PROXY_URL else "🔴 None"),
        ("URL", f"`{mask_proxy(PROXY_URL)}`"),
        ("SSL verification", "🟢 Enabled"),
    ], desc=f"`{PREFIX}proxytest` test the connection and show your IP", footer="Credentials are always masked")

    await ctx.message.edit(info)
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

@client.command()
async def debugmode(ctx):
    """Toggle debug mode."""
    global DEBUG
    DEBUG = not DEBUG
    status = "enabled" if DEBUG else "disabled"
    await ctx.message.edit(ui.ok(f"Debug mode **{status}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Aide: chaque rubrique est un « embed » Markdown. {p} = préfixe de commandes.
HELP_TOPICS = {
    "sniper": ("🎯", "Sniper & auto-buy", [
        ("How it works", "Messages in stock channels are filtered by **keywords** / **exclude keywords**, then price, quantity and URL are parsed. A notification is posted in the message channel."),
        ("Auto-buy", "When `{p}auto` is on, a snipe channel whose price is ≤ max price triggers the **auto mode** (`dm` or `ticket`), after an optional delay."),
        ("Commands", "`{p}auto` toggle auto-buy · `{p}setauto <dm|ticket>` · `{p}setmp <price>` max price · `{p}setdelay <seconds>` · `{p}setmsg [channel]`"),
        ("Snipe channels", "`{p}addsnipe <id>` · `{p}addsnipes <id-id-…>` · `{p}rmsnipe <id>` · `{p}rasnipe` · `{p}snipes` · `{p}sclean` · `{p}addcsn <category>`"),
    ]),
    "stock": ("📦", "Stock channels", [
        ("Channels", "`{p}addstock <id>` · `{p}addstocks <id-id-…>` · `{p}addst <stock> <ticket>` · `{p}rmstock <id>` · `{p}rmall` · `{p}addcst <category>`"),
        ("Lists", "`{p}stocks` · `{p}st` stock + tickets · `{p}deleted` · `{p}getcat <category>`"),
        ("Maintenance", "`{p}clean` · `{p}sclean` · `{p}tclean` remove unknown channels · `{p}update` / `{p}sync` save / reload"),
        ("Keywords", "`{p}keyword|exclude|label <word>` add · `{p}rmkw|rmex|rmlb <word>` remove · `{p}getkw|getex|getlb` list"),
    ]),
    "tickets": ("🎫", "Tickets", [
        ("Channels", "`{p}addticket <id>` · `{p}addtickets <id-id-…>` · `{p}rmticket <id>` · `{p}tickets` · `{p}tkt <category>` · `{p}gettkt <channel>`"),
        ("Button cache", "`{p}refreshcache` · `{p}manualcache <channel> <message>` · `{p}allcachestatus`"),
    ]),
    "crypto": ("💰", "Crypto", [
        ("Balances", "`{p}bal <address>` · `{p}mybal` · `{p}usdtbal [address]`"),
        ("Converters", "`{p}ltc <n>` · `{p}eur <n>` · `{p}usd <n>` · `{p}cv <n> <from> <to>` · `{p}cvt <n>` · `{p}crypto <symbol>` · `{p}croix <price> <amount>`"),
        ("Addresses", "`{p}addy` / `{p}addyu` show (with QR) · `{p}setaddy` / `{p}setaddyu <address>`"),
        ("Send funds", "`{p}setprivateltc|setprivateusdt <key> <password>` then `{p}send <address> <amount|all>` / `{p}sendu <address> <amount|all>`. Confirm with your password in the transaction channel. `{p}resetprivate` wipes the keys."),
        ("Transactions", "`{p}tltc <hash>` · `{p}lasttx [address]` · `{p}alltx [target] [type]` · `{p}waittx` / `{p}stopwaittx`"),
    ]),
    "nitro": ("🎁", "Nitro", [
        ("Stock", "`{p}stock [month|year|deco]` · `{p}checknb <type>` · `{p}checknbs <type>` · `{p}nbs <type>`"),
        ("Save / send", "`{p}savenb <supplier> <codes> <type>` · `{p}saveacc <supplier> <amount> <text>` · `{p}nb <number> <type>`"),
        ("Vouches", "`{p}vnb <number> <price> <type>` · `{p}vsb <number> <price>` · `{p}vrb <number> <price>` · `{p}vouch <text>`"),
        ("Redeemer", "`{p}redeemer` toggles auto-redeem of `discord.gift` links. Codes live in `gifts/<type>.txt`."),
    ]),
    "invites": ("🔗", "Invites", [
        ("Tracking", "`{p}inv <@user>` invite used by a member · `{p}invs <@inviter>` members invited · `{p}invites` all invites of the server"),
        ("Create / join", "`{p}createinvites` for snipe servers (saved to `invites.txt`) · `{p}joinfromfile`"),
    ]),
    "settings": ("⚙️", "Settings", [
        ("Channels", "`{p}setmsg [channel]` · `{p}settc [channel]` · `{p}setvouch [channel]`"),
        ("Status", "`{p}status` · `{p}activity <text>` · `{p}sleep [message]` · `{p}nomove` · `{p}debugmode`"),
        ("Network", "`{p}proxyinfo` · `{p}proxytest`"),
        ("Keys (memory only)", "`{p}settatum <key>` · `{p}settoken [token]`. Put permanent values in `.env`."),
    ]),
    "shop": ("🛍️", "Discord shop", [
        ("Browse", "`{p}shop [type] [name] [options]` · types: deco, effect, nameplate, frame, bundle, all"),
        ("Filters", "`min=` `max=` price · `nitro=yes` Nitro discount · `orbs=yes` buyable with orbs · `exclusive=yes`"),
        ("Sort / pages", "`sort=popular|new|old|price|nitro|orbs|name` · `order=desc` · `limit=` · `page=` · `country=FR` · `scan=`"),
        ("Examples", "`{p}shop deco max=7 sort=price` · `{p}shop venom` · `{p}shop bundle nitro=yes`"),
    ]),
    "tools": ("🧪", "Info & tools", [
        ("Info", "`{p}ping` · `{p}uptime` · `{p}userinfo [user]` · `{p}avatar [user]` · `{p}banner [user]` · `{p}serverinfo` · `{p}membercount` · `{p}channelinfo` · `{p}emojis` · `{p}snowflake <id>`"),
        ("Messages", "`{p}snipe [n]` last deleted · `{p}editsnipe [n]` last edited · `{p}firstmsg` first message of the channel"),
        ("Productivity", "`{p}afk [reason|off]` auto-reply · `{p}remind <1h30m> <text>` · `{p}reminders` · `{p}unremind <n>` · `{p}note add|list|del`"),
        ("Tools", "`{p}roll 2d6+1` · `{p}flip` · `{p}choose a | b` · `{p}8ball` · `{p}uuid` · `{p}color <hex>` · `{p}hash <algo> <text>` · `{p}b64 encode|decode <text>` · `{p}qr <text>`"),
        ("Web", "`{p}time <Europe/Paris>` · `{p}weather <city>` (wttr.in) · `{p}define <word>` (dictionaryapi.dev)"),
        ("Profile", "`{p}setstatus online|idle|dnd|invisible`"),
        ("Moderation", "`{p}kick <user>` · `{p}ban <id>` · `{p}unban <id>` · `{p}timeout <user> <1h|0>` · `{p}slowmode <s>` (needs server permissions)"),
    ]),
    "automation": ("⚡", "Automation", [
        ("Sniper control", "`{p}pause [1h|off|status]` pauses sniper + auto-buy + redeemer · `{p}stats [reset]` · `{p}export` / `{p}import [file]` config backup (no secrets)"),
        ("Shop", "`{p}shop watch <filters>` alerts on new items / price drops · `{p}shop watches` · `{p}shop unwatch <n>` · `{p}shop compare a | b`"),
        ("Gift", "`{p}gift info <code>` checks a Nitro gift without redeeming it"),
        ("Crypto", "`{p}price <sym>` · `{p}price alert <sym> above|below <€>` · `{p}price alerts` · `{p}price del <n>` · `{p}fees` · `{p}watch <ltc address>` · `{p}portfolio`"),
        ("Shortcuts", "`{p}alias add|list|del` · `{p}snippet add|list|del|<name>` · `{p}react <emoji>` · `{p}autoreact add|list|del|on|off`"),
        ("Misc", "`{p}translate <lang> <text>` · `{p}tz set|list|del|<user>` · `{p}poll q | a | b` · `{p}copyid` · `{p}jump <id>` · `{p}pins` · `{p}purgeuser <user> [n]`"),
    ]),
    "swap": ("💱", "Swap LTC ⇄ USDT", [
        ("Swap", "`{p}swapltc <amount|all> [0x address]` LTC → USDT (Polygon) · `{p}swapusdt <amount|all> [ltc address]` USDT → LTC · confirm with your password in the transaction channel"),
        ("Compare fees", "`{p}swaprates [amount] [ltc|usdt]` compares providers and their cost vs the market price (no API key needed)"),
        ("Follow up", "`{p}swapstatus <order id>` (Swapzone) · `{p}swapstatus <id> <token>` (FixedFloat)"),
        ("Automatic", "`{p}autosell <usd> <amount|all>` sells LTC once the price passes the threshold then dips · `{p}autobuy <usd> <amount|all>` buys back · `{p}stopautosell` · `{p}stopautobuy` · `{p}swapauto`"),
        ("Safety", "Refuses a swap costing more than SWAP_MAX_FEE_PCT vs market (override: `force=yes`), validates both addresses, one swap at a time."),
    ]),
    "utility": ("🧰", "Utilities", [
        ("Tools", "`{p}math <expr>` · `{p}dm <user> <text>` · `{p}spam <n> <text>` · `{p}stamp [date]` · `{p}rbxgp <url[_url…]>` · `{p}bingo`"),
        ("Moderation", "`{p}nuke [n]` delete your last messages · `{p}guilds` monitored servers"),
        ("Debug", "`{p}checkmsg [id]` · `{p}getappid` · `{p}debugappid` · `{p}debugmsg <id>`"),
    ]),
}
HELP_ALIASES = {"stocks": "stock", "invite": "invites", "ticket": "tickets", "commands": "all", "account": "invites"}


def build_help(topic=None):
    p = PREFIX
    if topic is None:
        rows = [f"> {icon} `{p}howto {key}` · {title}" for key, (icon, title, _) in HELP_TOPICS.items()]
        return ui.card("Help", icon="🤖", desc="Pick a category:\n" + "\n".join(rows),
                       footer=f"{p}howto all lists everything · prefix: {p}")
    key = HELP_ALIASES.get(topic.lower(), topic.lower())
    if key == "all":
        return "\n\n".join(build_help(k) for k in HELP_TOPICS)
    if key not in HELP_TOPICS:
        return ui.err(f"Unknown topic `{topic}`", f"Use `{p}howto` to see the categories")
    icon, title, sections = HELP_TOPICS[key]
    fields = [f"> **{label}**\n> {text.format(p=p)}" for label, text in sections]
    return ui.card(title, icon=icon, desc="\n".join(fields), footer=f"{p}howto · all categories")


@client.command()
async def howto(ctx, *, topic: str = None):
    """Display help information about bot commands and features."""
    pages = ui.chunk(build_help(topic))
    await ctx.message.edit(pages[0])
    for page in pages[1:]:
        await ctx.send(page)

@client.command()
async def shop(ctx, *, args: str = ""):
    """Browse the Discord shop (decorations, effects, nameplates, frames, bundles)."""
    if args.strip().lower() in ("help", "?"):
        await ctx.message.edit(ui.card("Discord shop", icon="🛍️", desc=shop_api.usage(PREFIX), level=2))
        return
    extra = client.get_cog("Automation")
    if extra is not None and await extra.shop_extra(ctx, args):
        return
    try:
        q = shop_api.parse_query(args)
    except shop_api.ShopError as e:
        await ctx.message.edit(ui.err(str(e), f"`{PREFIX}shop help` for usage"))
        return

    await ctx.message.edit(ui.wait("Loading the Discord shop…"))
    try:
        async with aiohttp.ClientSession() as session:
            items = await shop_api.fetch_items(session, TOKEN, q)
    except (shop_api.ShopError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        await ctx.message.edit(ui.err("Could not load the shop", str(e) or type(e).__name__))
        return

    found = shop_api.apply_filters(items, q)
    if not found:
        await ctx.message.edit(ui.warn("No shop item matches", f"{len(items)} items scanned · try `scan=` or looser filters"))
        return
    page_items, pages = shop_api.paginate(found, q.page, q.limit)
    page = min(q.page, pages)
    label = shop_api.TYPES[q.kind][1] + "s" if q.kind in shop_api.TYPES else "Items"
    title = f"{label}" + (f" · “{q.name}”" if q.name else "")
    body = ui.card(title, icon="🛍️", desc="\n".join(shop_api.format_item(i) for i in page_items),
                   footer=f"{len(found)} result(s) of {len(items)} scanned · page {page}/{pages} · "
                          f"{q.country} · sort {q.sort}")
    pages_out = ui.chunk(body)
    await ctx.message.edit(pages_out[0])
    for extra in pages_out[1:]:
        await ctx.send(extra)


async def getcategory(category):
    """
    Get all channel IDs in a specified category.

    Args:
        category: Category ID (as string) or category mention (e.g., "<#123456789>")

    Returns:
        List of channel IDs in the category, or empty list if not found
    """
    try:
        # Extract category ID from mention if present (e.g., "<#123456789>" -> "123456789")
        if category.startswith('<#') and category.endswith('>'):
            category_id = int(category[2:-1])
        else:
            category_id = int(category)

        # Search across all guilds the bot is in
        for guild in client.guilds:
            category = guild.get_channel(category_id)
            if category and isinstance(category, discord.CategoryChannel):
                # Return list of channel IDs in this category
                return [channel.id for channel in category.channels]

        # Category not found
        return []
    except (ValueError, AttributeError):
        # Invalid ID or other error
        return []

async def send_dm(user, message):
    global startTime

    try:
        await user.send(message)
        await send_message(msgChannel, ui.card("Auto DM", [("Person", user.mention), ("Time", f"`{round(time.time() - startTime, 2)}s`")], icon="💬", footer=client.user.mention), 'stock')
    except discord.Forbidden:
        print(f"Error: Forbidden to send message to {user.name}.")
    except discord.HTTPException as e:
        print(f"An error occurred: {str(e)}")

async def send_message(channel, message, type = 'normal'):
    message = message.replace('@everyone', 'everyone').replace('@here', 'here')
    target = client.get_channel(channel)
    if target is None:
        log.warning("send_message: canal %s introuvable", channel)
        return False
    try:
        sent_msg = await target.send(message)
        return sent_msg
    except discord.Forbidden:
        print(f"Error: Forbidden to send message to {getattr(target, 'name', channel)}.")
        return False
    except discord.HTTPException as e:
        print(f"An error occurred: {str(e)}")
        return False

async def cache_all_buttons():
    """Cache all interactive components (buttons, select menus, etc.) from ticket channels"""
    global button_cache

    printdebug("Caching button components from ticket channels...", "INFO")

    label_filter = {lbl.lower() for lbl in labelsCheck}
    if not label_filter:
        printdebug("labelsCheck is empty — will still cache single-component messages.", "WARNING")

    for ticket_channel_id in ticketChannels:
        try:
            channel = client.get_channel(int(ticket_channel_id))
            if not channel:
                printdebug(f"Channel {ticket_channel_id} not found while caching buttons, skipping.", "WARNING")
                continue

            # Search recent messages for components
            async for message in channel.history(limit=5):
                if not message.components:
                    continue

                # Extract application_id from message interaction
                app_id = None
                if message.interaction:
                    app_id = message.interaction.application_id

                if not app_id:
                    if message.author.bot:
                        app_id = message.author.id

                printdebug(f"Found application_id {app_id} for channel {ticket_channel_id}", "INFO")

                # Count total components (buttons only)
                total_buttons = 0
                for row in message.components:
                    for child in getattr(row, "children", []):
                        if isinstance(child, discord.Button):
                            total_buttons += 1

                # If there's exactly one button, cache it if it has a label
                if total_buttons == 1:
                    for row in message.components:
                        for child in getattr(row, "children", []):
                            if isinstance(child, discord.Button):
                                label = getattr(child, "label", None)
                                custom_id = getattr(child, "custom_id", None)

                                # Cache if: label exists AND (no filter OR label matches filter)
                                if label and (not label_filter or any(filter_lbl in label.lower() for filter_lbl in label_filter)):
                                    if custom_id:
                                        custom_id = str(custom_id)

                                    component_entry = {
                                        "label": label,
                                        "custom_id": custom_id,
                                        "type": "Button"
                                    }
                                    button_cache[int(ticket_channel_id)] = {
                                        "message_id": message.id,
                                        "application_id": app_id,
                                        "buttons": [component_entry]
                                    }
                                    printdebug(f"Cached single button for channel {ticket_channel_id} (label={'None' if not label else label}, custom_id={custom_id}, app_id={app_id})", "SUCCESS")
                                    break
                        else:
                            continue
                        break
                    # Continue to next message instead of breaking here
                    continue

                # Otherwise, cache buttons with matching labels
                components = []
                for row in message.components:
                    for child in getattr(row, "children", []):
                        if isinstance(child, discord.Button):
                            label = getattr(child, "label", None)
                            custom_id = getattr(child, "custom_id", None)

                            if custom_id:
                                custom_id = str(custom_id)

                            if label and (not label_filter or any(filter_lbl in label.lower() for filter_lbl in label_filter)):
                                components.append({
                                    "label": label,
                                    "custom_id": custom_id,
                                    "type": "Button"
                                })
                        elif isinstance(child, discord.SelectMenu):
                            custom_id = getattr(child, "custom_id", None)
                            if custom_id:
                                custom_id = str(custom_id)

                            for option in getattr(child, "options", []):
                                opt_label = getattr(option, "label", None)
                                if opt_label and (not label_filter or any(filter_lbl in opt_label.lower() for filter_lbl in label_filter)):
                                    components.append({
                                        "label": opt_label,
                                        "custom_id": custom_id,
                                        "type": "SelectMenu"
                                    })
                                    break

                if components:
                    button_cache[int(ticket_channel_id)] = {
                        "message_id": message.id,
                        "application_id": app_id,
                        "buttons": components
                    }
                    printdebug(f"Cached {len(components)} matching component(s) for channel {ticket_channel_id} with app_id={app_id}", "SUCCESS")
                    break  # Only break when we successfully cache something

        except Exception as e:
            printdebug(f"Error caching buttons for channel {ticket_channel_id}: {e}", "ERROR")

    printdebug(f"Button caching complete! ({len(button_cache)} channels cached)", "INFO")

async def interact_ticket(channel_id: int, message_id: int, component_type: int, custom_id: str, values: list = None):
    """Interact with a Discord component (button or select menu) - OPTIMIZED"""
    global client, button_cache

    url = "https://discord.com/api/v9/interactions"

    # Get application_id from cache first
    cached_entry = button_cache.get(int(channel_id))
    if cached_entry:
        application_id = cached_entry.get("application_id")
    else:
        application_id = None

    # If not in cache, try to fetch from message
    if not application_id:
        try:
            channel = client.get_channel(channel_id)
            if channel:
                message = await channel.fetch_message(message_id)
                if message.interaction:
                    application_id = message.interaction.application_id
                elif message.author.bot:
                    application_id = message.author.id
        except Exception as e:
            printdebug(f"Error fetching application_id: {e}", "WARNING")

    # Pas de fallback codé en dur: sans application_id on n'envoie pas l'interaction
    if not application_id:
        printdebug("application_id introuvable, interaction annulée", "ERROR")
        return None

    # Pre-compute headers
    headers = {
        "authorization": TOKEN,
        "content-type": "application/json; charset=utf-8",
        "user-agent": get_user_agent(),
    }

    # Get channel info
    channel = client.get_channel(channel_id)
    guild_id = channel.guild.id if channel and channel.guild else None

    # Sanitize custom_id - encode and decode to ensure proper UTF-8
    try:
        if isinstance(custom_id, bytes):
            custom_id = custom_id.decode('utf-8', errors='replace')
        else:
            custom_id = str(custom_id)

        # Encode to UTF-8 and back to ensure it's valid
        custom_id = custom_id.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    except Exception as e:
        printdebug(f"Error sanitizing custom_id: {e}", "ERROR")
        return None

    # Build data payload
    data = {
        "type": 3,
        "nonce": str(int(time.time() * 1000)),
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "application_id": application_id,
        "session_id": getattr(client, 'session_id', ''),
        "data": {
            "component_type": component_type,
            "custom_id": custom_id,
            "type": component_type,
        }
    }

    if component_type == 3 and values:
        data["data"]["values"] = values

    printdebug(f"Interacting: app_id={application_id}, channel={channel_id}, msg={message_id}, custom_id_len={len(custom_id)}", "INFO")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                printdebug(f"Response: {response.status}", "INFO")
                if response.status == 204:
                    printdebug(f"✅ Component interaction successful!", "SUCCESS")
                    return response.status
                elif response.status >= 400:
                    try:
                        error_text = await response.text()
                        printdebug(f"API Error {response.status}: {error_text[:200]}", "ERROR")
                    except:
                        pass
                return response.status

    except asyncio.TimeoutError:
        printdebug(f"Timeout on interaction", "WARNING")
        return None
    except UnicodeEncodeError as e:
        printdebug(f"Unicode encode error: {str(e)}", "ERROR")
        return None
    except Exception as e:
        printdebug(f"Exception: {str(e)}", "ERROR")
        return None

async def openticket(ticketChannel):
    global ticketTime, button_cache

    try:
        channel = client.get_channel(int(ticketChannel))
        if not channel:
            printdebug(f"Channel {ticketChannel} not found", "ERROR")
            return False

        label_set = {lbl.lower() for lbl in labelsCheck}
        if not label_set:
            printdebug("No labels configured - cannot match components", "WARNING")
            return False

        # Initialize this variable
        last_message_with_components = None

        # Check what's in cache
        cached = button_cache.get(int(ticketChannel))
        if cached:
            printdebug(f"Cache hit for {ticketChannel}: {len(cached.get('buttons', []))} buttons, app_id={cached.get('application_id')}", "INFO")
        else:
            printdebug(f"No cache entry for {ticketChannel}, searching history...", "INFO")

        # If we have a cached entry, try using cached info first
        if cached:
            printdebug(f"Attempting to use cached component for channel {ticketChannel}", "INFO")
            try:
                # Attempt to fetch the cached message to verify it still exists
                last_message_with_components = await channel.fetch_message(cached["message_id"])
                if not last_message_with_components.components:
                    last_message_with_components = None
                else:
                    # Try to find and interact with cached component
                    for row in last_message_with_components.components:
                        for child in getattr(row, "children", []):
                            c_label = getattr(child, "label", "") or ""
                            c_custom = getattr(child, "custom_id", None)

                            for cb in cached.get("buttons", []):
                                # Match by custom_id
                                if cb.get("custom_id") and cb.get("custom_id") == c_custom:
                                    if cb.get("label") is None or any(lbl in (cb.get("label") or "").lower() for lbl in label_set):
                                        try:
                                            ticketTime = time.time()
                                            if cb.get("type") == "Button" and isinstance(child, discord.Button):
                                                # Use interact_ticket for buttons
                                                response = await interact_ticket(
                                                    channel_id=channel.id,
                                                    message_id=last_message_with_components.id,
                                                    component_type=2,  # Button type
                                                    custom_id=c_custom
                                                )
                                                if response == 204:
                                                    printdebug(f"Clicked cached button in channel {ticketChannel}", "SUCCESS")
                                                    return True
                                            elif cb.get("type") == "SelectMenu" and isinstance(child, discord.SelectMenu):
                                                # Find matching option and use interact_ticket
                                                for option in child.options:
                                                    if getattr(option, "label", None) == cb.get("label"):
                                                        response = await interact_ticket(
                                                            channel_id=channel.id,
                                                            message_id=last_message_with_components.id,
                                                            component_type=3,  # Select menu type
                                                            custom_id=c_custom,
                                                            values=[option.value]
                                                        )
                                                        if response == 204:
                                                            printdebug(f"Selected cached menu option in channel {ticketChannel}", "SUCCESS")
                                                            return True
                                        except Exception as e:
                                            printdebug(f"Cached component interaction failed: {e}", "ERROR")
                                            pass

                                # Match by label
                                elif cb.get("label") and cb.get("label").lower() in c_label.lower():
                                    if any(lbl in cb.get("label").lower() for lbl in label_set):
                                        try:
                                            ticketTime = time.time()
                                            if cb.get("type") == "Button" and isinstance(child, discord.Button):
                                                response = await interact_ticket(
                                                    channel_id=channel.id,
                                                    message_id=last_message_with_components.id,
                                                    component_type=2,
                                                    custom_id=c_custom
                                                )
                                                if response == 204:
                                                    printdebug(f"Clicked cached button (label match) in channel {ticketChannel}", "SUCCESS")
                                                    return True
                                            elif cb.get("type") == "SelectMenu" and isinstance(child, discord.SelectMenu):
                                                for option in child.options:
                                                    if getattr(option, "label", None) == cb.get("label"):
                                                        response = await interact_ticket(
                                                            channel_id=channel.id,
                                                            message_id=last_message_with_components.id,
                                                            component_type=3,
                                                            custom_id=c_custom,
                                                            values=[option.value]
                                                        )
                                                        if response == 204:
                                                            printdebug(f"Selected cached menu option (label match) in channel {ticketChannel}", "SUCCESS")
                                                            return True
                                        except Exception as e:
                                            printdebug(f"Cached component interaction failed: {e}", "ERROR")
                                            pass
            except discord.NotFound:
                button_cache.pop(int(ticketChannel), None)
                last_message_with_components = None
                printdebug(f"Cached message for channel {ticketChannel} not found; removed from cache.", "WARNING")
            except Exception as e:
                printdebug(f"Error fetching cached message for channel {ticketChannel}: {e}", "ERROR")
                last_message_with_components = None

        # If cached attempt didn't succeed, search recent history
        if not last_message_with_components:
            async for message in channel.history(limit=5):
                if message.components:
                    last_message_with_components = message
                    break

        if not last_message_with_components:
            printdebug(f"No message with components found in channel {ticketChannel}", "INFO")
            return False

        # Full scan: interact with the first matching component
        for row in last_message_with_components.components:
            for child in getattr(row, "children", []):
                label = getattr(child, "label", None)
                custom_id = getattr(child, "custom_id", None)

                # Try to interact with buttons
                if isinstance(child, discord.Button):
                    if label and any(check_label in label.lower() for check_label in label_set):
                        try:
                            ticketTime = time.time()
                            response = await interact_ticket(
                                channel_id=channel.id,
                                message_id=last_message_with_components.id,
                                component_type=2,
                                custom_id=custom_id
                            )
                            if response == 204:
                                printdebug(f"Clicked button in channel {ticketChannel}", "SUCCESS")
                                return True
                        except Exception as e:
                            printdebug(f"Failed to click button in channel {ticketChannel}: {e}", "ERROR")
                            pass

                # Try to interact with select menus
                elif isinstance(child, discord.SelectMenu):
                    for option in getattr(child, "options", []):
                        opt_label = getattr(option, "label", None)
                        if opt_label and any(check_label in opt_label.lower() for check_label in label_set):
                            try:
                                ticketTime = time.time()
                                response = await interact_ticket(
                                    channel_id=channel.id,
                                    message_id=last_message_with_components.id,
                                    component_type=3,
                                    custom_id=custom_id,
                                    values=[option.value]
                                )
                                if response == 204:
                                    printdebug(f"Selected menu option in channel {ticketChannel}", "SUCCESS")
                                    return True
                            except Exception as e:
                                printdebug(f"Failed to select menu option in channel {ticketChannel}: {e}", "ERROR")
                                pass

        printdebug(f"Unable to interact with any component in channel {ticketChannel}", "WARNING")
        return False

    except discord.Forbidden:
        printdebug(f"Missing permissions for channel {ticketChannel}", "ERROR")
        return False
    except Exception as e:
        printdebug(f"Error opening ticket for channel {ticketChannel}: {e}", "ERROR")
        return False

async def getticket(ticketChannel, limit: int = 10):
    """
    Collect all component names (button labels and select menu option labels)
    from recent messages in a channel.

    Args:
        ticketChannel: Channel ID or channel-like object
        limit: number of recent messages to scan

    Returns:
        dict with message ids as keys and a dict of components as value:
        {
            message_id: {
                "buttons": [{"label": "...", "custom_id": "...", "style": ...}, ...],
                "selects": [{"custom_id": "...", "options": [{"label": "...", "value": "..."}, ...]}, ...]
            }, ...
        }
    """
    try:
        channel = client.get_channel(int(ticketChannel))
        if not channel:
            print(f"Channel {ticketChannel} not found")
            return {}

        components_info = {}

        def _sanitize(value, default=None, max_len: int = 120):
            """Return a safe, printable and reasonably sized string for display."""
            if value is None:
                return default
            try:
                s = str(value)
            except Exception:
                return repr(value)
            # remove backticks to avoid breaking markdown formatting
            s = s.replace("`", "")
            # replace non-printable characters
            s = "".join(ch if ch.isprintable() else "?" for ch in s)
            if len(s) > max_len:
                return s[:max_len] + "..."
            return s

        async for message in channel.history(limit=limit):
            if not message.components:
                continue

            msg_comp = {"buttons": [], "selects": []}

            try:
                for row in message.components:
                    for comp in row.children:
                        # Buttons
                        if isinstance(comp, discord.Button):
                            label = getattr(comp, "label", None)
                            custom_id = getattr(comp, "custom_id", None)
                            style = getattr(comp, "style", None)
                            # prefer readable enum name for style if available
                            style_str = getattr(style, "name", None) or _sanitize(style, "Unknown")
                            msg_comp["buttons"].append({
                                "label": _sanitize(label, "No Label"),
                                "custom_id": _sanitize(custom_id, "No ID"),
                                "style": style_str
                            })

                        # Select menus
                        elif isinstance(comp, discord.SelectMenu):
                            custom_id = getattr(comp, "custom_id", None)
                            options = []
                            for opt in getattr(comp, "options", []):
                                options.append({
                                    "label": _sanitize(getattr(opt, "label", None), "No Label"),
                                    "value": _sanitize(getattr(opt, "value", None), "No Value"),
                                    "description": _sanitize(getattr(opt, "description", None), "")
                                })
                            msg_comp["selects"].append({
                                "custom_id": _sanitize(custom_id, "No ID"),
                                "options": options
                            })

                        else:
                            # Generic fallback for unknown component types
                            comp_type = type(comp).__name__
                            msg_comp.setdefault("others", []).append({
                                "type": comp_type,
                                "repr": _sanitize(repr(comp), "")
                            })

                if msg_comp["buttons"] or msg_comp["selects"] or msg_comp.get("others"):
                    components_info[str(message.id)] = msg_comp

            except Exception as e:
                print(f"Error parsing components in message {message.id}: {e}")
                continue

        # Optionally print a summary for debugging
        if components_info:
            print(f"Found components in {len(components_info)} message(s) in channel {channel.id}")
        else:
            print(f"No components found in the last {limit} messages of channel {channel.id}")

        return components_info

    except discord.Forbidden:
        print(f"Missing permissions for channel {ticketChannel}")
        return {}
    except Exception as e:
        print(f"Error in getticket: {e}")
        return {}


@client.command()
async def bingo(ctx, max: int = 100, channel: int = None, delay: float = 1.0):
    """Send a number to the channel until stopped. Optional delay (seconds) to avoid rate limits."""
    global guessing, bingo_guessed_numbers, bingo_channel, bingo_max_number

    # Determine target channel
    target_channel = None
    if channel:
        target_channel = client.get_channel(channel)
        if not target_channel:
            printdebug(f"Channel {channel} not found", "ERROR")
            target_channel = ctx.channel
    else:
        target_channel = ctx.channel

    try:
        await ctx.message.delete()
    except Exception as e:
        printdebug(f"Error deleting command message: {str(e)}", "WARNING")

    # Toggle / stop instantly
    if guessing:
        guessing = False
        bingo_channel = None
        bingo_max_number = 1
        try:
            await target_channel.send(ui.info("Bingo stopped"))
        except Exception as e:
            printdebug(f"Error sending stop message: {str(e)}", "ERROR")
        return
    else:
        guessing = True
        bingo_channel = target_channel
        bingo_max_number = max

    # Track numbers tried in this session
    numbers = set()

    async def interruptible_sleep(total_seconds: float, step: float = 0.1):
        """Sleep in small steps so loop can stop almost instantly when guessing becomes False."""
        slept = 0.0
        while slept < total_seconds:
            if not guessing:  # Check immediately
                return False
            await asyncio.sleep(min(step, total_seconds - slept))
            slept += step
        return True

    try:
        current = 1
        # iterate sequentially from 1 to max, skipping already tried numbers
        while guessing and current <= max:
            # Skip numbers already tried in this session or globally
            if current in numbers or current in bingo_guessed_numbers:
                current += 1
                continue

            numbers.add(current)
            bingo_guessed_numbers.add(current)

            try:
                await target_channel.send(f"{current}")
                printdebug(f"Sent number: {current}", "INFO")
            except discord.HTTPException as e:
                if getattr(e, "status", None) == 429:  # Rate limited
                    printdebug(f"Rate limited (429). Waiting before retry...", "WARNING")
                    await asyncio.sleep(5)
                    continue  # retry same current after wait
                else:
                    printdebug(f"HTTP error sending number {current}: {str(e)}", "ERROR")
                    break
            except discord.Forbidden as e:
                printdebug(f"Permission error in channel {target_channel.id}: {str(e)}", "ERROR")
                break
            except Exception as e:
                printdebug(f"Unexpected error sending number {current}: {str(e)}", "ERROR")
                break

            # Move to next number
            current += 1

            # Check flag before sleeping
            if not guessing:
                break

            # Wait with interruptible sleep
            if not await interruptible_sleep(float(delay)):
                break

        if guessing and current > max:
            printdebug(f"All numbers from 1 to {max} attempted. Stopping bingo.", "INFO")

    except Exception as e:
        printdebug(f"Unexpected error in bingo loop: {str(e)}", "ERROR")

    finally:
        # Always reset globals in finally block
        guessing = False
        bingo_channel = None
        bingo_max_number = 1
        printdebug(f"Bingo stopped. Sent {len(numbers)} numbers.", "INFO")

@client.command()
async def rbxgp(ctx, *, url : str):
    """Scrap the price of a gamepass."""
    urls    = []
    total   = 0

    await ctx.message.edit(ui.wait("Fetching the price…"))

    if '_' in url:
        urls = url.split('_')
        for url in urls:
            price = await scrap_gamepass_price(url)
            await asyncio.sleep(0.5)
            if price:
                total += int(price.replace(',', ''))
    else:
        price = await scrap_gamepass_price(url)
        await asyncio.sleep(0.5)
        if price:
            total += int(price.replace(',', ''))
        else:
            await ctx.message.edit(ui.err("Could not fetch the price"))

    await ctx.message.edit(ui.card("Gamepass total", [("Total", f"<:robux:1220661393632530503> **{total}**")], icon="🎮"))

@client.command()
async def activity(ctx, *, msg: str):
    await ChangeActivity(msg)
    await ctx.message.edit(ui.ok(f"Activity set to **{msg}**"))

@client.command()
async def sleep(ctx, *, msg: str = f"I'm Sleeping"):
    global sleepUsers, sleepStatus, sleepMessage, stockSniper
    """Sleep with auto answer message."""

    if sleepStatus:
        await ctx.message.edit(ui.ok(f"Sleep mode is disabled"))
        join = '\n- '.join(f"<@{user}>" for user in sleepUsers)
        if len(sleepUsers) > 0:
            await send_message(msgChannel, ui.card("Unread DMs", icon="📬", desc=ui.bullets([f"<@{u}>" for u in sleepUsers], limit=30), footer=f"{len(sleepUsers)} while you were away"))
        await ChangeStatus("Online ✅", "online")
        sleepUsers = set()
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    else:
        await ctx.message.edit(ui.ok(f"Sleep mode is enabled with message **{msg}**"))
        await reset_private_litecoin()
        sleepMessage = msg + f" | <t:{round(datetime.now().timestamp())}:R>"
        await ChangeStatus("Sleeping 😴", "idle")
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    sleepStatus = not sleepStatus

@client.command()
async def setdelay(ctx, delay: float = None):
    global snipeDelay
    if delay is None:
        await ctx.message.edit(ui.ok(f"Snipe delay disabled"))
        snipeDelay = None
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    elif delay == 1337:
        snipeDelay = round(random.uniform(1, 2), 2)
        await ctx.message.edit(ui.ok(f"Snipe delay set to **{snipeDelay}** seconds"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    else:
        snipeDelay = delay
        await ctx.message.edit(ui.ok(f"Snipe delay set to **{snipeDelay}** seconds"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()

@client.command()
async def setauto(ctx, msg: str = None):
    global autoMode
    if msg is not None and msg.lower() in ('ticket', 'dm'):
        autoMode = msg.lower()
        await ctx.message.edit(ui.ok(f"Auto mode set to **__{msg}__**"))
    else:
        autoMode = "ticket"
        await ctx.message.edit(ui.ok(f"Auto mode reset"))

    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def setmp(ctx, msg: str = None):
    global maxprice
    if msg is not None:
        maxprice = msg
        await ctx.message.edit(ui.ok(f"Max price set to **__{float(msg)}__€**"))
    else:
        maxprice = 999
        await ctx.message.edit(ui.ok(f"Max price reset"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def settc(ctx, msg: str = None):
    global transactionChannel
    transactionChannel = ctx.channel.id
    if msg is not None:
        await ctx.message.edit(ui.ok(f"Transaction channel set to **{client.get_channel(int(msg)).mention}**"))
        transactionChannel = msg
    await ctx.message.edit(ui.ok(f"Transaction channel set to **{ctx.channel.mention}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def setmsg(ctx, msg: str = None):
    global msgChannel
    msgChannel = ctx.channel.id
    if msg is not None:
        await ctx.message.edit(ui.ok(f"Message channel set to **{client.get_channel(int(msg)).mention}**"))
        msgChannel = msg
    await ctx.message.edit(ui.ok(f"Message channel set to **{ctx.channel.mention}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def setvouch(ctx, msg: str = None):
    global vouchChannel
    vouchChannel = ctx.channel.id
    if msg is not None:
        await ctx.message.edit(ui.ok(f"Vouch channel set to **{client.get_channel(int(msg)).mention}**"))
        vouchChannel = msg
    await ctx.message.edit(ui.ok(f"Vouch channel set to **{ctx.channel.mention}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def settoken(ctx, msg: str = None):
    global CUSTOM_TOKEN
    if msg is not None:
        CUSTOM_TOKEN = msg
        register_secret(msg)
    else:
        CUSTOM_TOKEN = TOKEN
    await ctx.message.edit(ui.ok(f"Token set to **{mask_token(CUSTOM_TOKEN)}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def refreshcache(ctx):
    """Refresh the button component cache"""
    global button_cache
    button_cache.clear()
    await cache_all_buttons()
    await ctx.message.edit(ui.ok(f"Cache refreshed! ({len(button_cache)} channels cached)"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def manualcache(ctx, channel_id: str, message_id: str):
    """Manually cache button components from a specific message ID in a specific channel."""
    global button_cache

    try:
        channel = client.get_channel(int(channel_id))
        if not channel:
            await ctx.message.edit(ui.err(f"Channel with ID {channel_id} not found"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        message = await channel.fetch_message(int(message_id))
        if not message or not message.components:
            await ctx.message.edit(ui.err(f"Message with ID {message_id} not found or has no components"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        # Extract app_id
        app_id = None
        if message.interaction:
            app_id = message.interaction.application_id
        elif message.author.bot:
            app_id = message.author.id

        if not app_id:
            await ctx.message.edit(ui.err("Application ID not found for this message"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        # Extract components into proper format
        components = []
        for row in message.components:
            for child in getattr(row, "children", []):
                if isinstance(child, discord.Button):
                    components.append({
                        "label": getattr(child, "label", None),
                        "custom_id": str(getattr(child, "custom_id", "")),
                        "type": "Button"
                    })
                elif isinstance(child, discord.SelectMenu):
                    for option in getattr(child, "options", []):
                        components.append({
                            "label": getattr(option, "label", None),
                            "custom_id": str(getattr(child, "custom_id", "")),
                            "type": "SelectMenu"
                        })

        # Cache as DICTIONARY, not message object
        button_cache[int(channel_id)] = {
            "message_id": message.id,
            "application_id": app_id,
            "buttons": components
        }

        await ctx.message.edit(ui.ok(f"Manually cached {len(components)} component(s) from message ID {message_id} in channel ID {channel_id}"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    except Exception as e:
        await ctx.message.edit(ui.err(f"Error caching message: {str(e)}"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()

@client.command()
async def allcachestatus(ctx):
    """Display the status of the button component cache with channel -> message id mapping."""
    global button_cache

    if not button_cache:
        await ctx.message.edit(ui.card("Button cache", [("Cached channels", "`0`")], icon="🗂️"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    lines = []
    for ch_id, entry in button_cache.items():
        try:
            ch_mention = f"<#{int(ch_id)}>"
        except Exception:
            ch_mention = str(ch_id)

        # Normalize different entry types (dict from cache_all_buttons or discord.Message from manualcache)
        message_id = None
        app_id = None
        buttons_count = 0

        if isinstance(entry, dict):
            message_id = entry.get("message_id")
            app_id = entry.get("application_id")
            buttons_count = len(entry.get("buttons", [])) if entry.get("buttons") else 0
        else:
            # fallback for stored message object or other types
            message_id = getattr(entry, "id", None)
            app_id = getattr(entry, "application_id", None)
            # try to count components if it's a Message
            comps = getattr(entry, "components", None)
            if comps:
                for row in comps:
                    for c in getattr(row, "children", []):
                        if isinstance(c, discord.Button):
                            buttons_count += 1

        ch_obj = client.get_channel(int(ch_id)) if str(ch_id).isdigit() else None
        ch_name = ch_obj.name if ch_obj else "Unknown"

        lines.append(f"> • {ch_mention} · `{ch_name}`\n>   message `{message_id}` · **{buttons_count}** button(s) · app `{app_id}`")

    content = ui.card("Button cache", [("Cached channels", f"`{len(button_cache)}`")], icon="🗂️", desc="\n".join(lines))

    pages = ui.chunk(content)
    await ctx.message.edit(pages[0])
    for page in pages[1:]:
        await ctx.send(page)

@client.command()
async def getcat(ctx, *, msg):
    """Get all channels in a category (includes channel names)."""
    channels = await getcategory(msg)
    if channels:
        lines = []
        for ch_id in channels:
            try:
                ch_obj = client.get_channel(int(ch_id))
            except Exception:
                ch_obj = None
            name = ch_obj.name if ch_obj else "Unknown"
            lines.append(f"<#{ch_id}> · `{name}`")
        await ctx.message.edit(ui.list_card(f"Category · <#{msg}>", lines, icon="📂", limit=40))
    else:
        await ctx.message.edit(ui.err(f"No channels found in category <#{msg}> or invalid category ID"))
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

@client.command()
async def tkt(ctx, *, msg):
    """Open ticket."""
    await openticket(msg)
    await ctx.message.edit(ui.info(f"Test | **Open test ticket** in <#{msg}>"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def gettkt(ctx, channel_id: str):
    """Get all components from a ticket channel and display them."""

    try:
        # Get components from the ticket channel
        components = await getticket(channel_id, limit=10)

        if not components:
            await ctx.message.edit(ui.err("No components found in this channel"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        # Build the message with all components
        message_content = ui.card("Ticket components", [("Channel", f"<#{channel_id}>"), ("Messages", f"`{len(components)}`")], icon="🎫") + "\n\n"

        for msg_id, comp_data in components.items():
            message_content += f"### Message `{msg_id}`\n"

            # Display buttons
            if comp_data.get("buttons"):
                message_content += f"**Buttons** ({len(comp_data['buttons'])})\n"
                for btn in comp_data["buttons"]:
                    label = btn.get("label") or "No Label"
                    custom_id = btn.get("custom_id") or "No ID"
                    style = btn.get("style") or "Unknown"
                    message_content += f"> • `{label}` · ID `{custom_id}` · style `{style}`\n"

            # Display select menus
            if comp_data.get("selects"):
                message_content += f"**Select menus** ({len(comp_data['selects'])})\n"
                for menu in comp_data["selects"]:
                    custom_id = menu.get("custom_id") or "No ID"
                    message_content += f"> • Menu `{custom_id}`\n"
                    if menu.get("options"):
                        for opt in menu["options"]:
                            opt_label = opt.get("label") or "No Label"
                            opt_value = opt.get("value") or "No Value"
                            message_content += f">   ◦ `{opt_label}` → `{opt_value}`\n"

            # Display other components
            if comp_data.get("others"):
                message_content += f"**Other components** ({len(comp_data['others'])})\n"
                for other in comp_data["others"]:
                    comp_type = other.get("type") or "Unknown"
                    message_content += f"> • Type `{comp_type}`\n"

            message_content += "\n"

        pages = ui.chunk(message_content)
        await ctx.message.edit(pages[0])
        for page in pages[1:]:
            await ctx.send(page)

        printdebug(f"Retrieved {len(components)} messages with components from channel {channel_id}", "SUCCESS")

    except ValueError:
        await ctx.message.edit(ui.err("Invalid channel ID format"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    except Exception as e:
        await ctx.message.edit(ui.err(f"Error retrieving components: {str(e)}"))
        printdebug(f"Error in gettkt: {str(e)}", "ERROR")
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()

@client.command()
async def math(ctx, *, msg):
    """Do simple math calculation."""
    resultat = calcul(msg)
    await ctx.message.edit(ui.card("Math", [("Expression", f"`{msg}`"), ("Result", f"**`{resultat}`**")], icon="🧮"))
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

@client.command()
async def ppl(ctx):
    """Send a message to the channel."""
    await ctx.message.edit(f"## 💳 PayPal\n> **Friends & Family**\n> **Screenshot** of the transaction details\n> **Message** · This is a payment for a virtual ( intangible ) item . Physical shipement is not required . I understand that this is a non-refundable transaction . By sending this payment . I agree that i have already received the item and i am pleased with my purchase and the item is as described . I will not chargeback as the transaction is complete . I confirm that i am the owner of this PayPal account and i authorize this payment as i continue to proceed with the payment . If i chargeback , it would be considered to be fraud/scam.**")

@client.command()
async def vouch(ctx, *, msg):
    global vouchChannel
    """Send a message to the channel, voucher."""
    await ctx.message.edit(ui.vouch(f"<#{vouchChannel}>", f"+rep {client.user.mention} {msg}"))

@client.command()
async def vsb(ctx, number: str, price: str):
    global vouchChannel
    """Send a message to the channel, vouch server boost."""
    await ctx.message.edit(ui.vouch(f"<#{vouchChannel}>", f"+rep {client.user.mention} x14 Server Boosts {number} {pluralize('month',int(number))} for {price}€, legit !"))

@client.command()
async def vrb(ctx, number: str, price: str):
    global vouchChannel
    """Send a message to the channel, vouch server boost."""
    await ctx.message.edit(ui.vouch(f"<#{vouchChannel}>", f"+rep {client.user.mention} {number}k Robux for {price}€, legit"))

@client.command()
async def vnb(ctx, nmb: str, price: str, type: str):
    global vouchChannel

    """Send a message to the channel, vouch nitro boost."""
    if not type:
        await ctx.message.edit(ui.err("Specify the type of nitro code"))
        return
    elif type.lower() not in ('month', 'year', 'deco'):
        await ctx.message.edit(ui.err("Type must be `month`, `year` or `deco`"))
        return

    channel = client.get_channel(int(vouchChannel))

    if type == 'month':
        await ctx.message.edit(ui.vouch(channel.mention, f"+rep {client.user.mention} x{nmb} N1tr0 B00st for {price}€, legit !"))
    elif type == 'year':
        await ctx.message.edit(ui.vouch(channel.mention, f"+rep {client.user.mention} x{nmb} N1tr0 B00st Yearly for {price}€, legit !"))
    elif type == 'deco':
        await ctx.message.edit(ui.vouch(channel.mention, f"+rep {client.user.mention} x{nmb} Discord Decoration for {price}€, legit !"))

@client.command()
async def spam(ctx, nmb: int = 5, *, msg: str = None,):
    """Spam a message X time"""

    if msg is None:
        await ctx.message.edit(ui.err("Please enter a message to spam"))
        return

    for i in range(nmb):
        if i == 0:
            await ctx.message.delete()
        await ctx.send(f"{msg}")

@client.command()
async def addy(ctx):
    """Get the Litecoin address."""
    ltcqr = await qrfunc(ltc_address)
    await ctx.message.edit(ui.card("Litecoin", [("Address", f"`{ltc_address}`")], icon="<:litecoin:1147175971531784192>"))
    if ltcqr:
        await ctx.send(file=ltcqr)

@client.command()
async def setaddy(ctx, *, msg):
    """Set the Litecoin address."""
    global ltc_address
    ltc_address = msg
    await ctx.message.edit(ui.ok(f"Litecoin address set to **`{ltc_address}`**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def addyu(ctx):
    """Get the USDT address."""
    usdtqr = await qrfunc(usdt_address)
    await ctx.message.edit(ui.card("USDT (Polygon)", [("Address", f"`{usdt_address}`")], icon="<:usdt:1147175971531784192>"))
    if usdtqr:
        await ctx.send(file=usdtqr)

@client.command()
async def setaddyu(ctx, *, msg):
    """Set the USDT address."""
    global usdt_address
    usdt_address = msg
    await ctx.message.edit(ui.ok(f"USDT address set to **`{usdt_address}`**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def vpc(ctx, *, msg):
    """Send a message to the channel, vouch paypal."""
    crypto = msg.split('-')[0]
    price = msg.split('-')[1]
    await ctx.message.edit(ui.vouch("<#1126087095656648754>", f"+rep {client.user.mention} Exchange | PayPal to {crypto} [{price}€], legit !"))

@client.command()
async def vpsc(ctx, *, msg):
    """Send a message to the channel, vouch paypal."""
    currency = msg.split('-')[0]
    price = msg.split('-')[1]
    await ctx.message.edit(ui.vouch("<#1126087095656648754>", f"+rep {client.user.mention} Exchange | PaysafeCard to {currency} [{price}€], legit !"))

@client.command()
async def weed(ctx):
    """Schedule a weed message."""
    await weedSimple(ctx)

@client.command()
async def stamp(ctx, *, msg = None):
    """Timestamp from a message."""
    if msg is None:
        await ctx.message.edit(ui.card("Timestamp", [("Relative", f"<t:{round(datetime.now().timestamp())}:R>"), ("Unix", f"`{round(datetime.now().timestamp())}`")], icon="🕒"))
    else:
        msg = datetime.strptime(msg, '%Y-%m-%d %H:%M')
        await ctx.message.edit(ui.card("Timestamp", [("Relative", f"<t:{round(msg.timestamp())}:R>"), ("Unix", f"`{round(msg.timestamp())}`")], icon="🕒"))

@client.command()
async def getappid(ctx):
    """Get the application ID from a message"""
    try:
        # Look at recent messages in the channel
        async for message in ctx.channel.history(limit=10):
            if message.interaction:
                await ctx.message.edit(ui.info(f"Application ID: {message.interaction.application_id}"))
                return
        await ctx.message.edit(ui.err("No recent interactions found in this channel"))
    except Exception as e:
        await ctx.message.edit(ui.err(f"Error: {str(e)}"))

@client.command()
async def debugappid(ctx, channel_id: str = None):
    """Debug application IDs from messages in a channel"""
    if not channel_id:
        channel = ctx.channel
    else:
        channel = client.get_channel(int(channel_id))

    if not channel:
        await ctx.message.edit(ui.err("Channel not found"))
        return

    app_ids = {}
    async for message in channel.history(limit=20):
        if message.interaction:
            app_id = message.interaction.application_id
            if app_id not in app_ids:
                app_ids[app_id] = 0
            app_ids[app_id] += 1

    if app_ids:
        rows = [f"`{app_id}` · seen **{count}**×" for app_id, count in sorted(app_ids.items(), key=lambda x: x[1], reverse=True)]
        await ctx.message.edit(ui.list_card("Application IDs", rows, icon="🆔"))
    else:
        await ctx.message.edit(ui.warn("No application IDs found in recent messages"))

@client.command()
async def debugmsg(ctx, message_id: str):
    """Debug a message's interaction data"""
    try:
        message = await ctx.channel.fetch_message(int(message_id))

        dbg = [("Message", f"`{message.id}`"), ("Author", f"{message.author.name} · `{message.author.id}`"),
               ("Interaction", ui.yesno(message.interaction is not None))]
        if message.interaction:
            dbg.append(("App ID", f"`{message.interaction.application_id}`"))
        if message.webhook_id:
            dbg.append(("Webhook", f"`{message.webhook_id}`"))
        if message.components:
            dbg.append(("Components", f"`{len(message.components)}` row(s)"))
        debug_info = ui.card("Message debug", dbg, icon="🐞")
        for row in message.components or []:
            for child in row.children:
                debug_info += f"\n> ◦ {type(child).__name__} · `{getattr(child, 'custom_id', 'N/A')}`"

        await ctx.message.edit(debug_info)
    except Exception as e:
        await ctx.message.edit(ui.err(f"Error: {str(e)}"))

@client.command()
async def status(ctx):
    """Get the status of the bot."""
    await ctx.message.edit(ui.card("Bot status", [("Stock sniper", ui.yesno(stockSniper)), ("Auto buy", ui.yesno(autoBuy)), ("Redeemer", ui.yesno(autoRedeem)), ("No move", ui.yesno(noMove)), ("Sleep mode", ui.yesno(sleepStatus)), ("Max price", f"`{maxprice}€`"), ("Auto mode", f"`{autoMode}`")], icon="🤖", footer=f"Prefix {PREFIX}"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def dm(ctx, user: discord.User, *, msg: str):
    """Send a direct message to a user."""
    await user.send(msg)
    await ctx.message.edit(ui.ok(f"Message sent to **{user.name}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Command to check message id, if it contain an exclude keyword in text
@client.command()
async def checkmsg(ctx, message_id: str = None, channel_id: str = None):
    """Check if a message contains an exclude keyword."""
    try:
        # Si aucun ID n'est fourni, utiliser le message auquel on répond
        if message_id is None:
            if ctx.message.reference:
                # Récupérer le message référencé (message auquel on a répondu)
                referenced_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                msg_content = referenced_msg.content
                await ctx.message.edit(ui.wait("Checking the referenced message…", f"```{msg_content[:1000]}```"))
            else:
                await ctx.message.edit(ui.err("Please provide a message ID or reply to a message"))
                await asyncio.sleep(deleteTime)
                await ctx.message.delete()
                return
        else:
            # Si aucun channel_id n'est fourni, utiliser le channel actuel
            if channel_id is None:
                channel = ctx.channel
            else:
                channel = client.get_channel(int(channel_id))
                if not channel:
                    await ctx.message.edit(ui.err(f"Channel with ID {channel_id} not found"))
                    await asyncio.sleep(deleteTime)
                    await ctx.message.delete()
                    return

            # Récupérer le message par son ID
            try:
                message = await channel.fetch_message(int(message_id))
                msg_content = message.content
                await ctx.message.edit(ui.wait("Checking the message…", f"```{msg_content[:1000]}```"))
            except discord.NotFound:
                await ctx.message.edit(ui.err(f"Message with ID {message_id} not found in this channel"))
                await asyncio.sleep(deleteTime)
                await ctx.message.delete()
                return
            except discord.Forbidden:
                await ctx.message.edit(ui.err("I don't have permission to read messages in this channel"))
                await asyncio.sleep(deleteTime)
                await ctx.message.delete()
                return

        # Vérifier si le message contient des mots-clés exclus
        if any(keyword in msg_content.lower() for keyword in excludeKeywords):
            found_keywords = [keyword for keyword in excludeKeywords if keyword in msg_content.lower()]
            await ctx.message.edit(ui.card("Message analysis", icon="🔍", desc=ui.warn("Contains excluded keywords"), fields=[("Keywords", ", ".join(f"`{k}`" for k in found_keywords))]))
        else:
            await ctx.message.edit(ui.card("Message analysis", icon="🔍", desc=ui.ok("No excluded keywords found")))

    except Exception as e:
        await ctx.message.edit(ui.err(f"An error occurred: {str(e)}"))

    # Supprimer le message après un délai
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Command to check all nitro codes in a text file
@client.command()
async def checknb(ctx, type: str):
    """Check all nitro codes in a text file."""
    global validNitro
    validNitro.clear()
    await ctx.message.edit(ui.info("Checking all nitro codes in the file"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

    if not type:
        await ctx.message.edit(ui.err("Specify the type of nitro code"))
        return
    elif type.lower() not in ('month', 'year'):
        await ctx.message.edit(ui.err("Type must be `month` or `year`"))
        return

    nbText = "gifts/" + type + ".txt"

    # check if file exist
    if not os.path.exists(nbText):
        await ctx.message.edit(ui.err("File not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # opening the file in read mode
    nb_txt = open(nbText, "r")

    # reading the file
    nb_list = nb_txt.read().strip()

    # replacing end of line('/n') with ' ' and
    # splitting the text it further when '.' is seen.
    nb_into_list = nb_list.split('\n')

    start = time.time()

    for line in nb_into_list:
        # Check if the nitro code is discord.gift or discord.com/gifts
        await check_code(line)

    msg = await ctx.send(ui.ok(f"**{len(validNitro)}** valid Nitro found in `{round(time.time() - start, 2)}s`"))
    await asyncio.sleep(deleteTime)
    await msg.delete()

# Command to send a message for each line in a text file
@client.command()
async def nbs(ctx, type: str):
    """Send a message for each line in a text file."""
    counter = 0
    claimableCodes = set()
    claimedCodes = set()

    if not type:
        await ctx.message.edit(ui.err("Specify the type of nitro code"))
        return
    elif type.lower() not in ('month', 'year', 'deco'):
        await ctx.message.edit(ui.err("Type must be `month`, `year`, or `deco`"))
        return

    nbText = "gifts/" + type + ".txt"

    # Check if file exists
    if not os.path.exists(nbText):
        await ctx.message.edit(ui.err("File not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    await ctx.message.delete()

    # Open the file in read mode
    with open(nbText, "r") as nb_txt:
        # Read the file
        nb_list = nb_txt.read().strip()

    # Split the text into lines
    nb_into_list = nb_list.split('\n')

    start = time.time()

    for line in nb_into_list:
        counter += 1
        decoded_line = decode_code(line)
        if await check_code(decoded_line):
            claimableCodes.add(f"||<{decoded_line}>||")
        else:
            claimedCodes.add(f"||<{decoded_line}>||")

    # Send the results in one message
    join = '\n'.join(claimableCodes)
    if join:
        try:
            await ctx.send(ui.card("Claimable", icon="🟢", desc="> " + join.replace("\n", "\n> ")))
        except discord.errors.NotFound:
            print("Message not found. It may have been deleted.")
            return

    join = '\n'.join(claimedCodes)
    if join:
        try:
            await ctx.send(ui.card("Claimed", icon="🔴", desc="> " + join.replace("\n", "\n> ")))
        except discord.errors.NotFound:
            print("Message not found. It may have been deleted.")
            return

    msg = await ctx.send(ui.ok(f"**{counter}** Nitro {type.upper()} checked in `{round(time.time() - start, 2)}s`"))
    await asyncio.sleep(deleteTime)
    await msg.delete()

# Command to send a message for each line in a text file
@client.command()
async def checknbs(ctx, type: str):
    """Check all nitro codes in a text file."""
    counter         = 0
    claimableCodes  = set()
    claimedCodes    = set()

    if not type:
        await ctx.message.edit(ui.err("Specify the type of nitro code"))
        return
    elif type.lower() not in ('month', 'year'):
        await ctx.message.edit(ui.err("Type must be `month` or `year`"))
        return

    nbText = "gifts/" + type + ".txt"

    # check if file exist
    if not os.path.exists(nbText):
        await ctx.message.edit(ui.err("File not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    await ctx.message.delete()

    # opening the file in read mode
    nb_txt = open(nbText, "r")

    # reading the file
    nb_list = nb_txt.read().strip()

    # replacing end of line('/n') with ' ' and
    # splitting the text it further when '.' is seen.
    nb_into_list = nb_list.split('\n')

    start = time.time()

    for line in nb_into_list:
        counter += 1
        # await ctx.send(ui.info(f"**{counter}. ||<{line}>||**"))
        if await check_code(decode_code(line)):
            claimableCodes.add(f"||<{decode_code(line)}>||")
            # print(f"{counter} | Status: Claimable\nLink: {line}")
        else:
            claimedCodes.add(f"||<{decode_code(line)}>||")
            # print(f"{counter} | Status: Claimed\nLink: {line}")

    checkedCodes.clear()

    if claimableCodes:
        join = "\n- ".join(claimableCodes)
        print(f"Claimable:\n- {join}")

    if claimedCodes:
        join = "\n- ".join(claimedCodes)
        print(f"Claimed:\n- {join}")

    print(f"{counter} Nitro {type.upper()} checked in {round(time.time() - start, 2)}s")

# # Command to send a message to save account in the channel
@client.command()
async def saveacc(ctx, supp: str, amount: str, msg: str):
    """Save account in the channel."""
    message = ui.card(f"{amount}k Robux", [("Supplier", f"{supp} | <@{supp}>"), ("Bought", f"<t:{round(datetime.now().timestamp())}:R>"), ("Account", f"||{msg}||")], icon="🎮")
    await ctx.message.edit(f"{message}")

# Command to send a message to save nb in the channel and text file
@client.command()
async def savenb(ctx, supp: str, msg: str, type: str):
    """Save nitro codes in the channel and text file."""
    if not type:
        await ctx.message.edit(ui.err("Specify the type of nitro code"))
        return
    elif type.lower() not in ('month', 'year', 'deco'):
        await ctx.message.edit(ui.err("Type must be `month` or `year` or `deco`"))
        return

    nbs = str(msg).split('-')
    message = ui.card("Nitro saved", [("Supplier", f"{supp} | <@{supp}>"), ("Bought", f"<t:{round(datetime.now().timestamp())}:R>")], icon="🎁")

    nbText = "gifts/" + type + ".txt"

	# check if dir exist
    if not os.path.exists("gifts"):
        print("Directory does not exist.")
        os.mkdir("gifts")

    for nb in nbs:
        message = message + (f"\n> ||{nb.split('gift/')[1]}||")
        with open(nbText, "a") as nb_txt:
            nb_txt.write(f"{encode_code(nb)}\n")

    await ctx.message.edit(f"{message}")

# Command to send a message to specific user
@client.command()
async def nb(ctx, number: str, type: str):
    """Send a message to specific user."""
    if not type:
        await ctx.message.edit(ui.err("Specify the type of nitro code"))
        return
    elif type.lower() not in ('month', 'year', 'deco'):
        await ctx.message.edit(ui.err("Type must be `month` or `year` or `deco`"))
        return

    counter = 0
    max = 0
    droppedNb = set()
    nbs = f'## 🎁 Legit ? {client.get_channel(ctx.channel.id).recipient.mention}'

    nbText = "gifts/" + type + ".txt"

    # check if file exist
    if not os.path.exists(nbText):
        await ctx.message.edit(ui.err("File not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    await ctx.message.delete()

    # opening the file in read mode
    nb_txt = open(nbText, "r")

    # reading the file
    nb_list = nb_txt.read().strip()

    # replacing end of line('/n') with ' ' and
    # splitting the text it further when '.' is seen.
    nb_into_list = nb_list.split('\n')

    if len(nb_into_list) < int(number):
        max = len(nb_into_list)
    else:
        max = int(number)

    start = time.time()

    for line in nb_into_list:
        counter += 1

        if "https://" in decode_code(line):
            nbs = nbs + f"\n> ||<{decode_code(line)}>||"
        else:
            nbs = nbs + f"\n> ||<https://{decode_code(line)}>||"

        droppedNb.add(line)
        if counter == max:
            await ctx.send(f"{nbs}")
            break

    nb_txt.close()
    os.remove(nbText)

    if not len(droppedNb) == len(nb_into_list):
        with open(nbText, "a") as nb_txt:
            for nb in nb_into_list:
                if nb not in droppedNb:
                    nb_txt.write(f"{nb}\n")

    await send_message(msgChannel, ui.card("Nitro sent", [("Amount", f"`{max}`"), ("To", client.get_channel(ctx.channel.id).recipient.mention), ("Time", f"`{round(time.time() - start, 2)}s`")], icon="🎁"))

@client.command()
async def stock(ctx, type: str = None):
    """See all nitro in stock, optionally filtered by type"""
    global stocksChannels

    if type and type.lower() not in ('month', 'year', 'deco'):
        await ctx.message.edit(ui.err("Type must be `month`, `year`, or `deco`"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Count codes from files
    month = 0
    year = 0
    deco = 0

    # Count month codes
    if os.path.exists("gifts/month.txt"):
        with open("gifts/month.txt", "r") as f:
            month = len([line for line in f if line.strip()])

    # Count year codes
    if os.path.exists("gifts/year.txt"):
        with open("gifts/year.txt", "r") as f:
            year = len([line for line in f if line.strip()])

    # Count deco codes
    if os.path.exists("gifts/deco.txt"):
        with open("gifts/deco.txt", "r") as f:
            deco = len([line for line in f if line.strip()])

    if type:
        # Show only requested type
        if type.lower() == 'month':
            count = month
        elif type.lower() == 'year':
            count = year
        elif type.lower() == 'deco':
            count = deco
        await ctx.message.edit(f"# Stock 📦\n**- {type.upper()} : __`{count}`__**")
    else:
        # Show all types
        await ctx.message.edit(f"# Stock 📦\n**- Month : __`{month}`__\n- Year : __`{year}`__\n- Decoration : __`{deco}`__**")

@client.command()
async def auto(ctx):
    """Get status of the auto buyer Enable/Disable."""
    global autoBuy
    autoBuy = not autoBuy
    if autoBuy:
        await ctx.message.edit(ui.ok("Auto buyer enabled"))
    else:
        await ctx.message.edit(ui.ok("Auto buyer disabled"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def redeemer(ctx):
    """Get status of the redeemer Enable/Disable."""
    global autoRedeem
    autoRedeem = not autoRedeem
    if autoRedeem:
        await ctx.message.edit(ui.ok("Redeemer enabled"))
    else:
        await ctx.message.edit(ui.ok("Redeemer disabled"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# No move command
@client.command()
async def nomove(ctx):
    """Get status of the no move Enable/Disable."""
    global noMove, nomoveChannel
    # Check if the ctx is a voice channel
    if ctx.author.voice:
        noMove = not noMove
        if noMove:
            nomoveChannel = ctx.author.voice.channel
            await ctx.message.edit(ui.ok(f"No move enabled on {nomoveChannel.name}"))
        else:
            await ctx.message.edit(ui.ok("No move disabled"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    else:
        await ctx.message.edit(ui.info("You are not in a voice channel"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()

# To get stocks and ticket channels
@client.command()
async def st(ctx):
    """Get all stock and ticket channels."""
    stocks = set()
    tickets = set()
    if stocksChannels:
        for stockChannel in stocksChannels:
            stocks.add(f"<#{stockChannel}>")
        await ctx.message.edit(ui.list_card("Stock channels", sorted(stocks), icon="📦"))
    else:
        await ctx.message.edit(ui.warn('Stock channel list is empty'))
    if ticketChannels:
        for ticketChannel in ticketChannels:
            tickets.add(f"<#{ticketChannel}>")
        await ctx.send(ui.list_card("Ticket channels", sorted(tickets), icon="🎫"))
    else:
        await ctx.send(ui.warn('Ticket channel list is empty'))

# To upload ticket and stock channels to add them back after a restart
@client.command()
async def uploadst(ctx):
    """Upload stock and ticket channels."""
    if stocksChannels:
        await ctx.message.edit(ui.card("Stock channels", icon="📤", desc=f"**{len(stocksChannels)}** stock {pluralize('channel', len(stocksChannels))} · re-add them with:\n```{PREFIX}addstocks {'-'.join(map(str, stocksChannels))}```"))
    else:
        await ctx.message.edit(ui.warn('Stock channel list is empty'))
    if ticketChannels:
        await ctx.send(ui.card("Ticket channels", icon="📤", desc=f"**{len(ticketChannels)}** ticket {pluralize('channel', len(ticketChannels))} · re-add them with:\n```{PREFIX}addtickets {'-'.join(map(str, ticketChannels))}```"))
    else:
        await ctx.send(ui.warn('Ticket channel list is empty'))

# To upload snipe channels to add them back after a restart
@client.command()
async def uploadsn(ctx):
    """Upload snipe channels."""
    if snipeChannels:
        await ctx.message.edit(ui.card("Snipe channels", icon="📤", desc=f"**{len(snipeChannels)}** snipe {pluralize('channel', len(snipeChannels))} · re-add them with:\n```{PREFIX}addsnipes {'-'.join(map(str, snipeChannels))}```"))
    else:
        await ctx.message.edit(ui.info('Snipe channel list is currently empty'))

# To update the stock and ticket channels
@client.command()
async def addst(ctx, *, msg):
    """Add stock and ticket channels."""
    global stocksChannels, ticketChannels
    ticket = msg.split('-')[1]
    stock = msg.split('-')[0]

    if not stock.isdigit() or not ticket.isdigit():
        await ctx.message.edit(ui.err("Please enter a valid channel ID"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if int(stock) not in stocksChannels:
        stocksChannels.add(int(stock))
        ticketChannels.add(int(ticket))
        addGuild(client.get_channel(int(stock)).guild.id)
        await ctx.message.edit(ui.ok("Channels added", f"📦 Stock · **{stock}**\n🎫 Ticket · **{ticket}**"))
    else:
        await ctx.message.edit(ui.warn(f'**{stock}** is already in the list of stock channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To update the stock channel
@client.command()
async def addstock(ctx, *, msg):
    """Add stock channel."""
    global stocksChannels

    if not msg.isdigit():
        await ctx.message.edit(ui.err("Please enter a valid channel ID"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if int(msg) not in stocksChannels:
        stocksChannels.add(int(msg))
        addGuild(client.get_channel(int(msg)).guild.id)
        await ctx.message.edit(ui.ok(f'**{msg}** added to the list of stock channels'))
    else:
        await ctx.message.edit(ui.warn(f'**{msg}** is already in the list of stock channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To add the snipe channel
@client.command()
async def addsnipe(ctx, *, msg):
    """Add the snipe channel."""
    global snipeChannels

    if not msg.isdigit():
        await ctx.message.edit(ui.err("Please enter a valid channel ID"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if int(msg) not in snipeChannels:
        snipeChannels.add(int(msg))
        await ctx.message.edit(ui.ok(f'**{msg}** added to the list of snipe channels'))
    else:
        await ctx.message.edit(ui.warn(f'**{msg}** is already in the list of snipe channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To add the multiple snipe channels
@client.command()
async def addsnipes(ctx, *, msg):
    """Add multiple snipe channels."""
    global snipeChannels
    snipes = msg.split('-')

    for snipe in snipes:
        if snipe.isdigit():
            snipeChannels.add(int(snipe))
        else:
            print(f'{snipe} is not a valid channel ID')
    await ctx.message.edit(added_card("snipe", snipes))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To update the stock channel with multiple stock channels at once
@client.command()
async def addstocks(ctx, *, msg):
    """Add multiple stock channels."""
    global stocksChannels
    stocks = msg.split('-')

    for stock in stocks:
        if stock.isdigit():
            stocksChannels.add(int(stock))
        else:
            print(f'{stock} is not a valid channel ID')
    await ctx.message.edit(added_card("stock", stocks))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove the stock channel
@client.command()
async def rmstock(ctx, *, msg):
    """Remove stock channel."""
    global stocksChannels
    if int(msg) in stocksChannels:
        stocksChannels.remove(int(msg))
        await ctx.message.edit(ui.ok(f'**{msg}** removed from the list of stock channels'))
    else:
        await ctx.message.edit(ui.warn(f'**{msg}** is not in the list of stock channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove all stock channels and ticket channels
@client.command()
async def rmall(ctx):
    """Remove all stock channels and ticket channels."""
    global stocksChannels, ticketChannels
    stocksChannels.clear()
    ticketChannels.clear()
    await ctx.message.edit(ui.ok('All stock channels and ticket channels have been removed'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove the snipe channel
@client.command()
async def rmsnipe(ctx, *, msg):
    """Remove the snipe channel."""
    global snipeChannels
    if int(msg) in snipeChannels:
        snipeChannels.remove(int(msg))
        await ctx.message.edit(ui.ok(f'**{msg}** removed from the list of snipe channels'))
    else:
        await ctx.message.edit(ui.warn(f'**{msg}** is not in the list of snipe channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove all the snipe channels
@client.command()
async def rasnipe(ctx):
    """Remove all the snipe channels."""
    global snipeChannels
    snipeChannels.clear()
    await ctx.message.edit(ui.ok('All snipe channels have been removed'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove all stock channels and ticket channels that don't have guild anymore
@client.command()
async def clean(ctx):
    """Clean all stock channels and ticket channels that don't have guild anymore."""
    global stocksChannels, ticketChannels
    beforeDelete = len(stocksChannels)
    # Remove duplicate
    stocksChannels = set(stocksChannels)
    ticketChannels = set(ticketChannels)
    for stockChannel in list(stocksChannels):
        if not client.get_channel(int(stockChannel)):
            stocksChannels.remove(stockChannel)
    afterDelete = len(stocksChannels)
    await ctx.message.edit(ui.ok("Stock channels cleaned", f"Before **{beforeDelete}** → after **{afterDelete}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove all snipe channels that don't have guild anymore
@client.command()
async def sclean(ctx):
    """Clean all snipe channels that don't have guild anymore."""
    global snipeChannels
    beforeDelete = len(snipeChannels)
    # Remove duplicate
    snipeChannels = set(snipeChannels)
    for snipeChannel in list(snipeChannels):
        if not client.get_channel(int(snipeChannel)):
            snipeChannels.remove(snipeChannel)
    afterDelete = len(snipeChannels)
    await ctx.message.edit(ui.ok("Snipe channels cleaned", f"Before **{beforeDelete}** → after **{afterDelete}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To remove all ticket channels that don't have guild anymore
@client.command()
async def tclean(ctx):
    """Clean all ticket channels that don't have guild anymore."""
    global ticketChannels, guild_ticket_map
    beforeDelete = len(ticketChannels)
    # Remove duplicate
    ticketChannels = set(ticketChannels)
    for ticketChannel in list(ticketChannels):
        if not client.get_channel(int(ticketChannel)):
            ticketChannels.remove(ticketChannel)
            # Also remove from guild_ticket_map
            for guild_id, ch_id in list(guild_ticket_map.items()):
                if ch_id == int(ticketChannel):
                    del guild_ticket_map[guild_id]
    afterDelete = len(ticketChannels)
    await ctx.message.edit(ui.ok("Ticket channels cleaned", f"Before **{beforeDelete}** → after **{afterDelete}**"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To get all deleted stock channels
@client.command()
async def deleted(ctx):
    """Get all deleted stock channels."""
    if deletedStockChannels:
        await ctx.message.edit(ui.list_card("Deleted stock channels", [str(c) for c in deletedStockChannels], icon="🗑️"))
    else:
        await ctx.message.edit(ui.info('No deleted stock channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Get stock channel
@client.command()
async def stocks(ctx):
    """Get all stock channels."""
    stocks = set()
    if stocksChannels:
        for stockChannel in stocksChannels:
            stocks.add(f"<#{stockChannel}>")
        await ctx.message.edit(ui.list_card("Stock channels", sorted(stocks), icon="📦"))
    else:
        await ctx.message.edit(ui.warn('Stock channel list is empty'))

# Optional: Get snipe channel
@client.command()
async def snipes(ctx):
    """Get all snipe channels."""
    snipes = set()
    if snipeChannels:
        for snipeChannel in snipeChannels:
            snipes.add(f"<#{snipeChannel}>")
        await ctx.message.edit(ui.list_card("Snipe channels", sorted(snipes), icon="🎯"))
    else:
        await ctx.message.edit(ui.warn('Snipe channel list is empty'))

@client.command()
async def addcst(ctx, *, category):
    """Add all channels in a category to stock channels."""
    global stocksChannels, guildsId
    channels = await getcategory(category)
    if channels:
        for ch_id in channels:
            stocksChannels.add(int(ch_id))
        # Add the guild to guildsId (assuming all channels are in the same guild)
        if channels:
            guild_id = client.get_channel(int(channels[0])).guild.id
            addGuild(guild_id)
        await ctx.message.edit(added_card("stock", channels))
    else:
        await ctx.message.edit(ui.warn('No channels found in that category'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def addcsn(ctx, *, category):
    """Add all channels in a category to snipe channels."""
    global snipeChannels
    channels = await getcategory(category)
    if channels:
        for ch_id in channels:
            snipeChannels.add(int(ch_id))
        await ctx.message.edit(added_card("snipe", channels))
    else:
        await ctx.message.edit(ui.warn('No channels found in that category'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Add the multiple ticket channels
@client.command()
async def addtickets(ctx, *, msg):
    """Add multiple ticket channels."""
    global ticketChannels

    tickets = msg.split('-')
    for ticket in tickets:
        if ticket.isdigit():
            ticketChannels.add(int(ticket))
        else:
            print(f'{ticket} is not a valid channel ID')
    await ctx.message.edit(added_card("ticket", tickets))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Add the ticket channel
@client.command()
async def addticket(ctx, *, msg):
    """Add ticket channel."""
    global ticketChannels

    if not msg.isdigit():
        await ctx.message.edit(ui.err("Please enter a valid channel ID"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if int(msg) not in ticketChannels:
        ticketChannels.add(int(msg))
        await ctx.message.edit(ui.ok(f'**{msg}** added to the list of ticket channels'))
    else:
        await ctx.message.edit(ui.warn(f'**{msg}** is already in the list of ticket channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Remove the ticket channel
@client.command()
async def rmticket(ctx, *, msg):
    """Remove ticket channel."""
    global ticketChannels
    if int(msg) in ticketChannels:
        ticketChannels.remove(int(msg))
        await ctx.message.edit(ui.ok(f'**{msg}** removed from the list of ticket channels'))
    else:
        await ctx.message.edit(ui.warn(f'**{msg}** is not in the list of ticket channels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Get stock channel
@client.command()
async def tickets(ctx):
    """Get all ticket channels."""
    tickets = set()
    if ticketChannels:
        for ticketChannel in ticketChannels:
            tickets.add(f"<#{ticketChannel}>")
        await ctx.message.edit(ui.list_card("Ticket channels", sorted(tickets), icon="🎫"))
    else:
        await ctx.message.edit(ui.warn('Ticket channel list is empty'))

@client.command()
async def createinvites(ctx):
    """Create invites in all snipe guilds and save them to a file."""

    created_invites = []
    headers = {
        "authorization": TOKEN,
        "content-type": "application/json",
    }

    # Get all unique guilds from snipe channels
    snipe_guilds = set()
    for channel_id in snipeChannels:
        channel = client.get_channel(int(channel_id))
        if channel and channel.guild:
            snipe_guilds.add(channel.guild)

    if not snipe_guilds:
        await ctx.message.edit(ui.err("No snipe guilds found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    await ctx.message.edit(ui.wait(f"Creating invites for **{len(snipe_guilds)}** guilds…"))

    for guild in snipe_guilds:
        try:
            # Get the first channel in the guild
            channel = guild.text_channels[0] if guild.text_channels else None

            if not channel:
                print(f"No text channels found in {guild.name}")
                continue

            payload = {
                "max_age": 604800,
                "max_uses": 0,
                "temporary": False,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://discord.com/api/v9/channels/{channel.id}/invites",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status == 201:
                        invite_data = await response.json()
                        invite_code = invite_data['code']
                        invite_url = f"https://discord.gg/{invite_code}"
                        created_invites.append(f"{guild.name} | {invite_url}")
                        print(f"✅ Created invite for {guild.name}: {invite_url}")
                    else:
                        print(f"❌ Failed to create invite for {guild.name}: {response.status}")

        except Exception as e:
            print(f"Error creating invite for {guild.name}: {str(e)}")

    # Save invites to file
    if created_invites:
        with open("invites.txt", "w") as f:
            for invite in created_invites:
                f.write(invite + "\n")

        await ctx.message.edit(ui.ok(f"Created **{len(created_invites)}** invites and saved to `invites.txt`"))
    else:
        await ctx.message.edit(ui.err("Failed to create any invites"))

    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def joinfromfile(ctx):
    """Join all servers using invite codes from invites.txt"""

    if not os.path.exists("invites.txt"):
        await ctx.message.edit(ui.err("`invites.txt` file not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    await ctx.message.edit(ui.wait(f"Reading invites from file…"))

    joined_count = 0
    failed_count = 0
    failed_invites = []

    try:
        with open("invites.txt", "r") as f:
            invites = f.readlines()

        if not invites:
            await ctx.message.edit(ui.warn("`invites.txt` is empty"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        await ctx.message.edit(ui.wait(f"Joining **{len(invites)}** servers…"))

        for invite_line in invites:
            invite_line = invite_line.strip()
            if not invite_line:
                continue

            try:
                # Extract invite code from URL or direct code
                if "discord.gg/" in invite_line:
                    invite_code = invite_line.split("discord.gg/")[-1]
                elif "discord.com/invite/" in invite_line:
                    invite_code = invite_line.split("discord.com/invite/")[-1]
                else:
                    # Assume it's a direct code or from "Guild Name | URL" format
                    invite_code = invite_line.split(" | ")[-1].split("discord.gg/")[-1] if " | " in invite_line else invite_line

                # Remove any trailing characters
                invite_code = invite_code.rstrip()

                if not invite_code:
                    failed_invites.append(invite_line)
                    failed_count += 1
                    continue

                # Try to join the guild
                try:
                    invite = await client.fetch_invite(invite_code)
                    guild = invite.guild

                    # Check if already in guild
                    if guild in client.guilds:
                        print(f"⏭️ Already in guild: {guild.name}")
                        continue

                    # Accept the invite
                    await client.accept_invite(invite)
                    joined_count += 1
                    print(f"✅ Joined: {guild.name}")
                    await asyncio.sleep(0.5)  # Rate limiting

                except discord.NotFound:
                    print(f"❌ Invalid invite: {invite_code}")
                    failed_invites.append(invite_line)
                    failed_count += 1
                except discord.Forbidden:
                    print(f"❌ No permission to join: {invite_code}")
                    failed_invites.append(invite_line)
                    failed_count += 1
                except Exception as e:
                    print(f"❌ Error joining {invite_code}: {str(e)}")
                    failed_invites.append(invite_line)
                    failed_count += 1

            except Exception as e:
                print(f"❌ Error processing invite: {str(e)}")
                failed_invites.append(invite_line)
                failed_count += 1

        # Send summary
        failed_detail = ui.bullets([f"`{f}`" for f in failed_invites], limit=10) if failed_invites else ""
        await ctx.message.edit(ui.card("Guild joiner", [("✅ Joined", f"`{joined_count}`"), ("❌ Failed", f"`{failed_count}`")],
                                       icon="🚪", desc=failed_detail))

    except Exception as e:
        await ctx.message.edit(ui.err(f"Error reading file: {str(e)}"))

    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Get invite link member use to join the server
@client.command()
async def inv(ctx, *, msg):
    """Get invite link member use to join the server."""
    if '@' in msg:
        msg = msg[2:len(msg) - 1]
    elif client.get_user(int(msg)) is None:
        await ctx.message.edit(ui.err("User not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return
    elif ctx.guild is None:
        await ctx.message.edit(ui.err("This command can only be used in a server"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Get all invite from the guild
    try:
        invites = await ctx.guild.invites()
    except discord.Forbidden:
        await ctx.message.edit(ui.err("I do not have permission to fetch invites"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    guildId = ctx.guild.id

    # Check if the invite is valid
    if invites:
        headers={
            "accept": "*/*",
            "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "authorization": f"{TOKEN}",
            "content-type": "application/json",
            "priority": "u=1, i",
            "sec-ch-ua": "\"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", \"Google Chrome\";v=\"128\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": "en-US",
            "x-discord-timezone": "Europe/Paris",
            "x-super-properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6ImZyLUZSIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyOC4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTI4LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjMyMzMxNCwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
        }

        json={
            "or_query": {},
            "and_query": {},
            "limit": 250
        }

        # Fetch request to discord api to get the member that use the invite link
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://discord.com/api/v9/guilds/{guildId}/members-search", headers=headers, json=json) as response:
                if response:
                    members = (await response.json())['members']
                    response.close()
                    for data in members:
                        # print(data, data['member']['user'])
                        if data['member']['user']['id'] == msg:
                            if data['inviter_id'] is not None:
                                if client.get_user(int(data['inviter_id'])) is not None:
                                    await ctx.message.edit(ui.card(client.get_user(int(msg)).name, [("Invite", f"`{data['source_invite_code']}`"), ("By", f"{client.get_user(int(data['inviter_id'])).mention} | `{data['inviter_id']}`")], icon="🔗"))
                                else:
                                    await ctx.message.edit(ui.card(client.get_user(int(msg)).name, [("Invite", f"`{data['source_invite_code']}`"), ("By", f"`{data['inviter_id']}`")], icon="🔗"))
                                break
                            else:
                                await ctx.message.edit(ui.card(client.get_user(int(msg)).name, [("Invite", "`None`"), ("By", "`None`")], icon="🔗"))
                                break
                else:
                    print(f"An error occurred while fetching the data - {guildId}")
                    # await ctx.message.edit(ui.err(f"An error occurred while fetching the data - {guildId}"))
    else:
        await ctx.message.edit(ui.warn("No invite link found"))

# Get all members that use the invite link
@client.command()
async def invs(ctx, *, inviter):
    """Get all members that use the invite link."""
    if '@' in inviter:
        inviter = inviter[2:len(inviter) - 1]
    elif client.get_user(int(inviter)) is None:
        await ctx.message.edit(ui.err("User not found"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return
    elif ctx.guild is None:
        await ctx.message.edit(ui.err("This command can only be used in a server"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Get all invite from the guild
    try:
        invites = await ctx.guild.invites()
    except discord.Forbidden:
        await ctx.message.edit(ui.err("I do not have permission to fetch invites"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    invList = dict()
    memberList = dict()
    guildId = ctx.guild.id

    # Check if the invite is valid
    if invites:
        headers={
            "accept": "*/*",
            "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "authorization": f"{TOKEN}",
            "content-type": "application/json",
            "priority": "u=1, i",
            "sec-ch-ua": "\"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", \"Google Chrome\";v=\"128\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": "en-US",
            "x-discord-timezone": "Europe/Paris",
            "x-super-properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6ImZyLUZSIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyOC4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTI4LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjMyMzMxNCwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
        }

        json={
            "or_query": {},
            "and_query": {
                "or_query": {
                    "source_invite_code": inviter
                },
            },
            "limit": 1000
        }

        # Fetch request to discord api to get the member that use the invite link
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://discord.com/api/v9/guilds/{guildId}/members-search", headers=headers, json=json) as response:
                if response:
                    members = (await response.json())['members']
                    response.close()
                    msg = ""
                    count = 0
                    for data in members:
                        if data['source_invite_code'] is not None and data['inviter_id'] is not None and data['inviter_id'] == inviter:
                            if client.get_user(int(data['member']['user']['id'])) is not None:
                                count += 1
                                msg = msg + f"> - {client.get_user(int(data['member']['user']['id'])).name} | `{data['member']['user']['id']}`\n> `{data['source_invite_code']}`\n"
                            else:
                                count += 1
                                msg = msg + f"> - `{data['member']['user']['id']}`\n"
                    await ctx.message.edit(ui.card("Invite tracker", [("Inviter", client.get_user(int(inviter)).mention), ("Invited", f"`{count}`")], icon="🔗", desc=msg))
                else:
                    print(f"An error occurred with the value - {msg}")

# Get all invites in the server
@client.command()
async def invites(ctx):
    """Get all invites in the server."""
    if ctx.guild is None:
        await ctx.message.edit(ui.err("This command can only be used in a server"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Get all invite from the guild
    try:
        invites = await ctx.guild.invites()
    except discord.Forbidden:
        await ctx.message.edit(ui.err("I do not have permission to fetch invites"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    invList = dict()
    memberList = dict()
    guildId = ctx.guild.id

    # Check if the invite is valid
    if invites:
        headers={
            "accept": "*/*",
            "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "authorization": f"{TOKEN}",
            "content-type": "application/json",
            "priority": "u=1, i",
            "sec-ch-ua": "\"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", \"Google Chrome\";v=\"128\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": "en-US",
            "x-discord-timezone": "Europe/Paris",
            "x-super-properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6ImZyLUZSIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyOC4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTI4LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjMyMzMxNCwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
        }

        json={
            "or_query": {},
            "and_query": {},
            "limit": 1000
        }

        # Fetch request to discord api to get the member that use the invite link
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://discord.com/api/v9/guilds/{guildId}/members-search", headers=headers, json=json) as response:
                if response:
                    members = (await response.json())['members']
                    response.close()
                    msg = ""
                    for data in members:
                        if data['source_invite_code'] is not None and data['inviter_id'] is not None:
                            invList[data['source_invite_code']]         = data['inviter_id']
                            memberList[data['member']['user']['id']]    = data['source_invite_code']

                    for key, value in invList.items():
                        if value is not None:
                            if client.get_user(int(value)) is not None:
                                msg = ui.card("Invite logger", [("Invite", f"`{key}`"), ("By", f"{client.get_user(int(value)).mention} | `{value}`")], icon="🔗", desc="**Used by**") + "\n"
                            else:
                                msg = ui.card("Invite logger", [("Invite", f"`{key}`"), ("By", f"`{value}`")], icon="🔗", desc="**Used by**") + "\n"
                            for key2, value2 in memberList.items():
                                if key == value2:
                                    msg = msg + f"> - {client.get_user(int(key2)).mention} | `{key2}`\n"
                            # await ctx.send(msg)
                            print(msg)
                        else:
                            print(f"An error occurred with the value - {value}")

                    await ctx.message.edit(ui.info(f"**__{len(invList)}__** invite link(s) found"))
                    print(f"**__{len(invList)}__** invite link(s) found.")
                else:
                    print(f"An error occurred while fetching the data - {guildId}")
                    await ctx.message.edit(ui.err(f"An error occurred while fetching the data - {guildId}"))
    else:
        await ctx.message.edit(ui.warn("No invite link found"))

# Update the .txt file from table
@client.command()
async def update(ctx):
    """Update the .txt file from table."""
    table_to_file("stocks.txt", stocksChannels)
    table_to_file("tickets.txt", ticketChannels)
    await ctx.message.edit(ui.ok("Files have been updated"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To sync file with the stock and ticket channel
@client.command()
async def sync(ctx):
    """Sync file with the stock and ticket channel."""
    file_to_table("stocks.txt", stocksChannels)
    file_to_table("tickets.txt", ticketChannels)
    await ctx.message.edit(ui.ok("Tables have been synced"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Command to add a keyword to the list
@client.command()
async def keyword(ctx, *, word):
    """Add a keyword to the list."""
    if word not in keywords:
        keywords.add(word)
        _db().set('keywords', sorted(keywords))
        await ctx.message.edit(ui.ok(f'Keyword **"{word}"** added to the list'))
    else:
        await ctx.message.edit(ui.warn(f'Keyword **"{word}"** is already in the list'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Command to display the list of exclude keywords
@client.command()
async def getkw(ctx):
    """Get the list of keywords."""
    if keywords:
        await ctx.message.edit(ui.list_card("Keywords", [f"`{k}`" for k in sorted(keywords)], icon="🔑"))
    else:
        await ctx.message.edit(ui.warn('The keyword list is empty'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Command to display the list of exclude keywords
@client.command()
async def rmkw(ctx, *, word):
    """Remove a keyword from the list."""
    if word in keywords:
        keywords.remove(word)
        _db().set('keywords', sorted(keywords))
        await ctx.message.edit(ui.ok(f'Keyword **"{word}"** removed from the list'))
    else:
        await ctx.message.edit(ui.warn(f'Keyword **"{word}"** is not in the list'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Command to add a keyword to the list of exclude keywords
@client.command()
async def exclude(ctx, *, word):
    """Add a keyword to the list of exclude keywords."""
    if word not in excludeKeywords:
        excludeKeywords.add(word)
        _db().set('excludeKeywords', sorted(excludeKeywords))
        await ctx.message.edit(ui.ok(f'Keyword **"{word}"** added to the list'))
    else:
        await ctx.message.edit(ui.warn(f'Keyword **"{word}"** is already in the list'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Command to display the list of keywords
@client.command()
async def getex(ctx):
    """Get the list of exclude keywords."""
    if excludeKeywords:
        await ctx.message.edit(ui.list_card("Exclude keywords", [f"`{k}`" for k in sorted(excludeKeywords)], icon="🚫"))
    else:
        await ctx.message.edit(ui.warn('The exclude keyword list is empty'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Command to remove a keyword from the list of exclude keywords
@client.command()
async def rmex(ctx, *, word):
    """Remove a keyword from the list of exclude keywords."""
    if word in excludeKeywords:
        excludeKeywords.remove(word)
        _db().set('excludeKeywords', sorted(excludeKeywords))
        await ctx.message.edit(ui.ok(f'Keyword **"{word}"** removed from the list'))
    else:
        await ctx.message.edit(ui.warn(f'Keyword **"{word}"** is not in the list'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Command to add a keyword to the list of labels
@client.command()
async def label(ctx, *, word):
    """Add a label to the list."""
    if word not in labelsCheck:
        labelsCheck.add(word)
        _db().set('labelsCheck', sorted(labelsCheck))
        await ctx.message.edit(ui.ok(f'Keyword **"{word}"** added to the list of labels'))
    else:
        await ctx.message.edit(ui.warn(f'Keyword **"{word}"** is already in the list of labels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Optional: Command to display the list of labels
@client.command()
async def getlb(ctx):
    """Get the list of labels."""
    if labelsCheck:
        await ctx.message.edit(ui.list_card("Labels", [f"`{k}`" for k in sorted(labelsCheck)], icon="🏷️"))
    else:
        await ctx.message.edit(ui.warn('The keyword list is empty'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def rmlb(ctx, *, word):
    """Remove a label from the list."""
    if word in labelsCheck:
        labelsCheck.remove(word)
        _db().set('labelsCheck', sorted(labelsCheck))
        await ctx.message.edit(ui.ok(f'Keyword **"{word}"** removed from the list of labels'))
    else:
        await ctx.message.edit(ui.warn(f'Keyword **"{word}"** is not in the list of labels'))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

@client.command()
async def nuke(ctx, amount: int = 100):
    """Deletes all messages sent by the bot in a specific channel."""
    channel = client.get_channel(ctx.channel.id)
    if not channel:
        await ctx.message.edit(ui.err("Channel not found"))
        return

    # Fetch messages from the channel
    bot_messages = []
    async for msg in channel.history(limit=amount + 1):
        if msg.author == client.user:
            bot_messages.append(msg)

    # Delete the bot's messages
    for msg in bot_messages:
        try:
            if bot_messages.index(msg) == len(bot_messages) - 1 and not isinstance(ctx.channel, discord.DMChannel):
                await msg.edit(ui.info(f"Deleted {len(bot_messages) - 1} messages in {channel.name}"))
                await asyncio.sleep(1)
                await msg.delete()
            elif bot_messages.index(msg) == len(bot_messages) - 1 and isinstance(ctx.channel, discord.DMChannel):
                await msg.edit(ui.info(f"Deleted {len(bot_messages) - 1} messages with {channel.recipient.name}"))
                await asyncio.sleep(1)
                await msg.delete()
            else:
                await msg.delete()
        except discord.Forbidden:
            await ctx.message.edit(ui.err("I do not have permission to delete messages"))
            return
        except discord.HTTPException as e:
            await ctx.message.edit(ui.err(f"An error occurred: {str(e)}"))
            return

# To get all guilds id
@client.command()
async def guilds(ctx):
    """Get all guilds id."""
    global guildsId
    await ctx.message.edit(ui.list_card("Monitored guilds", [f"`{g}`" for g in sorted(guildsId)], icon="🏰", empty="No guild monitored yet"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Set usdtmatic private key
@client.command()
async def setprivateusdt(ctx, key, password):
    """Set the usdtmatic private key with password"""
    global usdtprivate, privatepass

    usdtprivate = key
    privatepass = password

    await ctx.message.edit(ui.ok("Private USDT key set with password"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Set litecoin private key
@client.command()
async def setprivateltc(ctx, key, password):
    """Set the litecoin private key with password"""
    global ltcprivate, privatepass

    ltcprivate = key
    privatepass = password

    await ctx.message.edit(ui.ok("Private LTC key set with password"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Reset litecoin private key
@client.command()
async def resetprivate(ctx):
    """Reset the litecoin private key"""
    await reset_private_litecoin()
    await ctx.message.edit(ui.ok("Private litecoin key reset"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Set Tatum API key
@client.command()
async def settatum(ctx, key):
    """Set the Tatum API key."""
    global tatumAPIkey

    tatumAPIkey = key  # en mémoire uniquement (persister via TATUM_API_KEY dans .env)
    register_secret(key)

    await ctx.message.edit(ui.ok("Tatum API key set"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# Produit en croix pour convertir les devises
@client.command()
async def croix(ctx, price: float = 0.0, amount: int = 0):
    """Cross product to convert currencies."""
    if price == 0 or amount == 0:
        await ctx.message.edit(ui.err("Please enter a valid amount and price"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    converted = round((1000 * price) / amount, 2)
    await ctx.message.edit(ui.card("Cross product", [("Rate", f"`{converted}€ / k`")], icon="✖️"))
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

# To convert eur to litecoin
@client.command()
async def eur(ctx, amount):
    """Convert EUR to Litecoin."""
    try:
        amount = float(amount)
    except ValueError:
        await ctx.message.edit(ui.err("Please enter a valid number"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Get the current price of Litecoin
    price = await get_crypto_info('litecoin', 'eur')
    if price:
        ltc = amount / price
        usd = amount * 1.10
        await ctx.message.edit(ui.card(f"{amount} €", [("💵 USD", f"`{round(usd,3)} $`"), ("🪙 LTC", f"`{round(ltc,3)} LTC`")], icon="💶"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data"))
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

# To convert usd to litecoin
@client.command()
async def usd(ctx, amount):
    """Convert USD to Litecoin."""
    try:
        amount = float(amount)
    except ValueError:
        await ctx.message.edit(ui.err("Please enter a valid number"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Get the current price of Litecoin
    price = await get_crypto_info('litecoin', 'usd')
    if price:
        ltc = amount / price
        eur = amount * 0.90
        await ctx.message.edit(ui.card(f"{amount} $", [("💶 EUR", f"`{round(eur,3)} €`"), ("🪙 LTC", f"`{round(ltc,3)} LTC`")], icon="💵"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data"))
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

# To convert litecoin to usd and eur
@client.command()
async def ltc(ctx, amount : str = 1):
    """Convert Litecoin to USD and EUR."""
    try:
        amount = float(amount)
    except ValueError:
        await ctx.message.edit(ui.err("Please enter a valid number"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    # Get the current price of Litecoin
    price_usd = await get_crypto_info('litecoin', 'usd')
    price_eur = await get_crypto_info('litecoin', 'eur')

    if isinstance(price_usd, str) or isinstance(price_eur, str):
        await ctx.message.edit(ui.err("An error occurred while fetching the data. Please try again later"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if price_usd and price_eur:
        usd = amount * price_usd
        eur = amount * price_eur
        await ctx.message.edit(ui.card(f"{amount} LTC", [("💶 EUR", f"`{round(eur,3)} €`"), ("💵 USD", f"`{round(usd,3)} $`")], icon="🪙"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data"))
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

LTC_EMOJI = "<:litecoin:1147175971531784192>"

# To convert Paypal to Litecoin
@client.command()
async def cvt(ctx, amount):
    """Convert Paypal to Litecoin."""
    try:
        amount = float(amount)
    except ValueError:
        await ctx.message.edit(ui.err("Please enter a valid number"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    def ppl_card(received):
        return ui.card("PayPal → Litecoin", [("<:paypal:1147175941274087504> PayPal", f"`{amount}€`"), (f"{LTC_EMOJI} Litecoin", f"`{round(received, 3)}€`")], icon="💱")

    if amount <= 10:
        await ctx.message.edit(ppl_card(amount - 1.00))
    elif amount > 10 and amount <= 25:
        await ctx.message.edit(ppl_card(amount * 0.90))
    elif amount > 25 and amount <= 50:
        await ctx.message.edit(ppl_card(amount * 0.91))
    elif amount > 50:
        await ctx.message.edit(ppl_card(amount * 0.92))

# To convert any currency
@client.command()
async def cv(ctx, amount: str, currency1: str, currency2: str):
    curr1 = currency1.upper()
    curr2 = currency2.upper()
    url = f"https://v6.exchangerate-api.com/v6/{exchangerateAPIkey}/pair/{curr1}/{curr2}/{amount}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                if data['result'] == 'success':
                    converted = float(amount) * data['conversion_rate']
                    await ctx.message.edit(ui.card("Currency converter", [("From", f"`{amount} {curr1}`"), ("To", f"`{round(converted,2)} {curr2}`")], icon="💱"))
                else:
                    await ctx.message.edit(ui.err("An error occurred while fetching the data"))
            else:
                await ctx.message.edit(ui.err("An error occurred while fetching the data"))

# To get the current price of a cryptocurrency
@client.command()
async def crypto(ctx, symbol):
    """Get the current price of a cryptocurrency."""
    price = await get_crypto_info(symbol, 'usd')
    if price:
        await ctx.message.edit(ui.card(symbol.upper(), [("Price", f"`{price} $`")], icon="📈"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data"))
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

# To get the balance of a Litecoin address
@client.command()
async def bal(ctx, address):
    """Get the balance of a Litecoin address."""
    balance = await get_ltc_balance(address)
    ltcprice = await get_crypto_info('litecoin', 'eur')
    if balance:
        await ctx.message.edit(ui.card("Litecoin wallet", [
            ("<:member:1325187883887366174> Target", f"`{address}`"),
            ("💰 Balance", f"`{balance['balance'] / 100000000} LTC`"),
            ("💶 Value", f"`{round(balance['balance'] / 100000000 * ltcprice, 3)}€`"),
            ("⏳ Incoming", f"`{balance['unconfirmed_balance'] / 100000000} LTC`"),
            ("✅ Final", f"`{balance['final_balance'] / 100000000} LTC`"),
        ], icon="<:litecoin:1147175971531784192>"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data", f"{balance}"))

# To get my Litecoin balance
@client.command()
async def mybal(ctx):
    """Get the balance of a Litecoin address."""
    balance = await get_ltc_balance(ltc_address)
    ltcprice = await get_crypto_info('litecoin', 'eur')
    if balance:
        await ctx.message.edit(ui.card("Litecoin wallet", [
            ("<:member:1325187883887366174> Target", "Me"),
            ("💰 Balance", f"`{balance['balance'] / 100000000} LTC`"),
            ("💶 Value", f"`{round(balance['balance'] / 100000000 * ltcprice, 3)}€`"),
            ("⏳ Incoming", f"`{balance['unconfirmed_balance'] / 100000000} LTC`"),
            ("✅ Final", f"`{balance['final_balance'] / 100000000} LTC`"),
        ], icon="<:litecoin:1147175971531784192>"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data", f"{balance}"))

# To get USDT balance
@client.command()
async def usdtbal(ctx, address: str = usdt_address):
    """Get the USDT balance."""
    balance = await get_bal_usdt(address)
    if balance:
        await ctx.message.edit(ui.card("USDT wallet", [("<:member:1325187883887366174> Target", f"`{address}`"), ("💰 Balance", f"`{balance['balance']} USDT`")], icon="<:Tether_USDT:1348735625389412486>"))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data", f"{balance}"))

@client.command()
async def sendu(ctx, address: str = None, amount: str = None):
    """
    Send USDT (Polygon) from wallet to another address.
    Usage: ;sendu <address> <amount>
    Examples:
    ;sendu USDT_ADDRESS 10
    ;sendu USDT_ADDRESS all
    """
    global privatepass, usdtprivate, transactionChannel, usdt_address

    # Input validation
    if not address or not amount:
        await ctx.message.edit(ui.err(f"Usage: `{PREFIX}sendu <address> <amount|all>`"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if not usdtprivate:
        await ctx.message.edit(ui.err("Please set the private key before sending USDT (Polygon)"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    try:
        # Get current balance
        balance = await get_bal_usdt(usdt_address)
        if not balance:
            await ctx.message.edit(ui.err("Failed to fetch wallet balance"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        usdt_balance = balance['balance']

        if amount.lower() == 'all':
            amount = usdt_balance
        else:
            amount = float(amount)

        # Validate amount
        if amount <= 0:
            raise ValueError("Amount must be greater than 0")

        if amount > usdt_balance:
            await ctx.message.edit(ui.err("Insufficient funds", f"Balance · `{usdt_balance} USDT`\nRequired · `{amount} USDT`"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        # Confirmation message
        confirm_msg = ui.card("Confirm transaction", [("From", f"`{usdt_address}`"), ("To", f"`{address}`"), ("Amount", f"`{amount} USDT`")],
                              icon="<:Tether_USDT:1348735625389412486>",
                              desc=ui.wait(f"Type your private password in {client.get_channel(transactionChannel).mention} (30s)"))

        await ctx.message.edit(confirm_msg)

        # Wait for confirmation
        try:
            msg = await client.wait_for(
                'message',
                timeout=30,
                check=lambda m: m.author == ctx.author and m.channel == client.get_channel(transactionChannel)
            )
        except asyncio.TimeoutError:
            await ctx.message.edit(ui.err("Transaction timed out"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        if privatepass and hmac.compare_digest(msg.content.encode(), privatepass.encode()):
            await msg.edit(ui.wait("**Transaction confirmed - Processing…**"))
            
            # Execute transaction
            tx = await send_usdt_matic(address, amount, usdtprivate)

            if not is_error_result(tx):
                success_msg = ui.card("Transaction successful", [("<:member:1325187883887366174> To", f"`{address}`"), ("💰 Amount", f"`{amount} USDT`"),
                                                         ("🔎 Explorer", f"[polygonscan](https://polygonscan.com/tx/0x{str(tx).removeprefix('0x')})")],
                              icon="✅")
                await send_message(transactionChannel, success_msg)
                await asyncio.sleep(1)
                await ctx.message.edit(success_msg)
            else:
                reason = tx[0].get("error") if isinstance(tx, tuple) and isinstance(tx[0], dict) else (tx.get("error") if isinstance(tx, dict) else "unknown")
                await ctx.message.edit(ui.err(f"Transaction failed: {str(reason)[:200]}"))

            await asyncio.sleep(2)
            await msg.delete()
        else:
            await msg.edit(ui.err("Invalid password - Transaction cancelled"))
            await asyncio.sleep(deleteTime)
            await msg.delete()
            await ctx.message.delete()

    except ValueError as e:
        await ctx.message.edit(ui.err(f"Invalid amount: {str(e)}"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    except Exception as e:
        await ctx.message.edit(ui.err(f"An error occurred: {str(e)}"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()

# To send litecoin from my wallet to another address
@client.command()
async def send(ctx, address: str = None, amount: str = None):
    """
    Send Litecoin from wallet to another address.
    Usage: ;send <address> <amount>[€/$]
    Examples:
    ;send LTC_ADDRESS 0.1
    ;send LTC_ADDRESS 10€
    ;send LTC_ADDRESS 10$
    ;send LTC_ADDRESS all
    """
    global privatepass, ltcprivate, transactionChannel, ltc_address

    # Input validation
    if not address or not amount:
        await ctx.message.edit(ui.err(f"Usage: `{PREFIX}send <address> <amount>[€/$|all]`"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    if not ltcprivate:
        await ctx.message.edit(ui.err("Please set the private key before sending Litecoin"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    tc_channel = client.get_channel(transactionChannel)
    if not tc_channel:
        await ctx.message.edit(ui.err("Transaction channel not configured or inaccessible"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
        return

    try:
          # Get current balance and rates
        balance = await get_ltc_balance(ltc_address)
        if not balance:
            await ctx.message.edit(ui.err("Failed to fetch wallet balance"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        ltc_balance = balance['balance'] / 100000000
        ltceur = await get_crypto_info('litecoin', 'eur')
        ltcusd = await get_crypto_info('litecoin', 'usd')

        # Convert to float if they are strings and handle errors
        try:
            if isinstance(ltceur, str):
                ltceur = float(ltceur)
            elif not isinstance(ltceur, (int, float)):
                ltceur = 0.0
        except (ValueError, TypeError):
            await ctx.message.edit(ui.err("Failed to fetch LTC/EUR price"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        try:
            if isinstance(ltcusd, str):
                ltcusd = float(ltcusd)
            elif not isinstance(ltcusd, (int, float)):
                ltcusd = 0.0
        except (ValueError, TypeError):
            await ctx.message.edit(ui.err("Failed to fetch LTC/USD price"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        # Convert amount based on currency symbol
        if '€' in amount or 'e' in amount:
            if ltceur > 0:
                amount = round(float(amount.replace('€', '').replace('e', '').strip()) / ltceur, 8)
            else:
                await ctx.message.edit(ui.err("Invalid LTC/EUR rate"))
                await asyncio.sleep(deleteTime)
                await ctx.message.delete()
                return
        elif '$' in amount:
            if ltcusd > 0:
                amount = round(float(amount.replace('$', '').strip()) / ltcusd, 8)
            else:
                await ctx.message.edit(ui.err("Invalid LTC/USD rate"))
                await asyncio.sleep(deleteTime)
                await ctx.message.delete()
                return
        elif amount.lower() == 'all':
            amount = round(ltc_balance - 0.00001, 8)  # Leave some for fee
        else:
            amount = round(float(amount), 8)

        # Validate amount
        if amount <= 0:
            raise ValueError("Amount must be greater than 0")

        if amount > ltc_balance:
            await ctx.message.edit(ui.err("Insufficient funds", f"Balance · `{ltc_balance} LTC`\nRequired · `{amount} LTC`"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        # Confirmation message
        confirm_msg = ui.card("Confirm transaction", [("From", f"`{ltc_address}`"), ("To", f"`{address}`"), ("Amount", f"`{amount} LTC`"),
                                                      ("💶 EUR", f"`{round(amount * ltceur, 2)}€`"), ("💵 USD", f"`{round(amount * ltcusd, 2)}$`")],
                              icon="<:litecoin:1147175971531784192>",
                              desc=ui.wait(f"Type your private password in {client.get_channel(transactionChannel).mention} (30s)"))

        await ctx.message.edit(confirm_msg)

        # Wait for confirmation
        try:
            msg = await client.wait_for(
                'message',
                timeout=30,
                check=lambda m: m.author == ctx.author and m.channel == client.get_channel(transactionChannel)
            )
        except asyncio.TimeoutError:
            await ctx.message.edit(ui.err("Transaction timed out"))
            await asyncio.sleep(deleteTime)
            await ctx.message.delete()
            return

        if privatepass and hmac.compare_digest(msg.content.encode(), privatepass.encode()):
            await msg.edit(ui.wait("**Transaction confirmed - Processing…**"))
            
            # Execute transaction
            tx = await send_ltc(ltc_address, address, amount)

            if tx:
                success_msg = ui.card("Transaction successful", [("<:member:1325187883887366174> To", f"`{address}`"), ("💰 Amount", f"`{amount} LTC`"),
                                                         ("💶 EUR", f"`{round(amount * ltceur, 2)}€`"), ("💵 USD", f"`{round(amount * ltcusd, 2)}$`"),
                                                         ("🔎 TX", f"[{str(tx)[:12]}…](https://live.blockcypher.com/ltc/tx/{tx})")],
                              icon="✅")
                await send_message(transactionChannel, success_msg)
                await asyncio.sleep(1)
                await ctx.message.edit(success_msg)
            else:
                await ctx.message.edit(ui.err("Transaction failed"))

            await asyncio.sleep(2)
            await msg.delete()
        else:
            await msg.edit(ui.err("Invalid password - Transaction cancelled"))
            await asyncio.sleep(deleteTime)
            await msg.delete()
            await ctx.message.delete()

    except ValueError as e:
        await ctx.message.edit(ui.err(f"Invalid amount: {str(e)}"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()
    except Exception as e:
        await ctx.message.edit(ui.err(f"An error occurred: {str(e)}"))
        await asyncio.sleep(deleteTime)
        await ctx.message.delete()

async def send_ltc(ltcaddy, address, amount):
    # URL de l'endpoint Tatum pour les transactions Litecoin
    url = "https://api.tatum.io/v3/litecoin/transaction"

    # Paramètres pour la transaction
    payload = {
        "fromAddress": [
            {
                "address": ltcaddy,  # Remplacez par votre adresse Litecoin source
                "privateKey": ltcprivate  # Remplacez par la clé privée associée à cette adresse
            }
        ],
        "to": [
            {
                "address": address,  # Remplacez par l'adresse de destination
                "value": float(amount)  # Remplacez par le montant en LTC
            }
        ],        "fee": "0.00005",
        "changeAddress": ltcaddy
    }

    # En-têtes HTTP
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": tatumAPIkey
    }

    # Requête POST pour créer la transaction
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    transaction = await response.json()
                    print("Transaction créée avec succès :", transaction)
                    return transaction['txId']
                elif response.status == 402:
                    print("Erreur: Credits insuffisants")
                    return None
                else:
                    print(f"Erreur lors de la création de la transaction : {response.status}\n{await response.text()}")
                    return None
    except aiohttp.ClientResponseError as e:
        print(f"Erreur lors de la création de la transaction : {e}")
        print(f"Response content: {e.response.content.decode()}")
        return None

async def get_bal_usdt(address: str = usdt_address):
    """Get USDT balance on Polygon network."""
    # Polygon RPC URL
    w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))

    # USDT Contract ABI (only balanceOf function needed)
    abi = json.loads('''[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]''')

    # USDT contract address on Polygon
    contract_address = '0xc2132D05D31c914a87C6611C10748AEb04B58e8F'

    try:
        # Create contract instance
        contract = w3.eth.contract(address=contract_address, abi=abi)

        # Get balance
        balance = contract.functions.balanceOf(address).call()
        balance_usdt = balance / 1e6  # Convert from smallest unit (6 decimals for USDT)

        return {
            'balance': balance_usdt,
            'address': address,
            'currency': 'USDT'
        }
    except Exception as e:
        return {"error": f"Request failed: {str(e)}"}

async def send_usdt_matic(to_address: str, amount: float, from_private_key: str):
    """Send USDT on Polygon (web3 est synchrone: exécuté dans un thread)."""
    return await asyncio.to_thread(_send_usdt_matic_sync, to_address, amount, from_private_key)

def _send_usdt_matic_sync(to_address: str, amount: float, from_private_key: str):
    # Initialize Web3 with Polygon RPC
    w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))

    # USDT Contract info
    contract_address = w3.to_checksum_address('0xc2132D05D31c914a87C6611C10748AEb04B58e8F')
    abi = json.loads('''[{
        "constant": false,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }]''')

    try:
        # Create contract instance
        contract = w3.eth.contract(address=contract_address, abi=abi)

        # Get account from private key
        account = w3.eth.account.from_key(from_private_key)

        # Convert amount to USDT units (6 decimals)
        amount_in_units = int(float(amount) * 1e6)

        # Convert to_address to checksum address
        to_address = w3.to_checksum_address(to_address)

        # Build transaction
        nonce = w3.eth.get_transaction_count(account.address)

        # Get current gas price
        gas_price = w3.eth.gas_price

        # Create transaction
        transaction = contract.functions.transfer(
            to_address,
            amount_in_units
        ).build_transaction({
            'chainId': 137,  # Polygon Mainnet
            'from': account.address,
            'nonce': nonce,
            'gasPrice': gas_price,  # Use gasPrice instead of maxFeePerGas
            'gas': 100000
        })

        # Sign and send transaction
        signed_txn = w3.eth.account.sign_transaction(transaction, from_private_key)
        # print("Sending transaction...", signed_txn)
        tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)

        txid = tx_hash.hex()

        # Wait for transaction receipt
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"Transaction confirmed in block {receipt['blockNumber']}")

        return txid

    except Exception as e:
        print(f"Error sending USDT: {str(e)}")
        return {"error": str(e)}

async def ChangeStatus(message, status = "online"):
    headers = {
        "Authorization": f"{TOKEN}",
        "User-Agent": get_user_agent(),
        "Content-Type": "application/json",
        "Accept": "*/*"
    }

    jsonData = {
        "status": status,
        "custom_status": {
            "emoji_name": "NoName",
            "emoji_id": "1115316607535812648",
            "text": message,
    }}
    async with aiohttp.ClientSession() as session:
        async with session.patch("https://discord.com/api/v8/users/@me/settings", headers=headers, json=jsonData) as response:
            return response.status

async def ChangeActivity(message):
    await client.change_presence(activity=discord.Game(name=message))

# Test - tltc cb518e29e4f315f40c5c14e1f16f77819cc2fda8a04a6321db17dc8331c3d14d
# To get the details of a Litecoin transaction
@client.command()
async def tltc(ctx, transaction_hash):
    """Get details of a Litecoin transaction."""
    global api_token

    transaction = await get_ltc_transaction_detail(api_token, transaction_hash)
    if isinstance(transaction, dict):
        allReceiversWithAmount = [f"**{output['addresses'][0]} got** `{output['value'] / ltcconvert} LTC`" for output in transaction['outputs']]
        receivers = ""
        for receiver in allReceiversWithAmount:
            if receiver == allReceiversWithAmount[len(allReceiversWithAmount) - 1]:
                receivers = receivers + receiver
            else:
                receivers = receivers + receiver + "\n"
        await ctx.message.edit(ui.card("Litecoin transaction", [("Total", f"`{transaction['outputs'][0]['value'] / ltcconvert} LTC`"), ("Confirmations", f"`{transaction['confirmations']}`")], icon="<:litecoin:1147175971531784192>", desc=ui.bullets(receivers.split("\n"))))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data"))
        print(f"Error fetching transaction details: {transaction}")

@client.command()
async def alltx(ctx, target: str = None, type: str = "send", address: str = ltc_address):
    """Get the last transactions receive of a Litecoin address."""
    transactions = await all_transactions(address, target, type)
    if transactions:
        for transaction in transactions:
            if int(transaction['time']) > 1000000000000:
                transaction['time'] = round(datetime.now().timestamp())
        ltcprice = await get_crypto_info('litecoin', 'eur')
        allTransactions = [f"> • [`{transaction['hash'][:10]}…`](https://live.blockcypher.com/ltc/tx/{transaction['hash']}) · `{transaction['amount']} LTC` · `{round(transaction['amount'] * ltcprice, 3)}€` · <t:{transaction['time']}:R>" for transaction in transactions]
        lastTransactions = ""
        for transaction in allTransactions:
            if transaction == allTransactions[len(allTransactions) - 1]:
                lastTransactions = lastTransactions + transaction
            else:
                lastTransactions = lastTransactions + transaction + "\n"
        await ctx.message.edit(ui.card("Address transactions", icon="<:litecoin:1147175971531784192>", desc=lastTransactions))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data", f"{transactions}"))
        print(f"Error fetching transactions: {transactions}")
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

@client.command()
async def lasttx(ctx, address: str = ltc_address):
    """Get the last transactions receive of a Litecoin address."""
    transactions = await last_transactions(address)
    if transactions:
        for transaction in transactions:
            if int(transaction['time']) > 1000000000000:
                transaction['time'] = round(datetime.now().timestamp())
        ltcprice = await get_crypto_info('litecoin', 'eur')
        allTransactions = [f"> • [`{transaction['hash'][:10]}…`](https://live.blockcypher.com/ltc/tx/{transaction['hash']}) · `{transaction['amount']} LTC` · `{round(transaction['amount'] * ltcprice, 3)}€` · <t:{transaction['time']}:R>" for transaction in transactions]
        lastTransactions = ""
        for transaction in allTransactions:
            if transaction == allTransactions[len(allTransactions) - 1]:
                lastTransactions = lastTransactions + transaction
            else:
                lastTransactions = lastTransactions + transaction + "\n"
        await ctx.message.edit(ui.card("Address transactions", icon="<:litecoin:1147175971531784192>", desc=lastTransactions))
    else:
        await ctx.message.edit(ui.err("An error occurred while fetching the data", f"{transactions}"))
        print(f"Error fetching transactions: {transactions}")
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

@client.command()
async def waittx(ctx, address: str = ltc_address):
    """Wait for a new transaction on a Litecoin address."""
    global waitingTx

    await ctx.message.edit(ui.card("Address transactions", icon="<:litecoin:1147175971531784192>", desc=ui.wait("Waiting for new transactions…")))

    waitingTx = True

    # First await the transactions
    initial_transactions = await last_transactions(address)
    if not initial_transactions:
        print("Error fetching transactions")
        return

    lastHash = initial_transactions[0]['hash']

    while waitingTx:
        transactions = await last_transactions(address)
        if transactions and transactions[0]['hash'] != lastHash:
            for transaction in transactions:
                if int(transaction['time']) > 1000000000000:
                    transaction['time'] = round(datetime.now().timestamp())
            ltcprice = await get_crypto_info('litecoin', 'eur')
            allTransactions = [f"> • [`{transaction['hash'][:10]}…`](https://live.blockcypher.com/ltc/tx/{transaction['hash']}) · `{transaction['amount']} LTC` · `{round(transaction['amount'] * ltcprice, 3)}€` · <t:{transaction['time']}:R>" for transaction in transactions]
            lastTransactions = ""
            for transaction in allTransactions:
                if transaction == allTransactions[len(allTransactions) - 1]:
                    lastTransactions = lastTransactions + transaction
                else:
                    lastTransactions = lastTransactions + transaction + "\n"
            waitingTx = False
            await ctx.message.edit(ui.card("New transaction", icon="<:litecoin:1147175971531784192>", desc=lastTransactions, footer=client.user.mention))
            await ackMessage(ctx.message)
        await asyncio.sleep(30)
    await asyncio.sleep(deleteTimeLong)
    await ctx.message.delete()

@client.command()
async def stopwaittx(ctx):
    global waitingTx

    await ctx.message.edit(ui.card("Address transactions", icon="<:litecoin:1147175971531784192>", desc=ui.info("Stopped waiting for new transactions")))
    waitingTx = False
    await asyncio.sleep(deleteTime)
    await ctx.message.delete()

async def last_transactions(address: str = ltc_address, page_size: int = 10):
    # URL de l'endpoint Tatum pour récupérer les transactions d'une adresse Litecoin
    url = f'https://api.tatum.io/v3/litecoin/transaction/address/{address}?pageSize={page_size}&sort=desc'

    # En-têtes de la requête avec la clé API
    headers = {
        'x-api-key': tatumAPIkey
    }

    # Effectuer la requête GET
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            # Vérifier si la requête a réussi
            if response.status == 200:  # Changed from status_code to status
                transactions = await response.json()  # Added await
                received_transactions = [
                    {
                        "hash": tx["hash"],
                        "amount": float(output["value"]),
                        "time": tx["time"]
                    }
                    for tx in transactions
                    if not any(inp["coin"].get("address") == address for inp in tx.get("inputs", set()))
                    for output in tx["outputs"]
                    if output.get("address") == address
                ]
                return received_transactions
            else:
                return f"Erreur: {response.status}, {await response.text()}"  # Changed status_code to status

async def all_transactions(address: str = ltc_address, target: str = None, type: str = "send"):
    # URL de l'endpoint Tatum pour récupérer les transactions d'une adresse Litecoin
    url = f'https://api.tatum.io/v3/litecoin/transaction/address/{address}?pageSize=50&sort=desc'

    # En-têtes de la requête avec la clé API
    headers = {
        'x-api-key': tatumAPIkey
    }

    # Effectuer la requête GET
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            # Vérifier si la requête a réussi
            if response.status == 200:  # Changed from status_code to status
                transactions = await response.json()  # Added await

                if type == "send":
                    received_transactions = [
                        {
                            "hash": tx["hash"],
                            "amount": float(output["value"]),
                            "time": tx["time"]
                        }
                        for tx in transactions
                        if any(inp["coin"].get("address") == address for inp in tx.get("inputs", set()))
                        for output in tx["outputs"]
                        if output.get("address") == target
                    ]
                elif type == "receive":
                    received_transactions = [
                        {
                            "hash": tx["hash"],
                            "amount": float(output["value"]),
                            "time": tx["time"]
                        }
                        for tx in transactions
                        if any(inp["coin"].get("address") == target for inp in tx.get("inputs", set()))
                        for output in tx["outputs"]
                        if output.get("address") == address
                    ]
                return received_transactions
            else:
                return f"Erreur: {response.status}, {await response.text()}"  # Changed status_code to status

async def reset_private_litecoin():
    global ltcprivate, privatepass

    ltcprivate = None
    privatepass = None

async def build_guild_ticket_map():
    """Build a dictionary mapping guild_id -> ticket_channel_id"""
    global guild_ticket_map, ticketChannels

    guild_ticket_map.clear()

    for ticket_channel_id in ticketChannels:
        channel = client.get_channel(int(ticket_channel_id))
        if channel and channel.guild:
            guild_id = channel.guild.id
            guild_ticket_map[guild_id] = int(ticket_channel_id)
            printdebug(f"Mapped guild {guild_id} -> ticket channel {ticket_channel_id}", "INFO")

async def foundticket(guild):
    for ticketChannel in ticketChannels:
        if not client.get_channel(int(ticketChannel)):
            deleteList.add(ticketChannel)
            print(f'Ticket channel **{ticketChannel}** has been removed from the list of ticket channels, foundticket.')
            # await send_message(msgChannel, f'Ticket channel **{ticketChannel}** has been removed from the list of ticket channels, foundticket.')
        elif client.get_channel(int(ticketChannel)).guild.id == int(guild):
            return ticketChannel
    return None

async def foundguild(guild):
    for ticketChannel in ticketChannels:
        if not client.get_channel(int(ticketChannel)):
            deleteList.add(ticketChannel)
            print(f'Ticket channel **{ticketChannel}** has been removed from the list of ticket channels, foundguild.')
            # await send_message(msgChannel, f'Ticket channel **{ticketChannel}** has been removed from the list of ticket channels, foundguild.')
        elif client.get_channel(int(ticketChannel)).guild.id == int(guild):
            return True
    return False

async def foundNukedChannel(guildId, channelName):
    global addtosnipe
    loop = 0
    while loop < 10:
        guildChannels = client.get_guild(int(guildId)).channels
        loop += 1
        for channel in guildChannels:
            if channel.category is not None and channel.category in deletedCategory:
                # print(f"{loop} - Checking channel: {channel.name} - {channelName}")
                if channel.name == channelName:
                    deletedCategory.remove(channel.category)
                    stocksChannels.add(int(channel.id))
                    if addtosnipe:
                        snipeChannels.add(int(channel.id))
                        addtosnipe = False
                    return True, channel.id
        await asyncio.sleep(1)
    return False, None

async def ackMessage(message):
    headers={
        "accept": "*/*",
        "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": f"{TOKEN}",
        "content-type": "application/json",
        "priority": "u=1, i",
        "sec-ch-ua": "\"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", \"Google Chrome\";v=\"128\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-debug-options": "bugReporterEnabled",
        "x-discord-locale": "en-US",
        "x-discord-timezone": "Europe/Paris",
    }

    json={
        "manual": True,
        "mention_count": 1
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(f"https://discord.com/api/v9/channels/{message.channel.id}/messages/{message.id}/ack", headers=headers, json=json) as response:
            if response:
                return True
            else:
                return False

async def weedSimple(ctx):
    await ctx.message.edit(ui.card("Simple Roleplay · Weed", [("Status", "⏳ Ready in **11 minutes**")], icon="🌿"))
    await asyncio.sleep(660)
    await ctx.message.edit(ui.card("Simple Roleplay · Weed", [("Status", "✅ **Ready!**"), ("Since", f"<t:{round(datetime.now().timestamp())}:R>")], icon="🌿", footer=client.user.mention))
    await ackMessage(ctx.message)

def getGuilds():
    for stock in stocksChannels:
        if client.get_channel(int(stock)):
            addGuild(client.get_channel(int(stock)).guild.id)

def addGuild(id):
    global guildsId
    if id not in guildsId:
        guildsId.add(id)
        #print(f'{id} has been added to guildsId')

def _set_name(file):
    return os.path.splitext(os.path.basename(str(file)))[0]

def file_to_table(file, table):
    """Charge un ensemble d'IDs depuis SQLite (import unique de l'ancien .txt s'il existe)."""
    db = _db()
    name = _set_name(file)
    if not db.get_set(name):
        db.import_txt(name, file)
    table.update(db.get_set(name))

def table_to_file(file, table):
    """Persiste l'ensemble d'IDs dans SQLite (plus de suppression/réécriture de .txt)."""
    _db().replace_set(_set_name(file), table)

async def get_ltc_balance(address):
    global api_token

    base_url = "https://api.blockcypher.com/v1/ltc/main"
    endpoint = f"/addrs/{address}/balance"

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Token {api_token}'
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url + endpoint, headers=headers) as response:
                data = await response.json()

                if 'error' in data:
                    return 0

                return data
    except Exception as e:
        return f"Erreur lors de la requête à l'API : {str(e)}"

# https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=eur
async def get_crypto_info(symbol, currency):
    # Faites une requête à l'API de cryptomonnaie pour obtenir les informations spécifiques à la cryptomonnaie
    api_url = f'https://api.coingecko.com/api/v3/simple/price?ids={symbol.lower()}&vs_currencies={currency.lower()}'

    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as response:
            data = await response.json()
            try:
                # Vérifier si la requête a réussi
                if data[symbol.lower()][currency.lower()] is not None:
                    # Retourner le prix de la cryptomonnaie
                    if 'error' not in data:
                        return float(data[symbol.lower()][currency.lower()])
                    return None
                else:
                    return None
            except Exception as e:
                return f"Erreur lors de la requête à l'API : {str(e)}"

async def get_ltc_transaction_detail(api_token, transaction_hash):
    base_url = "https://api.blockcypher.com/v1/ltc/main"
    endpoint = f"/txs/{transaction_hash}"

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Token {api_token}'
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url + endpoint, headers=headers) as response:
                data = await response.json()

                if 'error' in data:
                    return 0

                return data

    except Exception as e:
        return f"Erreur lors de la requête à l'API : {str(e)}"

async def check_code(nitro):
    global proxies_enabled
    """Uses discord API to check code"""
    # proxies_enabled = True
    if nitro.startswith("https://discord.gift/") or nitro.startswith("https://discord.com/gifts/"):
        just_code = nitro.split("/")[len(nitro.split("/"))-1]
    else:
        just_code = nitro
    api_url = f"https://discord.com/api/v9/entitlements/gift-codes/{just_code}?with_application=true&with_subscription_plan=true"
    if not just_code in checkedCodes:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=5) as response:
                    data = await response.json()
                    if response.status == 200:
                        checkedCodes.add(just_code)
                        if data['uses'] == 0:
                            return True
                        elif data['uses'] != 0:
                            return False
                    elif response.status == 429:
                        print(f"Rate limited.")
                        if not proxies_enabled:
                            proxies_enabled = True
        except Exception as e:
            print(f"Error : {e}")
            return False

async def get_code_info(code):
    """Get information about a nitro code using discord API"""
    global proxies_enabled
    # proxies_enabled = True
    api_url = f"https://discord.com/api/v9/entitlements/gift-codes/{code}?with_application=true&with_subscription_plan=true"

    headers = {
        "Authorization": f"{TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": get_user_agent()
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                elif response.status == 429:
                    print(f"Rate limited.")
                    if not proxies_enabled:
                        proxies_enabled = True
                else:
                    return None
    except Exception as e:
        print(f"Error getting code info: {str(e)}")
        return None

def redeem_card(code_name, code, message, code_author, redeem_time):
    """Notification d'un code Nitro utilisé."""
    return ui.card(code_name, [("Code", f"`{code}`"), ("User", message.author.mention), ("Channel", code_author),
                               ("Time", f"`{redeem_time}s`"), ("Status", "✅ Redeemed")],
                   icon="💎", desc=f"```{message.content.replace('`', '')[:500]}```", footer=client.user.mention)

async def redeem_code(code, message):
    global proxies_enabled
    """Redeems a nitro code using discord API"""
    # proxies_enabled = True

    start_time = time.time()

    api_url = f"https://discord.com/api/v9/entitlements/gift-codes/{code}/redeem"

    headers = {
        "Authorization": f"{CUSTOM_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": get_user_agent()
    }

    data = {
        "channel_id": message.channel.id
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=data, headers=headers) as response:
                end_time = time.time()

                redeem_time = round(end_time - start_time, 2)

                # Get the response text instead of raw content
                response_text = await response.text()

                codeInfo = await get_code_info(code)
                codeName = codeInfo['store_listing']['sku']['name'] if codeInfo and 'store_listing' in codeInfo and 'sku' in codeInfo['store_listing'] else "Unknown Code"

                if isinstance(message.channel, discord.DMChannel):
                    codeAuthor = f"{message.author.mention} (DM)"
                else:
                    codeAuthor = f"{message.channel.mention}"

                try:
                    # Try to parse as JSON first
                    response_json = await response.json()

                    if response.status == 200:
                        # Success - code redeemed
                        await send_message(msgChannel, redeem_card(codeName, code, message, codeAuthor, redeem_time))
                    elif response.status == 400:
                        print(f"Code already redeemed or invalid: {code} - {response_json}")
                    elif response.status == 403:
                        print(f"Code is locked or requires payment method: {code} - {response_json}")
                    elif response.status == 404:
                        print(f"Code not found: {code} - {response_json}")
                    else:
                        print(f"Unexpected response: {response.status} - {response_json}")

                except json.JSONDecodeError:
                    if 'nitro' in response_text.lower():
                        await send_message(msgChannel, redeem_card(codeName, code, message, codeAuthor, redeem_time))
                    else:
                        print(f"Unexpected response: {response.status} - {response_text}")

    except Exception as e:
        print(f"Error redeeming code: {str(e)}")

async def scrap_gamepass_price(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.text()
                    soup = BeautifulSoup(data, 'html.parser')
                    price_element = soup.select_one("#item-details > div.clearfix.price-container > div.price-container-text > div.price-info > div > span.text-robux-lg.wait-for-i18n-format-render")

                    if price_element:
                        price = price_element.text.strip()
                        return price
                    else:
                        return "Prix non trouvé"
    except Exception as e:
        print(f"Error : {e}")
        return 0

async def qrfunc(text: str=None):
    if not text:
        return None

    qr = qrcode.make(text)

    img_byte_arr = io.BytesIO()
    qr.save(img_byte_arr)
    img_byte_arr.seek(0)

    # await ctx.send(file=discord.File(img_byte_arr, "qr_code.png"))
    return discord.File(img_byte_arr, "qr_code.png")

def load_config():
    global keywords, excludeKeywords, labelsCheck

    db = _db()
    keywords = set(db.get('keywords', sorted(SETTINGS.filters.keywords)))
    excludeKeywords = set(db.get('excludeKeywords', sorted(SETTINGS.filters.exclude_keywords)))
    labelsCheck = set(db.get('labelsCheck', sorted(SETTINGS.filters.labels_check)))

def printdebug(message: str, level: str = "INFO"):
    """
    Print debug messages when DEBUG mode is enabled.

    Args:
        message: The debug message to print
        level: Debug level (INFO, WARNING, ERROR, SUCCESS)
    """
    global DEBUG

    if not DEBUG:
        return

    # Color codes for terminal output
    colors = {
        "INFO": "\033[94m",      # Blue
        "WARNING": "\033[93m",   # Yellow
        "ERROR": "\033[91m",     # Red
        "SUCCESS": "\033[92m",   # Green
        "RESET": "\033[0m"       # Reset
    }

    color = colors.get(level, colors["INFO"])
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"{color}[{timestamp}] [{level}] {message}{colors['RESET']}")

def _get_flags() -> dict:
    return {"stockSniper": stockSniper, "autoBuy": autoBuy, "autoRedeem": autoRedeem}


def _set_flags(**kw) -> None:
    global stockSniper, autoBuy, autoRedeem
    stockSniper = kw.get("stockSniper", stockSniper)
    autoBuy = kw.get("autoBuy", autoBuy)
    autoRedeem = kw.get("autoRedeem", autoRedeem)


def _export_config() -> dict:
    """Réglages exportables (jamais de token, clé API, adresse ou clé privée)."""
    return {
        "stock_channels": sorted(stocksChannels), "snipe_channels": sorted(snipeChannels),
        "ticket_channels": sorted(ticketChannels), "keywords": sorted(keywords),
        "exclude_keywords": sorted(excludeKeywords), "labels_check": sorted(labelsCheck),
        "max_price": maxprice if isinstance(maxprice, (int, float)) else float(maxprice),
        "auto_mode": autoMode, "snipe_delay": snipeDelay,
    }


def _import_config(c: dict) -> None:
    """Applique un export déjà validé (toolkit.validate_config) et le persiste."""
    global keywords, excludeKeywords, labelsCheck, maxprice, autoMode, snipeDelay
    db = _db()
    keywords, excludeKeywords, labelsCheck = set(c["keywords"]), set(c["exclude_keywords"]), set(c["labels_check"])
    db.set("keywords", sorted(keywords))
    db.set("excludeKeywords", sorted(excludeKeywords))
    db.set("labelsCheck", sorted(labelsCheck))
    for table, key, fname in ((stocksChannels, "stock_channels", "stocks.txt"), (ticketChannels, "ticket_channels", "tickets.txt")):
        table.clear()
        table.update(c[key])
        table_to_file(fname, table)
    snipeChannels.clear()
    snipeChannels.update(c["snipe_channels"])
    if c.get("max_price") is not None:
        maxprice = c["max_price"]
    if c.get("auto_mode"):
        autoMode = c["auto_mode"]
    snipeDelay = c.get("snipe_delay")


async def _swap_balance(coin: str):
    """Solde LTC / USDT (None si indisponible)."""
    if coin == "ltc":
        bal = await get_ltc_balance(ltc_address)
        return bal["balance"] / 1e8 if bal and "balance" in bal else None
    bal = await get_bal_usdt(usdt_address)
    return float(bal["balance"]) if bal and "balance" in bal else None


async def _swap_price(coin_id: str):
    price = await get_crypto_info(coin_id, "usd")
    return price if isinstance(price, (int, float)) and price > 0 else None


async def _swap_send(coin: str, deposit: str, amount: float):
    if coin == "ltc":
        return await send_ltc(ltc_address, deposit, amount)
    return await send_usdt_matic(deposit, amount, usdtprivate)


def _swap_bridge():
    api = SETTINGS.api
    return swapcog.SwapBridge(
        ltc_address=lambda: ltc_address, usdt_address=lambda: usdt_address, privatepass=lambda: privatepass,
        has_key=lambda coin: bool(ltcprivate if coin == "ltc" else usdtprivate),
        transaction_channel=lambda: transactionChannel, send_funds=_swap_send, balance=_swap_balance,
        price_usd=_swap_price, providers=lambda: swap_mod.build_providers(api.swapzone, api.fixedfloat_key, api.fixedfloat_secret),
        provider_pref=lambda: SETTINGS.swap_provider, max_fee=lambda: SETTINGS.swap_max_fee, send_message=send_message,
    )


def _automation_bridge():
    return automation_mod.Bridge(
        token=lambda: TOKEN, prefix=PREFIX, data_dir=SETTINGS.data_dir,
        get_flags=_get_flags, set_flags=_set_flags, export_config=_export_config, import_config=_import_config,
        stats=STATS, ltc_address=lambda: ltc_address, usdt_address=lambda: usdt_address,
        get_ltc_balance=get_ltc_balance, get_usdt_balance=get_bal_usdt, last_transactions=last_transactions,
    )


def run_bot():
    """Valide la configuration puis lance le client. Erreur explicite si le token manque."""
    setup_logging("selfbot", SETTINGS.data_dir / "logs", secrets=[SETTINGS.token, SETTINGS.proxy_url])
    try:
        SETTINGS.validate(require_token=True)
    except ConfigError as e:
        log.error("%s", e)
        print(str(e), file=sys.stderr)
        sys.exit(2)

    load_config()
    log.info("Starting instance %s with token %s", SETTINGS.instance_id, mask_token(SETTINGS.token))
    try:
        client.run(SETTINGS.token)
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down")
    except discord.LoginFailure:
        log.error("Login refusé par Discord: token invalide ou révoqué (%s)", mask_token(SETTINGS.token))
        print("Token invalide ou révoqué: régénérez-le puis mettez à jour DISCORD_TOKEN.", file=sys.stderr)
        sys.exit(2)  # fatal: le manager ne relance pas
    except Exception as e:
        log.error("Error running bot: %s", e, exc_info=True)
        sys.exit(1)
