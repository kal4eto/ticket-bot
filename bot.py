import os
import re
import io
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg

# ============================================================
# CONFIG / ENV
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or "0")

CLAIM_CATEGORY_ID = int(os.getenv("CLAIM_CATEGORY_ID", "0") or "0")
CUSTOM_CATEGORY_ID = int(os.getenv("CUSTOM_CATEGORY_ID", "0") or "0")
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0") or "0")

TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0") or "0")

STATUS_ROTATE_SECONDS = int(os.getenv("STATUS_ROTATE_SECONDS", "15") or "15")
DELETE_COUNTDOWN_SECONDS = int(os.getenv("DELETE_COUNTDOWN_SECONDS", "5") or "5")

AF_BLUE = 0x1E90FF
AF_LOGO_URL = os.getenv(
    "AF_LOGO_URL",
    "https://cdn.discordapp.com/attachments/1430717412944248872/1472312239791931402/af_logo.png"
)
AF_BANNER_URL = os.getenv(
    "AF_BANNER_URL",
    "https://cdn.discordapp.com/attachments/1430717412944248872/1472312218543456419/af_tickets.png"
)

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")
if not GUILD_ID:
    raise RuntimeError("Missing GUILD_ID")


# ============================================================
# DISCORD BOT
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True  # needed to track first staff response time via on_message

bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: asyncpg.Pool | None = None


# ============================================================
# DATABASE SCHEMA
# ============================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticket_counters (
  kind TEXT PRIMARY KEY,
  next_num INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
  channel_id BIGINT PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  owner_id BIGINT NOT NULL,
  kind TEXT NOT NULL,                 -- claim/custom/support
  ticket_num INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', -- open/closed/deleted
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_activity TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  claimed_by BIGINT NULL,
  priority TEXT NULL,                 -- low/medium/high
  first_staff_response_seconds INTEGER NULL,
  control_message_id BIGINT NULL,

  -- to reduce rate-limit edits for animated status
  last_footer_text TEXT NULL,
  last_topic_text TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_open_owner_kind
ON tickets (guild_id, owner_id, kind)
WHERE status='open';

CREATE INDEX IF NOT EXISTS idx_status
ON tickets (status);
"""


async def ensure_db() -> None:
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
    Atomically increments counter for this ticket kind.
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
            kind
        )
        return int(row["next_num"])


# ============================================================
# HELPERS
# ============================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:18] if name else "user"


def kind_label(kind: str) -> str:
    return {"claim": "Claim Order", "custom": "Custom Order", "support": "Issues/Help"}.get(kind, "Support")


def kind_prefix(kind: str) -> str:
    # you asked for support-user-0004 format; we keep per type too:
    return {"claim": "claim", "custom": "custom", "support": "support"}.get(kind, "support")


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


def category_for_kind(kind: str) -> int:
    if kind == "claim":
        return CLAIM_CATEGORY_ID
    if kind == "custom":
        return CUSTOM_CATEGORY_ID
    return SUPPORT_CATEGORY_ID


def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None


def is_staff(member: discord.Member) -> bool:
    role = get_staff_role(member.guild)
    return (role in member.roles) if role else False


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


def make_channel_name(kind: str, owner: discord.abc.User, num: int, priority: str | None) -> str:
    # Example: 🔴-support-username-0004
    return f"{priority_emoji(priority)}-{kind_prefix(kind)}-{safe_name(owner.name)}-{num:04d}"


# ============================================================
# EMBEDS
# ============================================================
def panel_embed() -> discord.Embed:
    e = discord.Embed(
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
        color=AF_BLUE,
    )
    # keep stable links (no expiring query params)
    e.set_author(name="AF SERVICES Support System", icon_url=AF_LOGO_URL)
    e.set_thumbnail(url=AF_LOGO_URL)
    e.set_image(url=AF_BANNER_URL)
    e.set_footer(text="Support Team | AF SERVICES")
    return e


def ticket_embed(
    kind: str,
    owner_mention: str,
    claimed_by_mention: str | None,
    priority: str | None,
    first_staff_seconds: int | None,
    footer_text: str | None
) -> discord.Embed:
    title = kind_label(kind)
    claimed_line = claimed_by_mention or "This ticket has not been claimed!"
    pr_text = (priority or "not set").upper()

    e = discord.Embed(
        title=f"{title} ({title})",
        description=(
            "Thank you for contacting us.\n"
            "Please describe your request clearly.\n\n"
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
        e.add_field(name="First staff response", value=f"{mins}m {secs}s", inline=False)

    e.set_author(name="AF SERVICES Tickets", icon_url=AF_LOGO_URL)
    e.set_thumbnail(url=AF_LOGO_URL)
    # keep banner off ticket embeds to reduce clutter; you can enable if you want
    if footer_text:
        e.set_footer(text=footer_text)
    else:
        e.set_footer(text="AF SERVICES")
    return e


# ============================================================
# TRANSCRIPT (FORMATTED TXT)
# ============================================================
async def build_formatted_transcript(channel: discord.TextChannel) -> bytes:
    lines: list[str] = []
    lines.append(f"Transcript for: {channel.name}")
    lines.append(f"Channel ID: {channel.id}")
    lines.append(f"Generated: {utcnow().isoformat()}")
    lines.append("=" * 80)

    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} ({msg.author.id})"
        content = msg.content or ""
        content = content.replace("\r", "")

        lines.append(f"[{ts}] {author}")
        if content.strip():
            for line in content.split("\n"):
                lines.append(f"  {line}")

        if msg.attachments:
            lines.append("  Attachments:")
            for a in msg.attachments:
                lines.append(f"    - {a.url}")

        if msg.embeds:
            lines.append(f"  Embeds: {len(msg.embeds)}")

        lines.append("-" * 80)

    lines.append("END OF TRANSCRIPT")
    return ("\n".join(lines)).encode("utf-8", errors="replace")


async def send_transcript_txt(
    guild: discord.Guild,
    ticket_channel: discord.TextChannel,
    close_reason: str,
    closed_by: str
) -> bool:
    log_ch = await get_log_channel(guild)
    if not log_ch:
        return False

    try:
        data = await build_formatted_transcript(ticket_channel)
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
        print("Transcript send failed:", e)
        return False


# ============================================================
# PERSISTENT CUSTOM IDS (must be unique per ticket)
# ============================================================
PANEL_SELECT_CID = "af_panel_select"

def cid_close(channel_id: int) -> str:
    return f"af_close:{channel_id}"

def cid_claim(channel_id: int) -> str:
    return f"af_claim:{channel_id}"

def cid_priority(channel_id: int) -> str:
    return f"af_priority:{channel_id}"


# ============================================================
# CLOSE MODAL
# ============================================================
class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Close reason",
        style=discord.TextStyle.long,
        required=True,
        max_length=400,
        placeholder="Example: Delivered / Resolved / Duplicate / etc."
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not is_staff(interaction.user):
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


# ============================================================
# PRIORITY SELECT (staff only)
# ============================================================
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
            custom_id=cid_priority(channel_id),
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can set priority.", ephemeral=True)
            return

        priority = self.values[0]

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

        # rename channel (keep ORIGINAL creator!)
        ch = interaction.guild.get_channel(self.channel_id)
        if isinstance(ch, discord.TextChannel):
            owner = interaction.guild.get_member(int(row["owner_id"])) or interaction.user
            new_name = make_channel_name(str(row["kind"]), owner, int(row["ticket_num"]), priority)
            try:
                await ch.edit(name=new_name)
            except Exception:
                pass

            await refresh_ticket_control_message(ch)

        await interaction.response.send_message(f"✅ Priority set to **{priority.upper()}**.", ephemeral=True)


# ============================================================
# TICKET CONTROLS VIEW (persistent)
# ============================================================
class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

        self.add_item(PrioritySelect(channel_id))

        close_btn = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id=cid_close(channel_id),
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

        claim_btn = discord.ui.Button(
            label="Claim",
            style=discord.ButtonStyle.success,
            emoji="✋",
            custom_id=cid_claim(channel_id),
        )
        claim_btn.callback = self._claim_callback
        self.add_item(claim_btn)

    async def _close_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CloseReasonModal(self.channel_id))

    async def _claim_callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return

        row = await db_fetchrow(
            "SELECT owner_id, kind, ticket_num, claimed_by, priority, status FROM tickets WHERE channel_id=$1",
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

        # keep channel name based on ORIGINAL creator
        owner = interaction.guild.get_member(int(row["owner_id"]))
        ch = interaction.guild.get_channel(self.channel_id)
        if owner and isinstance(ch, discord.TextChannel):
            new_name = make_channel_name(str(row["kind"]), owner, int(row["ticket_num"]), row["priority"])
            try:
                await ch.edit(name=new_name)
            except Exception:
                pass
            await refresh_ticket_control_message(ch)

        await interaction.response.send_message("✅ Ticket claimed.", ephemeral=True)


# ============================================================
# PANEL SELECT VIEW (persistent)
# ============================================================
class TicketPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Claim Order", value="claim", description="Claim your order", emoji="📦"),
            discord.SelectOption(label="Custom Order", value="custom", description="Request a custom order", emoji="🛒"),
            discord.SelectOption(label="Issues/Help", value="support", description="Get help", emoji="🛠️"),
        ]
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=PANEL_SELECT_CID,
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

        # Create channel
        cat_id = category_for_kind(kind)
        category = guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category not configured correctly.", ephemeral=True)
            return

        num = await get_next_ticket_num(kind)
        priority = None  # not set initially
        channel_name = make_channel_name(kind, user, num, priority)

        staff_role = get_staff_role(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        # Insert DB row
        await db_execute(
            """
            INSERT INTO tickets(channel_id, guild_id, owner_id, kind, ticket_num, status)
            VALUES ($1,$2,$3,$4,$5,'open')
            """,
            channel.id, guild.id, user.id, kind, num
        )

        # Send control message
        embed = ticket_embed(
            kind=kind,
            owner_mention=user.mention,
            claimed_by_mention=None,
            priority=None,
            first_staff_seconds=None,
            footer_text="AF SERVICES • Status: Waiting for staff"
        )
        msg = await channel.send(content=user.mention, embed=embed, view=TicketControlView(channel.id))

        await db_execute(
            "UPDATE tickets SET control_message_id=$1 WHERE channel_id=$2",
            msg.id, channel.id
        )

        # Register view persistently to this message
        bot.add_view(TicketControlView(channel.id), message_id=msg.id)

        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())


# ============================================================
# STAFF CHECK + COMMANDS
# ============================================================
def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return is_staff(interaction.user)
    return app_commands.check(predicate)


@bot.tree.command(name="ticket_panel", description="Post the AF SERVICES ticket panel.")
@staff_only()
async def ticket_panel(interaction: discord.Interaction):
    await interaction.response.send_message(embed=panel_embed(), view=TicketPanelView())


@bot.tree.command(name="ticket_stats", description="Show ticket stats overview (server).")
@staff_only()
async def ticket_stats(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use in a server.", ephemeral=True)
        return

    # totals
    totals = await db_fetchrow(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
          SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN status='deleted' THEN 1 ELSE 0 END) AS deleted
        FROM tickets
        WHERE guild_id=$1
        """,
        guild.id
    )

    by_kind = await db_fetch(
        """
        SELECT kind,
               COUNT(*) AS total,
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open
        FROM tickets
        WHERE guild_id=$1
        GROUP BY kind
        ORDER BY kind
        """,
        guild.id
    )

    avg_resp = await db_fetchrow(
        """
        SELECT
          AVG(first_staff_response_seconds)::float AS avg_first_response
        FROM tickets
        WHERE guild_id=$1 AND first_staff_response_seconds IS NOT NULL
        """,
        guild.id
    )

    avg_seconds = avg_resp["avg_first_response"]
    if avg_seconds is None:
        avg_text = "N/A"
    else:
        avg_seconds = int(avg_seconds)
        avg_text = f"{avg_seconds // 60}m {avg_seconds % 60}s"

    e = discord.Embed(
        title="AF SERVICES • Ticket Stats",
        color=AF_BLUE
    )
    e.set_thumbnail(url=AF_LOGO_URL)

    e.add_field(name="Total tickets", value=str(totals["total"]), inline=True)
    e.add_field(name="Open", value=str(totals["open"] or 0), inline=True)
    e.add_field(name="Closed", value=str(totals["closed"] or 0), inline=True)
    e.add_field(name="Deleted", value=str(totals["deleted"] or 0), inline=True)
    e.add_field(name="Avg first staff response", value=avg_text, inline=False)

    lines = []
    for r in by_kind:
        lines.append(f"**{kind_label(r['kind'])}** — total {r['total']}, open {r['open'] or 0}")
    e.add_field(name="By category", value="\n".join(lines) if lines else "No data", inline=False)

    await interaction.response.send_message(embed=e, ephemeral=True)


# ============================================================
# REFRESH CONTROL MESSAGE
# ============================================================
async def refresh_ticket_control_message(channel: discord.TextChannel):
    row = await db_fetchrow(
        """
        SELECT owner_id, kind, status, claimed_by, priority,
               first_staff_response_seconds, control_message_id, last_footer_text
        FROM tickets WHERE channel_id=$1
        """,
        channel.id
    )
    if not row:
        return

    owner = channel.guild.get_member(int(row["owner_id"]))
    owner_mention = owner.mention if owner else f"<@{int(row['owner_id'])}>"

    claimed_by_mention = None
    if row["claimed_by"]:
        claimer = channel.guild.get_member(int(row["claimed_by"]))
        claimed_by_mention = claimer.mention if claimer else f"<@{int(row['claimed_by'])}>"

    footer_text = row["last_footer_text"] or "AF SERVICES"
    embed = ticket_embed(
        kind=str(row["kind"]),
        owner_mention=owner_mention,
        claimed_by_mention=claimed_by_mention,
        priority=row["priority"],
        first_staff_seconds=row["first_staff_response_seconds"],
        footer_text=footer_text
    )

    mid = row["control_message_id"]
    if not mid:
        return

    try:
        msg = await channel.fetch_message(int(mid))
        if str(row["status"]) == "open":
            await msg.edit(embed=embed, view=TicketControlView(channel.id))
            bot.add_view(TicketControlView(channel.id), message_id=msg.id)
        else:
            await msg.edit(embed=embed, view=None)
    except Exception:
        pass


# ============================================================
# CLOSE FLOW: transcript -> countdown -> delete
# ============================================================
async def close_ticket_flow(channel: discord.TextChannel, closed_by: str, reason: str):
    # mark closed
    await db_execute(
        "UPDATE tickets SET status='closed', last_activity=NOW() WHERE channel_id=$1",
        channel.id
    )

    # transcript first
    transcript_sent = await send_transcript_txt(
        guild=channel.guild,
        ticket_channel=channel,
        close_reason=reason,
        closed_by=closed_by
    )

    # message in channel
    base = (
        f"🔒 **Ticket Closed**\n"
        f"Closed by: {closed_by}\n"
        f"Reason: {reason}\n"
        f"{'✅ Transcript saved.' if transcript_sent else '⚠️ Transcript failed (check log channel + permissions).'}\n\n"
        f"🗑️ Deleting in {DELETE_COUNTDOWN_SECONDS} seconds…"
    )
    countdown_msg = await channel.send(base)

    # countdown edits
    for i in range(DELETE_COUNTDOWN_SECONDS - 1, 0, -1):
        await asyncio.sleep(1)
        try:
            await countdown_msg.edit(content=base.replace(
                f"Deleting in {DELETE_COUNTDOWN_SECONDS} seconds…",
                f"Deleting in {i} seconds…"
            ))
        except Exception:
            pass

    await asyncio.sleep(1)

    # delete channel
    await db_execute("UPDATE tickets SET status='deleted' WHERE channel_id=$1", channel.id)
    try:
        await channel.delete(reason="Ticket closed (auto delete after transcript).")
    except Exception as e:
        print("Channel delete failed:", e)


# ============================================================
# FIRST STAFF RESPONSE TIME (DB ONLY) + activity update
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    row = await db_fetchrow(
        "SELECT created_at, first_staff_response_seconds, status FROM tickets WHERE channel_id=$1",
        message.channel.id
    )
    if not row:
        return

    # update last activity always (even though we removed inactivity close, this is still useful for stats later)
    await db_execute("UPDATE tickets SET last_activity=NOW() WHERE channel_id=$1", message.channel.id)

    # first staff response stored ONLY in DB (no messages sent)
    if row["status"] == "open" and isinstance(message.author, discord.Member) and is_staff(message.author):
        if row["first_staff_response_seconds"] is None:
            created_at: datetime = row["created_at"]
            seconds = int((utcnow() - created_at).total_seconds())
            await db_execute(
                "UPDATE tickets SET first_staff_response_seconds=$1 WHERE channel_id=$2",
                seconds, message.channel.id
            )
            # update embed field
            await refresh_ticket_control_message(message.channel)

    await bot.process_commands(message)


# ============================================================
# ANIMATED STATUS (Method C: footer + topic)
# ============================================================
def compute_status_strings(kind: str, claimed_by: int | None, priority: str | None, tick: int) -> tuple[str, str]:
    """
    Returns (footer_text, topic_text).
    Rotate among a few phrases. Also reflect claimed/priority.
    """
    base_states = [
        "Waiting for staff",
        "Processing",
        "AF SERVICES Support",
        "Please provide details",
    ]
    state = base_states[tick % len(base_states)]

    pr = (priority or "not set").upper()
    claimed = "Claimed" if claimed_by else "Unclaimed"

    footer = f"AF SERVICES • Status: {state} • {claimed} • Priority: {pr}"
    topic = f"AF SERVICES Ticket | {claimed} | Priority: {pr} | {state}"
    return footer, topic


@tasks.loop(seconds=STATUS_ROTATE_SECONDS)
async def status_rotator():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    rows = await db_fetch(
        """
        SELECT channel_id, kind, claimed_by, priority, control_message_id, last_footer_text, last_topic_text
        FROM tickets
        WHERE guild_id=$1 AND status='open'
        """,
        guild.id
    )

    tick = int(utcnow().timestamp() // STATUS_ROTATE_SECONDS)

    for r in rows:
        channel_id = int(r["channel_id"])
        ch = guild.get_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            continue

        footer_text, topic_text = compute_status_strings(
            kind=str(r["kind"]),
            claimed_by=r["claimed_by"],
            priority=r["priority"],
            tick=tick
        )

        # Update channel topic only if changed (rate-limit friendly)
        if (r["last_topic_text"] or "") != topic_text:
            try:
                await ch.edit(topic=topic_text)
                await db_execute("UPDATE tickets SET last_topic_text=$1 WHERE channel_id=$2", topic_text, channel_id)
            except Exception:
                pass

        # Update embed footer only if changed
        if (r["last_footer_text"] or "") != footer_text:
            await db_execute("UPDATE tickets SET last_footer_text=$1 WHERE channel_id=$2", footer_text, channel_id)
            await refresh_ticket_control_message(ch)


# ============================================================
# READY: init DB, sync commands, register persistent views
# ============================================================
@bot.event
async def on_ready():
    await ensure_db()

    # Sync slash commands to your guild (fast)
    try:
        gobj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=gobj)
        await bot.tree.sync(guild=gobj)
    except Exception as e:
        print("Command sync error:", e)

    # persistent panel view
    bot.add_view(TicketPanelView())

    # re-attach controls to existing open tickets
    rows = await db_fetch(
        "SELECT channel_id, control_message_id FROM tickets WHERE guild_id=$1 AND status='open' AND control_message_id IS NOT NULL",
        GUILD_ID
    )
    for r in rows:
        try:
            bot.add_view(TicketControlView(int(r["channel_id"])), message_id=int(r["control_message_id"]))
        except Exception:
            pass

    if not status_rotator.is_running():
        status_rotator.start()

    print(f"✅ Ticket bot online as {bot.user}")


bot.run(DISCORD_TOKEN)
