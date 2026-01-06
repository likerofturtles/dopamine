import discord
from discord.ext import commands
from discord import app_commands
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from config import MAX_PATH, FONT_PATH
from typing import Optional

class MaxWithStrapOn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._bg_image: Optional[Image.Image] = None
        self._font_nick: Optional[ImageFont.FreeTypeFont] = None
        self._font_username: Optional[ImageFont.FreeTypeFont] = None
        self._avatar_mask: Optional[Image.Image] = None
        self._avatar_size = 120

    async def cog_load(self):
        """Load and cache resources when cog is loaded."""
        try:
            self._bg_image = Image.open(MAX_PATH).convert('RGBA')

            self._font_nick = ImageFont.truetype(FONT_PATH, 18)
            self._font_username = ImageFont.truetype(FONT_PATH, 26)

            self._avatar_mask = Image.new('L', (self._avatar_size, self._avatar_size), 0)
            mask_draw = ImageDraw.Draw(self._avatar_mask)
            mask_draw.ellipse((0, 0, self._avatar_size, self._avatar_size), fill=255)
        except Exception as e:
            print(f"Error loading resources in MaxWithStrapOn cog: {e}")

    async def cog_unload(self):
        """Clean up resources when cog is unloaded."""
        if self._bg_image:
            self._bg_image.close()
            self._bg_image = None
        self._font_nick = None
        self._font_username = None
        if self._avatar_mask:
            self._avatar_mask.close()
            self._avatar_mask = None

    def _get_background_image(self) -> Image.Image:
        """Get background image, loading from cache or disk if needed."""
        if self._bg_image is None:
            self._bg_image = Image.open(MAX_PATH).convert('RGBA')
        return self._bg_image.copy()

    def _get_fonts(self) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
        """Get fonts, loading from cache or disk if needed."""
        if self._font_nick is None:
            self._font_nick = ImageFont.truetype(FONT_PATH, 18)
        if self._font_username is None:
            self._font_username = ImageFont.truetype(FONT_PATH, 26)
        return self._font_nick, self._font_username

    def _get_avatar_mask(self) -> Image.Image:
        """Get avatar mask, creating if needed."""
        if self._avatar_mask is None:
            self._avatar_mask = Image.new('L', (self._avatar_size, self._avatar_size), 0)
            mask_draw = ImageDraw.Draw(self._avatar_mask)
            mask_draw.ellipse((0, 0, self._avatar_size, self._avatar_size), fill=255)
        return self._avatar_mask

    async def check_vote_access(self, user_id: int) -> bool:
        """Check if user has voted (copied logic)."""
        voter_cog = self.bot.get_cog('TopGGVoter')
        if not voter_cog:
            return True
        return await voter_cog.check_vote_access(user_id)

    @app_commands.command(
        name="maxwithstrapon",
        description="Ignore the command's name - This command turns anyone into Max Verstappen!"
    )
    @app_commands.describe(user="User to insert into the image")
    @app_commands.allowed_contexts(discord.app_commands.AppCommandContext.guild, discord.app_commands.AppCommandContext.private_channel, discord.app_commands.AppCommandContext.dm_channel)
    async def maxwithstrapon(
        self,
        interaction: discord.Interaction,
        user: discord.User
    ):
        try:
            if not await self.check_vote_access(interaction.user.id):
                embed = discord.Embed(
                    title="Vote to Use This Feature!",
                    description=f"This command requires voting! To access this feature, please vote for Dopamine here: [top.gg](https://top.gg/bot/{self.bot.user.id})",
                    color=0xffaa00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            is_ephemeral = False

            await interaction.response.defer(ephemeral=is_ephemeral)

            bg = self._get_background_image()

            try:
                avatar_asset = user.display_avatar.with_format("png").with_size(256)
                avatar_bytes = await avatar_asset.read()
            except Exception as e:
                raise Exception(f"Failed to fetch user avatar: {e}")

            try:
                pfp = Image.open(BytesIO(avatar_bytes)).convert('RGBA')
            except Exception as e:
                raise Exception(f"Failed to process avatar image: {e}")

            pfp = pfp.resize((self._avatar_size, self._avatar_size), Image.Resampling.LANCZOS)
            mask = self._get_avatar_mask()
            pfp = Image.composite(pfp, Image.new("RGBA", (self._avatar_size, self._avatar_size)), mask)

            circle_center = (204, 120)
            upper_left = (circle_center[0] - self._avatar_size // 2, circle_center[1] - self._avatar_size // 2)
            bg.paste(pfp, upper_left, mask=pfp)

            pfp.close()

            font_nick, font_username = self._get_fonts()

            if interaction.guild:
                member = interaction.guild.get_member(user.id)
                if member:
                    nickname = member.display_name
                else:
                    nickname = getattr(user, 'global_name', None) or user.display_name
            else:
                nickname = getattr(user, 'global_name', None) or user.display_name

            username = str(user)

            draw = ImageDraw.Draw(bg)

            bbox_nick = draw.textbbox((0, 0), nickname, font=font_nick)
            w_nick = bbox_nick[2] - bbox_nick[0]
            h_nick = bbox_nick[3] - bbox_nick[1]
            nick_pos = (495 - w_nick // 2, 125 - h_nick // 2)
            draw.text(nick_pos, nickname, font=font_nick, fill="white")

            bbox_user = draw.textbbox((0, 0), username, font=font_username)
            w_user = bbox_user[2] - bbox_user[0]
            h_user = bbox_user[3] - bbox_user[1]
            user_pos = (495 - w_user // 2, 155 - h_user // 2)
            draw.text(user_pos, username, font=font_username, fill="white")

            try:
                with BytesIO() as image_binary:
                    bg.save(image_binary, 'PNG', optimize=True)
                    image_binary.seek(0)
                    await interaction.followup.send(
                        file=discord.File(image_binary, filename="maxwithstrapon.png"),
                        ephemeral=is_ephemeral
                    )
            finally:
                bg.close()
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred: {str(e)}",
                color=discord.Color.red()
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(MaxWithStrapOn(bot))
