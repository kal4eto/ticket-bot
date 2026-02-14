import os
import re
import discord
from discord.ext import commands
from discord import app_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or "0")
CLAIM_CATEGORY_ID = int(os.getenv("CLAIM_CATEGORY_ID", "0") or "0")
CUSTOM_CATEGORY_ID = int(os.getenv("CUSTOM_CATEGORY_ID", "0") or "0")
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0") or "0")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")  # optional but recommended for fast slash sync

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def safe_name(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\-]", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:60] if s else "user"

def staff_role(guild: discord.Guild):
    return guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None

def category_for(kind: str) -> int:
    if kind == "claim":
        return CLAIM_CATEGORY_ID
    if kind == "custom":
        return CUSTOM_CATEGORY_ID
    return SUPPORT_CATEGORY_ID

def channel_name_for(kind: str, user: discord.abc.User) -> str:
    uname = safe_name(user.name)
    if kind == "claim":
        return f"claim-{uname}"
    if kind == "custom":
        return f"support-{uname}"  # matches your screenshot style
    return f"support-{uname}"

def find_existing_ticket(guild: discord.Guild, kind: str, user: discord.abc.User):
    target = channel_name_for(kind, user)
    for ch in guild.text_channels:
        if ch.name == target:
            return ch
    return None

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return

        sr = staff_role(interaction.guild)
        is_staff = sr in interaction.user.roles if sr else False

        # Allow staff OR the ticket owner (if you want owner-only too, keep as is)
        # Here: staff-only close (common for shops). Change to True if you want buyer can close too.
        if not is_staff:
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return

        await interaction.response.send_message("Closing ticket in 3 seconds...", ephemeral=True)
        await discord.utils.sleep_until(discord.utils.utcnow())  # no-op safe
        await discord.utils.sleep_until(discord.utils.utcnow())  # no-op safe
        await discord.utils.sleep_until(discord.utils.utcnow())  # no-op safe
        # simpler:
        await discord.utils.sleep_until(discord.utils.utcnow())  # harmless
        await interaction.channel.delete(reason=f"Closed by {interaction.user}")

class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Claim Order",
                value="claim",
                description="Click here to Claim Your Order",
                emoji="📦"
            ),
            discord.SelectOption(
                label="Custom Order",
                value="custom",
                description="Click here to get a Custom Order",
                emoji="🛒"
            ),
            discord.SelectOption(
                label="Issues/Help",
                value="support",
                description="Click here for Issues/Help",
                emoji="🛠️"
            ),
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

        kind = self.values[0]
        guild = interaction.guild
        user = interaction.user

        cat_id = category_for(kind)
        category = guild.get_channel(cat_id) if cat_id else None
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category is not configured correctly.", ephemeral=True)
            return

        existing = find_existing_ticket(guild, kind, user)
        if existing:
            await interaction.response.send_message(f"You already have a ticket: {existing.mention}", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        sr = staff_role(guild)
        if sr:
            overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        ch_name = channel_name_for(kind, user)
        channel = await guild.create_text_channel(
            name=ch_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        embed = discord.Embed(
            title="Ticket Created",
            description=(
                f"Hello {user.mention} — a staff member will assist you shortly.\n\n"
                f"**Category:** `{kind}`\n"
                f"Please describe your request clearly."
            ),
        )

        await channel.send(content=(sr.mention if sr else ""), embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        sr = staff_role(interaction.guild)
        return sr in interaction.user.roles if sr else False
    return app_commands.check(predicate)

@bot.tree.command(name="ticket_panel", description="Send the ticket panel message.")
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
  embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1430717412944248872/1472312218543456419/af_tickets.png?ex=69921d1b&is=6990cb9b&hm=ad9f42dc18d938a3794c3006b81cdbdf16bfdac371b4d21ac21ff42ebc597d4d&")
  embed.set_image(url="https://cdn.discordapp.com/attachments/1430717412944248872/1472312239791931402/af_logo.png?ex=69921d20&is=6990cba0&hm=66ca7e1bc3457f613612b86b25add577d3bbe5e5c09c539cfea825f495134cec&")
  embed.set_footer(text="Support Team | AF SERVICES")
    await interaction.response.send_message(embed=embed, view=TicketPanelView())

@bot.event
async def on_ready():
    # persistent views so buttons/select keep working after restarts
    bot.add_view(TicketPanelView())
    bot.add_view(CloseTicketView())

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

    print(f"Ticket bot online as {bot.user}")

bot.run(DISCORD_TOKEN)
