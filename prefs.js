import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const BUS = 'org.gnome.Shell.Extensions.MixerRapido';
const PATH = '/org/gnome/Shell/Extensions/MixerRapido';

export default class MixerPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({title: 'Identificação', icon_name: 'audio-x-generic-symbolic'});
        window.add(page);
        const narrator = new Adw.PreferencesGroup({title: 'Vozes dos narradores',
            description: 'No menu do mixer, reproduza a voz desejada e use “Cadastrar voz desta faixa”. A captura dura 10 segundos e usa somente aquela faixa. Evite música e outras vozes na mesma faixa. Fala não cadastrada também usa o volume de VOZ.'});
        page.add(narrator);
        const status = new Adw.ActionRow({title: 'Consultando serviço…'});
        narrator.add(status);
        const cancelEnrollment = new Gtk.Button({label: 'Cancelar captura', valign: Gtk.Align.CENTER,
            visible: false});
        status.add_suffix(cancelEnrollment);
        const voices = new Adw.PreferencesGroup({title: 'Vozes cadastradas',
            description: 'Todas usam o volume padrão de VOZ. A compatibilidade por voz pode ser incerta.'});
        page.add(voices);
        const voiceRows = [];
        let profileSignature = '';
        const cancel = new Gio.Cancellable();
        let proxy = null;
        const call = (method, params, callback = () => {}) => {
            if (!proxy?.g_name_owner) {
                status.subtitle = 'Serviço de análise indisponível';
                return;
            }
            proxy.call(method, params, Gio.DBusCallFlags.NONE, 4000, cancel, (p, result) => {
                try {
                    callback(p.call_finish(result));
                } catch (e) {
                    if (!cancel.is_cancelled())
                        status.subtitle = e.message;
                }
            });
        };
        const refresh = () => call('GetState', null, value => {
            const state = JSON.parse(value.deep_unpack()[0]);
            status.title = `${state.profiles.length} voz(es) cadastrada(s)`;
            status.subtitle = state.enrollment || 'As amostras permanecem neste computador';
            cancelEnrollment.visible = state.enrolling && state.enrollment_seconds > 0;
            const signature = JSON.stringify(state.profiles);
            if (signature !== profileSignature) {
                profileSignature = signature;
                for (const row of voiceRows)
                    voices.remove(row);
                voiceRows.length = 0;
                for (const profile of state.profiles) {
                    const row = new Adw.EntryRow({title: 'Nome da voz', text: profile.name,
                        show_apply_button: true});
                    row.connect('apply', () => call('RenameNarrator',
                        new GLib.Variant('(ss)', [profile.id, row.text]), refresh));
                    const remove = new Gtk.Button({label: 'Remover', valign: Gtk.Align.CENTER});
                    remove.connect('clicked', () => call('RemoveNarrator',
                        new GLib.Variant('(s)', [profile.id]), refresh));
                    row.add_suffix(remove);
                    voices.add(row);
                    voiceRows.push(row);
                }
            }
        });
        Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            null, BUS, PATH, BUS, cancel, (_source, result) => {
                try {
                    proxy = Gio.DBusProxy.new_for_bus_finish(result);
                    if (!cancel.is_cancelled())
                        refresh();
                } catch (e) {
                    status.subtitle = e.message;
                }
            });
        cancelEnrollment.connect('clicked', () => call('CancelEnrollment', null, refresh));
        const threshold = Adw.SpinRow.new_with_range(.3, .95, .05);
        threshold.title = 'Similaridade mínima do narrador';
        threshold.subtitle = 'Maior valor exige mais semelhança com a amostra';
        threshold.digits = 2;
        settings.bind('narrator-threshold', threshold, 'value', Gio.SettingsBindFlags.DEFAULT);
        narrator.add(threshold);

        const policy = new Adw.PreferencesGroup({title: 'Novas faixas',
            description: 'Os volumes de voz e sons ficam no menu do mixer. Ajustes individuais protegem a faixa contra alterações automáticas; Auto retoma a automação.'});
        page.add(policy);
        const provisional = Adw.SpinRow.new_with_range(0, 100, 1);
        provisional.title = 'Volume provisório máximo (%)';
        provisional.subtitle = 'Limitado também pelos volumes de voz e sons';
        settings.bind('provisional-volume', provisional, 'value', Gio.SettingsBindFlags.DEFAULT);
        policy.add(provisional);
        const deadline = Adw.SpinRow.new_with_range(100, 5000, 100);
        deadline.title = 'Prazo de identificação (ms)';
        deadline.subtitle = 'Atraso mínimo; a fila aguarda a decisão até o limite abaixo';
        settings.bind('classification-deadline-ms', deadline, 'value', Gio.SettingsBindFlags.DEFAULT);
        policy.add(deadline);
        const maximum = Adw.SpinRow.new_with_range(500, 5000, 100);
        maximum.title = 'Retenção máxima antes do volume provisório (ms)';
        maximum.subtitle = 'Padrão 2000 ms; VAD rápido identifica fala sem aguardar o narrador';
        settings.bind('classification-max-hold-ms', maximum, 'value', Gio.SettingsBindFlags.DEFAULT);
        policy.add(maximum);
        policy.add(new Adw.ActionRow({title: 'Retenção PipeWire por faixa',
            subtitle: 'Atraso mínimo de 500 ms e espera limitada pela classificação. Depois da liberação, o atraso fica constante; jogos e vídeo precisam considerar essa latência.'}));
        const timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => {
            refresh();
            return GLib.SOURCE_CONTINUE;
        });
        window.connect('close-request', () => {
            cancel.cancel();
            GLib.Source.remove(timer);
            return false;
        });
    }
}
