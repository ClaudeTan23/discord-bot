"""What "the message did not get there" actually looks like.

:class:`discord.HTTPException` is Discord *answering* with an error - a 403, a
429, a 400 for an embed that broke a limit. It says nothing about not reaching
Discord at all, and that is a different pile of exceptions entirely: a TLS
handshake the far end refuses, a connection dropped mid-request, a name that
does not resolve, a socket that times out. Those surface from ``aiohttp`` and
from the socket layer, and none of them is an ``HTTPException``.

Guarding only the first is the easy mistake, because it is the one that shows
up in testing. The failure it misses looks like this::

    aiohttp.client_exceptions.ClientConnectorSSLError:
        Cannot connect to host discord.com:443 ssl:default
        [[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ...]

which escaped a ``try`` whose whole purpose was to stop a missing announcement
taking the audio down with it.
"""

from __future__ import annotations

import aiohttp
import discord

#: Every way sending a message can fail.
#:
#: ``aiohttp.ClientError`` and ``OSError`` overlap but neither contains the
#: other - ``ClientConnectorSSLError`` is both, ``ServerDisconnectedError`` is
#: only the first, ``TimeoutError`` only the second - so both are named.
#:
#: Catch this wherever a message is decoration rather than the point: a Now
#: Playing announcement, a repaint, a typing indicator, an error reply. Do not
#: catch it where the send *is* the command's outcome, because there the
#: failure is worth surfacing.
DELIVERY_FAILED = (discord.HTTPException, aiohttp.ClientError, OSError)
