from collections import namedtuple
from concurrent.futures import CancelledError, TimeoutError
import asyncio
import logging
from enum import Enum, unique

import config
from server.types import Address
from .decorators import with_logger

from server.natpacketserver import NatPacketServer

_natserver = None

async def send_natpacket(addr, msg):
    global _natserver
    if not _natserver:
        _natserver = NatPacketServer()
        await _natserver.listen()
    _natserver.send_natpacket_to(msg, addr)

logger = logging.getLogger(__name__)

@unique
class ConnectivityState(Enum):
    """
    Describes the connectivity level of a peer
    Three levels are defined:
        - PUBLIC:
        The peer is publicly accessible without prior communication
        - STUN:
            The peer must first send an outbound packet
            before being able to receive on the inbound port
        - PROXY:
            The peer is unable to connect by other means than a TCP proxy
    """
    PUBLIC = "PUBLIC"
    STUN = "STUN"
    PROXY = "PROXY"

Connectivity = namedtuple('Connectivity', ['addr', 'state'])

@with_logger
class NatHelper:
    def __init__(self):
        self.nat_packets = {}

    async def wait_for_natpacket(self, message: str, sender: Address=None):
        fut = asyncio.Future()
        self.nat_packets[message] = fut
        self._logger.info("Awaiting nat packet {} from {}".format(message, sender or 'anywhere'))
        addr, msg = await fut
        if fut.done():
            self._logger.info("Received {} from {}".format(msg, addr))
            if (addr == sender or sender is None) and msg == message:
                return addr, msg
        else:
            return False

    def process_nat_packet(self, address: Address, message: str):
        self._logger.debug("<<{}: {}".format(address, message))
        if message in self.nat_packets and isinstance(self.nat_packets[message], asyncio.Future):
            if not self.nat_packets[message].done():
                self.nat_packets[message].set_result((address, message))
                del self.nat_packets[message]

    def send_nat_packet(self, address: Address, message: str):
        self._logger.debug(">>{}/udp: {}".format(address, message))
        self.send({
            "command": "SendNatPacket",
            "target": "connectivity",
            "args": ["{}:{}".format(*address), message]
        })

@with_logger
class ConnectivityTest:
    """
    Determine the connectivity state of a single peer.
    """

    def __init__(self,
                 connection: NatHelper,
                 host: str,
                 port: int,
                 identifier: str):
        """
        :return: None
        """
        super(ConnectivityTest, self).__init__()
        self.connection = connection
        self.connectivity_state = None
        self.remote_addr = (host, port)
        self.identifier = identifier
        self.connection.log.debug("Testing peer connectivity")
        self.client_packets = []
        self.server_packets = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @asyncio.coroutine
    def determine_connectivity(self):
        """
        Determine connectivity of peer

        :return: Connectivity(addr, ConnectivityState)
        """
        try:
            if (yield from self.test_public()):
                return Connectivity(addr="{}:{}".format(*self.remote_addr), state=ConnectivityState.PUBLIC)
            addr = yield from self.test_stun()
            if addr:
                return Connectivity(addr=addr, state=ConnectivityState.STUN)
            else:
                return Connectivity(addr=None, state=ConnectivityState.PROXY)
        except (TimeoutError, CancelledError):
            pass
        return Connectivity(addr=None, state=ConnectivityState.PROXY)

    async def test_public(self):
        self._logger.debug("Testing PUBLIC")
        message = "Are you public? {}".format(self.identifier)
        received_packet = self.connection.wait_for_natpacket(message)
        for i in range(0, 3):
            await send_natpacket(self.remote_addr, message)
        try:
            result = await asyncio.wait_for(received_packet, 1)
            self._logger.info("Result: {}".format(result))
            return True
        except (CancelledError, TimeoutError):
            return False

    async def test_stun(self):
        self._logger.debug("Testing STUN")
        message = "Hello {}".format(self.identifier)
        for i in range(0, 3):
            fut = _natserver.await_packet(message)
            self.connection.send_gpgnet_message('SendNatPacket', ["%s:%s" % (config.LOBBY_IP,
                                                                     config.LOBBY_UDP_PORT),
                                                          message])
            await asyncio.sleep(0.1)
            try:
                received, addr = await asyncio.wait_for(fut, 0.5)
                if received == message:
                    return addr
            except (CancelledError, TimeoutError):
                pass

