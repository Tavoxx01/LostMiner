#!/usr/bin/env python3
"""LostMiner Bridge Server.

Minecraft Java 1.12.2: protocol 340 on MC_PORT.
LostMiner adapter: newline-delimited JSON on LOSTMINER_PORT.

The authoritative world is a 3-D Minecraft world constrained to a thin Z slice.
This makes it look 2-D while keeping the Minecraft client fully 3-D.
"""
import argparse, asyncio, json, logging, math, os, time, uuid
from pathlib import Path

from twisted.internet import asyncioreactor
try:
    asyncioreactor.install()
except Exception:
    pass
from twisted.internet import reactor
from quarry.net.server import ServerFactory, ServerProtocol
from quarry.net.protocol import ProtocolError
from quarry.types.uuid import UUID
from quarry.types.chunk import BlockArray, PackedArray
from twisted.internet.task import LoopingCall

LOG = logging.getLogger("lostminer_bridge")

AIR, STONE, GRASS, DIRT, BEDROCK = 0, 1, 2, 3, 7

class Registry:
    max_bits = 13
    def encode_block(self, obj):
        return int(obj) if isinstance(obj, int) else int(obj.get("id", 0))
    def decode_block(self, val): return val
    def is_air_block(self, obj): return int(obj) == AIR

REGISTRY = Registry()

class Light:
    def __init__(self, value=15):
        self.data = bytes([0xFF if value else 0x00] * 2048)
    def to_bytes(self): return self.data

class World:
    def __init__(self, path, slice_z=0, slice_depth=1, radius=64, height=64):
        self.path = Path(path); self.slice_z = slice_z; self.slice_depth = slice_depth
        self.radius = radius; self.height = height; self.blocks = {}
        self.load()
    def key(self, x, y, z): return f"{x},{y},{z}"
    def get(self, x, y, z):
        if z < self.slice_z or z >= self.slice_z + self.slice_depth: return AIR
        k = self.key(x,y,z)
        if k in self.blocks: return self.blocks[k]
        # 2-D cave/platform: bedrock, dirt, grass, air.
        if y <= 0: return BEDROCK
        if y == 1: return DIRT
        if y == 2: return GRASS
        return AIR
    def set(self, x, y, z, block):
        if z < self.slice_z or z >= self.slice_z + self.slice_depth or not (0 <= y < 256): return False
        self.blocks[self.key(x,y,z)] = int(block); return True
    def load(self):
        if self.path.exists():
            try:
                self.blocks = json.loads(self.path.read_text())
            except Exception as e: LOG.warning("Falha ao carregar mundo: %s", e)
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(self.blocks, separators=(',', ':'))); tmp.replace(self.path)
    def chunk_sections(self, cx, cz):
        sections = []
        for sy in range(16):
            blocks = BlockArray.empty(REGISTRY)
            # All sections are air except the low sections, but keep section 0-3
            # populated so clients receive a visible floor.
            for lx in range(16):
                for ly in range(16):
                    for lz in range(16):
                        x, y, z = cx*16+lx, sy*16+ly, cz*16+lz
                        b = self.get(x,y,z)
                        if b: blocks[(ly*16+lz)*16+lx] = b
            if not blocks.is_empty(): sections.append((blocks, PackedArray.empty_light(), PackedArray.empty_light()))
            else: sections.append(None)
        return sections

class BridgeState:
    def __init__(self, world): self.world=world; self.players={}; self.lock=asyncio.Lock()
    def snapshot(self):
        return {"type":"snapshot", "slice_z":self.world.slice_z, "slice_depth":self.world.slice_depth,
                "players":[p.public_state() for p in self.players.values()]}
    async def broadcast_lm(self, obj, exclude=None):
        for p in list(self.players.values()):
            if p is not exclude: await p.send(obj)
    def chat(self, name, text):
        return {"type":"chat", "from":name, "text":text[:256], "ts":int(time.time())}

class MCProtocol(ServerProtocol):
    def player_joined(self):
        super().player_joined(); self.entity_id = id(self) & 0x7fffffff
        self.x, self.y, self.z = 0.5, 3.0, 0.5; self.yaw=0.0; self.pitch=0.0
        self.factory.bridge.mc_players.add(self)
        self.send_world(); self.send_chat("Bridge", "Bem-vindo ao mundo fatiado 2D do LostMiner Bridge!")
        LOG.info("Minecraft entrou: %s", self.display_name)
    def player_left(self):
        self.factory.bridge.mc_players.discard(self); super().player_left(); LOG.info("Minecraft saiu: %s", self.display_name)
    def send_chat(self, text, sender=None):
        msg = (f"<{sender}> {text}" if sender else text).replace('\\', '')
        self.send_packet("chat_message", self.buff_type.pack_chat(msg), self.buff_type.pack('B', 0))
    def send_world(self):
        b=self.buff_type
        self.send_packet("join_game", b.pack('i B i B B', self.entity_id, 0, 0, 2, 20), b.pack_string("default"), b.pack('?', False) )
        self.send_packet("spawn_position", b.pack_position(0,3,0))
        self.send_packet("player_abilities", b.pack('B ff', 0, 0.05, 0.1))
        self.send_packet("time_update", b.pack('q q', 0, 6000))
        for cx in range(-4,5):
            for cz in range(-4,5): self.send_chunk(cx,cz)
        self.send_packet("player_position_and_look", b.pack('dddffB i', self.x,self.y,self.z,self.yaw,self.pitch,0,0))
    def send_chunk(self,cx,cz):
        b=self.buff_type; sections=self.factory.bridge.world.chunk_sections(cx,cz)
        data=b.pack_chunk(sections)
        mask=b.pack_chunk_bitmask(sections)
        payload=b.pack('ii?',cx,cz,True)+mask+b.pack_varint(len(data))+data+b.pack_varint(0)
        self.send_packet("chunk_data", payload)
    def packet_chat_message(self, buff):
        text=buff.unpack_string(); self.factory.broadcast_mc(self, f"<{self.display_name}> {text[:256]}")
    def packet_player_position(self,buff):
        self.x=buff.unpack('d'); self.y=buff.unpack('d'); self.z=buff.unpack('d'); buff.discard()
    def packet_player_position_and_look(self,buff):
        self.x=buff.unpack('d'); self.y=buff.unpack('d'); self.z=buff.unpack('d'); self.yaw=buff.unpack('f'); self.pitch=buff.unpack('f'); buff.discard()
    def packet_keep_alive(self,buff): buff.discard()
    def packet_player_digging(self,buff):
        status=buff.unpack_varint(); pos=buff.unpack_position(); face=buff.unpack('B'); buff.discard()
        x,y,z=pos
        if status in (0,2):
            self.factory.bridge.world.set(x,y,z,AIR); self.factory.broadcast_block(x,y,z,AIR)
    def packet_use_item(self,buff): buff.discard()

class MCFactory(ServerFactory):
    protocol=MCProtocol; motd="LostMiner Bridge | mundo 2D fatiado"; max_players=50; online_mode=False
    force_protocol_version=340; compression_threshold=256
    def __init__(self, bridge): super().__init__(); self.bridge=bridge; self.mc_players=set(); self.minecraft_versions={340:'1.12.2'}
    def broadcast_mc(self, source, text):
        for p in list(self.mc_players):
            if not p.closed: p.send_chat(text)
    def broadcast_block(self,x,y,z,block):
        b=next(iter(self.mc_players)).buff_type if self.mc_players else None
        if not b: return
        # 1.12 block state ids: directly encoded in VarInt.
        for p in list(self.mc_players):
            if not p.closed: p.send_packet("block_change", b.pack_position(x,y,z)+b.pack_varint(block))

class LMClient:
    def __init__(self,state,reader,writer): self.state=state; self.reader=reader; self.writer=writer; self.name="Player"; self.x=0.5; self.y=3.; self.z=0.5
    def public_state(self): return {"name":self.name,"x":self.x,"y":self.y,"z":self.z,"skin":"female_default"}
    async def send(self,obj):
        self.writer.write((json.dumps(obj,separators=(',',':'))+'\n').encode()); await self.writer.drain()
    async def run(self):
        await self.send({"type":"hello","protocol":1,"world":"lostminer-2d-slice","slice_z":self.state.world.slice_z})
        await self.send(self.state.snapshot())
        while True:
            line=await self.reader.readline()
            if not line: break
            obj=json.loads(line); typ=obj.get('type')
            if typ=='login': self.name=str(obj.get('name','Player'))[:32]; self.state.players[self.name]=self; await self.state.broadcast_lm({"type":"player_joined","player":self.public_state()},self)
            elif typ=='chat': await self.state.broadcast_lm(self.state.chat(self.name,str(obj.get('text',''))),None)
            elif typ=='move': self.x=float(obj.get('x',self.x)); self.y=float(obj.get('y',self.y)); self.z=float(obj.get('z',self.z)); await self.state.broadcast_lm({"type":"player_move","player":self.public_state()},self)
            elif typ in ('break','place'):
                x,y,z=int(obj['x']),int(obj['y']),int(obj.get('z',self.state.world.slice_z)); block=0 if typ=='break' else int(obj.get('block',STONE)); self.state.world.set(x,y,z,block); await self.state.broadcast_lm({"type":"block_change","x":x,"y":y,"z":z,"block":block},None)
            elif typ=='ping': await self.send({"type":"pong"})
    async def close(self):
        if self.state.players.get(self.name) is self: self.state.players.pop(self.name,None)
        try: await self.state.broadcast_lm({'type':'player_left','name':self.name},self)
        except Exception: pass
        self.writer.close()
        try: await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError): pass

async def lm_handler(reader,writer,state):
    c=LMClient(state,reader,writer); LOG.info('Conexão LostMiner de %s',writer.get_extra_info('peername'))
    try: await c.run()
    except Exception as e: LOG.warning('LostMiner desconectado: %s',e)
    finally: await c.close()

async def run_lm(state,host,port): return await asyncio.start_server(lambda r,w:lm_handler(r,w,state),host,port)

async def http_health(reader, writer, state):
    try:
        await reader.readline()
        while True:
            line = await reader.readline()
            if not line or line in (b'\r\n', b'\n'): break
        body = json.dumps({"ok": True, "service": "lostminer-bridge", "protocol": 340,
                           "minecraft_port": 25565, "lostminer_port": 25570,
                           "players": len(state.players) + len(state.mc_players)}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
    finally:
        writer.close()
        try: await writer.wait_closed()
        except Exception: pass

async def run_http(state, host, port):
    return await asyncio.start_server(lambda r,w: http_health(r,w,state), host, port)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--host',default='0.0.0.0'); ap.add_argument('--mc-port',type=int,default=25565); ap.add_argument('--lostminer-port',type=int,default=25570); ap.add_argument('--world',default='world/world.json'); ap.add_argument('--slice-z',type=int,default=0); ap.add_argument('--slice-depth',type=int,default=1)
    a=ap.parse_args(); logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    world=World(a.world,a.slice_z,a.slice_depth); state=BridgeState(world); factory=MCFactory(state)
    loop=asyncio.get_event_loop(); factory.listen(a.host,a.mc_port)
    lm_server=loop.run_until_complete(run_lm(state,a.host,a.lostminer_port))
    render_port = os.environ.get('PORT')
    http_server = loop.run_until_complete(run_http(state,a.host,int(render_port))) if render_port else None
    LOG.info('Minecraft: %s:%d | LostMiner: %s:%d',a.host,a.mc_port,a.host,a.lostminer_port)
    if render_port: LOG.info('HTTP health: %s:%s',a.host,render_port)
    def save(): world.save(); LOG.info('Mundo salvo')
    reactor.addSystemEventTrigger('before','shutdown',save)
    try: reactor.run()
    except KeyboardInterrupt: reactor.stop()

if __name__=='__main__': main()
