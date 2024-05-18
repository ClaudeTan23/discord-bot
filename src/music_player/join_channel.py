import time
from typing import List
import discord
from discord import Interaction, Embed
from discord import app_commands as appCommand
from discord.ext import commands

from music_player import disconnect_timer
class JoinChannel(commands.Cog):
    
    v_channels_connected = {}
    disconnect_timer = {}
    skip_status = {}
    ignore_status = {}
    
    def __init__(self, bot):
        self.bot = bot
        
    @commands.hybrid_command(name="join", description="Join voice channel")
    @appCommand.describe(channel="Choose Voice Channel to join #voice channel id")
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
                disconnect_timer.Disconnect_timer(self.bot, str(guildId)).timer()
                self.initial_skip_status(str(guildId))
                
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
                        disconnect_timer.Disconnect_timer(self.bot, str(guildId)).timer()
                        self.initial_skip_status(str(guildId))
                        
                        await ctx.send(embed=embed)
                else:
                    if(self.v_channels_connected[str(guildId)].is_playing()):
                        self.v_channels_connected[str(guildId)].pause()
                        
                        await self.v_channels_connected[str(guildId)].move_to(discord.Object(id=int(channel)))
                        disconnect_timer.Disconnect_timer(self.bot, str(guildId)).timer()
                        embed = Embed(colour=discord.Colour.brand_green(), description=f":white_check_mark: Paused audio and move to **`{voice_channel.name}`** voice channel.")
                        await ctx.send(embed=embed)
                        
                    else:
                        voice_connected = await self.v_channels_connected[str(guildId)].move_to(discord.Object(id=int(channel)))
                        embed = Embed(colour=discord.Colour.brand_green(), description=f":white_check_mark: Have move to **`{voice_channel.name}`** voice channel.")
                        disconnect_timer[guildId].cancel()
                        disconnect_timer.Disconnect_timer(self.bot, str(guildId)).timer()
                    
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
            
    
    def initial_skip_status(self, guildId):
        skip_status = self.skip_status
        ignore_status = self.ignore_status
        
        if(not guildId in skip_status):
            skip_status[guildId] = False
        else:
            skip_status[guildId] = False
            
        if(not guildId in ignore_status):
            ignore_status[guildId] = False
        else:
            ignore_status[guildId] = False