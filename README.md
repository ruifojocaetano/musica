# Rui Caetano Player

Leitor de música/vídeo do YouTube com fila, histórico e memória da posição de cada faixa. Existem duas versões independentes, com o mesmo conceito e o mesmo aspeto de "fila + controlos":

| | Versão Windows | Versão Web |
|---|---|---|
| Ficheiro | `musica.pyw` (~975 linhas) | `index.html` (~420 linhas) |
| Tecnologia | Python 3.13 + Tkinter, `mpv` + `yt-dlp` | HTML/CSS/JS puro, YouTube IFrame API |
| Distribuição | `.exe` PyInstaller (`dist/` e `Rui Caetano Player.zip`, ~127 MB) | Servido localmente por `servir.pyw` |
| Estado guardado em | `%APPDATA%\MusicaLeve\dados.json` | `localStorage` (chave `musicaleve-v1`) |

> Tudo (código fonte, build e este README) está agora nesta pasta. O repositório GitHub (`ruifojocaetano/musica`) leva só o código; `dist/`, zip e `.exe` ficam de fora (`.gitignore`). A versão web também está publicada em https://ruifojocaetano.github.io/musica/.
> O nome interno ainda é "MusicaLeve" (pasta de dados, chave do localStorage); o nome de apresentação é "Rui Caetano Player".

## Funcionalidades comuns

- Colar links do YouTube (um por linha); colar adiciona logo e começa a tocar se nada estiver a tocar.
- Fila editável, anterior/seguinte, ±10 s, barra de tempo, velocidade (0.75x a 2x).
- Aleatório e repetir (não / fila / uma).
- **Retoma onde ficou**: guarda a posição de cada faixa; ao voltar a tocá-la continua daí (só se tiver >= 5 s e faltarem > 10 s para o fim).
- Histórico (máx. 500 na app, 300 na web) com "Terminou" / "Parou em mm:ss".
- Se uma faixa falha, salta para a seguinte (limitado ao tamanho da fila para não entrar em ciclo).

## Versão Windows (`musica.pyw`)

**Arquitetura**
- `Mpv`: lança o `mpv.exe` e fala com ele por *named pipe* (`\\.\pipe\musicaleve-<pid>-<ts>`) com JSON IPC. A leitura usa `PeekNamedPipe` (ctypes) porque num pipe síncrono uma leitura bloqueada impede as escritas. Observa `time-pos`, `duration`, `pause`, `media-title`.
- `App`: janela Tkinter. Threads de fundo para downloads, metadados e playlists; comunicam com a UI por uma `queue.Queue` consumida a cada 100 ms (`tick`). Grava o estado a cada ~10 s, e atomicamente (`.tmp` + `os.replace`).
- Ferramentas (`mpv.exe`, `yt-dlp.exe`, `deno.exe`) são procuradas primeiro na pasta `ferramentas\` ao lado do exe e depois nas instalações do sistema (MPV Player, WinGet). A pasta é posta no `PATH` porque o `yt-dlp` precisa do **deno** como runtime de JavaScript para o YouTube.

**Funcionalidades exclusivas**
- Aceita links de **playlist** (`/playlist?`), expandidos com `yt-dlp --flat-playlist`.
- **Só áudio** (por omissão) ou vídeo até 480p. Usa formatos HLS (`m3u8`) porque o YouTube devolve 403 nos links diretos.
- **Download automático** para ouvir offline: ao tocar, descarrega a faixa atual + as 2 seguintes (m4a), em `~\Music\MusicaLeve` (pasta configurável). Tenta até 5 vezes com retries infinitos do `yt-dlp` (retoma do `.part`). Faixas descarregadas aparecem com "✔ offline" e passam a tocar do ficheiro local.
- Fila com seleção múltipla, mover ▲/▼, remover (Delete), volume, atalhos Espaço (pausa) e ←/→ (±5 s).
- Janela de histórico (Treeview) com tocar, adicionar à fila, apagar.
- Títulos e durações obtidos em segundo plano com `yt-dlp --print`.

**Build** (pelo conteúdo de `dist/`): PyInstaller em modo *onedir* (`Rui Caetano Player.exe` + `_internal/` + `ferramentas/`). O zip tem 950 ficheiros e ~270 MB descomprimido, quase tudo `mpv.exe` (120 MB) e `deno.exe` (98 MB). Não encontrei `.spec` nem script de build para este projeto, por isso o comando exato usado não está registado.

## Versão Web (`index.html` + `servir.pyw`)

**Arquitetura**
- Página única, sem dependências. Usa o leitor oficial (`youtube.com/iframe_api`) e o endpoint `oembed` para os títulos.
- Estado `S` (fila, índice, metadados, histórico, aleatório, repetir, velocidade) em `localStorage`, guardado a cada 5 s e em `visibilitychange` / `pagehide`.
- Interface responsiva (largura máx. 640 px), tema claro/escuro automático, pensada para telemóvel.
- `Media Session API`: título, capa e botões no ecrã de bloqueio / auscultadores.
- Aceita `musica.html?add=LINK` para adicionar um link pelo endereço.

**Servidor** (`servir.pyw`): `http.server` em `127.0.0.1:8765`, sem consola (`pythonw`). Existe só porque o YouTube não aceita o leitor embutido em páginas abertas como `file://`; precisa de uma origem `http://`. `abrir-web.bat` arranca o servidor (com o `pythonw` do PATH, a partir da própria pasta) e abre `http://localhost:8765/index.html`.

**Limitações (regras do YouTube / da abordagem)**
- Só links de **vídeo** (playlists são recusadas com mensagem).
- O vídeo tem de ficar **visível**; não há modo só-áudio nem offline.
- Alguns vídeos bloqueiam a incorporação (erro 101/150) e são saltados.
- O estado fica só no browser/aparelho onde foi usado; não sincroniza com a versão Windows.

## Pontos de atenção

1. ~~Caminhos fixos em `abrir-web.bat`~~: corrigido, já não depende do PC (precisa de `pythonw` no PATH).
2. **Binários no OneDrive**: `dist/` e o zip (>350 MB no total) estão numa pasta sincronizada. Convém excluí-los da sincronização ou guardá-los fora, e manter só o código fonte + este README.
3. **Sem controlo de versões** nem testes automáticos; sem `requirements.txt` próprio (o da pasta `Claude` é de outro projeto). O `.pyw` só usa a biblioteca standard.
4. **Aviso legal**: os downloads com `yt-dlp` podem violar os termos do YouTube; usar apenas para conteúdo próprio ou permitido.
5. **Sem `.spec` / script de build**: convém guardar o comando do PyInstaller para reproduzir o exe.
6. `deno.exe` e `yt-dlp.exe` envelhecem depressa; o YouTube muda e o `yt-dlp` precisa de atualizações frequentes (`yt-dlp -U`).

## Como usar

- **Windows**: executar `dist\Rui Caetano Player\Rui Caetano Player.exe` (ou descomprimir o zip noutro PC; não precisa de instalar nada).
- **Web**: executar `abrir-web.bat` ou abrir https://ruifojocaetano.github.io/musica/ (GitHub Pages, sem servidor), ou `pythonw servir.pyw` e abrir `http://localhost:8765/index.html`.
