from typing import List
import discord
from discord import Interaction, Embed
from discord import app_commands as appCommand
from discord.ext import commands

class JoinChannel(commands.Cog):
    
    v_channels_connected = {}
    
    def __init__(self, bot):
        self.bot = bot
        
    @commands.command()
    async def em(self, ctx):
        embed = discord.Embed(colour=discord.Colour.brand_green(), description=":white_check_mark: **`description`**")
        # embed = discord.Embed(colour=discord.Colour.brand_green(), title="title", url="https://discordpy.readthedocs.io/en/latest/api.html?highlight=embed#discord.Embed", description="description")
        # embed.add_field(name="name", value="value", inline=True)
        # embed.add_field(name="name", value="value", inline=True)
        # embed.set_author(name="authorname")
        # embed.set_thumbnail(url="https://sm.ign.com/ign_ap/feature/w/what-is-di/what-is-discord_ezv3.jpg")
        # embed.set_footer(text="footer", icon_url=ctx.author.avatar.url)
        await ctx.send(embed=embed)
        
    @commands.hybrid_command(name="join", description="Join voice channel")
    @appCommand.describe(channel="Choose Voice Channel to join")
    async def join(self, ctx: commands.context, channel):
        listChannel = [
            str(channel.id).strip()
            for channel in ctx.guild.voice_channels
        ]

        if(channel in listChannel):
            voice_channel = await ctx.guild.fetch_channel(int(channel))
            guildId = ctx.guild.id
            connected_channels = self.v_channels_connected
            
            if(not str(guildId) in connected_channels):
                voice_connected = await voice_channel.connect()
                self.v_channels_connected[str(guildId)] = voice_connected
                embed = Embed(colour=discord.Colour.brand_green(), description=f":white_check_mark: Joined **`{voice_channel.name}`** voice channel.")
                
                await ctx.send(embed=embed)
            else:
                if(self.v_channels_connected[str(guildId)].channel.id == int(channel)):
                    
                    if(self.v_channels_connected[str(guildId)].is_connected()):
                        embed = Embed(colour=discord.Colour.brand_red(), description="The selected voice channel had already connected.")
                        await ctx.send(embed=embed)
                    else:
                        voice_connected = await voice_channel.connect()
                        self.v_channels_connected[str(guildId)] = voice_connected
                        embed = Embed(colour=discord.Colour.brand_green(), description=f":white_check_mark: Joined **`{voice_channel.name}`** voice channel.")
                        
                        await ctx.send(embed=embed)
                else:
                    voice_connected = await self.v_channels_connected[str(guildId)].move_to(discord.Object(id=int(channel)))
                    embed = Embed(colour=discord.Colour.brand_green(), description=f":white_check_mark: Have move to **`{voice_channel.name}`** voice channel.")
                
                    await ctx.send(embed=embed)
        else:
            await ctx.send("Invalid voice channel.")
        
    @join.autocomplete("channel")
    async def join_autocomplete(self: commands.Bot, interact: Interaction, current: str) -> List[appCommand.Choice[str]]:
        voice_channels = interact.guild.voice_channels
        listChannel = []
        
        for channel in voice_channels:
            listChannel.append({"name": channel.name, "id": channel.id})

        return [
            appCommand.Choice(name=channel["name"], value=str(channel["id"]))
            for channel in listChannel if current.lower().strip() in channel["name"].lower().strip()
        ]
    
    @join.error
    async def error(self, ctx, error: appCommand.AppCommandError):
        errorType = type(error)
        
        if(errorType == commands.errors.MissingRequiredArgument):
            embed = Embed(colour=discord.Colour.brand_red(), description="Missing required **`option/argument`** in the command.")
            await ctx.send(embed=embed)