import discord 
import discord.ext

class ChannelValidation:
    
    def __init__(self, channels, guildId):
        self.channels = channels
        self.guildId = guildId
    
    
    def isConnected(self) -> bool:

        if(self.in_Voice_Channel()):
            return self.channels[self.guildId].is_connected()
        else:
            return False
    
    
    def isPlaying(self) -> bool:

        if(self.in_Voice_Channel()):
            return self.channels[self.guildId].is_playing()
        else:
            return False    
    
    
    def isPaused(self) -> bool:

        if(self.in_Voice_Channel()):
            return self.channels[self.guildId].is_paused()
        else:
            return False
    
    
    def in_Voice_Channel(self) -> bool:
        guildId = self.guildId
        
        if(guildId in self.channels):
            return True 
        else:
            return False
