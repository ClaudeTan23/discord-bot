from music_player import join_channel, channelValidation
from threading import Timer
import asyncio

class Disconnect_timer():
    
    def __init__(self, bot, guildId):
        self.bot = bot
        self.guildId = guildId
        
    
    async def run(self):
        channels = join_channel.JoinChannel(self.bot).v_channels_connected
        guildId = self.guildId
        
        if(guildId in channels):
            voice_channel = channelValidation.ChannelValidation(channels=channels, guildId=guildId)
            current_channel = channels[guildId]
            
            if(voice_channel.isConnected()):
                if(not voice_channel.isPlaying() or voice_channel.isPaused()):
                    channels.pop(guildId)
                    await current_channel.disconnect()
    
    
    def timer(self):
        join_channel.JoinChannel(self.bot).disconnect_timer[self.guildId] = Timer(69.0, lambda: asyncio.run_coroutine_threadsafe(self.run(), self.bot.loop))
        join_channel.JoinChannel(self.bot).disconnect_timer[self.guildId].start()
        
    