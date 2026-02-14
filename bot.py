import os
import re
import io
import time
import discord
from discord.ext import commands, tasks
from discord import app_commands

# =========================
# ENV
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or "0")
CLAIM_CATEGORY_ID = int(os.getenv("CLAIM_CATEGORY_ID", "0") or "0")
CUSTOM_CATEGORY_ID = int(os.getenv("CUSTOM_CATEGORY_ID", "0") or "0")
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0") or "0")
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0") or "0")

INACTIVITY_MINUTES = int(os.getenv("INACTIVITY_MINUTES", "60") or "60")
STATUS_ROTATE_SECONDS = int(os.getenv("STATUS_ROTATE_SECONDS", "10") or "10")

AF_LOGO_URL = "https://cdn.discordapp.com/attachments/1430717412944248872/1472312239791931402/af_logo.png"
AF_BLUE = 0x1E90FF  # theme blue

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

# =========================
# BOT
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True  # needed for on_message + history transcripts
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# In-memory state
# (resets if bot restarts; good enough for Railway)
# =========================
ticket_counters = {"claim": 0, "custom": 0, "support": 0}  # scanned on startup
ticket_meta = {}  # channel_id -> dict(owner_id, kind, created_ts, last_activity_ts, claimed_by, priority, first_staff_ts, control_message_id)

STATUS_TEXTS = [
    "🔵 Waiting for staff...",
    "🟡 Processing...",
    "🟢 Staff online",
    "🔷 AF SERVICES Support",
]

# =========================
# HELPERS
# =========================
def safe_name(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\-]", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:20] if s else "user"

def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None

def is_staff(member: discord.Member) -> bool:
    sr = get_staff_role(member.guild)
    return (sr in member.roles) if sr else False

def kind_to_category_id(kind: str) -> int:
    if kind == "claim":
        return CLAIM_CATEGORY_ID
    if kind == "custom":
        return CUSTOM_CATEGORY_ID
    return SUPPORT_CATEGORY_ID

def kind_label(kind: str) -> str:
    return {"claim": "Claim Order", "custom": "Custom Order", "support": "Support"}.get(kind, "Support")

def kind_prefix(kind: str) -> str:
    # You wanted "support-" style for custom/support; claim uses claim-
    if kind == "claim":
        return "claim"
    return "support"

def parse_ticket_number(name: str) -> int | None:
    m = re.search(r"-(\d{4})$", name)
    return int(m.group(1)) if m else None

def make_ticket_name(kind: str, user: discord.abc.User, num: int, priority: str | None = None) -> str:
    uname = safe_name(user.name)
    base = f"{kind_prefix(kind)}-{uname}-{num:04d}"
    # optional emoji prefix for category/priority “color”
    # (Discord categories can't be colored; we simulate color with emoji)
    if priority == "high":
        return f"🔴-{base}"
    if priority == "medium":
        return f"🟡-{base}"
    if priority == "low":
        return f"🟢-{base}"
    # default category hint
    if kind == "claim":
        return f"🔷-{base}"
    return f"🔵-{base}"

def is_ticket_channel(ch: discord.abc.GuildChannel) -> bool:
    return isinstance(ch, discord.TextChannel) and ch.id in ticket_meta

async def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if not TICKET_LOG_CHANNEL_ID:
        return None
    ch = guild.get_channel(TICKET_LOG_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        fetched = await bot.fetch_channel(TICKET_LOG_CHANNEL_ID)
        return fetched if isinstance(fetched, discord.TextChannel) else None
    except Exception:
        return None

async def build_transcript(channel: discord.TextChannel) -> bytes:
    lines = []
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} ({msg.author.id})"
        content = (msg.content or "").replace("\n", "\\n")
        attach = ""
        if msg.attachments:
            attach = " | " + " ".join(a.url for a in msg.attachments)
        lines.append(f"{ts} | {author} | {content}{attach}")
    return ("\n".join(lines)).encode("utf-8", errors="replace")

async def send_transcript_to_log(guild: discord.Guild, channel: discord.TextChannel, close_reason: str, closed_by: str):
    log_ch = await get_log_channel(guild)
    if not log_ch:
        return
    data = await build_transcript(channel)
    f = discord.File(io.BytesIO(data), filename=f"{channel.name}.txt")
    await log_ch.send(
        content=(
            f"🧾 **Ticket Transcript**\n"
            f"Channel: {channel.mention}\n"
            f"Closed by: {closed_by}\n"
            f"Reason: {close_reason}"
        ),
        file=f
    )

def scan_existing_counters(guild: discord.Guild):
    # If you restart, we continue numbers by scanning channels.
    max_seen = {"claim": 0, "custom": 0, "support": 0}
    for ch in guild.text_channels:
        n = parse_ticket_number(ch.name)
        if not n:
            continue
        # infer kind from name prefix
        low = ch.name.lower()
        if "claim-" in low:
            max_seen["claim"] = max(max_seen["claim"], n)
        else:
            # support-... used for both custom/support tickets
            # we don't know which kind, but we keep support/custom counters in sync
            max_seen["custom"] = max(max_seen["custom"], n)
            max_seen["support"] = max(max_seen["support"], n)
    for k in max_seen:
        ticket_counters[k] = max_seen[k]

def find_existing_open_ticket(guild: discord.Guild, kind: str, owner_id: int) -> discord.TextChannel | None:
    for cid, meta in ticket_meta.items():
        if meta.get("owner_id") == owner_id and meta.get("kind") == kind and meta.get("status") == "open":
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                return ch
    return None

# =========================
# EMBEDS
# =========================
def ticket_embed(kind: str, owner_mention: str, claimed_by: str | None, priority: str | None, first_staff_seconds: int | None) -> discord.Embed:
    title = kind_label(kind)
    claimed_text = claimed_by if claimed_by else "This ticket has not been claimed!"
    priority_text = (priority or "not set").upper()

    embed = discord.Embed(
        title=f"{title} ({title})",
        description=(
            "Thank you for purchasing from us.\n"
            "Feel free to add any additional info here.\n\n"
            "**Claimed by**\n"
            f"{claimed_text}\n\n"
            f"**Priority:** {priority_text}\n"
            f"**Owner:** {owner_mention}"
        ),
        color=AF_BLUE
    )

    if priority == "low":
        embed.color = 0x2ECC71
    elif priority == "medium":
        embed.color = 0xF1C40F
    elif priority == "high":
        embed.color = 0xE74C3C

    if first_staff_seconds is not None:
        mins = first_staff_seconds // 60
        secs = first_staff_seconds % 60
        embed.add_field(name="First staff response", value=f"{mins}m {secs}s", inline=False)

    embed.set_author(name="AF SERVICES Tickets", icon_url=AF_LOGO_URL)
    embed.set_thumbnail(url=AF_LOGO_URL)
    embed.set_footer(text="AF SERVICES")
    return embed

# =========================
# UI Components
# =========================
class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Reason for closing",
        style=discord.TextStyle.long,
        required=True,
        max_length=500,
        placeholder="Example: Resolved / Delivered / Duplicate / No response..."
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return

        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Ticket channel not found.", ephemeral=True)
            return

        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return

        await close_ticket(
            channel=ch,
            closed_by=f"{interaction.user} ({interaction.user.id})",
            reason=str(self.reason.value),
            lock_only=True
        )

        await interaction.response.send_message("✅ Ticket closed.", ephemeral=True)

class PrioritySelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        options = [
            discord.SelectOption(label="Low", value="low", emoji="🟢"),
            discord.SelectOption(label="Medium", value="medium", emoji="🟡"),
            discord.SelectOption(label="High", value="high", emoji="🔴"),
        ]
        super().__init__(
            placeholder="Set ticket priority…",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return

        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can set priority.", ephemeral=True)
            return

        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel) or ch.id not in ticket_meta:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        priority = self.values[0]
        ticket_meta[ch.id]["priority"] = priority

        # Rename channel to include emoji “color”
        meta = ticket_meta[ch.id]
        owner_id = meta["owner_id"]
        owner = interaction.guild.get_member(owner_id)
        num = meta["num"]
        new_name = make_ticket_name(meta["kind"], owner or interaction.user, num, priority=priority)
        try:
            await ch.edit(name=new_name)
        except Exception:
            pass

        # Update control message embed
        await refresh_ticket_embed(ch)

        await interaction.response.send_message(f"✅ Priority set to **{priority.upper()}**.", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.add_item(PrioritySelect(channel_id))

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CloseReasonModal(self.channel_id))

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="✋")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return

        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return

        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel) or ch.id not in ticket_meta:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        meta = ticket_meta[ch.id]
        if meta.get("claimed_by"):
            await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)
            return

        meta["claimed_by"] = interaction.user.mention
        await refresh_ticket_embed(ch)

        await interaction.response.send_message("✅ Ticket claimed.", ephemeral=True)

class ReopenView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.success, emoji="🔓")
    async def reopen_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return

        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can reopen tickets.", ephemeral=True)
            return

        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel) or ch.id not in ticket_meta:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        meta = ticket_meta[ch.id]
        meta["status"] = "open"
        meta["last_activity_ts"] = time.time()

        guild = interaction.guild
        sr = get_staff_role(guild)
        owner = guild.get_member(meta["owner_id"])

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if sr:
            overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        await ch.edit(overwrites=overwrites)
        await ch.send("🔓 Ticket reopened.", view=TicketControlView(ch.id))
        await interaction.response.send_message("✅ Reopened.", ephemeral=True)

# =========================
# Ticket Select + Panel
# =========================
class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Claim Order", value="claim", description="Click here to Claim Your Order", emoji="📦"),
            discord.SelectOption(label="Custom Order", value="custom", description="Click here to get a Custom Order", emoji="🛒"),
            discord.SelectOption(label="Issues/Help", value="support", description="Click here for Issues/Help", emoji="🛠️"),
        ]
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        guild = interaction.guild
        user = interaction.user
        kind = self.values[0]

        existing = find_existing_open_ticket(guild, kind, user.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
            return

        cat_id = kind_to_category_id(kind)
        category = guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category is not configured correctly.", ephemeral=True)
            return

        ticket_counters[kind] += 1
        num = ticket_counters[kind]
        ch_name = make_ticket_name(kind, user, num, priority=None)

        sr = get_staff_role(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if sr:
            overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=ch_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        # store meta
        now = time.time()
        ticket_meta[channel.id] = {
            "owner_id": user.id,
            "kind": kind,
            "num": num,
            "created_ts": now,
            "last_activity_ts": now,
            "status": "open",
            "claimed_by": None,
            "priority": None,
            "first_staff_ts": None,
            "control_message_id": None,
        }

        embed = ticket_embed(kind, user.mention, None, None, None)
        msg = await channel.send(content=user.mention, embed=embed, view=TicketControlView(channel.id))
        ticket_meta[channel.id]["control_message_id"] = msg.id

        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())

# =========================
# Admin check + panel command
# =========================
def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return is_staff(interaction.user)
    return app_commands.check(predicate)

@bot.tree.command(name="ticket_panel", description="Send the AF SERVICES ticket panel.")
@admin_check()
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="AF SERVICES Tickets",
        description=(
            "Do you require assistance with anything? If so,\n"
            "please open a ticket and our support team will answer your queries.\n\n"
            "**What can we help with?**\n"
            "• Claim Order\n"
            "• Custom Order\n"
            "• Issues/Help\n\n"
            "Please be precise and straight forward with your query."
        ),
        color=0x2B2D31
    )

    # NOTE: your links should be direct image links; Discord CDN query params can expire.
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1430717412944248872/1472312218543456419/af_tickets.png")
    embed.set_image(url=AF_LOGO_URL)
    embed.set_footer(text="Support Team | AF SERVICES")

    await interaction.response.send_message(embed=embed, view=TicketPanelView())

# =========================
# Core operations
# =========================
async def refresh_ticket_embed(channel: discord.TextChannel):
    meta = ticket_meta.get(channel.id)
    if not meta:
        return

    try:
        owner = channel.guild.get_member(meta["owner_id"])
        owner_mention = owner.mention if owner else f"<@{meta['owner_id']}>"
        claimed_by = meta.get("claimed_by")
        priority = meta.get("priority")
        first_staff_ts = meta.get("first_staff_ts")
        first_staff_seconds = None
        if first_staff_ts:
            first_staff_seconds = int(first_staff_ts - meta["created_ts"])

        embed = ticket_embed(meta["kind"], owner_mention, claimed_by, priority, first_staff_seconds)

        # edit the control message if we have it
        mid = meta.get("control_message_id")
        if mid:
            msg = await channel.fetch_message(mid)
            await msg.edit(embed=embed, view=(TicketControlView(channel.id) if meta.get("status") == "open" else None))
        else:
            # fallback: find last bot message with an embed
            async for m in channel.history(limit=25):
                if m.author == bot.user and m.embeds:
                    meta["control_message_id"] = m.id
                    await m.edit(embed=embed, view=(TicketControlView(channel.id) if meta.get("status") == "open" else None))
                    break
    except Exception:
        pass

async def close_ticket(channel: discord.TextChannel, closed_by: str, reason: str, lock_only: bool = True):
    meta = ticket_meta.get(channel.id)
    if not meta:
        return

    if meta.get("status") != "open":
        return

    meta["status"] = "closed"
    meta["last_activity_ts"] = time.time()

    guild = channel.guild
    sr = get_staff_role(guild)
    owner = guild.get_member(meta["owner_id"])

    # transcript
    await send_transcript_to_log(guild, channel, reason, closed_by)

    # lock channel for owner (staff stays)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if sr:
        overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)

    try:
        await channel.edit(overwrites=overwrites)
    except Exception:
        pass

    await channel.send(
        content=f"🔒 **Ticket closed**\nReason: {reason}",
        view=ReopenView(channel.id)
    )

# =========================
# Staff response time tracking
# =========================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    ch = message.channel
    if not isinstance(ch, discord.TextChannel):
        return

    meta = ticket_meta.get(ch.id)
    if not meta:
        return

    # update activity for any message in the ticket
    meta["last_activity_ts"] = time.time()

    # First staff response time tracking
    if isinstance(message.author, discord.Member) and is_staff(message.author):
        if meta.get("first_staff_ts") is None:
            meta["first_staff_ts"] = time.time()
            # log response time
            log_ch = await get_log_channel(message.guild)
            if log_ch:
                delta = int(meta["first_staff_ts"] - meta["created_ts"])
                mins = delta // 60
                secs = delta % 60
                await log_ch.send(
                    f"⏱️ **First staff response**\n"
                    f"Ticket: {ch.mention}\n"
                    f"Staff: {message.author.mention}\n"
                    f"Time: **{mins}m {secs}s**"
                )
            # update embed
            await refresh_ticket_embed(ch)

    await bot.process_commands(message)

# =========================
# Inactivity auto-close
# =========================
@tasks.loop(seconds=30)
async def inactivity_watcher():
    now = time.time()
    for cid, meta in list(ticket_meta.items()):
        if meta.get("status") != "open":
            continue
        last_ts = meta.get("last_activity_ts", now)
        if (now - last_ts) >= (INACTIVITY_MINUTES * 60):
            # close for inactivity
            guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
            if not guild:
                continue
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                await close_ticket(
                    channel=ch,
                    closed_by="System (inactivity)",
                    reason=f"Inactivity timeout ({INACTIVITY_MINUTES} minutes).",
                    lock_only=True
                )

# =========================
# Animated status footer rotator
# =========================
@tasks.loop(seconds=max(5, STATUS_ROTATE_SECONDS))
async def status_rotator():
    # rotate footer text on ticket control embed to feel animated
    idx = int(time.time()) % len(STATUS_TEXTS)
    status = STATUS_TEXTS[idx]

    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if not guild:
        return

    for cid, meta in list(ticket_meta.items()):
        if meta.get("status") != "open":
            continue
        ch = guild.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            continue

        # Try edit the control message embed footer
        try:
            mid = meta.get("control_message_id")
            if not mid:
                continue
            msg = await ch.fetch_message(mid)
            if not msg.embeds:
                continue
            emb = msg.embeds[0]
            emb.set_footer(text=f"AF SERVICES • {status}")
            await msg.edit(embed=emb, view=TicketControlView(ch.id))
        except Exception:
            pass

# =========================
# READY
# =========================
@bot.event
async def on_ready():
    # Sync slash commands fast to your server
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("Command sync error:", e)

    # Scan existing channels to continue numbering
    if GUILD_ID:
        g = bot.get_guild(GUILD_ID)
        if g:
            scan_existing_counters(g)

    # Start background tasks
    if not inactivity_watcher.is_running():
        inactivity_watcher.start()
    if not status_rotator.is_running():
        status_rotator.start()

    print(f"Ticket Bot Online as {bot.user}")

bot.run(DISCORD_TOKEN)
