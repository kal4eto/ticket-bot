import os
import re
import io
import time
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

import asyncpg

# =========================
# ENV / CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or "0")

CLAIM_CATEGORY_ID = int(os.getenv("CLAIM_CATEGORY_ID", "0") or "0")
CUSTOM_CATEGORY_ID = int(os.getenv("CUSTOM_CATEGORY_ID", "0") or "0")
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0") or "0")

TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0") or "0")

INACTIVITY_MINUTES = int(os.getenv("INACTIVITY_MINUTES", "60") or "60")

AF_LOGO_URL = "https://cdn.discordapp.com/attachments/1430717412944248872/1472312239791931402/af_logo.png"
AF_BANNER_URL = "https://cdn.discordapp.com/attachments/1430717412944248872/1472312218543456419/af_tickets.png"

AF_BLUE = 0x1E90FF

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL (Railway Postgres plugin should provide this).")
if not GUILD_ID:
    raise RuntimeError("Missing GUILD_ID")

# =========================
# DISCORD BOT
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = False  # not needed

bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: asyncpg.Pool | None = None

# =========================
# DB SCHEMA
# =========================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticket_counters (
  kind TEXT PRIMARY KEY,
  next_num INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
  channel_id BIGINT PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  owner_id BIGINT NOT NULL,
  kind TEXT NOT NULL,
  ticket_num INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', -- open/closed/deleted
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_activity TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  claimed_by BIGINT NULL,
  priority TEXT NULL, -- low/medium/high
  first_staff_response_seconds INTEGER NULL,
  control_message_id BIGINT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_open_owner_kind
ON tickets (guild_id, owner_id, kind)
WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_tickets_status_activity
ON tickets (status, last_activity);
"""

# =========================
# HELPERS
# =========================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def safe_name(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\-]", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:20] if s else "user"

def kind_label(kind: str) -> str:
    return {"claim": "Claim Order", "custom": "Custom Order", "support": "Support"}.get(kind, "Support")

def kind_to_category_id(kind: str) -> int:
    if kind == "claim":
        return CLAIM_CATEGORY_ID
    if kind == "custom":
        return CUSTOM_CATEGORY_ID
    return SUPPORT_CATEGORY_ID

def kind_prefix(kind: str) -> str:
    # You wanted support- for custom/support, claim- for claim
    return "claim" if kind == "claim" else "support"

def priority_color(priority: str | None) -> int:
    if priority == "low":
        return 0x2ECC71
    if priority == "medium":
        return 0xF1C40F
    if priority == "high":
        return 0xE74C3C
    return AF_BLUE

def priority_emoji(priority: str | None) -> str:
    if priority == "high":
        return "🔴"
    if priority == "medium":
        return "🟡"
    if priority == "low":
        return "🟢"
    return "🔵"

def build_channel_name(kind: str, user: discord.abc.User, num: int, priority: str | None) -> str:
    # example: 🔴-support-user-0004
    base = f"{kind_prefix(kind)}-{safe_name(user.name)}-{num:04d}"
    return f"{priority_emoji(priority)}-{base}"

def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None

def member_is_staff(member: discord.Member) -> bool:
    sr = get_staff_role(member.guild)
    return (sr in member.roles) if sr else False

async def ensure_db():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as con:
            await con.execute(SCHEMA_SQL)

async def db_fetchrow(q: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetchrow(q, *args)

async def db_fetch(q: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetch(q, *args)

async def db_execute(q: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.execute(q, *args)

async def get_next_ticket_num(kind: str) -> int:
    """
    Atomically increments counter for kind and returns the new number.
    """
    assert db_pool is not None
    async with db_pool.acquire() as con:
        row = await con.fetchrow(
            """
            INSERT INTO ticket_counters(kind, next_num)
            VALUES ($1, 1)
            ON CONFLICT (kind) DO UPDATE
            SET next_num = ticket_counters.next_num + 1
            RETURNING next_num;
            """,
            kind,
        )
        return int(row["next_num"])

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

async def send_transcript_to_log(
    guild: discord.Guild,
    ticket_channel: discord.TextChannel,
    close_reason: str,
    closed_by: str
) -> bool:
    """
    Returns True if transcript successfully sent, else False.
    """
    log_ch = await get_log_channel(guild)
    if not log_ch:
        print("Transcript log channel not found. Check TICKET_LOG_CHANNEL_ID.")
        return False

    try:
        data = await build_transcript(ticket_channel)
        f = discord.File(io.BytesIO(data), filename=f"{ticket_channel.name}.txt")
        await log_ch.send(
            content=(
                f"🧾 **Ticket Transcript**\n"
                f"Channel: `{ticket_channel.name}`\n"
                f"Closed by: {closed_by}\n"
                f"Reason: {close_reason}"
            ),
            file=f
        )
        return True
    except Exception as e:
        print("Failed to send transcript:", e)
        return False

# =========================
# EMBEDS
# =========================
def ticket_embed(
    kind: str,
    owner_mention: str,
    claimed_by_mention: str | None,
    priority: str | None,
    first_staff_seconds: int | None
) -> discord.Embed:
    title = kind_label(kind)
    claimed_line = claimed_by_mention or "This ticket has not been claimed!"
    pr_text = (priority or "not set").upper()

    embed = discord.Embed(
        title=f"{title} ({title})",
        description=(
            "Thank you for purchasing from us.\n"
            "Feel free to add any additional info here.\n\n"
            "**Claimed by**\n"
            f"{claimed_line}\n\n"
            f"**Priority:** {pr_text}\n"
            f"**Owner:** {owner_mention}"
        ),
        color=priority_color(priority),
    )
    if first_staff_seconds is not None:
        mins = first_staff_seconds // 60
        secs = first_staff_seconds % 60
        embed.add_field(name="First staff response", value=f"{mins}m {secs}s", inline=False)

    embed.set_author(name="AF SERVICES Tickets", icon_url=AF_LOGO_URL)
    embed.set_thumbnail(url=AF_LOGO_URL)
    embed.set_footer(text="AF SERVICES")
    return embed

def panel_embed() -> discord.Embed:
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
        color=AF_BLUE
    )
    embed.set_author(name="AF SERVICES Support System", icon_url=AF_LOGO_URL)
    embed.set_image(url=AF_BANNER_URL)
    embed.set_footer(text="Support Team | AF SERVICES")
    return embed

# =========================
# Persistent UI (custom_id + timeout=None)
# =========================
def cid_close(channel_id: int) -> str:
    return f"af_close:{channel_id}"

def cid_claim(channel_id: int) -> str:
    return f"af_claim:{channel_id}"

def cid_priority(channel_id: int) -> str:
    return f"af_priority:{channel_id}"

def cid_reopen(channel_id: int) -> str:
    return f"af_reopen:{channel_id}"

PANEL_SELECT_CUSTOM_ID = "af_panel_select"

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
        if not member_is_staff(interaction.user):
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return

        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Ticket channel not found.", ephemeral=True)
            return

        await interaction.response.send_message("Closing ticket…", ephemeral=True)
        await close_ticket_flow(
            channel=ch,
            closed_by=f"{interaction.user} ({interaction.user.id})",
            reason=str(self.reason.value)
        )

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
            options=options,
            custom_id=cid_priority(channel_id)
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not member_is_staff(interaction.user):
            await interaction.response.send_message("Only staff can set priority.", ephemeral=True)
            return

        priority = self.values[0]

        # Load ticket data
        row = await db_fetchrow(
            "SELECT owner_id, kind, ticket_num, status FROM tickets WHERE channel_id=$1",
            self.channel_id
        )
        if not row or row["status"] != "open":
            await interaction.response.send_message("Ticket not found or not open.", ephemeral=True)
            return

        await db_execute(
            "UPDATE tickets SET priority=$1 WHERE channel_id=$2",
            priority, self.channel_id
        )

        # Rename channel with emoji prefix
        ch = interaction.guild.get_channel(self.channel_id)
        if isinstance(ch, discord.TextChannel):
            owner = interaction.guild.get_member(int(row["owner_id"])) or interaction.user
            new_name = build_channel_name(str(row["kind"]), owner, int(row["ticket_num"]), priority)
            try:
                await ch.edit(name=new_name)
            except Exception:
                pass

            await refresh_ticket_control_message(ch)

        await interaction.response.send_message(f"✅ Priority set to **{priority.upper()}**.", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

        self.add_item(PrioritySelect(channel_id))

        close_btn = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id=cid_close(channel_id)
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

        claim_btn = discord.ui.Button(
            label="Claim",
            style=discord.ButtonStyle.success,
            emoji="✋",
            custom_id=cid_claim(channel_id)
        )
        claim_btn.callback = self._claim_callback
        self.add_item(claim_btn)

    async def _close_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CloseReasonModal(self.channel_id))

    async def _claim_callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not member_is_staff(interaction.user):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return

        row = await db_fetchrow(
            "SELECT claimed_by, status FROM tickets WHERE channel_id=$1",
            self.channel_id
        )
        if not row or row["status"] != "open":
            await interaction.response.send_message("Ticket not found or not open.", ephemeral=True)
            return
        if row["claimed_by"] is not None:
            await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)
            return

        await db_execute(
            "UPDATE tickets SET claimed_by=$1 WHERE channel_id=$2",
            interaction.user.id, self.channel_id
        )

        ch = interaction.guild.get_channel(self.channel_id)
        if isinstance(ch, discord.TextChannel):
            await refresh_ticket_control_message(ch)

        await interaction.response.send_message("✅ Ticket claimed.", ephemeral=True)

class ReopenView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

        reopen_btn = discord.ui.Button(
            label="Reopen",
            style=discord.ButtonStyle.success,
            emoji="🔓",
            custom_id=cid_reopen(channel_id)
        )
        reopen_btn.callback = self._reopen_callback
        self.add_item(reopen_btn)

    async def _reopen_callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not member_is_staff(interaction.user):
            await interaction.response.send_message("Only staff can reopen tickets.", ephemeral=True)
            return

        row = await db_fetchrow(
            "SELECT owner_id, status FROM tickets WHERE channel_id=$1",
            self.channel_id
        )
        if not row:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        # Reopen in DB
        await db_execute(
            "UPDATE tickets SET status='open', last_activity=NOW() WHERE channel_id=$1",
            self.channel_id
        )

        ch = interaction.guild.get_channel(self.channel_id)
        if isinstance(ch, discord.TextChannel):
            guild = interaction.guild
            sr = get_staff_role(guild)
            owner = guild.get_member(int(row["owner_id"]))

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            if owner:
                overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            if sr:
                overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

            await ch.edit(overwrites=overwrites)

            await ch.send("🔓 Ticket reopened.", view=TicketControlView(self.channel_id))
            await refresh_ticket_control_message(ch)

        await interaction.response.send_message("✅ Reopened.", ephemeral=True)

class TicketPanelSelect(discord.ui.Select):
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
            options=options,
            custom_id=PANEL_SELECT_CUSTOM_ID
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        guild = interaction.guild
        user = interaction.user
        kind = self.values[0]

        # Only 1 open ticket per user per category
        existing = await db_fetchrow(
            "SELECT channel_id FROM tickets WHERE guild_id=$1 AND owner_id=$2 AND kind=$3 AND status='open'",
            guild.id, user.id, kind
        )
        if existing:
            ch = guild.get_channel(int(existing["channel_id"]))
            if isinstance(ch, discord.TextChannel):
                await interaction.response.send_message(f"You already have an open ticket: {ch.mention}", ephemeral=True)
            else:
                await interaction.response.send_message("You already have an open ticket.", ephemeral=True)
            return

        cat_id = kind_to_category_id(kind)
        category = guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category is not configured correctly.", ephemeral=True)
            return

        num = await get_next_ticket_num(kind)
        channel_name = build_channel_name(kind, user, num, priority=None)

        sr = get_staff_role(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if sr:
            overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        # Insert ticket into DB
        await db_execute(
            """
            INSERT INTO tickets(channel_id, guild_id, owner_id, kind, ticket_num, status)
            VALUES ($1, $2, $3, $4, $5, 'open')
            """,
            channel.id, guild.id, user.id, kind, num
        )

        # Send control message with embed + controls
        embed = ticket_embed(kind, user.mention, None, None, None)
        msg = await channel.send(content=user.mention, embed=embed, view=TicketControlView(channel.id))

        await db_execute(
            "UPDATE tickets SET control_message_id=$1 WHERE channel_id=$2",
            msg.id, channel.id
        )

        # Attach the view persistently to this message id (survives restarts)
        bot.add_view(TicketControlView(channel.id), message_id=msg.id)

        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())

# =========================
# COMMAND CHECKS / PANEL COMMAND
# =========================
def staff_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return member_is_staff(interaction.user)
    return app_commands.check(predicate)

@bot.tree.command(name="ticket_panel", description="Send the AF SERVICES ticket panel.")
@staff_check()
async def ticket_panel(interaction: discord.Interaction):
    await interaction.response.send_message(embed=panel_embed(), view=TicketPanelView())

# =========================
# TICKET CONTROL MESSAGE REFRESH
# =========================
async def refresh_ticket_control_message(channel: discord.TextChannel):
    row = await db_fetchrow(
        """
        SELECT owner_id, kind, status, claimed_by, priority, first_staff_response_seconds, control_message_id
        FROM tickets WHERE channel_id=$1
        """,
        channel.id
    )
    if not row:
        return

    owner = channel.guild.get_member(int(row["owner_id"]))
    owner_mention = owner.mention if owner else f"<@{int(row['owner_id'])}>"

    claimed_by_id = row["claimed_by"]
    claimed_by_mention = None
    if claimed_by_id:
        m = channel.guild.get_member(int(claimed_by_id))
        claimed_by_mention = m.mention if m else f"<@{int(claimed_by_id)}>"

    embed = ticket_embed(
        str(row["kind"]),
        owner_mention,
        claimed_by_mention,
        row["priority"],
        row["first_staff_response_seconds"]
    )

    mid = row["control_message_id"]
    if not mid:
        return

    try:
        msg = await channel.fetch_message(int(mid))
        if str(row["status"]) == "open":
            await msg.edit(embed=embed, view=TicketControlView(channel.id))
        else:
            await msg.edit(embed=embed, view=None)
    except Exception:
        pass

# =========================
# CLOSE FLOW (transcript -> deleting in 5 -> delete)
# =========================
async def close_ticket_flow(channel: discord.TextChannel, closed_by: str, reason: str):
    # Update DB
    await db_execute(
        "UPDATE tickets SET status='closed', last_activity=NOW() WHERE channel_id=$1",
        channel.id
    )

    # Lock channel (owner cannot talk)
    row = await db_fetchrow("SELECT owner_id FROM tickets WHERE channel_id=$1", channel.id)
    if row:
        guild = channel.guild
        sr = get_staff_role(guild)
        owner = guild.get_member(int(row["owner_id"]))

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

    # Send transcript
    sent = await send_transcript_to_log(channel.guild, channel, reason, closed_by)

    # Countdown message (always)
    base = (
        f"🔒 **Ticket Closed**\n"
        f"Closed by: {closed_by}\n"
        f"Reason: {reason}\n\n"
        f"{'✅ Transcript saved.' if sent else '⚠️ Transcript failed (check log channel permissions).'}\n"
        f"🗑️ Deleting in 5 seconds..."
    )
    countdown_msg = await channel.send(base, view=ReopenView(channel.id))
    # Attach reopen view persistently to that message
    bot.add_view(ReopenView(channel.id), message_id=countdown_msg.id)

    # Countdown edits
    for i in range(4, 0, -1):
        await asyncio.sleep(1)
        try:
            await countdown_msg.edit(content=base.replace("Deleting in 5 seconds...", f"Deleting in {i} seconds..."))
        except Exception:
            pass

    await asyncio.sleep(1)

    # Delete channel and mark in DB
    try:
        await db_execute("UPDATE tickets SET status='deleted' WHERE channel_id=$1", channel.id)
        await channel.delete(reason="Ticket closed (auto delete after transcript).")
    except Exception as e:
        print("Failed to delete channel:", e)

# =========================
# STAFF RESPONSE TIME TRACKING + LAST ACTIVITY
# =========================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    if not isinstance(message.channel, discord.TextChannel):
        return

    # Is this a ticket channel?
    row = await db_fetchrow(
        "SELECT created_at, first_staff_response_seconds, status FROM tickets WHERE channel_id=$1",
        message.channel.id
    )
    if not row:
        return

    # Update last activity for any message
    await db_execute(
        "UPDATE tickets SET last_activity=NOW() WHERE channel_id=$1",
        message.channel.id
    )

    # First staff response time
    if isinstance(message.author, discord.Member) and row["status"] == "open":
        if member_is_staff(message.author) and row["first_staff_response_seconds"] is None:
            created_at: datetime = row["created_at"]
            seconds = int((utcnow() - created_at).total_seconds())
            await db_execute(
                "UPDATE tickets SET first_staff_response_seconds=$1 WHERE channel_id=$2",
                seconds, message.channel.id
            )

            # Log it
            log_ch = await get_log_channel(message.guild)
            if log_ch:
                mins = seconds // 60
                secs = seconds % 60
                await log_ch.send(
                    f"⏱️ **First staff response**\n"
                    f"Ticket: {message.channel.mention}\n"
                    f"Staff: {message.author.mention}\n"
                    f"Time: **{mins}m {secs}s**"
                )

            # Refresh embed
            await refresh_ticket_control_message(message.channel)

    await bot.process_commands(message)

# =========================
# INACTIVITY AUTO-CLOSE
# =========================
@tasks.loop(seconds=30)
async def inactivity_watcher():
    # Close tickets that are open and inactive
    rows = await db_fetch(
        """
        SELECT channel_id
        FROM tickets
        WHERE status='open'
          AND last_activity < (NOW() - ($1::int * INTERVAL '1 minute'))
        """,
        INACTIVITY_MINUTES
    )

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    for r in rows:
        cid = int(r["channel_id"])
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel):
            # System close
            await close_ticket_flow(
                channel=ch,
                closed_by="System (inactivity)",
                reason=f"Inactivity timeout ({INACTIVITY_MINUTES} minutes)."
            )

# =========================
# READY: init DB, sync commands, attach persistent views
# =========================
@bot.event
async def on_ready():
    await ensure_db()

    # Fast guild sync
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
    except Exception as e:
        print("Command sync error:", e)

    # Persistent panel view (select menu)
    bot.add_view(TicketPanelView())

    # Re-attach views to existing open/closed tickets (survive restarts)
    rows = await db_fetch(
        "SELECT channel_id, control_message_id, status FROM tickets WHERE guild_id=$1 AND status IN ('open','closed')",
        GUILD_ID
    )
    for r in rows:
        cid = int(r["channel_id"])
        mid = r["control_message_id"]
        status = str(r["status"])

        if mid:
            try:
                if status == "open":
                    bot.add_view(TicketControlView(cid), message_id=int(mid))
                else:
                    # closed tickets: you might have a reopen message, but we attach reopen view when closing
                    pass
            except Exception:
                pass

    if not inactivity_watcher.is_running():
        inactivity_watcher.start()

    print(f"✅ Ticket Bot Online as {bot.user}")

# =========================
# RUN
# =========================
bot.run(DISCORD_TOKEN)
