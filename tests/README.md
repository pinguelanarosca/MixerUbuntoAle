# Verificação do projeto

Os scripts escrevem evidências somente em `test-results/`, ignorado pelo Git. Instale os pré-requisitos do README principal; os testes de modelos/áudio usam o runtime e os pesos da instalação existente. Não há download implícito durante a execução dos classificadores.

## Verificações sem alterar a instalação ativa

```bash
python3 -m unittest discover -s tests -p test_install.py -v
python3 -m unittest discover -s tests -p test_policy.py -v
python3 install.py --dry-run
python3 install.py --check
```

O teste do instalador prepara arquivos e schemas em diretórios temporários, verifica preservação e reversão em falha de cópia e verifica o opt-in/integridade dos downloads. Ele não instala a extensão no host e não substitui uma instalação completa em Ubuntu novo.

## Classificadores reais

```bash
python3 tests/prepare_fixtures.py
~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/.venv/bin/python tests/test_models.py
```

`prepare_fixtures.py` é o passo explícito que obtém a amostra de música e gera outra voz com FFmpeg/Flite. O manifesto já inclui duas amostras reais de fala. Os testes usam Silero, YAMNet e SpeechBrain reais, incluindo voz cadastrada e outra voz.

## Buffer e ganho inicial no PipeWire real

```bash
dbus-run-session -- ~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/.venv/bin/python tests/test_initial_gain.py
dbus-run-session -- ~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/.venv/bin/python tests/test_gain_ramp.py
dbus-run-session -- ~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/.venv/bin/python tests/test_retention.py
```

Esses testes iniciam servidores privados PipeWire, Pulse e WirePlumber, usando os binários/modelos da instalação original, sem outra instalação da extensão. Reproduzem PCM real por clientes rotulados como Chrome e gravam saídas virtuais; comparam as primeiras amostras, sinal completo, cauda, ganho e integridade. Não automatizam abas reais do navegador.

Para exercer a recuperação forçada do motor no servidor privado:

```bash
dbus-run-session -- ~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/.venv/bin/python tests/test_retention.py --recovery-stress
```

O relatório separa retomada segura de reprodução sem perdas. SIGKILL pode perder um bloco em trânsito; esse teste não deve ser interpretado como garantia de perda zero sob falha.

## Sessão principal e interface

```bash
~/.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper/.venv/bin/python tests/test_initial_gain_live.py
python3 tests/test_shell.py
```

O primeiro usa saída virtual temporária no PipeWire principal, respeita os sliders atuais e não reinstala a extensão. O segundo carrega os arquivos instalados em GNOME 50 headless, com D-Bus privado e encaminhamento ao auxiliar real; exercita volume, mute, cadastro e preferências. Ele pode provocar reconfiguração de dispositivos na sessão: execute sem reproduções importantes e confira a saída selecionada depois.

Os testes legados `test_pipewire.py` e `test_retention_live.py` alteram temporariamente padrões e controles e não são a verificação preferida de uma sessão em uso. Testes virtuais não certificam latência física, Bluetooth, sincronização com vídeo ou jogos reais. Esses cenários precisam de reprodução e avaliação no equipamento.
