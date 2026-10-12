# LostMiner Bridge Server

Servidor Python experimental para compartilhar um mundo autoritativo entre Minecraft Java 1.12.2 e um conector LostMiner.

## O que já funciona

- Minecraft Java 1.12.2 / protocolo 340.
- Login offline para rede local.
- Mundo 3D com uma fatia jogável limitada no eixo Z, produzindo o visual de mundo 2D.
- Plataforma de bedrock, terra e grama.
- Envio de chunks ao Minecraft.
- Chat Minecraft.
- Posição de jogadores.
- Quebra de blocos via pacote de digging.
- Salvamento em `world/world.json`.
- Listener local para um cliente LostMiner modificado em `25570`.
- Sinalização nativa do APK original via UDP `4773` e oferta/resposta WebRTC.

## Limitação importante

O APK original do LostMiner usa UDP/WebRTC e não conecta diretamente ao listener JSON deste pacote. O listener `25570` é o contrato do **conector que deverá ser adicionado ao APK modificado**. Não é correto afirmar que o APK original já funciona com ele.

O servidor agora também implementa a primeira camada do protocolo nativo do APK: descoberta `0xFF`, `WANTSCLIENT` (`20`), `CLIENTOK` (`21`), `IDOK` (`22`) e negociação de oferta/resposta WebRTC usando os canais `RC` e `UC`.

Para testar o APK original em uma rede local, use a porta UDP `4773`:

```bash
python3 server.py --host 0.0.0.0 --mc-port 25565 --lostminer-port 25570 --lostminer-udp-port 4773
```

O APK original deve receber o IP do servidor e a porta `4773`. A porta `25570` continua sendo somente do conector JSON experimental.

### Liberar a porta UDP automaticamente

Na VPS Ubuntu, execute:

```bash
python3 setup_firewall.py
```

Esse script libera somente `4773/udp` no UFW. Ele não cria, remove nem altera regras da porta TCP do Minecraft.

Para conferir as regras sem alterar nada:

```bash
python3 setup_firewall.py --check
```

Para descobrir o IP público da VPS:

```bash
curl -4 ifconfig.me
```

O endereço usado no LostMiner será:

```text
IP_PUBLICO_DA_VPS:4773
```

## Instalação Linux

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip
cd lostminer_bridge
bash start.sh
```

Ou, sem instalar a cada execução:

```bash
python3 -m pip install -r requirements.txt
python3 server.py --host 0.0.0.0 --mc-port 25565 --lostminer-port 25570
```

## Conexão Minecraft

Use a versão **Java 1.12.2**, crie uma instalação com modo offline/local e conecte em:

```text
192.168.15.20:25565
```

O computador que executar o servidor precisa realmente possuir esse IP. No sandbox de desenvolvimento o IP LAN não está disponível; por isso o servidor escuta em `0.0.0.0`.

## Ajuste da fatia 2D

Por padrão o mundo usa somente `Z=0`:

```bash
python3 server.py --slice-z 0 --slice-depth 1
```

Para permitir três camadas de profundidade:

```bash
python3 server.py --slice-z -1 --slice-depth 3
```

## Protocolo do conector LostMiner

Cada mensagem é uma linha JSON UTF-8.

Cliente -> servidor:

```json
{"type":"login","name":"Jogador"}
{"type":"move","x":1.5,"y":3.0,"z":0.5}
{"type":"chat","text":"olá"}
{"type":"break","x":4,"y":2,"z":0}
{"type":"place","x":4,"y":3,"z":0,"block":1}
{"type":"ping"}
```

Servidor -> cliente:

```json
{"type":"hello","protocol":1,"world":"lostminer-2d-slice"}
{"type":"snapshot","players":[]}
{"type":"player_joined","player":{"name":"Jogador","skin":"female_default"}}
{"type":"player_move","player":{}}
{"type":"block_change","x":4,"y":2,"z":0,"block":0}
{"type":"chat","from":"Jogador","text":"olá"}
```

## Firewall

No computador servidor, libere as portas TCP:

```bash
sudo ufw allow 25565/tcp
sudo ufw allow 25570/tcp
```

## Próxima etapa

O APK modificado precisa ganhar uma tela/ opção `Servidor Python` e implementar o contrato acima. A rede original do LostMiner continuará disponível separadamente.

## Deploy no Railway

O repositório inclui `Dockerfile`, `railway.toml` e política de reinício. No Railway:

1. Crie um projeto a partir de `Tavoxx01/LostMiner`.
2. Faça o deploy usando o `Dockerfile` da raiz.
3. Adicione um **Volume** montado em `/app/world` para o `world.json` sobreviver aos redeploys.
4. Em **Settings > Networking**, crie um **TCP Proxy** para a porta interna `25565`.
5. Use o host e a porta TCP gerados pelo Railway no Minecraft.
6. Para o conector LostMiner, crie outro TCP Proxy para a porta interna `25570`.

O multiplayer nativo do APK original precisa de UDP `4773` para descoberta/sinalização. Railway TCP Proxy não encaminha UDP; portanto, o APK original não conseguirá usar o protocolo nativo através somente do Railway. Para isso, use uma VPS/host que ofereça UDP público ou mantenha o servidor na rede local. No Railway, use o conector TCP modificado em `25570`.

O domínio HTTP do Railway não é o endereço do Minecraft. Para o Minecraft, use o host e a porta mostrados no TCP Proxy.

## Deploy em VPS Ubuntu

```bash
sudo apt update && sudo apt install -y python3 python3-pip git
sudo git clone https://github.com/Tavoxx01/LostMiner.git /opt/lostminer_bridge
cd /opt/lostminer_bridge/lostminer_bridge
sudo pip3 install -r requirements.txt
sudo useradd --system --home /opt/lostminer_bridge --shell /usr/sbin/nologin lostminer || true
sudo chown -R lostminer:lostminer /opt/lostminer_bridge
sudo cp lostminer-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lostminer-bridge
sudo ufw allow 25565/tcp
sudo ufw allow 25570/tcp
sudo systemctl status lostminer-bridge
```

## Sobre Render

O Render Web Service foi projetado para receber tráfego HTTP em uma porta informada pela variável `PORT`; ele não fornece uma porta TCP pública arbitrária para clientes Minecraft. Portanto, este servidor não deve ser publicado diretamente como Web Service no Render para jogadores entrarem pelo endereço HTTP.

Para hospedar o protocolo Minecraft, use um host que ofereça TCP público, uma VPS, Railway com TCP Proxy, Koyeb TCP Proxy ou um túnel TCP como playit.gg. Se você usar o Render somente para painel/health-check, o jogo ainda precisará ficar em um host TCP separado.

### Teste gratuito no Render

O código agora detecta a variável `PORT` do Render e abre um endpoint HTTP de saúde nessa porta. Isso permite testar o deploy gratuitamente:

1. No Render, clique em **New > Web Service**.
2. Selecione o repositório privado `Tavoxx01/LostMiner`.
3. Branch: `main`.
4. Runtime: `Python 3`.
5. Build Command:

   ```bash
   pip install -r lostminer_bridge/requirements.txt
   ```

6. Start Command:

   ```bash
   python3 lostminer_bridge/server.py --host 0.0.0.0 --mc-port 25565 --lostminer-port 25570
   ```

7. Selecione o plano **Free** e crie o serviço.
8. Quando terminar, abra a URL `https://NOME.onrender.com`.

Ela deverá responder com JSON contendo `ok: true`, `protocol: 340` e a quantidade de jogadores. Esse teste confirma que o processo iniciou, mas **não transforma a URL do Render em endereço Minecraft**: o Render encaminha apenas uma porta HTTP pública por Web Service.
