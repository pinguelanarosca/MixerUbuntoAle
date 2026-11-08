# Mixer Rápido

Extensão para o GNOME Shell 50. O ícone na barra superior abre os controles de volume por faixa. No mesmo menu, **Novas faixas do Chrome** define o volume inicial, que começa em 60%.

## Reinstalação

1. Guarde uma cópia de `mixer-rapido@alee.local.shell-extension.zip` fora do computador. Após reinstalar o Ubuntu, confirme que o ambiente usa o GNOME Shell 50 com `gnome-shell --version`.
2. Abra o terminal na pasta onde salvou o ZIP e instale o pacote.

   ```bash
   gnome-extensions install ./mixer-rapido@alee.local.shell-extension.zip
   ```

3. Saia da sessão e entre novamente. Habilite a extensão.

   ```bash
   gnome-extensions enable mixer-rapido@alee.local
   ```

4. Clique no ícone na barra superior para abrir o mixer.
