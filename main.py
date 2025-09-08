import tkinter as tk
from tkinter import ttk
import vlc
import yt_dlp
import threading
import os
import time
from datetime import datetime
import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import psutil
from queue import Queue
import asyncio
import edge_tts
import tempfile
import random
import logging

# ===========================
# LOGGING (console + arquivo)
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(threadName)s] %(message)s',
    handlers=[
        logging.FileHandler('radio_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('radio')

# ===========================
# CONFIGURAÇÕES
# ===========================
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
CREDS_FILE = "creds.json"
SPREADSHEET_ID = "x" # coloque o id de sua sheet aqui!
LOTE_LEITURA = 20
INTERVALO_CHECK_NOVAS_MUSICAS = 60    # seg
INTERVALO_CHECK_HORARIOS = 300        # seg

# ===========================
# GOOGLE SHEETS
# ===========================
def autenticar_gspread():
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
        client = gspread.authorize(creds)
        log.info("[GSHEETS] Autenticação realizada com sucesso.")
        return client
    except Exception as e:
        log.error(f"[GSHEETS] ERRO CRÍTICO ao autenticar: {e}")
        return None

client = autenticar_gspread()
if not client:
    raise SystemExit("Não foi possível autenticar Google Sheets. Verifique creds.json.")

sheet_pedidos = client.open_by_key(SPREADSHEET_ID).worksheet("Playlist")
sheet_horarios = client.open_by_key(SPREADSHEET_ID).worksheet("Horarios")

# ===========================
# VARIÁVEIS GLOBAIS
# ===========================
player = None
is_paused = False

# itens da playlist: (linha, video_id, titulo, arquivo, nome_usuario, mensagem, status_msg)
playlist = []

download_queue = Queue()

# cache por ID e por título (para retrocompatibilidade)
cache_by_id = {}       # video_id -> caminho
cache_by_title = {}    # titulo_normalizado -> caminho

baixando_musicas = set()   # guarda video_ids em download
current_line = None
current_title = ""
current_video_id = None
musica_rodando = False

# controle de leitura da planilha
ultima_linha_lida = 0
linha_fim_atual = None
fim_da_lista = False
linha_atual = 0

# HORÁRIOS / TTS desligamento
horarios_cache = []
ultima_atualizacao = 0
INTERVALO_ATUALIZACAO = 300  # 5 minutos
ultimo_horario_fim = None
avisou_fim = False

# TTS
proximo_tts_file = None
tts_fim_horario_file = None
lock_tts = threading.Lock()

# locks
lock_geral = threading.Lock()
arrastando_barra = False

# ===========================
# FUNÇÕES AUXILIARES
# ===========================
def monitorar_desempenho(intervalo_log=5, arquivo="logs.txt"):
    pid = os.getpid()
    processo = psutil.Process(pid)
    with open(arquivo, "a", encoding="utf-8") as f:
        f.write(f"--- Monitor iniciado em {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    while True:
        try:
            uso_cpu = processo.cpu_percent(interval=5.0)
            uso_mem = processo.memory_info().rss / (1024*1024)
            num_threads = processo.num_threads()
            linha = f"CPU: {uso_cpu:.1f}% | Memória: {uso_mem:.1f} MB | Threads: {num_threads}"
            log.info(f"[MONITOR] {linha}")
            with open(arquivo, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + linha + "\n")
            time.sleep(intervalo_log)
        except (psutil.NoSuchProcess, FileNotFoundError):
            log.info("[MONITOR] Processo finalizado. Encerrando monitor.")
            break
        except Exception as e:
            log.error(f"[MONITOR] Erro inesperado: {e}")
            time.sleep(10)

# ===========================
# HORÁRIOS (cache + atualização controlada)
# ===========================
def atualizar_horarios():
    global horarios_cache, ultima_atualizacao
    try:
        rows = sheet_horarios.get_all_values()[1:]  # pula cabeçalho
        horarios_cache = []
        for row in rows:
            if len(row) < 3:
                continue
            inicio_str, fim_str, ativo = row[:3]
            if ativo.strip().lower() == "sim":
                try:
                    inicio = datetime.strptime(inicio_str.strip(), "%H:%M").time()
                    fim = datetime.strptime(fim_str.strip(), "%H:%M").time()
                    horarios_cache.append((inicio, fim))
                except Exception:
                    continue
        ultima_atualizacao = time.time()
        log.info("[Horarios] Atualizado cache de horários: %s", horarios_cache)
    except Exception as e:
        log.error(f"[Horarios] Erro ao atualizar cache: {e}")

def pode_tocar():
    """
    Usa cache para decisão rápida; atualiza o cache apenas a cada INTERVALO_ATUALIZACAO.
    Também atualiza global ultimo_horario_fim e avisou_fim (para coordenação com TTS).
    """
    global ultima_atualizacao, ultimo_horario_fim, avisou_fim

    tz = pytz.timezone("America/Sao_Paulo")
    agora_dt = datetime.now(tz)
    agora = agora_dt.time()

    # Atualiza cache só de tempos em tempos (para evitar bater sempre na API)
    if time.time() - ultima_atualizacao > INTERVALO_ATUALIZACAO:
        atualizar_horarios()

    for inicio, fim in horarios_cache:
        # faixa normal
        if inicio <= fim and inicio <= agora <= fim:
            ultimo_horario_fim = fim
            avisou_fim = False
            return True
        # faixa atravessando meia-noite
        if inicio > fim and (agora >= inicio or agora <= fim):
            ultimo_horario_fim = fim
            avisou_fim = False
            return True
    return False

# ===========================
# CACHE OFFLINE (por video_id + fallback título)
# ===========================
def _normalizar_titulo(nome: str) -> str:
    return ''.join(c for c in os.path.splitext(nome)[0] if c.isalnum() or c == ' ').strip()

def _indexar_arquivo(path: str):
    """
    Indexa 'path' nos caches:
      - se nome contiver '__<id>' no final (antes da extensão), usa esse id;
      - sempre indexa também por título normalizado (fallback).
    """
    base = os.path.basename(path)
    nome, ext = os.path.splitext(base)
    video_id = None
    if "__" in nome:
        # pega o sufixo depois de '__'
        possible_id = nome.split("__")[-1]
        if possible_id:
            video_id = possible_id
            cache_by_id[video_id] = path
    titulo_norm = _normalizar_titulo(nome.split("__")[0])
    if titulo_norm:
        cache_by_title[titulo_norm] = path

def atualizar_cache_offline():
    cache_by_id.clear()
    cache_by_title.clear()
    log.info("[CACHE] Atualizando cache de músicas offline...")
    for sub in os.listdir(DOWNLOADS_DIR):
        pasta = os.path.join(DOWNLOADS_DIR, sub)
        if os.path.isdir(pasta):
            for f in os.listdir(pasta):
                if not f.lower().endswith((".mp3", ".m4a", ".aac", ".opus", ".wav", ".flac", ".ogg")):
                    continue
                _indexar_arquivo(os.path.join(pasta, f))
    log.info(f"[CACHE] Cache atualizado: {len(cache_by_id)} por ID, {len(cache_by_title)} por título.")

def buscar_arquivo_offline(video_id: str, titulo: str):
    """
    Procura primeiro por video_id; se não achar, tenta por título normalizado (legado).
    """
    if video_id and video_id in cache_by_id:
        return cache_by_id[video_id]
    tnorm = _normalizar_titulo(titulo or "")
    if tnorm and tnorm in cache_by_title:
        return cache_by_title[tnorm]
    return None

# ===========================
# BUSCAR NOVAS MÚSICAS (Sheets)
# ===========================
def buscar_novas_musicas_worker():
    global ultima_linha_lida, linha_fim_atual, fim_da_lista, linha_atual
    while True:
        try:
            total_rows = len(sheet_pedidos.get_all_values())
            inicio = ultima_linha_lida + 1
            fim = min(ultima_linha_lida + LOTE_LEITURA, total_rows)

            if inicio > fim:
                time.sleep(INTERVALO_CHECK_NOVAS_MUSICAS)
                continue

            range_para_ler = f"C{inicio}:G{fim}"
            dados = sheet_pedidos.get(range_para_ler)

            if not dados:
                time.sleep(INTERVALO_CHECK_NOVAS_MUSICAS)
                continue

            for i, row in enumerate(dados):
                linha_atual = inicio + i

                # Atualiza GUI com a linha atual
                try:
                    root.after(
                        0,
                        lambda la=linha_atual, lf=linha_fim_atual: linha_label.config(
                            text=f"Linha atual: {la} | Linha Final: {lf}"
                        ),
                    )
                except Exception:
                    pass

                name = row[0].strip() if len(row) > 0 else ""
                message = row[1].strip() if len(row) > 1 else ""
                link = row[2].strip() if len(row) > 2 else ""
                status = row[3].strip() if len(row) > 3 else ""
                status_msg = row[4].strip() if len(row) > 4 else ""

                # Detecta fim da lista
                if link == "":
                    linha_fim_atual = linha_atual
                    fim_da_lista = True
                    log.info(f"[GSHEETS] Fim da lista detectado na linha {linha_atual}.")
                    break

                # Detecta novas músicas após o fim
                if fim_da_lista:
                    valores = sheet_pedidos.get_all_values()
                    valores = [linha for linha in valores if any(c.strip() for c in linha)]
                    nova_ultima = len(valores)
                    if nova_ultima > (linha_fim_atual or 0):
                        log.info(
                            f"[GSHEETS] Novas músicas adicionadas ({nova_ultima - (linha_fim_atual or 0)}). Retomando execução."
                        )
                        fim_da_lista = False
                        linha_fim_atual = None
                        ultima_linha_lida = nova_ultima - 1
                        break

                # Processa pedido aceito
                if status.upper() == "ACEITO":
                    log.info(f"[GSHEETS] Pedido ACEITO na linha {linha_atual}. Enfileirando download.")
                    download_queue.put((linha_atual, link, name, message, status_msg))

            else:
                # só avança o ponteiro se não deu break no loop
                ultima_linha_lida += len(dados)

        except Exception as e:
            log.error(f"[GSHEETS] ERRO ao buscar novas músicas: {e}. Tentando novamente em 5 minutos.")
            time.sleep(300)

        time.sleep(INTERVALO_CHECK_NOVAS_MUSICAS)

# ===========================
# DOWNLOAD WORKER (apenas 1)
# ===========================
def download_worker():
    while True:
        item = download_queue.get()
        video_id = None
        titulo_real = None
        try:
            if not item or len(item) < 5:
                download_queue.task_done()
                continue

            linha, link, nome_usuario, mensagem, status_msg = item
            titulo_real = "Desconhecido"

            # 1) Extrai metadados SEM baixar (id + título)
            try:
                ydl_opts_info = {
                    'quiet': True,
                    'skip_download': True,
                    'extractor_args': {'youtube': {'skip': ['dash', 'hls']}}
                }
                with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                    info = ydl.extract_info(link, download=False)
                    video_id = info.get('id') or info.get('webpage_url_basename')
                    titulo_real = info.get('title', 'Desconhecido')
            except Exception as e:
                log.warning(f"[DOWNLOAD] Não foi possível extrair info do link: {e}")

            # 2) Tenta HIT no cache (id -> título)
            arquivo_existente = buscar_arquivo_offline(video_id, titulo_real)
            if arquivo_existente:
                log.info(f"[DOWNLOAD] Cache hit: '{titulo_real}' (id={video_id}). Adicionando à playlist.")
                with lock_geral:
                    playlist.append((linha, video_id, titulo_real, arquivo_existente, nome_usuario, mensagem, status_msg))
                download_queue.task_done()
                continue

            # 3) Evita downloads duplicados (por video_id)
            if video_id:
                with lock_geral:
                    if video_id in baixando_musicas:
                        log.info(f"[DOWNLOAD] '{titulo_real}' (id={video_id}) já está em download. Ignorando duplicata.")
                        download_queue.task_done()
                        continue
                    baixando_musicas.add(video_id)

            # 4) Prepara caminho de saída
            titulo_norm = _normalizar_titulo(titulo_real or "desconhecido")
            subpasta = titulo_norm[0].upper() if titulo_norm else '_'
            pasta = os.path.join(DOWNLOADS_DIR, subpasta)
            os.makedirs(pasta, exist_ok=True)

            # Nomeia com __<video_id> para facilitar reindexação posterior
            arquivo_base = f"{titulo_norm}__{video_id}" if video_id else f"{titulo_norm}"
            output_template = os.path.join(pasta, f"{arquivo_base}.%(ext)s")

            ydl_opts_download = {
                'format': 'bestaudio[abr<=96]/bestaudio',
                'outtmpl': output_template,
                'quiet': True,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '96'
                }]
            }

            # 5) Baixa
            log.info(f"[DOWNLOAD] Iniciando download: '{titulo_real}' (id={video_id})")
            try:
                with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
                    ydl.download([link])
                arquivo_path_final = os.path.join(pasta, f"{arquivo_base}.mp3")
            except Exception as e:
                log.error(f"[DOWNLOAD] ERRO no download de '{link}': {e}")
                continue

            # 6) Atualiza caches
            _indexar_arquivo(arquivo_path_final)

            # 7) Adiciona à playlist
            with lock_geral:
                playlist.append((linha, video_id, titulo_real, arquivo_path_final, nome_usuario, mensagem, status_msg))
            log.info(f"[DOWNLOAD] Concluído: '{titulo_real}'. Adicionado à playlist.")

        except Exception as e:
            log.error(f"[DOWNLOAD] ERRO no worker: {e}")
        finally:
            if video_id:
                with lock_geral:
                    baixando_musicas.discard(video_id)
            download_queue.task_done()

# ===========================
# TTS + MÚSICA + TTS FIM HORÁRIO
# ===========================
def gerar_tts(texto, tag):
    try:
        voz = random.choice(["pt-BR-ThalitaMultilingualNeural", "pt-BR-MacerioMultilingualNeural"])
        temp_file = os.path.join(tempfile.gettempdir(), f"tts_{tag}_{int(time.time())}.mp3")
        log.info(f"[TTS] Gerando áudio ({tag})...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        communicate = edge_tts.Communicate(texto, voice=voz, rate="-10%", volume="+30%")
        loop.run_until_complete(communicate.save(temp_file))
        loop.close()
        log.info(f"[TTS] Áudio gerado: {temp_file}")
        return temp_file
    except Exception as e:
        log.error(f"[TTS] ERRO ao gerar áudio: {e}")
        return None

def preparar_proximo_tts():
    global proximo_tts_file, playlist
    with lock_geral:
        if not playlist:
            return
        linha, vid, titulo, _, nome_usuario, mensagem, status_msg = playlist[0]

    texto_tts = ""
    if status_msg.lower() == "ler nome e mensagem":
        texto_tts = f"A seguir, um pedido de {nome_usuario}, que disse: {mensagem}. Vem aí: {titulo}."
    elif status_msg.lower() == "ler apenas o nome":
        texto_tts = f"A seguir, {titulo}, um pedido do usuário {nome_usuario}."

    if texto_tts:
        tts_file_path = gerar_tts(texto_tts, f"linha{linha}")
        with lock_tts:
            # limpa TTS antigo
            if proximo_tts_file and os.path.exists(proximo_tts_file):
                try:
                    os.remove(proximo_tts_file)
                except OSError:
                    pass
            proximo_tts_file = tts_file_path
            if tts_file_path:
                log.info(f"[PLAYER] TTS da PRÓXIMA música preparado (linha {linha}).")

def preparar_tts_fim_horario(horario_fim):
    """
    Gera o TTS de aviso de fim de horário e guarda em tts_fim_horario_file.
    Não gera se já existir arquivo pronto.
    """
    global tts_fim_horario_file
    with lock_tts:
        if tts_fim_horario_file is not None:
            return  # já tem um preparado

    texto = f"Horário limite alcançado, desligando player."
    log.info("[PLAYER] Iniciando geração do TTS final (fim de horário) em background.")
    tts_file = gerar_tts(texto, f"fim_{int(time.time())}")
    if tts_file:
        with lock_tts:
            tts_fim_horario_file = tts_file
        log.info(f"[PLAYER] TTS de fim de horário pré-gerado: {tts_file}")
    else:
        log.warning("[PLAYER] Falha ao gerar TTS de fim de horário.")

def tocar_tts_final(arquivo):
    """Toca o TTS final e tenta remover o arquivo ao fim."""
    try:
        if not arquivo or not os.path.exists(arquivo):
            return
        log.info("[PLAYER] Tocando TTS final (desligamento).")
        tts_player = vlc.MediaPlayer(arquivo)
        tts_player.play()
        time.sleep(0.4)
        while tts_player.get_state() not in [vlc.State.Ended, vlc.State.Stopped, vlc.State.Error]:
            time.sleep(0.2)
        try:
            tts_player.stop()
        except:
            pass
        try:
            os.remove(arquivo)
        except OSError as e:
            log.warning(f"[PLAYER] Não foi possível remover arquivo TTS final: {e}")
    except Exception as e:
        log.error(f"[PLAYER] Erro ao tocar TTS final: {e}")

def tocar_proxima_musica():
    global player, musica_rodando, current_title, current_line, current_video_id, is_paused
    global playlist, proximo_tts_file, tts_fim_horario_file, ultimo_horario_fim, avisou_fim

    if musica_rodando or is_paused:
        return
    musica_rodando = True

    # Atualiza cache de horários se estiver próximo do intervalo para garantir decisão correta
    if time.time() - ultima_atualizacao > INTERVALO_ATUALIZACAO:
        atualizar_horarios()

    if not pode_tocar():
        # Se tem TTS final pré-gerado e ainda não avisou, toca o TTS de desligamento
        with lock_tts:
            ttf = tts_fim_horario_file
            if ttf and not avisou_fim:
                avisou_fim = True
                tts_fim_horario_file = None
                threading.Thread(target=tocar_tts_final, args=(ttf,), daemon=True, name="TocarTTFFinal").start()

        log.info("[PLAYER] Fora do horário. Aguardando 60s...")
        musica_rodando = False
        root.after(60000, tocar_proxima_musica)
        return

    proxima_musica = None
    tts_para_tocar_agora = None

    with lock_geral:
        if playlist:
            proxima_musica = playlist.pop(0)

    with lock_tts:
        tts_para_tocar_agora = proximo_tts_file
        proximo_tts_file = None

    if not proxima_musica:
        musica_rodando = False
        if tts_para_tocar_agora:
            with lock_tts:
                proximo_tts_file = tts_para_tocar_agora
        root.after(5000, tocar_proxima_musica)
        return

    current_line, current_video_id, current_title, arquivo, nome_usuario, mensagem, status_msg = proxima_musica
    log.info(f"[PLAYER] Preparando para tocar '{current_title}' (linha {current_line}, id={current_video_id}).")
    try:
        root.after(0, lambda: titulo_label.config(text=f"Tocando: {current_title}"))
        root.after(0, lambda: status_label.config(text="Iniciando..."))
    except Exception:
        pass

    # Se estamos dentro de um horário ativo, prepara TTS de fim de horário em background
    if horarios_cache:
        tz = pytz.timezone("America/Sao_Paulo")
        agora = datetime.now(tz).time()
        found_fim = None
        for inicio, fim in horarios_cache:
            if (inicio <= fim and inicio <= agora <= fim) or (inicio > fim and (agora >= inicio or agora <= fim)):
                found_fim = fim
                break
        if found_fim:
            ultimo_horario_fim = found_fim
            with lock_tts:
                need_prep = (tts_fim_horario_file is None)
            if need_prep:
                threading.Thread(target=preparar_tts_fim_horario, args=(found_fim,), daemon=True, name="PrepTTSFIM").start()

    def rodar():
        global player
        try:
            # 1) Toca TTS se existir
            if tts_para_tocar_agora and os.path.exists(tts_para_tocar_agora):
                log.info("[PLAYER] Tocando anúncio pré-gerado (TTS).")
                tts_player = vlc.MediaPlayer(tts_para_tocar_agora)
                tts_player.play()
                time.sleep(0.5)
                while tts_player.get_state() not in [vlc.State.Ended, vlc.State.Stopped, vlc.State.Error]:
                    time.sleep(0.2)
                tts_player.stop()
                try:
                    os.remove(tts_para_tocar_agora)
                except OSError as e:
                    log.warning(f"[PLAYER] Não foi possível remover TTS: {e}")
            else:
                texto_tts = ""
                if status_msg.lower() == "ler nome e mensagem":
                    texto_tts = f"O usuário {nome_usuario} disse: {mensagem}. Tocando agora: {current_title}."
                elif status_msg.lower() == "ler apenas o nome":
                    texto_tts = f"O usuário {nome_usuario} pediu a música {current_title}."
                if texto_tts:
                    log.info("[PLAYER] Gerando anúncio em tempo real (primeira música/sem pré-TTS).")
                    tts_file = gerar_tts(texto_tts, f"linha{current_line}")
                    if tts_file:
                        tts_player = vlc.MediaPlayer(tts_file)
                        tts_player.play()
                        time.sleep(0.5)
                        while tts_player.get_state() not in [vlc.State.Ended, vlc.State.Stopped, vlc.State.Error]:
                            time.sleep(0.2)
                        tts_player.stop()
                        try:
                            os.remove(tts_file)
                        except OSError as e:
                            log.warning(f"[PLAYER] Não foi possível remover TTS: {e}")

            # 2) Toca a música
            log.info(f"[PLAYER] Tocando arquivo: {arquivo}")
            player = vlc.MediaPlayer(arquivo)
            player.play()

            # 3) Assim que começar, já prepara o TTS da PRÓXIMA
            threading.Thread(target=preparar_proximo_tts, daemon=True, name="PrepProxTTS").start()

            # 4) Atualiza planilha
            def update_sheet():
                try:
                    sheet_pedidos.update_cell(current_line, 6, "Tocado")
                    log.info(f"[GSHEETS] Linha {current_line} marcada como 'Tocado'.")
                except Exception as e:
                    log.error(f"[GSHEETS] ERRO ao atualizar linha {current_line}: {e}")
            threading.Thread(target=update_sheet, daemon=True, name="UpdSheet").start()

            # 5) Evento fim de mídia
            em = player.event_manager()
            def on_end(event):
                global musica_rodando
                log.info(f"[PLAYER] Música '{current_title}' finalizada.")
                musica_rodando = False
                root.after(100, tocar_proxima_musica)
            em.event_attach(vlc.EventType.MediaPlayerEndReached, on_end)

        except Exception as e:
            log.error(f"[PLAYER] ERRO ao tocar música: {e}")
            globals()['musica_rodando'] = False
            try:
                root.after(1000, tocar_proxima_musica)
            except Exception:
                pass

    threading.Thread(target=rodar, daemon=True, name="PlayThread").start()

# ===========================
# CONTROLES GUI
# ===========================
def toggle_play_pause():
    global player, is_paused
    if not player:
        return
    if player.is_playing():
        player.pause()
        is_paused = True
        status_label.config(text="Pausado")
    else:
        player.play()
        is_paused = False
        status_label.config(text="Tocando")

def proxima_musica_manual():
    global player, musica_rodando, is_paused, proximo_tts_file
    log.info("[PLAYER] Comando manual: Próxima música.")
    if player:
        player.stop()

    # Limpa TTS pré-gerado ao pular
    with lock_tts:
        if proximo_tts_file and os.path.exists(proximo_tts_file):
            try:
                os.remove(proximo_tts_file)
                log.info("[PLAYER] TTS pré-gerado removido devido ao skip.")
            except OSError:
                pass
        proximo_tts_file = None

    musica_rodando = False
    is_paused = False
    root.after(100, tocar_proxima_musica)

def iniciar_arrasto(event):
    globals()['arrastando_barra'] = True

def finalizar_arrasto(event):
    global arrastando_barra, player
    arrastando_barra = False
    if player and player.is_seekable():
        pos = barra_progresso.get()
        player.set_position(pos / 100.0)

def atualizar_barra_progresso():
    try:
        if player and player.get_length() > 0 and player.is_playing() and not arrastando_barra:
            progresso_percent = player.get_position() * 100
            barra_progresso.set(progresso_percent)
            tempo_atual_ms = player.get_time()
            tempo_total_ms = player.get_length()
            atual_s = tempo_atual_ms // 1000
            total_s = tempo_total_ms // 1000
            tempo_label.config(text=f"{atual_s//60:02d}:{atual_s%60:02d} / {total_s//60:02d}:{total_s%60:02d}")
    finally:
        root.after(500, atualizar_barra_progresso)

# ===========================
# GUI
# ===========================
root = tk.Tk()
root.title("Rádio Player Py")
root.geometry("450x300")
root.minsize(450, 300)

style = ttk.Style(root)
style.theme_use('clam')

main_frame = tk.Frame(root, padx=10, pady=10)
main_frame.pack(expand=True, fill="both")

titulo_label = tk.Label(main_frame, text="Música: Nenhuma", font=("Helvetica", 12, "bold"), wraplength=400, justify="center")
titulo_label.pack(pady=(0, 5), fill="x")

status_label = tk.Label(main_frame, text="Iniciando...", wraplength=400, justify="center")
status_label.pack(pady=5, fill="x")

tempo_label = tk.Label(main_frame, text="00:00 / 00:00")
tempo_label.pack(pady=2)

barra_progresso = tk.Scale(main_frame, from_=0, to=100, orient='horizontal', showvalue=0)
barra_progresso.pack(pady=5, fill="x")
barra_progresso.bind("<ButtonPress-1>", iniciar_arrasto)
barra_progresso.bind("<ButtonRelease-1>", finalizar_arrasto)

control_frame = tk.Frame(main_frame)
control_frame.pack(pady=10)

btn_play_pause = tk.Button(control_frame, text="▶ / ⏸", command=toggle_play_pause, width=15, height=2)
btn_play_pause.pack(side="left", padx=5)

btn_next = tk.Button(control_frame, text="⏭ Próxima", command=proxima_musica_manual, width=15, height=2)
btn_next.pack(side="left", padx=5)

linha_label = tk.Label(main_frame, text="Linha atual: 0", anchor="e")
linha_label.pack(side="bottom", fill="x", padx=5, pady=5)

# ===========================
# INICIALIZAÇÃO
# ===========================
log.info("[SYSTEM] Iniciando aplicação da rádio...")
atualizar_cache_offline()
atualizar_horarios()

# Threads de background
threading.Thread(target=monitorar_desempenho, daemon=True, name="Monitor").start()
threading.Thread(target=buscar_novas_musicas_worker, daemon=True, name="SheetsPoll").start()

# *** APENAS 1 WORKER DE DOWNLOAD ***
threading.Thread(target=download_worker, daemon=True, name="DownloadWorker").start()
log.info("[SYSTEM] 1 worker de download iniciado (como solicitado).")

root.after(500, atualizar_barra_progresso)
root.after(2000, tocar_proxima_musica)
root.after(INTERVALO_CHECK_HORARIOS * 1000, atualizar_horarios)

root.mainloop()
