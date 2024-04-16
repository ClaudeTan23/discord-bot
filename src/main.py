# from yt_dlp import YoutubeDL
# from yt_dlp import postprocessor


# URLS = ['https://www.youtube.com/watch?v=3oyco5XURTk']
# with YoutubeDL() as ydl:
#     info = ydl.extract_info('https://www.youtube.com/watch?v=3oyco5XURTk', download=False)
#     # ydl.download(URLS)
#     ydl.



    
    
    

# print(y['url'])


import os
from pydoc import describe
from random import choice, choices
from turtle import title
from typing import List
import discord
from discord.ext import commands
import discord.ui

import yt_dlp
from pathlib import Path

# class MyClient(discord.Client):
#     async def on_ready(self):
#         print(f'Logged on as {self.user}!')

#     async def on_message(self, message):
#         print(f'Message from {message.author}: {message.content}')




URLS = 'https://www.youtube.com/watch?v=mNEUkkoUoIA'

ydl_opts = {
    'format': 'bestaudio/best'
}

# with yt_dlp.YoutubeDL(ydl_opts) as ydl:
#     y = ydl.extract_info(URLS, download=False)

# FFMPEG_OPTIONS = {
#             'before_options':
#             '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 200M',
#             'options': '-vn'
#         }

# url = y['url']
# path = str(Path.cwd()).split('\\')
# path[len(path) - 1] = "ffmpeg-master-latest-win64-gpl\\bin\\ffmpeg.exe"

# cPath = '\\'.join(path)


intents = discord.Intents.all()
intents.message_content = True

bot = commands.Bot(command_prefix='?', intents=intents)
# bot = discord.Client(intents=intents)
# tree = discord.app_commands.CommandTree(bot)

# @commands.command()
# async def test(ctx, *args):
    # await ctx.author.voice.channel.connect()
    # # file = open("1.opus")
    # opu = discord.FFmpegPCMAudio(source=url, executable=cPath, before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 200M', options = '-vn')
    # ctx.voice_client.play(opu)
    
    
# @bot.event
# async def on_ready():
#     await bot.change_presence(status=discord.Status.idle, activity=discord.Game("playing testing"))
#     await tree.sync(guild=discord.Object(id=893068438988292126))
#     print("ok")

# @bot.event
# async def on_message(ctx):
#     await tree.sync(guild=discord.Object(id=ctx.guild.id))
#     print("ok")
    
# tree.command(name="test", description="testing", guilds=[discord.Object(id=893068438988292126)])
# async def self(interation: discord.Interaction):
#     await interation.response.send_message("ok")

# @bot.hybrid_command()
# async def test(ctx):
#     await ctx.send("ok")


@bot.command()
async def asd(ctx, arg):
    await ctx.send(arg)
    

@bot.hybrid_command(name="h", description='asd')
async def high(ctx, name):
    await ctx.send("ok")
    


@bot.hybrid_group()
async def group(ctx, name):
    await ctx.send("ok")
    
    
@bot.hybrid_command()
@discord.app_commands.describe(fruit='The fruit',
    quantity='fruit quantity')
async def fruits(c, fruit: str, quantity: str):
    await c.send(f"{fruit} and {quantity}")

@fruits.autocomplete('fruit')
async def fruits_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[discord.app_commands.Choice[str]]:
    fruits = ['Banana', 'Pineapple', 'Apple', 'Watermelon', 'Melon', 'Cherry']
    return [
        discord.app_commands.Choice(name=fruit, value=fruit+'d')
        for fruit in fruits if current.lower() in fruit.lower()
    ]
    

@fruits.autocomplete('quantity')
async def fruits_autocompleteaa(
    interaction: discord.Interaction,
    current: str,
) -> List[discord.app_commands.Choice[str]]:
    fruits = ['Banana', 'Pineapple', 'Apple', 'Watermelon', 'Melon', 'Cherry']
    return [
        discord.app_commands.Choice(name=fruit, value=fruit)
        for fruit in fruits if current.lower() in fruit.lower()
    ]
    
    
@bot.hybrid_command(description='Bans a member', name='ban')
@discord.app_commands.describe(member='The member to ban',
    reason='The reason for the ban',
    days='The number of days worth of messages to delete',)
async def ban(ctx, member, reason, days):
    await ctx.send(f'Banned {member}')


@bot.hybrid_command(description='guild members', name='members')
@discord.app_commands.describe(member='fetch members')
async def member(ctx, member):
    await ctx.send(f'fetch {member}')


@member.autocomplete('member')
async def member_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[discord.app_commands.Choice[str]]:
    async for member in interaction.guild.fetch_members(limit=10):
        print(member.global_name)
        
    fruits = ['Banana', 'Pineapple', 'Apple', 'Watermelon', 'Melon', 'Cherry']
    return [
        discord.app_commands.Choice(name=fruit, value=fruit)
        for fruit in fruits if current.lower() in fruit.lower()
    ]
    
    
class Questionnaire(discord.ui.Modal, title='Questionnaire Response'):
    name = discord.ui.TextInput(label='Name')
    answer = discord.ui.TextInput(label='Answer', style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction, modal):
        await interaction.response.send_message(f'Thanks for your response, {self.name}!', ephemeral=True)
    
class MyViews(discord.ui.View): 
    @discord.ui.button(label="button2", style=discord.ButtonStyle.primary) 
    async def button_callback(self, interaction, button):
        await interaction.response.edit_message(content="ok")


class MyView(discord.ui.View): 
    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text])
    async def select_channels(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        return await interaction.response.send_message(f'You selected {select.values[0].mention}')
    
    @discord.ui.button(label="button", style=discord.ButtonStyle.secondary) 
    async def button_callback(self, interaction, button):
        # await interaction.response.edit_message(content="ok", view=MyViews())
        # print(interaction.message.author.guild)
        await interaction.response.edit_message(content='.', view=None, embed=None)
        await interaction.message.delete()
        await interaction.message.channel.send("okk")
        

        

@bot.hybrid_command()
async def t(ctx):
    e = discord.Embed(colour=discord.Colour.dark_blue())
    e.set_thumbnail(url='https://sm.ign.com/ign_ap/feature/w/what-is-di/what-is-discord_ezv3.jpg')
    await ctx.send(content='dd', embed=e, view=MyView())
    # await ctx.send(content='dd', embed=e, view=MyView(timeout=100), delete_after=10)

# @goodnight.command()
# async def night(ctx):
#     await ctx.send("Goodnight everyone!")


@fruits.before_invoke
async def fruits_autocompletes(c):
    print('sd')

# @bot.hybrid_group()
# async def tag(ctx, name):
#     await ctx.send(f"Showing tag: {name}")

@group.command()
async def _(ctx, name, age):
    await ctx.send(f"Created tag: {name}")

# @bot.hybrid_command()
# async def test(ctx):
#     await ctx.send("ok")

# @bot.tree.command(name="test", description="testing")
# async def self(interation: discord.Interaction):
#     await interation.response.send_message("ok")
    
@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.idle, activity=discord.Game("testing"))
    await bot.tree.sync()
    print(bot.user)

bot.run('bot token here')