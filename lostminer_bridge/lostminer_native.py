"""Native LostMiner LAN signaling/WebRTC adapter.

The original APK does not speak JSON/TCP. It sends UDP packets with the
following header: type:u8, id:i32, ip_length:i32, ip:bytes, port:i32,
payload:bytes. This module implements discovery, WANTSCLIENT/IDOK and the
WebRTC offer/answer exchange used by LClient/LHost.
"""
import asyncio, json, logging
from dataclasses import dataclass
from aiortc import RTCPeerConnection, RTCSessionDescription

LOG = logging.getLogger("lostminer_native")
DISCOVERY = -1
WANTSCLIENT, CLIENTOK, IDOK = 20, 21, 22
OFFER, OFFEROK, ICE, ICEOK = 23, 24, 25, 26
REJECT, SIGNOUT, SPLIT = 34, 33, 32


def read_packet(data, addr):
    if not data: return None
    typ = data[0] if data[0] < 128 else data[0] - 256
    if typ == DISCOVERY: return {"type": typ, "addr": addr, "payload": b""}
    if len(data) < 13: return None
    ident = int.from_bytes(data[1:5], "big", signed=True)
    n = int.from_bytes(data[5:9], "big", signed=True)
    if n < 0 or 9+n+4 > len(data): return None
    ip = data[9:9+n].decode(errors="replace")
    port = int.from_bytes(data[9+n:13+n], "big", signed=True)
    return {"type": typ, "id": ident, "ip": ip, "port": port,
            "payload": data[13+n:], "addr": addr}


def packet(typ, ident, ip="0.0.0.0", port=-1, payload=b""):
    ib = ip.encode()
    return bytes([typ & 255]) + int(ident).to_bytes(4, "big", signed=True) + len(ib).to_bytes(4, "big") + ib + int(port).to_bytes(4, "big", signed=True) + payload


@dataclass
class Peer:
    ident: int
    addr: tuple
    name: str = "Player"
    pc: object = None
    reliable: object = None
    unreliable: object = None


class LostMinerNativeProtocol(asyncio.DatagramProtocol):
    def __init__(self, bridge):
        self.bridge = bridge
        self.transport = None
        self.peers = {}
        self.next_id = 1001

    def connection_made(self, transport):
        self.transport = transport
        LOG.info("LostMiner nativo UDP escutando em %s", transport.get_extra_info("sockname"))

    def datagram_received(self, data, addr):
        try:
            msg = read_packet(data, addr)
            if not msg: return
            typ = msg["type"]
            if typ == DISCOVERY:
                self.transport.sendto(bytes([DISCOVERY & 255]), addr)
            elif typ == WANTSCLIENT:
                self.handle_wants(msg)
            elif typ == IDOK:
                asyncio.ensure_future(self.handle_idok(msg))
            elif typ == OFFER:
                asyncio.ensure_future(self.handle_offer(msg))
            elif typ == ICE:
                asyncio.ensure_future(self.handle_ice(msg))
            elif typ == ICEOK:
                pass
            elif typ == SIGNOUT:
                self.peers.pop(msg.get("id"), None)
        except Exception:
            LOG.exception("Falha ao processar pacote UDP LostMiner")

    def target(self, msg):
        # Direct/local mode sends 0.0.0.0:-1; use the actual UDP source.
        return msg["addr"]

    def send(self, data, addr):
        self.transport.sendto(data, addr)

    def handle_wants(self, msg):
        ident = self.next_id; self.next_id += 1
        peer = Peer(ident, msg["addr"])
        self.peers[ident] = peer
        # The APK only needs a non-empty payload to proceed to step 1.
        self.send(packet(CLIENTOK, ident, "0.0.0.0", -1, b"1.0"), msg["addr"])
        LOG.info("LostMiner solicitou entrada: %s id=%d", msg["addr"], ident)

    async def handle_idok(self, msg):
        peer = self.peers.get(msg.get("id"))
        if not peer: return
        peer.addr = msg["addr"]
        raw = msg.get("payload", b"")
        peer.name = raw.decode(errors="replace")[:64] or "Player"
        peer.pc = RTCPeerConnection()
        peer.reliable = peer.pc.createDataChannel("RC", ordered=True)
        peer.unreliable = peer.pc.createDataChannel("UC", ordered=False, maxRetransmits=0)
        for channel in (peer.reliable, peer.unreliable):
            channel.on("message", lambda data, p=peer: self.on_data(p, data))
        @peer.pc.on("connectionstatechange")
        async def state_change():
            LOG.info("LostMiner WebRTC id=%d estado=%s", peer.ident, peer.pc.connectionState)
            if peer.pc.connectionState in ("failed", "closed", "disconnected"):
                await peer.pc.close()
                self.peers.pop(peer.ident, None)
        offer = await peer.pc.createOffer()
        await peer.pc.setLocalDescription(offer)
        desc = peer.pc.localDescription
        self.send(packet(OFFER, peer.ident, "0.0.0.0", -1,
                         json.dumps({"type": desc.type, "sdp": desc.sdp}, separators=(",", ":")).encode()), peer.addr)
        LOG.info("Oferta WebRTC enviada ao LostMiner id=%d", peer.ident)

    async def handle_offer(self, msg):
        peer = self.peers.get(msg.get("id"))
        if not peer or not peer.pc: return
        try:
            obj = json.loads(msg["payload"].decode().strip())
            await peer.pc.setRemoteDescription(RTCSessionDescription(sdp=obj["sdp"], type=obj["type"]))
            self.send(packet(OFFEROK, peer.ident, "0.0.0.0", -1, b""), msg["addr"])
            LOG.info("Resposta WebRTC recebida do LostMiner id=%d", peer.ident)
        except Exception:
            LOG.exception("Resposta WebRTC inválida id=%s", msg.get("id"))

    async def handle_ice(self, msg):
        # Current aiortc offers bundle candidates in SDP. Keep accepting ICE
        # signaling packets for clients configured with trickle ICE; unsupported
        # candidate formats are safely ignored until the bundled SDP connects.
        peer = self.peers.get(msg.get("id"))
        if peer: self.send(packet(ICEOK, peer.ident, "0.0.0.0", -1, msg.get("payload", b"0")), msg["addr"])

    def on_data(self, peer, data):
        if isinstance(data, str): data = data.encode()
        if not data: return
        header = data[0]
        msg_type = (header >> 4) & 15
        flags = header & 15
        pos = 1
        fields = []
        for bit in (0, 1, 2):
            if flags & (1 << bit):
                if pos + 4 > len(data): return
                fields.append(int.from_bytes(data[pos:pos+4], "big", signed=True)); pos += 4
            else:
                fields.append(-1)
        payload = data[pos:]
        LOG.debug("LostMiner mensagem id=%d bytes=%d tipo=%d", peer.ident, len(data), msg_type)
        if msg_type == 6:
            # NearbyConnectionsAux uses type 6 to announce the player's name
            # and multiplayer version after the reliable RC channel opens.
            peer.name = payload.decode(errors="replace")[:64] or "Player"
            self.send_lm(peer, 1, peer.name.encode(), from_id=1, to_id=peer.ident)
            self.send_lm(peer, 2, b"Bridge", from_id=1, extra=1, to_id=peer.ident)
            LOG.info("LostMiner entrou na sala: %s id=%d", peer.name, peer.ident)
        elif msg_type == 8:
            # Gameplay messages are forwarded to the bridge's authoritative
            # adapter in a later layer; keep the transport alive for now.
            pass
        # The APK's own keepalive is type 9; echo it on the reliable channel.
        if msg_type == 9 and peer.reliable and peer.reliable.readyState == "open":
            peer.reliable.send(data)

    def send_lm(self, peer, msg_type, payload=b"", from_id=-1, to_id=-1, extra=-1, reliable=True):
        flags = (1 if from_id != -1 else 0) | (2 if to_id != -1 else 0) | (4 if extra != -1 else 0) | (8 if reliable else 0)
        out = bytearray([(msg_type << 4) | flags])
        for present, value in ((from_id != -1, from_id), (to_id != -1, to_id), (extra != -1, extra)):
            if present: out.extend(int(value).to_bytes(4, "big", signed=True))
        out.extend(payload)
        channel = peer.reliable if reliable else peer.unreliable
        if channel and channel.readyState == "open": channel.send(bytes(out))
