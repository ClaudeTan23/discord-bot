import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Dict, List
import discord 
from discord.ext import commands
from discord import Interaction, app_commands, Embed
from yt_dlp import YoutubeDL
from music_player import join_channel, channelValidation, disconnect_timer
import re


class Player(commands.Cog):
    
    def __init__(self, bot):
        
        path = str(Path.cwd()).split('\\')
        path[len(path) - 1] = "ffmpeg-master-latest-win64-gpl\\bin\\ffmpeg.exe"
        
        self.ffmpegPath = '\\'.join(path)
        self.bot = bot
        self.ydl = YoutubeDL({"extractor_args": {"youtube": {"player_client": ["web"]}}, 'format': 'bestaudio/best', "ignoreerrors": True, "extract_flat": True, "no_warnings": True, "skip_download": True})
        self.playlist = {}
        self.vol = {}
        self.FFMPEG_OPTIONS = { 'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 300M',
                                'options': '-vn'}
        self.yt_dlp_cmd = 'yt-dlp --simulate --extractor-args "youtube:player_client=web" --skip-download --ignore-errors --quiet --flat-playlist --print playlist_title --print title --print original_url --print duration --encoding utf-8'
        
    
    @commands.hybrid_command(name="play", description="Play audio/music in voice channel")
    async def play(self, ctx):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = str(ctx.guild.id).strip()
        
        if(guildId in channels):
                voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
                current_channel = channels[guildId]
                
                if(voice_channel.isPlaying()):
                    embed = Embed(colour=discord.Colour.blurple(), description="Is playing right now.")
                    await ctx.send(embed=embed)
                    
                    return None
                elif(voice_channel.isPaused()):
                    embed = Embed(colour=discord.Colour.blurple(), description="The song is paused, use **`?resume`** command to resume playing.")
                    await ctx.send(embed=embed)
                    
                    return None
        
        join_channel.JoinChannel(self.bot).skip_status[guildId] = False
        join_channel.JoinChannel(self.bot).ignore_status[guildId] = False
        await self.play_musics(ctx, ctx.channel.id, False)
        
    
    async def play_musics(self, ctx, text_channel_id, skip):
        
        async with ctx.typing():
             
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
                                    current_channel.play(opu, after= lambda e: asyncio.run_coroutine_threadsafe(self.play_next(ctx, text_channel_id, skip), self.bot.loop))
                                    channels[guildId].source = discord.PCMVolumeTransformer(current_channel.source, 10/100 if not guildId in self.vol else self.vol[guildId])
                                    user = await ctx.guild.fetch_member(playlist[guildId][0]["userId"])
                                    embed = self.playing_embed(title=info["title"], icon_thumbnail="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcST8wdNfmD5TMIpIQ71p2O1MiBx2GFS8M9NzyHFsmyplw&s",
                                                               thumbnail=info["thumbnail"], url=playlist[guildId][0]["url"], duration=self.cal_duration(info["duration"]), user=user)
                                    join_channel.JoinChannel(self.bot).disconnect_timer[guildId].cancel()
                                    
                                    await ctx.send(embed=embed)
                                else:
                                    embed = Embed(colour=discord.Colour.brand_red(), description=f'[{self.playlist[guildId][0]["title"]}](<{self.playlist[guildId][0]["url"]}>) is not available, skip to next song.')
       
                                    await ctx.send(embed=embed) 
                                    await self.play_next(ctx, text_channel_id, skip)
                                    
                            except Exception as e:
                                print(e)
                                embed = Embed(colour=discord.Colour.brand_red(), description="Error occurred :face_with_spiral_eyes:")
                                await ctx.send(embed=embed)
                            
                        else:
                            embed = Embed(colour=discord.Colour.brand_red(), description="Didn't have any songs in playlist/queue to play.")
                            await ctx.send(embed=embed)

                    else:
                        embed = Embed(colour=discord.Colour.blurple(), description="Is playing right now.")
                        # await ctx.send(embed=embed)
                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                    await ctx.send(embed=embed)           
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                await ctx.send(embed=embed)
            
    
    async def play_next(self, ctx, text_channel_id, skip):
        guildId = str(ctx.guild.id)
        playlist = self.playlist
   
        if(join_channel.JoinChannel(self.bot).skip_status[guildId] == True and skip == False):
            return None
        
        if(join_channel.JoinChannel(self.bot).ignore_status[guildId] == True):
            return None
   
        msg = await self.bot.fetch_channel(text_channel_id)
        
        if(guildId in playlist):
            
            if(len(playlist[guildId]) > 1):
                playlist[guildId].pop(0)
                join_channel.JoinChannel(self.bot).skip_status[guildId] = False
                
                await self.play_musics(msg, text_channel_id, False)
                
            else:
                playlist.pop(guildId)
                embed = Embed(colour=discord.Colour.green(), description="Have played all of the songs.")
                disconnect_timer.Disconnect_timer(self.bot, str(guildId)).timer()
                join_channel.JoinChannel(self.bot).skip_status[guildId] = False
                
                await ctx.send(embed=embed)
            
    
    @commands.hybrid_group(fallback="queue", description="Clear the music/song in the queue")
    async def clear(self, ctx):
        guildId = str(ctx.guild.id)
        playlist = self.playlist
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        
        if(not guildId in playlist):
            embed = Embed(colour=discord.Colour.dark_magenta(), description="Doesn't have any song in the queue.")
            await ctx.send(embed=embed)
            return None
        
        queue = playlist[guildId]
        
        if(len(queue) <= 0):
            embed = Embed(colour=discord.Colour.dark_magenta(), description="Doesn't have any song in the queue.")
            await ctx.send(embed=embed)
            return None
            
        voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
            
        if(voice_channel.in_Voice_Channel() and voice_channel.isConnected()):

            if(voice_channel.isPlaying() or voice_channel.isPaused()):
                    
                for i in range(1, len(playlist[guildId])):
                    playlist[guildId].pop()
     
                embed = Embed(colour=discord.Colour.brand_green(), description="Playlist/queue have been cleared.")
                await ctx.send(embed=embed)
            
            else:
                embed = Embed(colour=discord.Colour.brand_green(), description="Playlist/queue have been cleared.")
                await ctx.send(embed=embed)
                playlist.pop(guildId)
        else:
            embed = Embed(colour=discord.Colour.brand_green(), description="Playlist/queue have been cleared.")
            await ctx.send(embed=embed)
            playlist.pop(guildId)
                   
    
    @commands.hybrid_command(name="queue", description="Display the queued songs.")
    async def queue(self, ctx):
        guildId = str(ctx.guild.id)
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        play_status = ""

        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)

            if(voice_channel.isConnected()):
                if(voice_channel.isPlaying()):
                    play_status += "(Playing) "
                    
                elif(voice_channel.isPaused()):
                    play_status += "(Paused)"
                    
                    
        if(guildId in self.playlist):
            queue = self.playlist[guildId]
            queue_num = len(self.playlist[guildId])
            queue_txt = ""
            page_num = queue_num // 10
            
            if((queue_num % 10) > 0):
                page_num += 1
            
            index = 1
            
            if(page_num == 0):
                page_num = 1
                
            for song in queue:
                if(index <= 10):
                    queue_txt += f'{index}. {play_status if index == 1 else ""}[{song["title"]}](<{song["url"]}>) ({self.cal_duration(song["duration"])}) - Requested by <@{song["userId"]}>\n'
                index += 1
               
            embed = Embed(colour=discord.Colour.dark_magenta(), description=queue_txt, title="Queue")
            embed.set_footer(text=f"Page 1/{page_num}")
                
            await ctx.send(embed=embed)
            
        else:
            embed = Embed(colour=discord.Colour.dark_magenta(), description="Doesn't have any song in the queue.")
            await ctx.send(embed=embed)     
        
    
    @commands.hybrid_group(fallback="page", description="Fetch the queue page")
    @app_commands.describe(number="The number of the page")
    async def queueto(self, ctx, number):
        num = (int(number) - 1) * 10
        guildId = str(ctx.guild.id)

        if(guildId in self.playlist):
            queue_num = len(self.playlist[guildId])
            queue = self.playlist[guildId]
            queue_txt = ""
            index = 1
            page_num = queue_num // 10
            
            if((queue_num % 10) > 0):
                page_num += 1
                 
            if(int(number) <= page_num and int(number) > 0):
                
                for i in range(num, queue_num):

                    if(index <= 10):
                        queue_txt += f'{i + 1}. [{queue[i]["title"]}](<{queue[i]["url"]}>) ({self.cal_duration(queue[i]["duration"])}) - Requested by <@{queue[i]["userId"]}>\n'
                        
                    index += 1
                    
                embed = Embed(colour=discord.Colour.dark_magenta(), description=queue_txt, title="Queue")
                embed.set_footer(text=f"Page {number}/{page_num}")
                    
                await ctx.send(embed=embed)
                
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="The queue page does not exist.")
                await ctx.send(embed=embed)  
            
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description="Doesn't have any song in the queue.")
            await ctx.send(embed=embed)    
        
        
            
    @queueto.error
    async def queueto_error(self, ctx, error: app_commands.AppCommandError):
        errorType = type(error)
        print(error)
        
        if(errorType == commands.errors.MissingRequiredArgument):
            embed = Embed(colour=discord.Colour.brand_red(), description="Missing required **`number/argument`** in the command.")
            await ctx.send(embed=embed)
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description="Please use number for the required **`number/argument`** in the command.")
            await ctx.send(embed=embed)
    
    
    @commands.hybrid_command(name="skip", description="Skip to the next songs")
    async def skip(self, ctx):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = str(ctx.guild.id).strip()
        playlist = self.playlist
        
        if(guildId in playlist):
                
            if(guildId in channels):
                voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
                    
                if(voice_channel.isConnected()):
                    join_channel.JoinChannel(self.bot).skip_status[guildId] = True
                    join_channel.JoinChannel(self.bot).ignore_status[guildId] = False
                    embed = Embed(colour=discord.Colour.dark_gold(), description="Skip to the next songs.")
                    channels[guildId].stop()
                    msg = await self.bot.fetch_channel(ctx.channel.id)
                    
                    await ctx.send(embed=embed) 
                    await self.play_next(msg, ctx.channel.id, True)    

                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                    await ctx.send(embed=embed) 
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                await ctx.send(embed=embed)  
        else:
            embed = Embed(colour=discord.Colour.dark_magenta(), description="Doesn't have any song in the queue.")
            await ctx.send(embed=embed)      
                  
                            
    @commands.hybrid_command(name="skipto", description="Skip to chosen songs")   
    @app_commands.describe(number="The number of the song in the queue")
    async def skipto(self, ctx, number):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = str(ctx.guild.id).strip()
        playlist = self.playlist
        num = int(number)
        
        if(guildId in playlist):
            
            if(num <= len(playlist[guildId]) and not num <= 0):
                for i in range(0, num - 2):
                    playlist[guildId].pop(0)
                    
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="The number of the song does not exist.")
                await ctx.send(embed=embed) 
                
                return None
                
            if(guildId in channels):
                voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
                    
                if(voice_channel.isConnected()):
                    join_channel.JoinChannel(self.bot).skip_status[guildId] = True
                    join_channel.JoinChannel(self.bot).ignore_status[guildId] = False
                    embed = Embed(colour=discord.Colour.dark_gold(), description="Skip to the next songs.")
                    channels[guildId].stop()
                    msg = await self.bot.fetch_channel(ctx.channel.id)
                    
                    await ctx.send(embed=embed) 
                    await self.play_next(msg, ctx.channel.id, True)    

                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                    await ctx.send(embed=embed) 
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                await ctx.send(embed=embed)  
        else:
            embed = Embed(colour=discord.Colour.dark_magenta(), description="Doesn't have any song in the queue.")
            await ctx.send(embed=embed)     
                        
    @skipto.error
    async def skipto_error(self, ctx, error: app_commands.AppCommandError):
        errorType = type(error)
        print(error)
        
        if(errorType == commands.errors.MissingRequiredArgument):
            embed = Embed(colour=discord.Colour.brand_red(), description="Missing required **`number/argument`** in the command.")
            await ctx.send(embed=embed)
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description="Please use number for the required **`number/argument`** in the command.")
            await ctx.send(embed=embed)
                    
    
    @commands.hybrid_command(name="resume", description="Resume the paused audio in voice channel")
    async def resume(self, ctx):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = str(ctx.guild.id).strip()

        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
            current_channel = channels[guildId]
            
            if(voice_channel.isConnected()):
                
                if(voice_channel.isPlaying()):
                    embed = Embed(colour=discord.Colour.blurple(), description="Is playing right now.")
                    await ctx.send(embed=embed)
                    
                elif(voice_channel.isPaused()):
                    current_channel.resume()
                    join_channel.JoinChannel(self.bot).ignore_status[guildId] = False
                    join_channel.JoinChannel(self.bot).disconnect_timer[guildId].cancel()
                    
                    embed = Embed(colour=discord.Colour.blurple(), description="Resuming the paused song.")
                    await ctx.send(embed=embed)
                    
                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="There's no music playing.")
                    await ctx.send(embed=embed)
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                await ctx.send(embed=embed)
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
            await ctx.send(embed=embed)
            
            
    @commands.hybrid_command(name="pause", description="Pause current playing audio in voice channel")
    async def pause(self, ctx):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = str(ctx.guild.id).strip()
        
        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
            current_channel = channels[guildId]
            
            if(voice_channel.isConnected()):
                
                if(voice_channel.isPlaying()):
                    current_channel.pause()
                    join_channel.JoinChannel(self.bot).ignore_status[guildId] = True
                    disconnect_timer.Disconnect_timer(self.bot, str(guildId)).timer()
                    
                    embed = Embed(colour=discord.Colour.brand_green(), description=":white_check_mark: Song have been paused.")
                    await ctx.send(embed=embed)
                    
                elif(voice_channel.isPaused()):
                    embed = Embed(colour=discord.Colour.brand_green(), description="The song had already paused.")
                    await ctx.send(embed=embed)
                    
                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="There's no music playing.")
                    await ctx.send(embed=embed)
            else:
                embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
                await ctx.send(embed=embed)
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description="Not in voice channel :face_with_spiral_eyes:")
            await ctx.send(embed=embed)
        
        
    @commands.hybrid_command(name="add", description="Add music by using youtube link/URL")
    @app_commands.describe(url="Youtube link/Url")
    async def add(self, ctx: commands.Context, url):
        guildId = str(ctx.guild.id)
        
        async with ctx.typing():
            
            try:
                rUrl = ""
                
                if(re.search("^<http://.*>$", url.strip())):
                    rUrl = url.strip()[1:]
                    rUrl = rUrl[:len(rUrl.strip()) - 1]
                else:
                    rUrl = url.strip()
                            
                if(re.search("^<https://.*>$", url.strip())):
                    rUrl = url.strip()[1:]
                    rUrl = rUrl[:len(rUrl.strip()) - 1]
                else:
                    rUrl = url.strip()
            
                split_url = rUrl.split("https://www.youtube.com/")
        
                completed_url = ""
                
                if(len(split_url) == 2):
                    
                    if(("watch?v=" in split_url[1] and "&list=" in split_url[1])):
                        watchId = split_url[1].split("&list=")[0]
                        completed_url = f"https://www.youtube.com/{watchId}"
                        
                    elif(split_url[1].strip() == ""):
                        completed_url = ""
                        
                    else:
                        completed_url = rUrl
                                               
                else:
                    completed_url = rUrl
                    
                cmd = subprocess.Popen(f'{self.yt_dlp_cmd} {completed_url}', stdout=subprocess.PIPE)
                out, err = cmd.communicate()
                info = {}
                    
                if(cmd.returncode == 0):
                    result = str(out.decode(encoding="utf-8", errors="ignore").strip()).split('\n')

                    if(len(result) > 0):
                        
                        if(result[0] == "NA"):
                            info["webpage_url_basename"] = "watch"
                            info["title"] = result[1]
                            info["original_url"] = result[2]
                            info["duration"] = result[3]
                            
                        else:
                            info["webpage_url_basename"] = "playlist"
                            info["entries"] = []
                            index = 0
                            for video in result:
                                
                                if(index == 1):
                                    info["entries"].append({"title": video})
                                elif(index == 2):
                                    info["entries"][len(info["entries"]) - 1]["url"] = video
                                elif(index == 3):
                                    info["entries"][len(info["entries"]) - 1]["duration"] = video
                                elif(index == 4):
                                    index = 0
                                    
                                index += 1
                    else:
                        embed = Embed(colour=discord.Colour.brand_red(), description="Invalid link or unavailable video/playlist :face_with_spiral_eyes:")
                        await ctx.send(embed=embed)
                        return None 
                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="Invalid link or unavailable video/playlist :face_with_spiral_eyes:")
                    await ctx.send(embed=embed) 
                    return None
                    
                # info = self.ydl.extract_info(url=completed_url, download=False)
           
                if(info != None):
                    
                    if(info["webpage_url_basename"] == "playlist"):
                        
                        if(len(info["entries"]) != 0):
                            
                            if(guildId in self.playlist):
                                countVideo = 0
                                
                                for yt in info["entries"]:
                                    if yt["duration"] != "NA":
                                        self.playlist[guildId].append({"url": yt["url"], "title": yt["title"], "duration": int(yt["duration"]), "user": ctx.author.name, "userId": ctx.author.id})
                                        countVideo += 1
                                
                                title = info["entries"][0]["title"]
                                infoUrl = info["entries"][0]["url"]
                                
                                if(len(info["entries"]) == 1):
                                    embed = Embed(colour=discord.Colour.dark_blue(), description=f"Added [{title}](<{infoUrl}>) in to playlist.")
                                    await ctx.send(embed=embed) 
                                else:
                                    embed = Embed(colour=discord.Colour.dark_blue(), description=f"Added [{title}](<{infoUrl}>) and {countVideo - 1} songs in to playlist.")
                                    await ctx.send(embed=embed) 
                                
                            else:
                                ytList = [
                                    {"url": yt["url"], "title": yt["title"], "duration": int(yt["duration"]), "user": ctx.author.name, "userId": ctx.author.id}
                                    for yt in info["entries"] if yt["duration"] != "NA"
                                ]
                                
                                title = ytList[0]["title"]
                                infoUrl = ytList[0]["url"]
                                listInt = len(ytList) - 1
                                
                                self.playlist[guildId] = ytList
                                embed = Embed(colour=discord.Colour.dark_blue(), description=f"Added [{title}](<{infoUrl}>) and {listInt} songs in to playlist.")
                                await ctx.send(embed=embed) 
                        else:
                            embed = Embed(colour=discord.Colour.brand_red(), description="The playlist did not have any videos.")
                            await ctx.send(embed=embed) 
                            
                    elif(info["webpage_url_basename"] == "watch"):
                        
                        if(guildId in self.playlist):
                            self.playlist[guildId].append({"url": info["original_url"], "title": info["title"], "duration": int(info["duration"]), "user": ctx.author.name, "userId": ctx.author.id})
                            
                            embed = Embed(colour=discord.Colour.dark_blue(), description=f'Added [{info["title"]}](<{info["original_url"]}>) in to playlist.')
                            await ctx.send(embed=embed) 
                            
                        else:
                            self.playlist[guildId] = [{"url": info["original_url"], "title": info["title"], "duration": int(info["duration"]), "user": ctx.author.name, "userId": ctx.author.id}]
                            
                            embed = Embed(colour=discord.Colour.dark_blue(), description=f'Added [{info["title"]}](<{info["original_url"]}>) in to playlist.')
                            await ctx.send(embed=embed) 
                else:
                    embed = Embed(colour=discord.Colour.brand_red(), description="Invalid link or unavailable video/playlist :face_with_spiral_eyes:")
                    await ctx.send(embed=embed) 
                
            except Exception as e:
                print(e)
                embed = Embed(colour=discord.Colour.brand_red(), description="Error occurred :face_with_spiral_eyes:")
                await ctx.send(embed=embed)
    
    
    @add.error
    async def add_error(self, ctx, error: app_commands.AppCommandError):
        errorType = type(error)
        
        if(errorType == commands.errors.MissingRequiredArgument):
            embed = Embed(colour=discord.Colour.brand_red(), description="Missing required **`url/argument`** in the command.")
            await ctx.send(embed=embed)
        
        
    @add.autocomplete("url")
    async def add_autocomplete(self: commands.Bot, interact: Interaction, current: str):
    
        if(current.strip() != ""):
            
            try:
                url = ""
                
                if(re.search("^<http://.*>$", current.strip())):
                    url = current.strip()[1:]
                    url = url[:len(url.strip()) - 1]
                else:
                    url = current.strip()
                    
                if(re.search("^<https://.*>$", current.strip())):
                    url = current.strip()[1:]
                    url = url[:len(url.strip()) - 1]
                else:
                    url = current.strip()
                    
                split_url = url.strip().split("https://www.youtube.com/")
                completed_url = ""

                if(len(split_url) == 2):
                    
                    if(("watch?v=" in split_url[1] and "&list=" in split_url[1])):
                        watchId = split_url[1].split("&list=")[0]
                        completed_url = f"https://www.youtube.com/{watchId}"
                        
                    elif(split_url[1].strip() == ""):
                        completed_url = ""
                    
                    else:
                        completed_url = url.strip()
                    
                else:
                    completed_url = url.strip()
                    
                cmd = subprocess.Popen(f'{self.yt_dlp_cmd} {completed_url}', stdout=subprocess.PIPE)
                out, err = cmd.communicate()

                if(cmd.returncode == 0):
                    result = str(out.decode(encoding="utf-8", errors="ignore").strip()).split('\n')
        
                    if(len(result) > 0):
                        await interact.response.autocomplete([app_commands.Choice(name=f'{result[0] if result[0] != "NA" else result[1]}', value=completed_url)])
                    else:
                        await interact.response.autocomplete([])
                else:
                    print(err)
                    await interact.response.autocomplete([])
                 
                # info = self.ydl.extract_info(url=completed_url, download=False, process=False)
                
                # video_info_choice = app_commands.Choice(name=info["title"], value=info["original_url"])
                # await interact.response.autocomplete([video_info_choice])
                
            except Exception as e:
                print(e)
        else:
            await interact.response.autocomplete([])
            
    
    @commands.hybrid_command(name="volume", description="Set the volume for the player")
    @app_commands.describe(number="0 to 100")
    async def volume(self, ctx, number):
        guildId = str(ctx.guild.id)
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        num = int(number)
        
        if(num > 100 or num < 0):
            embed = Embed(colour=discord.Colour.brand_red(), description="Please choose 0 to 100 to set the volume.")
            await ctx.send(embed=embed)
            return None
        
        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
            
            if(voice_channel.isConnected()):
                
                if(voice_channel.isPlaying() or voice_channel.isPaused()):
                    self.vol[guildId] = num /100
                    channels[guildId].source.volume = num / 100
                else:
                    self.vol[guildId] = num /100
            else:
                self.vol[guildId] = num /100
        else:
            self.vol[guildId] = num /100
                
        embed = Embed(colour=discord.Colour.brand_green(), description=f"Volume have set to **`{num}%`**.")
        await ctx.send(embed=embed)
    
    @volume.error 
    async def volume_error(self, ctx, error: app_commands.errors):
        errorType = type(error)
        print(error)
        
        if(errorType == commands.errors.MissingRequiredArgument):
            embed = Embed(colour=discord.Colour.brand_red(), description="Missing required **`number/argument`** in the command.")
            await ctx.send(embed=embed)
        else:
            embed = Embed(colour=discord.Colour.brand_red(), description="Please use number for the required **`number/argument`** in the command.")
            await ctx.send(embed=embed)
            
                
    @commands.hybrid_command(name="stop", description="Stop playing, leave voice channel and clear the playlist/queue")
    async def stop(self, ctx):
        guildId = str(ctx.guild.id)
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        playlist = self.playlist
        
        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
                    
            if(voice_channel.isConnected()):
                join_channel.JoinChannel(self.bot).ignore_status[guildId] = True  
                await channels[guildId].disconnect()
                channels.pop(guildId)
                
                if(not voice_channel.isPlaying()):
                    join_channel.JoinChannel(self.bot).disconnect_timer[guildId].cancel()
        
        
        if(guildId in playlist):
            
            if(len(playlist[guildId]) > 0):
                playlist.pop(guildId)
        
        embed = Embed(colour=discord.Colour.brand_green(), description=":white_check_mark: Stop playing, leaving voice channel and queue have been cleared.")
        await ctx.send(embed=embed)        
        
    
    def playing_embed(self, title, icon_thumbnail, url, thumbnail, duration, user) -> Embed:
        avatar_url = ""
        if(user.avatar == None):
            avatar_url = "https://lisboaparapessoas.pt/wp-content/uploads/2022/02/discordlxparapessoas.png"
        else:
            avatar_url = user.avatar.url
            
        embed = Embed(colour=discord.Colour.blurple(), title=title, description=f"Duration: {duration}", url=url)
        embed.set_author(name="Now playing", icon_url=icon_thumbnail, url=url)
        embed.set_thumbnail(url=thumbnail)
        embed.set_footer(text=f'Requested by {user.name}', icon_url=avatar_url)
        return embed
    
    
    def cal_duration(self, duration) -> str:
        min = duration // 60
        sec = duration % 60
        
        if(len(str(sec)) <= 1):
            sec = "0" + str(sec)
        
        return f"{min}:{sec}"