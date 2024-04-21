import asyncio
import json
from pathlib import Path
import subprocess
import time
from typing import Dict, List
import discord 
from discord.ext import commands
from discord import Interaction, app_commands, Embed
from yt_dlp import YoutubeDL
from music_player import join_channel, channelValidation


class Player(commands.Cog):
    
    def __init__(self, bot):
        
        path = str(Path.cwd()).split('\\')
        path[len(path) - 1] = "ffmpeg-master-latest-win64-gpl\\bin\\ffmpeg.exe"
        
        self.ffmpegPath = '\\'.join(path)
        self.bot = bot
        self.ydl = YoutubeDL({'format': 'bestaudio/best', "ignoreerrors": True, "extract_flat": True})
        self.playlist = {}
        self.FFMPEG_OPTIONS = { 'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 200M',
                                'options': '-vn'}
        
    
    @commands.hybrid_command(name="play", description="Play audio/music in voice channel")
    async def play(self, ctx):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = str(ctx.guild.id).strip()
        playlist = self.playlist
        
        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
            current_channel = channels[guildId]
            
            if(voice_channel.in_Voice_Channel()):
                
                if(not voice_channel.isPlaying()):
                    
                    if(guildId in playlist):
                        
                        try:
                            info = self.ydl.extract_info(playlist[guildId][0]["url"], download=False)
                            
                            if(info != None):
                                opu = discord.FFmpegPCMAudio(source= info["url"], executable= self.ffmpegPath, **self.FFMPEG_OPTIONS)
                                # current_channel.play(opu, after=lambda e: asyncio.run_coroutine_threadsafe(self.a, self.bot.loop))
                                current_channel.play(opu, after= lambda e: self.a)
                                
                                await ctx.send(info["title"])
                            else:
                                await ctx.send("video is not available")
                                
                        except Exception as e:
                            print(e)
                            await ctx.send("error")
                           
                    else:
                        await ctx.send("Did not have any song to play")
                else:
                    await ctx.send("is playing")
            else:
                await ctx.send("Did not join the voice channel")  
                        
            
        else:
            await ctx.send("not in voice channel")
        
    
    def a(self):
        print("ok")
        
    @commands.hybrid_command(name="add", description="Add music by using youtube link/URL")
    @app_commands.describe(url="Youtube link/Url")
    async def add(self, ctx: commands.Context, url):
        guildId = str(ctx.guild.id)
        url = url.strip()

        rUrl = url.split("https://www.youtube.com/")
        
        async with ctx.typing():
            
            try:
                completed_url = ""
                
                if(len(rUrl) == 2):
                    
                    if(("watch?v=" in rUrl[1] and "&list=" in rUrl[1])):
                        watchId = rUrl[1].split("&list=")[0]
                        completed_url = f"https://www.youtube.com/{watchId}"
                        
                    elif(rUrl[1].strip() == ""):
                        completed_url = ""
                        
                    else:
                        completed_url = url
                                               
                else:
                    completed_url = url
                
                
                info = self.ydl.extract_info(url=completed_url, download=False)
           
                if(info != None):
                    
                    if(info["webpage_url_basename"] == "playlist"):
                        
                        if(len(info["entries"]) != 0):
                            
                            if(guildId in self.playlist):
                                for yt in info["entries"]:
                                    if yt["duration"] != None:
                                        self.playlist[guildId].append({"url": yt["url"], "title": yt["title"], "user": ctx.author.name})
                                
                                await ctx.send("ok playlist")    
                                
                            else:
                                ytList = [
                                    {"url": yt["url"], "title": yt["title"], "user": ctx.author.name}
                                    for yt in info["entries"] if yt["duration"] != None
                                ]
                                print(len(ytList))
                                
                                self.playlist[guildId] = ytList
                                await ctx.send("ok playlist")
                        else:
                            await ctx.send("The playlist did not have any videos.")
                            
                    elif(info["webpage_url_basename"] == "watch"):
                        
                        if(guildId in self.playlist):
                            self.playlist[guildId].append({"url": info["original_url"], "title": info["title"], "user": ctx.author.name})
                            await ctx.send("ok video")
                            
                        else:
                            self.playlist[guildId] = [{"url": info["original_url"], "title": info["title"], "user": ctx.author.name}]
                            await ctx.send("ok video")
                else:
                    await ctx.send("Invalid link or unavailable video/playlist :face_with_spiral_eyes:")    
                
            except Exception as e:
                print(e)
                await ctx.send("Error occurred :face_with_spiral_eyes:")
        
        
    @add.autocomplete("url")
    async def add_autocomplete(self: commands.Bot, interact: Interaction, current: str):
        # a = subprocess.Popen("yt-dlp --ignore-errors --flat-playlist --print playlist_title --print title --print id https://www.youtube.com/watch?v=h2XTsWgN0CU&list=PLARTaPRpDfravaX8AGJlHUNzoQaxsNCzO&index=3", stdout=subprocess.PIPE)
        # out, err = a.communicate()
        # print(a.returncode)
        # bb = str(out.decode(encoding="utf-8").strip()).split('\n')
        # print(bb)
        # print(err)
        # print("ok")

        if(current.strip() != ""):
            
            try:
                url = current.strip().split("https://www.youtube.com/")
                completed_url = ""

                if(len(url) == 2):
                    
                    if(("watch?v=" in url[1] and "&list=" in url[1])):
                        watchId = url[1].split("&list=")[0]
                        completed_url = f"https://www.youtube.com/{watchId}"
                        
                    elif(url[1].strip() == ""):
                        completed_url = ""
                    
                    else:
                        completed_url = current.strip()
                    
                else:
                    completed_url = current.strip()
                    
                    
                info = self.ydl.extract_info(url=completed_url, download=False, process=False)
                
                video_info_choice = app_commands.Choice(name=info["title"], value=info["original_url"])
                await interact.response.autocomplete([video_info_choice])
                
            except Exception as e:
                print(e)
    
    