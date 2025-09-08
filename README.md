# Radio-Escolar
# 🎶 Radio Escolar O **Radio Escolar** é um **player de música autônomo** desenvolvido em Python.   Ele foi pensado para rodar 24/7, com a maioria das suas configurações sendo moldadas via Google Sheets, minimizando a intervenção manual.
---

## 📦 Dependências

- [gspread](https://pypi.org/project/gspread/) → integração com Google Sheets  
- [oauth2client](https://pypi.org/project/oauth2client/) → autenticação com Google Cloud  
- [yt-dlp](https://pypi.org/project/yt-dlp/) → download de músicas do YouTube  
- [edge-tts](https://pypi.org/project/edge-tts/) → geração de voz (IA TTS)  

Instalação rápida:

```bash
pip install gspread oauth2client yt-dlp edge-tts psutil python-vlc pytz
```

---

## 🔑 Pré-requisitos

1. **Conta no Google Cloud** com as bibliotecas do **Google Sheets** e **Google Drive** habilitadas.  
2. Criar uma **credencial de serviço (JSON)** e renomear o arquivo para:
   ```
   creds.json
   ```
   Coloque o arquivo na **pasta raiz do projeto**.  
3. Configure uma planilha com as mesmas abas e colunas do modelo oficial:  
   👉 [Modelo Google Sheets](https://docs.google.com/spreadsheets/d/19WtNm8up0-Rf9UnwX9PpxlEdXn3qr0YRc6ceaH4kKnw/edit?usp=sharing)

---

## ⚙️ Funcionamento

### 🎧 Radio Player - Programa Primário
1. **Inicialização**  
   - Inicia o monitor de desempenho (log de CPU/RAM).  
   - Indexa o **cache** de músicas já baixadas.  
   - Atualiza os **horários de funcionamento** (via planilha).  

2. **Execução**  
   - Lê a aba **Playlist** em busca de músicas com status `Aceito`.  
   - Após tocar, o status muda automaticamente para `Tocado`.  

3. **Downloader**  
   - Baixa músicas em fila:  
     - Se a música não estiver em cache, baixa antes de tocar.  
     - Enquanto toca, já baixa as próximas em background.  
   - Mesmo processo é usado para geração de **TTS (mensagens de voz)**.  

4. **Loop Contínuo**  
   - Busca constantemente novas músicas.  
   - Respeita os **horários configurados**.  
   - Qualquer mudança na planilha tem efeito **imediato**.  

---

### 🛠️ Back-End - Programa Secundário
1. **Pedidos**  
   - Verifica a aba **Pedidos**.  
   - Regras automáticas de recusa:  
     - E-mail na **blacklist**.  
     - Nome ou mensagem contém **link**.  
     - Nome da música com mais de **32 caracteres**.  
     - Mensagem com mais de **72 caracteres**.  
   - Se recusado, move para **Histórico** com status `Recusado`.  

2. **Moderação**  
   - Aba **Moderação**:  
     - Se marcado como `Aceito` → move para **Playlist**.  
     - Se marcado como `Recusado` → move para **Histórico**.  

3. **Horários**  
   - Aba **Horários** define quando o programa pode tocar músicas.  
   - Se fora do horário, move as músicas `Tocadas` para **Histórico**.  

4. **Loop Automático**  
   - Repetição a cada **1 minuto**.  

---

## 📂 Estrutura do Projeto

```
.
├── downloads/          # Cache das músicas baixadas
├── creds.json          # Credenciais do Google Cloud
├── radio_bot.log       # Log do sistema (execução)
├── logs.txt            # Monitoramento de CPU/RAM
├── main.py             # Código principal
├── back.py             # Código secundario
└── README.md
```

---

## ▶️ Execução

```bash
python main.py
python back.py
```

Interface gráfica:
- ▶ / ⏸ → Play / Pause  
- ⏭ Próxima → Pular música  
- Barra de progresso → Avançar/retroceder  

---

## 📊 Logs
- `radio_bot.log` → Execução, downloads, mensagens TTS.  
- `logs.txt` → Uso de CPU/RAM e threads.  

---

## 🔮 Melhorias Futuras
- Painel web em vez de Tkinter.  
- Bot de controle remoto (Telegram/Discord).  
- Configurações extras de voz (multi-idiomas).  
