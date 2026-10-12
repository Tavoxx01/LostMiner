#!/usr/bin/env python3
"""Libera a porta UDP do multiplayer nativo do LostMiner.

Não altera a porta TCP do Minecraft. Execute na VPS/Ubuntu onde o servidor roda.
"""
import argparse
import shutil
import subprocess
import sys


def run(cmd):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=False).returncode


def main():
    ap = argparse.ArgumentParser(description="Configura firewall UDP do LostMiner")
    ap.add_argument("--port", type=int, default=4773, help="porta UDP do LostMiner (padrão: 4773)")
    ap.add_argument("--check", action="store_true", help="mostra o estado do UFW sem alterar regras")
    args = ap.parse_args()

    if shutil.which("ufw") is None:
        print("Erro: ufw não está instalado. Instale com: sudo apt update && sudo apt install -y ufw", file=sys.stderr)
        return 2
    if args.check:
        return run(["sudo", "ufw", "status", "verbose"])

    # O Minecraft/TCP não é modificado por este script.
    rc = run(["sudo", "ufw", "allow", f"{args.port}/udp", "comment", "LostMiner native multiplayer"])
    if rc != 0:
        return rc
    print(f"OK: UDP {args.port} liberada para o LostMiner.")
    print("A porta TCP do Minecraft não foi alterada.")
    print("IP público sugerido:")
    print("  curl -4 ifconfig.me")
    print(f"Conexão do LostMiner: IP_DA_VPS:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
