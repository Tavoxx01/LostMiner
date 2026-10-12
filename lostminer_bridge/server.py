#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from twisted.internet import asyncioreactor

laco_asyncio = asyncio.new_event_loop()
asyncio.set_event_loop(laco_asyncio)
try:
    asyncioreactor.install(laco_asyncio)
except Exception:
    pass

from twisted.internet import reactor
from twisted.internet.task import LoopingCall
from quarry.net.server import ServerFactory, ServerProtocol
from quarry.types.chunk import BlockArray
from lostminer_native import LostMinerNativeProtocol

LOG = logging.getLogger("lostminer_bridge")

PROTOCOLO = 340
AR, PEDRA, GRAMA, TERRA, BEDROCK = 0, 1, 2, 3, 7
BIOMAS = bytes([1]) * 256
FACES = {0: (0, -1, 0), 1: (0, 1, 0), 2: (0, 0, -1), 3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0)}


class Registro:
    max_bits = 13

    def encode_block(self, obj):
        bloco = int(obj.get("id", 0)) if isinstance(obj, dict) else int(obj)
        return bloco << 4

    def decode_block(self, val):
        return int(val) >> 4

    def is_air_block(self, obj):
        return int(obj) == AR


REGISTRO = Registro()


class Luz:
    def __init__(self, valor=15):
        self.dados = bytes([0xFF if valor else 0x00] * 2048)

    def to_bytes(self):
        return self.dados


LUZ = Luz()


class Mundo:
    def __init__(self, caminho, slice_z=0, slice_depth=1):
        self.caminho = Path(caminho)
        self.slice_z = slice_z
        self.slice_depth = slice_depth
        self.blocos = {}
        self.indice = set()
        self.sujo = False
        self.carregar()

    def chave(self, x, y, z):
        return f"{x},{y},{z}"

    def indexar(self, x, y, z):
        self.indice.add((x // 16, z // 16, y // 16))

    def pegar(self, x, y, z):
        if z < self.slice_z or z >= self.slice_z + self.slice_depth:
            return AR
        k = self.chave(x, y, z)
        if k in self.blocos:
            return self.blocos[k]
        if y <= 0:
            return BEDROCK
        if y == 1:
            return TERRA
        if y == 2:
            return GRAMA
        return AR

    def colocar(self, x, y, z, bloco):
        if z < self.slice_z or z >= self.slice_z + self.slice_depth or not (0 <= y < 256):
            return False
        self.blocos[self.chave(x, y, z)] = int(bloco)
        self.indexar(x, y, z)
        self.sujo = True
        return True

    def carregar(self):
        if not self.caminho.exists():
            return
        try:
            dados = json.loads(self.caminho.read_text())
            self.blocos = {str(k): int(v) for k, v in dados.items()}
            for k in self.blocos:
                x, y, z = (int(n) for n in k.split(","))
                self.indexar(x, y, z)
        except Exception as e:
            LOG.warning("Falha ao carregar mundo: %s", e)
            self.blocos = {}
            self.indice = set()

    def salvar(self):
        try:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.caminho.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.blocos, separators=(",", ":")))
            tmp.replace(self.caminho)
            self.sujo = False
        except Exception as e:
            LOG.warning("Falha ao salvar mundo: %s", e)

    def secoes(self, cx, cz):
        z0 = self.slice_z
        z1 = self.slice_z + self.slice_depth
        resultado = []
        for sy in range(16):
            if sy > 0 and (cx, cz, sy) not in self.indice:
                resultado.append(None)
                continue
            blocos = BlockArray.empty(REGISTRO)
            for lz in range(16):
                z = cz * 16 + lz
                if z < z0 or z >= z1:
                    continue
                for lx in range(16):
                    x = cx * 16 + lx
                    for ly in range(16):
                        b = self.pegar(x, sy * 16 + ly, z)
                        if b:
                            blocos[(ly * 16 + lz) * 16 + lx] = b
            if blocos.is_empty():
                resultado.append(None)
            else:
                resultado.append((blocos, LUZ, LUZ))
        return resultado


class Ponte:
    def __init__(self, mundo):
        self.mundo = mundo
        self.players = {}
        self.mc_players = set()
        self.fabrica = None

    def snapshot(self):
        return {"type": "snapshot", "slice_z": self.mundo.slice_z, "slice_depth": self.mundo.slice_depth,
                "players": [p.public_state() for p in self.players.values()]}

    async def broadcast_lm(self, obj, excluir=None):
        for p in list(self.players.values()):
            if p is excluir:
                continue
            try:
                await p.send(obj)
            except Exception:
                if self.players.get(p.name) is p:
                    self.players.pop(p.name, None)

    def agendar_lm(self, obj):
        asyncio.ensure_future(self.broadcast_lm(obj))

    def chat(self, nome, texto):
        return {"type": "chat", "from": nome, "text": texto[:256], "ts": int(time.time())}


class MCProtocolo(ServerProtocol):
    def data_received(self, data):
        try:
            return super().data_received(data)
        except Exception as e:
            self.descartar(e)

    def protocol_error(self, err):
        self.descartar(err)

    def descartar(self, motivo):
        if self.in_game:
            LOG.warning("Erro de protocolo com %s: %s", getattr(self, "display_name", "?"), motivo)
        else:
            LOG.info("Conexao invalida descartada de %s: %s", self.remote_addr.host, motivo)
        self.closed = True
        try:
            self.transport.abortConnection()
        except Exception:
            pass

    def packet_handshake(self, buff):
        try:
            super().packet_handshake(buff)
        finally:
            if self.protocol_version not in self.factory.minecraft_versions:
                self.protocol_version = PROTOCOLO
                self.buff_type = self.factory.get_buff_type(PROTOCOLO)

    def player_joined(self):
        super().player_joined()
        self.id_entidade = id(self) & 0x7fffffff
        self.x, self.y, self.z = 0.5, 3.0, 0.5
        self.yaw = 0.0
        self.pitch = 0.0
        self.contador_ka = 0
        self.factory.bridge.mc_players.add(self)
        self.enviar_mundo()
        self.enviar_chat("Bem-vindo ao mundo fatiado 2D do LostMiner Bridge!", "Bridge")
        self.laco_ka = LoopingCall(self.enviar_keepalive)
        self.laco_ka.start(10, now=False)
        LOG.info("Minecraft entrou: %s", self.display_name)

    def player_left(self):
        try:
            if self.laco_ka.running:
                self.laco_ka.stop()
        except Exception:
            pass
        self.factory.bridge.mc_players.discard(self)
        super().player_left()
        LOG.info("Minecraft saiu: %s", self.display_name)

    def enviar_keepalive(self):
        if self.closed:
            return
        self.contador_ka += 1
        self.send_packet("keep_alive", self.buff_type.pack("q", self.contador_ka))

    def enviar_chat(self, texto, remetente=None):
        msg = f"<{remetente}> {texto}" if remetente else texto
        self.send_packet("chat_message", self.buff_type.pack_chat(msg), self.buff_type.pack("B", 0))

    def enviar_mundo(self):
        b = self.buff_type
        self.send_packet("join_game", b.pack("i B i B B", self.id_entidade, 1, 0, 0, 20),
                         b.pack_string("default"), b.pack("?", False))
        self.send_packet("spawn_position", b.pack_position(0, 3, 0))
        self.send_packet("player_abilities", b.pack("B ff", 0x0D, 0.05, 0.1))
        self.send_packet("time_update", b.pack("q q", 0, 6000))
        for cx in range(-3, 4):
            for cz in range(-3, 4):
                self.enviar_chunk(cx, cz)
        self.send_packet("player_position_and_look",
                         b.pack("dddffB", self.x, self.y, self.z, self.yaw, self.pitch, 0) + b.pack_varint(0))

    def enviar_chunk(self, cx, cz):
        b = self.buff_type
        secoes = self.factory.bridge.mundo.secoes(cx, cz)
        dados = b.pack_chunk(secoes) + BIOMAS
        mascara = b.pack_chunk_bitmask(secoes)
        carga = b.pack("ii?", cx, cz, True) + mascara + b.pack_varint(len(dados)) + dados + b.pack_varint(0)
        self.send_packet("chunk_data", carga)

    def packet_chat_message(self, buff):
        texto = buff.unpack_string()[:256]
        self.factory.transmitir_mc(f"<{self.display_name}> {texto}")
        self.factory.bridge.agendar_lm(self.factory.bridge.chat(self.display_name, texto))

    def packet_player_position(self, buff):
        self.x = buff.unpack("d")
        self.y = buff.unpack("d")
        self.z = buff.unpack("d")
        buff.discard()

    def packet_player_position_and_look(self, buff):
        self.x = buff.unpack("d")
        self.y = buff.unpack("d")
        self.z = buff.unpack("d")
        self.yaw = buff.unpack("f")
        self.pitch = buff.unpack("f")
        buff.discard()

    def packet_keep_alive(self, buff):
        buff.discard()

    def packet_player_digging(self, buff):
        status = buff.unpack_varint()
        x, y, z = buff.unpack_position()
        buff.discard()
        if status in (0, 2):
            ponte = self.factory.bridge
            if ponte.mundo.colocar(x, y, z, AR):
                self.factory.enviar_bloco(x, y, z, AR)
                ponte.agendar_lm({"type": "block_change", "x": x, "y": y, "z": z, "block": AR})
            else:
                self.send_packet("block_change", self.buff_type.pack_position(x, y, z) + self.buff_type.pack_varint(0))

    def packet_player_block_placement(self, buff):
        x, y, z = buff.unpack_position()
        face = buff.unpack_varint()
        buff.discard()
        dx, dy, dz = FACES.get(face, (0, 0, 0))
        x, y, z = x + dx, y + dy, z + dz
        ponte = self.factory.bridge
        if ponte.mundo.colocar(x, y, z, PEDRA):
            self.factory.enviar_bloco(x, y, z, PEDRA)
            ponte.agendar_lm({"type": "block_change", "x": x, "y": y, "z": z, "block": PEDRA})
        else:
            self.send_packet("block_change", self.buff_type.pack_position(x, y, z) + self.buff_type.pack_varint(0))

    def packet_use_item(self, buff):
        buff.discard()


class MCFabrica(ServerFactory):
    protocol = MCProtocolo
    motd = "LostMiner Bridge | mundo 2D fatiado"
    max_players = 50
    online_mode = False
    force_protocol_version = PROTOCOLO
    compression_threshold = 256

    def __init__(self, ponte):
        super().__init__()
        self.bridge = ponte
        self.minecraft_versions = {PROTOCOLO: "1.12.2"}

    def transmitir_mc(self, texto):
        for p in list(self.bridge.mc_players):
            if not p.closed:
                p.enviar_chat(texto)

    def enviar_bloco(self, x, y, z, bloco):
        for p in list(self.bridge.mc_players):
            if not p.closed:
                b = p.buff_type
                p.send_packet("block_change", b.pack_position(x, y, z) + b.pack_varint(int(bloco) << 4))


class LMCliente:
    def __init__(self, ponte, reader, writer):
        self.ponte = ponte
        self.reader = reader
        self.writer = writer
        self.name = "Player"
        self.x = 0.5
        self.y = 3.0
        self.z = 0.5

    def public_state(self):
        return {"name": self.name, "x": self.x, "y": self.y, "z": self.z, "skin": "female_default"}

    async def send(self, obj):
        self.writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        await self.writer.drain()

    async def run(self):
        ponte = self.ponte
        await self.send({"type": "hello", "protocol": 1, "world": "lostminer-2d-slice", "slice_z": ponte.mundo.slice_z})
        await self.send(ponte.snapshot())
        while True:
            linha = await self.reader.readline()
            if not linha:
                break
            try:
                obj = json.loads(linha)
                tipo = obj.get("type")
            except Exception:
                continue
            try:
                await self.tratar(tipo, obj)
            except (KeyError, ValueError, TypeError):
                continue

    async def tratar(self, tipo, obj):
        ponte = self.ponte
        if tipo == "login":
            self.name = str(obj.get("name", "Player"))[:32]
            ponte.players[self.name] = self
            await ponte.broadcast_lm({"type": "player_joined", "player": self.public_state()}, self)
        elif tipo == "chat":
            texto = str(obj.get("text", ""))
            await ponte.broadcast_lm(ponte.chat(self.name, texto), None)
            if ponte.fabrica:
                ponte.fabrica.transmitir_mc(f"<{self.name}> {texto[:256]}")
        elif tipo == "move":
            self.x = float(obj.get("x", self.x))
            self.y = float(obj.get("y", self.y))
            self.z = float(obj.get("z", self.z))
            await ponte.broadcast_lm({"type": "player_move", "player": self.public_state()}, self)
        elif tipo in ("break", "place"):
            x, y = int(obj["x"]), int(obj["y"])
            z = int(obj.get("z", ponte.mundo.slice_z))
            bloco = AR if tipo == "break" else int(obj.get("block", PEDRA))
            if ponte.mundo.colocar(x, y, z, bloco):
                await ponte.broadcast_lm({"type": "block_change", "x": x, "y": y, "z": z, "block": bloco}, None)
                if ponte.fabrica:
                    ponte.fabrica.enviar_bloco(x, y, z, bloco)
        elif tipo == "ping":
            await self.send({"type": "pong"})

    async def close(self):
        if self.ponte.players.get(self.name) is self:
            self.ponte.players.pop(self.name, None)
        try:
            await self.ponte.broadcast_lm({"type": "player_left", "name": self.name}, self)
        except Exception:
            pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


async def lm_handler(reader, writer, ponte):
    c = LMCliente(ponte, reader, writer)
    LOG.info("Conexao LostMiner de %s", writer.get_extra_info("peername"))
    try:
        await c.run()
    except Exception as e:
        LOG.warning("LostMiner desconectado: %s", e)
    finally:
        await c.close()


async def run_lm(ponte, host, porta):
    return await asyncio.start_server(lambda r, w: lm_handler(r, w, ponte), host, porta)


async def http_health(reader, writer, ponte, mc_port, lm_port):
    try:
        try:
            await asyncio.wait_for(reader.readline(), 5)
            while True:
                linha = await asyncio.wait_for(reader.readline(), 5)
                if not linha or linha in (b"\r\n", b"\n"):
                    break
        except Exception:
            pass
        corpo = json.dumps({"ok": True, "service": "lostminer-bridge", "protocol": PROTOCOLO,
                            "minecraft_port": mc_port, "lostminer_port": lm_port,
                            "players": len(ponte.players) + len(ponte.mc_players)}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(corpo)}\r\nConnection: close\r\n\r\n".encode() + corpo)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_http(ponte, host, porta, mc_port, lm_port):
    return await asyncio.start_server(lambda r, w: http_health(r, w, ponte, mc_port, lm_port), host, porta)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--mc-port", type=int, default=25565)
    ap.add_argument("--lostminer-port", type=int, default=25570)
    ap.add_argument("--lostminer-udp-port", type=int, default=4773)
    ap.add_argument("--world", default="world/world.json")
    ap.add_argument("--slice-z", type=int, default=0)
    ap.add_argument("--slice-depth", type=int, default=1)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mundo = Mundo(a.world, a.slice_z, a.slice_depth)
    ponte = Ponte(mundo)
    fabrica = MCFabrica(ponte)
    ponte.fabrica = fabrica
    laco = getattr(reactor, "_asyncioEventloop", laco_asyncio)
    fabrica.listen(a.host, a.mc_port)
    laco.run_until_complete(run_lm(ponte, a.host, a.lostminer_port))
    laco.run_until_complete(laco.create_datagram_endpoint(
        lambda: LostMinerNativeProtocol(ponte), local_addr=(a.host, a.lostminer_udp_port)))
    porta_http = os.environ.get("PORT")
    if porta_http and int(porta_http) not in (a.mc_port, a.lostminer_port):
        laco.run_until_complete(run_http(ponte, a.host, int(porta_http), a.mc_port, a.lostminer_port))
        LOG.info("HTTP health: %s:%s", a.host, porta_http)
    LOG.info("Minecraft TCP: %s:%d | LostMiner JSON TCP: %s:%d | LostMiner nativo UDP: %s:%d",
             a.host, a.mc_port, a.host, a.lostminer_port, a.host, a.lostminer_udp_port)

    def salvar():
        mundo.salvar()
        LOG.info("Mundo salvo")

    def salvar_se_mudou():
        if mundo.sujo:
            mundo.salvar()

    LoopingCall(salvar_se_mudou).start(30, now=False)
    reactor.addSystemEventTrigger("before", "shutdown", salvar)
    try:
        reactor.run()
    except KeyboardInterrupt:
        reactor.stop()


if __name__ == "__main__":
    main()
