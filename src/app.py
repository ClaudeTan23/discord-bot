import discord
from discord import Status, Game
from discord.ext import commands
from dotenv import load_dotenv
import os
from discord import app_commands
from music_player import join_channel as join, leave_channel as leave, player as player

load_dotenv()
botToken = os.environ['Bot-Token']
intents = discord.Intents.all()
intents.message_content = True

bot = commands.Bot(command_prefix='?', intents=intents)

# @bot.event 
# async def on_message(ctx):
#     print(join.JoinChannel(bot).v_channels_connected)

@bot.event
async def on_ready():
    await bot.change_presence(status=Status.idle, activity=Game("Testing"))
    await bot.add_cog(join.JoinChannel(bot))
    await bot.add_cog(leave.LeaveChannel(bot))
    await bot.add_cog(player.Player(bot))
    await bot.tree.sync()
    print(f"{bot.user} online")

bot.run(botToken)




# bot.add_command(test)