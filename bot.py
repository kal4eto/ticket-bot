import os
import re
import io
import discord
from discord.ext import commands
from discord import app_commands

# =========================
# ENV VARIABLES
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
CLAIM_CATEGORY_ID = int(os.getenv("CLAIM_CATEGORY_ID", "0"))
CUSTOM_CATEGORY_ID = int(os.getenv("CUSTOM_CATEGORY_ID", "0"))
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0"))
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0"))

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

ticket_counters = {"claim": 0, "custom": 0, "support": 0}
closed_ticket_info = {}  # channel_id -> (owner_id, kind)

# =========================
# HELPERS
# =========================
def safe_name(name):
    name = name.lower()
    name = re.sub(r"[^a-z0-9]", "-", name)
    return name[:20]

def get_staff_role(guild):
    return guild.get_role(STAFF_ROLE_ID)

def category_id_for(kind):
    if kind == "claim":
        return CLAIM_CATEGORY_ID
    if kind == "custom":
        return CUSTOM_CATEGORY_ID
    return SUPPORT_CATEGORY_ID

def prefix_for(kind):
    if kind == "claim":
        return "claim"
    return "support"

def ticket_name(kind, user, number):
    return f"{prefix_for(kind)}-{safe_name(user.name)}-{number:04d}"

def is_ticket(channel):
    return channel.topic and "ticket_owner=" in channel.topic

def owner_from_topic(topic):
    m = re.search(r"ticket_owner=(\d+)", topic or "")
    return int(m.group(1)) if m else None

def kind_from_topic(topic):
    m = re.search(r"kind=(\w+)", topic or "")
    return m.group(1) if m else None

async def transcript(channel):
    lines = []
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
        content = msg.content.replace("\n", " ")
        lines.append(f"[{ts}] {msg.author}: {content}")
    return "\n".join(lines)

# =========================
# CLOSE MODAL
# =========================
class CloseModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.long)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction):
        guild = interaction.guild
        staff = get_staff_role(guild)

        if staff not in interaction.user.roles:
            await interaction.response.send_message("Only staff can close.", ephemeral=True)
            return

        reason = self.reason.value
        owner = owner_from_topic(self.channel.topic)
        kind = kind_from_topic(self.channel.topic)

        closed_ticket_info[self.channel.id] = (owner, kind)

        log = guild.get_channel(TICKET_LOG_CHANNEL_ID)
        if log:
            file = discord.File(
                io.BytesIO((await transcript(self.channel)).encode()),
                filename=f"{self.channel.name}.txt"
            )
            await log.send(
                f"Ticket closed by {interaction.user.mention}\nReason: {reason}",
                file=file
            )

        await self.channel.edit(
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.get_member(owner): discord.PermissionOverwrite(view_channel=False),
                staff: discord.PermissionOverwrite(view_channel=True)
            }
        )

        await self.channel.send(
            f"🔒 Ticket closed.\nReason: {reason}",
            view=ReopenView()
        )

        await interaction.response.send_message("Closed.", ephemeral=True)

# =========================
# REOPEN BUTTON
# =========================
class ReopenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.success)
    async def reopen(self, interaction, button):
        staff = get_staff_role(interaction.guild)
        if staff not in interaction.user.roles:
            await interaction.response.send_message("Only staff can reopen.", ephemeral=True)
            return

        owner, kind = closed_ticket_info.get(interaction.channel.id, (None, None))
        if not owner:
            return

        await interaction.channel.edit(
            overwrites={
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.guild.get_member(owner): discord.PermissionOverwrite(view_channel=True),
                staff: discord.PermissionOverwrite(view_channel=True)
            }
        )

        await interaction.channel.send("🔓 Ticket reopened.")
        await interaction.response.send_message("Reopened.", ephemeral=True)

# =========================
# TICKET VIEW
# =========================
class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Claim Order", value="claim"),
            discord.SelectOption(label="Custom Order", value="custom"),
            discord.SelectOption(label="Issues/Help", value="support"),
        ]
        super().__init__(placeholder="Select a category...", options=options)

    async def callback(self, interaction):
        guild = interaction.guild
        user = interaction.user
        kind = self.values[0]

        # Only 1 open ticket per user per category
        for ch in guild.text_channels:
            if is_ticket(ch):
                if owner_from_topic(ch.topic) == user.id and kind_from_topic(ch.topic) == kind:
                    await interaction.response.send_message(
                        f"You already have a ticket: {ch.mention}",
                        ephemeral=True
                    )
                    return

        ticket_counters[kind] += 1
        name = ticket_name(kind, user, ticket_counters[kind])

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True),
            get_staff_role(guild): discord.PermissionOverwrite(view_channel=True)
        }

        channel = await guild.create_text_channel(
            name=name,
            category=guild.get_channel(category_id_for(kind)),
            overwrites=overwrites,
            topic=f"ticket_owner={user.id} kind={kind}"
        )

        await channel.send(
            f"{user.mention} A staff member will assist you shortly.",
            view=CloseButtonView()
        )

        await interaction.response.send_message(
            f"✅ Ticket created: {channel.mention}",
            ephemeral=True
        )

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

class CloseButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        await interaction.response.send_modal(CloseModal(interaction.channel))

# =========================
# PANEL COMMAND
# =========================
def admin_check():
    async def predicate(interaction):
        return get_staff_role(interaction.guild) in interaction.user.roles
    return app_commands.check(predicate)

@bot.tree.command(name="ticket_panel", description="Send ticket panel")
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

    embed.set_thumbnail(
        url="https://cdn.discordapp.com/attachments/1430717412944248872/1472312218543456419/af_tickets.png"
    )

    embed.set_image(
        url="https://cdn.discordapp.com/attachments/1430717412944248872/1472312239791931402/af_logo.png"
    )

    embed.set_footer(text="Support Team | AF SERVICES")

    await interaction.response.send_message(embed=embed, view=TicketPanelView())

# =========================
# READY
# =========================
@bot.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

    print(f"Ticket Bot Online as {bot.user}")

bot.run(DISCORD_TOKEN)
