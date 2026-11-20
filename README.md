# MixerUbuntoAle — Mixer Rápido

Mixer por fluxo para **GNOME Shell 50**, com captura individual do Chrome, classificação local de áudio e retenção antes da saída. UUID: `mixer-rapido@alee.local`.

O ambiente de referência é **Ubuntu 26.04 LTS x86_64**, Python 3.14, PipeWire 1.6.2 e WirePlumber 0.5.13. Outras versões do Ubuntu/GNOME e outras arquiteturas não foram validadas com este lock de dependências.

## Instalação em um Ubuntu limpo

Execute os comandos na sessão gráfica do seu usuário. Somente a instalação dos pacotes do sistema usa `sudo`; o instalador da extensão não deve ser executado como root.

```bash
sudo apt update
sudo apt install git python3 python3-venv python3-gi gjs \
  build-essential pkg-config libpipewire-0.3-dev \
  pipewire-bin pipewire-pulse wireplumber libspa-0.2-modules \
  pulseaudio-utils ffmpeg dconf-cli libglib2.0-bin \
  gir1.2-adw-1 gir1.2-gtk-4.0 gnome-shell-extension-prefs
mkdir -p ~/Dev
git clone https://github.com/pinguelanarosca/MixerUbuntoAle.git ~/Dev/MixerRapido
cd ~/Dev/MixerRapido
python3 install.py --dry-run
python3 install.py --download
```

`--download` autoriza explicitamente a preparação do runtime CPU, dos modelos e, se faltarem headers do sistema, dos headers do PipeWire. Reserve alguns GB de disco e conexão com a internet. Nenhum desses arquivos pesados fica no Git. O instalador:

1. Confere GNOME, Python e ferramentas necessárias; compila o motor nativo e os schemas em área temporária.
2. Prepara `helper/.venv` no destino definitivo, com as versões de `helper/requirements.lock`; baixa os modelos de `helper/models.lock.json` e verifica tamanho/SHA-256.
3. Copia somente os arquivos necessários para `~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local`.
4. Instala as unidades systemd do usuário e o fragmento próprio `~/.config/wireplumber/wireplumber.conf.d/95-mixer-buffer.conf`, preservando os demais fragmentos.
5. Ativa os auxiliares e habilita a extensão para o próximo login.

**Saia e entre na sessão** para carregar o menu. O GNOME mantém módulos JavaScript em cache; desabilitar/habilitar não troca um módulo já importado. A ativação reinicia o WirePlumber: faça a instalação com reproduções fechadas e, se necessário, reconecte a saída Bluetooth.

O instalador recusa alterar uma instalação com auxiliares ativos ou filas retidas. `--dry-run` só verifica o ambiente e mostra o plano; `--check` só verifica a instalação existente. Sem `--download`, arquivos ausentes geram erro e não são baixados.

O ZIP antigo da versão 2 foi retirado do versionamento. Use o instalador do checkout atual.

## Uso

- **Novas faixas de voz** e **Novas faixas de sons** controlam os volumes iniciais, de forma independente e persistente. Ao migrar uma versão antiga, ambos recebem o antigo padrão único.
- Cada fluxo tem volume e mute próprios, associados ao serial PipeWire enquanto existir. O círculo azul destaca o fluxo cujo áudio começou mais recentemente, incluindo retomadas após silêncio; a ordem de criação ou posição no menu não define a atividade.
- **VOZ**, **SONS** e **INDEFINIDO** mostram evidência dos modelos. Os escores não são probabilidades calibradas nem garantia de acerto.
- **Voz**, **Sons** e **?** corrigem a categoria. Ajustar volume ou mute protege o volume contra automação; **Auto** libera a proteção e solicita nova análise, mantendo o mute.

### Cadastrar várias vozes pela própria faixa

1. Reproduza a voz desejada no Chrome, de preferência sem música ou outras vozes.
2. No mixer, use **Cadastrar voz desta faixa (10 s)**; a captura copia apenas o fluxo selecionado.
3. Em **Narrador e identificação…**, renomeie a amostra, por exemplo para “ChatGPT” ou “Google”. É possível cadastrar até 32 vozes, renomear, remover e cancelar capturas.

Fala desconhecida também pode receber VOZ e usar o respectivo padrão. Detectar fala não identifica o narrador: a comparação SpeechBrain exige uma amostra cadastrada e similaridade suficiente. Correspondências fracas ou empatadas permanecem incertas.

Nomes e vetores ficam em `~/.config/mixer-rapido/voices.json` e `voices/*.npy`. O áudio do cadastro fica em memória. A fila temporária de reprodução fica em `$XDG_RUNTIME_DIR/mixer-buffer`.

## Como a retenção funciona

O hook do WirePlumber intercepta novos fluxos do Chrome antes do link com a saída. Um processo C cria uma fila float32 estéreo exclusiva por fluxo; o ganho é aplicado ao ler essa fila. O início é armazenado e reproduzido integralmente nos cenários normais, sem substituir retenção por mute ou volume zero.

- **Silero VAD:** worker leve, independente e pré-carregado; pode definir VOZ por evidência sustentada, sem aguardar o narrador.
- **YAMNet:** classifica fala, música e outros sons a partir de um segundo de áudio. Ausência de fala, sozinha, não declara SONS.
- **SpeechBrain ECAPA:** compara a voz com amostras cadastradas, a partir de três segundos; essa etapa não bloqueia o VAD rápido.

O atraso mínimo padrão é **500 ms**. Se a decisão não estiver pronta, o início continua retido até a classificação ou até a retenção máxima padrão de **2.000 ms**, configurável nas preferências. Ao começar a saída, o atraso fica constante: não há aceleração, descarte ou duplicação para recuperar a espera. Mudanças automáticas posteriores usam rampa de **120 ms**; antes da primeira amostra, o ganho escolhido é aplicado integralmente.

Se a identificação exceder o limite ou falhar, libera-se a fila no menor valor entre os padrões de VOZ, SONS e o teto provisório, inicialmente **20%**. O teto não é aumentado para compensar demora. Ajustes manuais mantêm prioridade.

Captura e inferência rodam fora do GNOME Shell, com comunicação D-Bus assíncrona. O runtime não baixa modelos: usa arquivos locais, modo offline e sockets locais nas unidades systemd. Fontes identificadas param de solicitar classificação, salvo reconhecimento de narrador ainda pendente; a medição de atividade continua. O worker de conteúdo libera modelos após ociosidade; o VAD permanece pronto.

Fluxos anteriores à ativação conservam o roteamento e usam `parec --monitor-stream` apenas para análise. A retenção nova usa a cópia do próprio buffer. Outros aplicativos não recebem esse roteamento.

### Limitações

- A retenção introduz latência real, de pelo menos 500 ms e possivelmente até o limite configurado. Isso afeta jogos. O motor declara a latência no grafo, mas compensação de vídeo e latência física/Bluetooth dependem do aplicativo e não foram certificadas.
- O motor foi validado com grafo de **48 kHz estéreo**. O PipeWire converte o formato da entrada; outros formatos de grafo não foram validados.
- **Não há garantia de perda zero sob SIGKILL, reinício do servidor ou falha de hardware.** A fila já escrita permanece em memória compartilhada para tentativa de retomada, mas um bloco em trânsito pode se perder. O systemd reinicia os auxiliares; isso não torna a recuperação uma transação de áudio sem perdas.
- A fila comporta 32 segundos por fluxo e suspende a captura perto do limite sem saída. Indisponibilidades prolongadas não foram qualificadas.
- Sons e vozes já misturados pelo Chrome em um único fluxo não podem receber ganhos separados. Após identificação, **Auto** solicita reanálise quando o conteúdo mudar.
- Silero/YAMNet podem permanecer inconclusivos; SpeechBrain não garante reconhecer qualquer voz, inclusive vozes sintéticas.

## Verificação, atualização e recuperação

Verificação sem reinstalar, reiniciar serviços ou baixar arquivos:

```bash
cd ~/Dev/MixerRapido
python3 install.py --check
systemctl --user status mixer-rapido.service mixer-buffer.service
```

Para atualizar arquivos de uma instalação existente, primeiro feche as reproduções do Chrome e aguarde as filas drenarem. A desativação recusa descartar filas pendentes:

```bash
python3 ~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/install_buffer.py disable
systemctl --user stop mixer-rapido.service
cd ~/Dev/MixerRapido
git pull --ff-only
python3 install.py --download
```

Preserve alterações próprias no checkout antes do `git pull`. O instalador cria backup dos arquivos anteriores e preferências em `~/.local/state/mixer-rapido/backups/install-*`; vozes e configurações pessoais não são apagadas. O backup não duplica ambientes virtuais ou pesos.

Para verificar ou preparar explicitamente o runtime/modelos, sem copiar a extensão:

```bash
python3 helper/setup.py
python3 helper/setup.py --dependencies --models
```

Para compilar o motor manualmente, use `python3 helper/build_buffer.py`; ele usa headers do sistema ou um cache já preparado. `--download-headers` autoriza baixar os headers da versão instalada. A compilação produz um manifesto com versão e hashes em `helper/buffer-build.json` da instalação. Não substitua o motor durante reprodução.

Para restaurar um backup concreto, feche o Chrome, aguarde a drenagem e execute o auxiliar de reversão, informando o diretório real do backup:

```bash
python3 helper/rollback.py CAMINHO_DO_BACKUP
```

A reversão preserva os cadastros de voz. `--restore-settings` também restaura as preferências antigas, substituindo ajustes posteriores. Reentre na sessão após restaurar código antigo. Para apenas retirar a interceptação, use `helper/install_buffer.py disable`; isso não apaga vozes nem dependências.

Procedimentos de teste estão em [tests/README.md](tests/README.md). Resultados, gravações, caches, runtime, pesos, binários gerados e configurações particulares permanecem fora do Git.
