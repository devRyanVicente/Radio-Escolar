import time
import logging
import threading
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import re

import gspread
from gspread.exceptions import APIError
from oauth2client.service_account import ServiceAccountCredentials
import yt_dlp

# ==============================
# LOG
# ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("robo_playlist.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ==============================
# GOOGLE SHEETS
# ==============================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
client = gspread.authorize(creds)

SHEET_ID = "*"

ws_pedidos   = client.open_by_key(SHEET_ID).worksheet("Pedidos")
ws_playlist  = client.open_by_key(SHEET_ID).worksheet("Playlist")
ws_historico = client.open_by_key(SHEET_ID).worksheet("Historico")
ws_moderacao = client.open_by_key(SHEET_ID).worksheet("Moderação")
ws_blacklist = client.open_by_key(SHEET_ID).worksheet("Blacklist")
ws_horarios  = client.open_by_key(SHEET_ID).worksheet("Horarios")

# Convenções de colunas (1-based):
# 1: ? (timestamp ou id), 2: Email, 3: Nome, 4: Mensagem, 5: Link, 6: Status, 7: Status Mensagem
# Na aba Historico, escrevemos também a 8: Observação

# ==============================
# UTIL
# ==============================
def contem_link(texto: str) -> bool:
    if not texto:
        return False
    padrao = re.compile(r"(https?://|www\.|\.[a-z]{2,})", re.IGNORECASE)
    return bool(padrao.search(texto))

def safe_delete_row(ws, idx, cols=7, planilha_nome=""):
    """Tenta deletar a linha. Se for planilha de Form (erro 400), 'limpa' a linha."""
    try:
        ws.delete_rows(idx)
    except APIError as e:
        msg = str(e)
        if "Cannot delete row with form questions" in msg or "Invalid requests[0].deleteDimension" in msg:
            # limpa a linha inteira (mantém estrutura do Form)
            rng = f"A{idx}:{chr(ord('A')+cols-1)}{idx}"
            ws.update(rng, [ [""]*cols ])
            log.info(f"[{planilha_nome}] Linha {idx} limpa (não deletada por ser aba de formulário).")
        else:
            raise

# ==============================
# VALIDAÇÃO DE LINKS
# ==============================
def eh_link_youtube(url: str) -> bool:
    try:
        p = urlparse(url)
        d = p.netloc.lower()
        return ("youtube.com" in d or "youtu.be" in d)
    except:
        return False

def _parece_video_youtube(url: str) -> (bool, str):
    """Heurística rápida: aceita watch?v=, youtu.be/<id>, /shorts/<id>, /live/<id>. Recusa se tiver list=."""
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
        path = p.path.lower()

        if "list" in q or "playlist" in path:
            return False, "Link é playlist"

        if "youtu.be" in p.netloc.lower():
            # youtu.be/<id>
            return (len(path.strip("/")) > 0, "Formato de link inválido" if len(path.strip("/")) == 0 else "OK")

        if "/watch" in path and "v" in q and q["v"][0].strip():
            return True, "OK"

        if path.startswith("/shorts/") and len(path.split("/")) >= 3 and path.split("/")[2].strip():
            return True, "OK"

        if path.startswith("/live/") and len(path.split("/")) >= 3 and path.split("/")[2].strip():
            return True, "OK"

        return False, "Formato de link inválido"
    except Exception:
        return False, "Formato de link inválido"

def validar_link_youtube(url: str):
    """
    Camada 1: heurística rápida (domínio + formato + sem playlist).
    Camada 2: yt_dlp para confirmar (recusa +18 e qualquer playlist/multivídeo).
    """
    if not eh_link_youtube(url):
        return False, "Link não é do YouTube"

    ok_formato, motivo_formato = _parece_video_youtube(url)
    if not ok_formato:
        return False, motivo_formato

    try:
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "socket_timeout": 10,
            "nocheckcertificate": True,
            "geo_bypass": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Recusa conteúdo adulto
        if info.get("age_limit", 0) >= 18:
            return False, "Restrição de idade"

        # Recusa playlists/multivídeo
        if info.get("_type") in ("playlist", "multi_video"):
            return False, "Link é uma playlist"

        # Se passou, é vídeo OK (normal, shorts ou live)
        return True, "OK"
    except Exception as e:
        return False, f"Erro ao validar link: {e}"

# ==============================
# MOVIMENTAÇÃO ENTRE ABAS
# ==============================
def mover_para_historico_com_recusa(row_original, motivo):
    """
    Garante 8 colunas na escrita do Histórico e ajusta:
    - Status (col 6) = 'Recusado'
    - Status Mensagem (col 7) = ''
    - Observação (col 8) = motivo
    """
    base = (row_original + [""] * (7 - len(row_original)))[:7]
    base[5] = "Recusado"  # Status
    base[6] = ""          # Status Msg
    historico = base + [f"Recusado pelo Robo: {motivo}"]  # Observação
    ws_historico.append_row(historico, value_input_option="USER_ENTERED")
    log.info(f"[Historico] Recusado -> Nome='{base[2]}' Link='{base[4]}' | {motivo}")

def mover_para_moderacao(row):
    row = (row + [""] * (7 - len(row)))[:7]
    row[5] = "Aguardando Aprovação"  # Status
    ws_moderacao.append_row(row, value_input_option="USER_ENTERED")
    log.info(f"[Moderação] Enviado -> Nome='{row[2]}' Link='{row[4]}'")

def mover_para_playlist(row):
    ws_playlist.append_row(row, value_input_option="USER_ENTERED")
    log.info(f"[Playlist] Adicionado -> Nome='{row[2]}' Link='{row[4]}'")

    # Garante linha vazia no final
    valores = ws_playlist.get_all_values()
    if valores and any(c.strip() for c in valores[-1]):
        ws_playlist.append_row([""] * 7, value_input_option="USER_ENTERED")
        log.info("[Playlist] Linha vazia adicionada no final.")

# ==============================
# HORÁRIOS / LIMPEZA PLAYLIST
# ==============================
def horario_ativo():
    try:
        rows = ws_horarios.get_all_values()[1:]  # pula cabeçalho
        agora = datetime.now().time()
        for r in rows:
            if len(r) < 3:
                continue
            inicio_str, fim_str, ativo = r[:3]
            if ativo.strip().lower() != "sim":
                continue
            inicio = datetime.strptime(inicio_str.strip(), "%H:%M").time()
            fim    = datetime.strptime(fim_str.strip(), "%H:%M").time()
            if inicio <= fim:
                if inicio <= agora <= fim:
                    return True
            else:
                # faixa atravessa meia-noite
                if agora >= inicio or agora <= fim:
                    return True
        return False
    except Exception as e:
        log.error(f"[Horarios] Erro: {e}")
        return True  # fallback seguro

def mover_playlist_para_historico_quando_fora_do_horario():
    """
    Move APENAS linhas com Status == 'Tocado'/'Tocada' para o Histórico.
    Mantém (ou cria) uma linha 100% vazia no final da Playlist.
    """
    try:
        rows = ws_playlist.get_all_values()
        if len(rows) <= 1:
            return

        corpo = rows[1:]  # sem cabeçalho
        linhas_para_apagar = []

        for idx_excel, r in enumerate(corpo, start=2):  # 2 = porque linha 1 é cabeçalho
            status = (r[5].strip().lower() if len(r) > 5 else "")
            if status in ("tocado", "tocada"):
                base = (r + [""] * (7 - len(r)))[:7]
                historico = base + ["Encerrado pelo horário"]
                ws_historico.append_row(historico, value_input_option="USER_ENTERED")
                linhas_para_apagar.append(idx_excel)

        if not linhas_para_apagar:
            return

        # Apaga de baixo pra cima
        for idx in reversed(linhas_para_apagar):
            try:
                ws_playlist.delete_rows(idx)
            except APIError as e:
                # Playlist normalmente não é formulário; se falhar, limpamos a linha
                rng = f"A{idx}:G{idx}"
                ws_playlist.update(rng, [[""] * 7])

        # Garante linha vazia final
        valores = ws_playlist.get_all_values()
        if not valores or any(c.strip() for c in valores[-1]):
            ws_playlist.append_row([""] * 7, value_input_option="USER_ENTERED")

        log.info(f"[Playlist] Movidas {len(linhas_para_apagar)} músicas 'Tocado' para histórico (fim do horário).")
    except Exception as e:
        log.error(f"[Playlist] Erro mover -> {e}")

# ==============================
# PROCESSOS
# ==============================
def processar_pedidos():
    rows = ws_pedidos.get_all_values()
    # blacklist (pula cabeçalho)
    blacklist = [r[0].strip().lower() for r in ws_blacklist.get_all_values()[1:] if r and r[0].strip()]

    aceitos, recusados = 0, 0

    for i in range(len(rows), 1, -1):  # de baixo pra cima
        row = rows[i-1]
        if not any((c or "").strip() for c in row):
            continue

        email = (row[1] if len(row) > 1 else "").strip().lower()
        nome  = (row[2] if len(row) > 2 else "").strip()
        msg   = (row[3] if len(row) > 3 else "").strip()
        link  = (row[4] if len(row) > 4 else "").strip()

        # 1) Blacklist
        if email in blacklist:
            mover_para_historico_com_recusa(row, "Email em blacklist")
            safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
            recusados += 1
            continue

        # 2) Nome/Mensagem com links
        if contem_link(nome):
            mover_para_historico_com_recusa(row, "Nome contém link")
            safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
            recusados += 1
            continue

        if contem_link(msg):
            mover_para_historico_com_recusa(row, "Mensagem contém link")
            safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
            recusados += 1
            continue

        # 3) Tamanho
        if len(nome) > 32:
            mover_para_historico_com_recusa(row, "Nome muito grande")
            safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
            recusados += 1
            continue

        if len(msg) > 72:
            mover_para_historico_com_recusa(row, "Mensagem muito grande")
            safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
            recusados += 1
            continue

        # 4) Link YouTube
        ok, motivo = validar_link_youtube(link)
        if not ok:
            mover_para_historico_com_recusa(row, motivo)
            safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
            recusados += 1
            continue

        # 5) Envia pra moderação
        mover_para_moderacao((row + [""] * (7 - len(row)))[:7])
        safe_delete_row(ws_pedidos, i, cols=7, planilha_nome="Pedidos")
        aceitos += 1

    log.info(f"[Pedidos] Aceitos={aceitos} | Recusados={recusados}")

def processar_moderacao():
    rows = ws_moderacao.get_all_values()
    aceitos, recusados = 0, 0

    for i in range(len(rows), 1, -1):  # de baixo pra cima
        row = rows[i-1]
        if not any((c or "").strip() for c in row):
            continue

        status     = (row[5] if len(row) > 5 else "").strip().lower()
        status_msg = (row[6] if len(row) > 6 else "").strip()

        if status == "recusado":
            base = (row + [""] * (7 - len(row)))[:7]
            historico = base + ["Recusado pelo Moderador"]
            ws_historico.append_row(historico, value_input_option="USER_ENTERED")
            try:
                ws_moderacao.delete_rows(i)
            except APIError:
                rng = f"A{i}:G{i}"
                ws_moderacao.update(rng, [[""] * 7])
            recusados += 1

        elif status == "aceito" and status_msg:
            mover_para_playlist((row + [""] * (7 - len(row)))[:7])
            try:
                ws_moderacao.delete_rows(i)
            except APIError:
                rng = f"A{i}:G{i}"
                ws_moderacao.update(rng, [[""] * 7])
            aceitos += 1

    log.info(f"[Moderação] Aceitos={aceitos} | Recusados={recusados}")

# ==============================
# WORKERS
# ==============================
def worker_pedidos():
    while True:
        try:
            processar_pedidos()
        except Exception as e:
            log.error(f"[Worker Pedidos] Erro: {e}")
        time.sleep(60)

def worker_moderacao():
    while True:
        try:
            processar_moderacao()
        except Exception as e:
            log.error(f"[Worker Moderacao] Erro: {e}")
        time.sleep(60)

def worker_horarios():
    while True:
        try:
            if not horario_ativo():
                mover_playlist_para_historico_quando_fora_do_horario()
        except Exception as e:
            log.error(f"[Worker Horarios] Erro: {e}")
        time.sleep(60)

# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    log.info("=== Robo iniciado com workers ===")
    threading.Thread(target=worker_pedidos,   daemon=True, name="T-Pedidos").start()
    threading.Thread(target=worker_moderacao, daemon=True, name="T-Moderacao").start()
    threading.Thread(target=worker_horarios,  daemon=True, name="T-Horarios").start()

    # Mantém vivo
    while True:
        time.sleep(1)
