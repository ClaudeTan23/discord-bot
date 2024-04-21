import discord 
from discord.ext import commands
from discord import app_commands as appCommand
from music_player import join_channel
from discord import Embed

class LeaveChannel(commands.Cog):
    
    def __init__(self, bot):
        self.bot = bot
        
    @commands.hybrid_command(name="leave", description="Leave current joined voice channel")
    async def leave(self, ctx):
        guildId = ctx.guild.id
        voice_channels = join_channel.JoinChannel(self.bot).v_channels_connected
        
        if(str(guildId) in voice_channels):
            embed = Embed(colour=discord.Colour.brand_green(), description=f":white_check_mark: Have leave the voice channel.")
            await voice_channels.get(str(guildId)).disconnect()
            
            voice_channels.pop(str(guildId))
            await ctx.send(embed=embed)
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description=f"Did not join any voice channel.")
            await ctx.send(embed=embed)
        