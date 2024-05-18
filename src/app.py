import discord
from discord import Status, Game, Embed
from discord.ext import commands
from dotenv import load_dotenv
import os
from discord import app_commands
from music_player import join_channel as join, leave_channel as leave, player as player

load_dotenv()
botToken = os.environ['Bot-Token']
intents = discord.Intents.all()
intents.message_content = True

bot = commands.Bot(command_prefix='?', intents=intents, help_command=None)

@bot.hybrid_command(name="help", description="Display all the commands and manuals")
async def help(ctx):
    try:
        help_txt = open("help.txt", "r")
        help_manual = help_txt.read()
        embed = Embed(colour=discord.Colour.dark_grey(), description=help_manual, title="All commands and manuals")
        await ctx.send(embed=embed)
    except Exception as e:
        print(e)
        embed = Embed(colour=discord.Colour.brand_red(), description="Error occurred :face_with_spiral_eyes:")
        await ctx.send(embed=embed)


@bot.event
async def on_ready():
    await bot.change_presence(status=Status.idle, activity=Game("Deeznuts | /help"))
    await bot.add_cog(join.JoinChannel(bot))
    await bot.add_cog(leave.LeaveChannel(bot))
    await bot.add_cog(player.Player(bot))
    await bot.tree.sync()
    print(f"{bot.user} online")

bot.run(botToken)

# bot.add_command(test)