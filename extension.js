import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gvc from 'gi://Gvc';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';
import {getMixerControl} from 'resource:///org/gnome/shell/ui/status/volume.js';

const TITLE = 'Mixer de áudio';
const AMPLIFIED_VOLUME = 'allow-volume-above-100-percent';
const HELPER_BUS = 'org.gnome.Shell.Extensions.MixerRapido';
const HELPER_PATH = '/org/gnome/Shell/Extensions/MixerRapido';

class Helper {
    constructor(changed) {
        this.state = null;
        this._changed = changed;
        this._cancel = new Gio.Cancellable();
        Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            null, HELPER_BUS, HELPER_PATH, HELPER_BUS, this._cancel, (_source, result) => {
                try {
                    this._proxy = Gio.DBusProxy.new_for_bus_finish(result);
                    if (this._cancel.is_cancelled())
                        return;
                    this._signalId = this._proxy.connect('g-signal', (_proxy, _sender, name, params) => {
                        if (name === 'StateChanged')
                            this._receive(params.deep_unpack()[0]);
                    });
                    this._ownerId = this._proxy.connect('notify::g-name-owner', () => this._refresh());
                    this._refresh();
                } catch (error) {
                    if (!this._cancel.is_cancelled())
                        console.error(`Mixer: ${error.message}`);
                }
            });
        // No capture or inference runs in the Shell process.
        try {
            Gio.Subprocess.new(['systemctl', '--user', 'start', 'mixer-rapido.service'],
                Gio.SubprocessFlags.NONE);
        } catch (error) {
            console.error(`Mixer: ${error.message}`);
        }
    }

    _receive(json) {
        if (this._cancel.is_cancelled())
            return;
        try {
            this.state = JSON.parse(json);
            this._changed();
        } catch (error) {
            console.error(`Mixer: ${error.message}`);
        }
    }

    _refresh() {
        if (!this._proxy.g_name_owner) {
            this.state = null;
            this._changed();
            return;
        }
        this._proxy.call('GetState', null, Gio.DBusCallFlags.NONE, 2000, this._cancel,
            (proxy, result) => {
                try {
                    this._receive(proxy.call_finish(result).deep_unpack()[0]);
                } catch (error) {
                    if (!this._cancel.is_cancelled())
                        console.error(`Mixer: ${error.message}`);
                }
            });
    }

    call(method, type, args) {
        if (!this._proxy?.g_name_owner)
            return false;
        this._proxy.call(method, new GLib.Variant(type, args), Gio.DBusCallFlags.NONE,
            4000, this._cancel, (proxy, result) => {
                try {
                    proxy.call_finish(result);
                } catch (error) {
                    if (!this._cancel.is_cancelled())
                        Main.notify('Mixer Rápido', error.message);
                }
            });
        return true;
    }

    destroy() {
        this._cancel.cancel();
        if (this._signalId)
            this._proxy.disconnect(this._signalId);
        if (this._ownerId)
            this._proxy.disconnect(this._ownerId);
    }
}

function isChrome(stream) {
    return [stream.name, stream.applicationId, stream.iconName].some(value =>
        ['google chrome', 'google-chrome', 'com.google.chrome', 'chromium',
            'chromium-browser', 'org.chromium.chromium'].includes(value?.toLowerCase()));
}

const StreamRow = GObject.registerClass(
class StreamRow extends St.BoxLayout {
    _init(stream, control, settings, chromeTrack, helper) {
        super._init({vertical: true, style: 'spacing: 4px; padding: 5px 0;'});
        this._stream = stream;
        this._chromeTrack = chromeTrack;
        this._helper = helper;
        this._info = null;
        this._normalVolume = control.get_vol_max_norm();

        const header = new St.BoxLayout({style: 'spacing: 8px;'});
        this._name = new St.Label({x_expand: true});
        this._percent = new St.Label();
        header.add_child(this._name);
        header.add_child(this._percent);
        this.add_child(header);

        const controls = new St.BoxLayout({style: 'spacing: 8px;'});
        this._muteIcon = new St.Icon({icon_size: 16});
        this._muteButton = new St.Button({can_focus: true, child: this._muteIcon});
        this._muteButton.connect('clicked', () => {
            const muted = this._info?.buffered ? this._info.mute : stream.isMuted;
            if (!this._info || !helper.call('SetMute', '(sb)', [this._info.serial, !muted]))
                stream.change_is_muted(!muted);
        });
        controls.add_child(this._muteButton);
        this.slider = new Slider(0);
        controls.add_child(this.slider);
        this.add_child(controls);
        const setVolume = volume => {
            if (stream.set_volume(volume))
                stream.push_volume();
        };
        this._sliderChangedId = this.slider.connect('notify::value', () => {
            if (!this._info || !helper.call('SetVolume', '(sd)',
                [this._info.serial, this.slider.value * 100]))
                setVolume(Math.round(this.slider.value * this._normalVolume));
        });

        if (chromeTrack) {
            this._classification = new St.Label({style: 'font-size: 0.85em;'});
            this.add_child(this._classification);
            this._choices = new St.BoxLayout({style: 'spacing: 6px;'});
            for (const [label, value] of [['Voz', 'VOZ'], ['Sons', 'SONS'], ['?', 'INDEFINIDO'], ['Auto', '']]) {
                const button = new St.Button({label, can_focus: true,
                    style_class: 'button', style: 'padding: 3px 8px;',
                    accessible_name: value ? `Classificar como ${value}` : 'Retomar classificação e volume automáticos'});
                button.connect('clicked', () => {
                    if (this._info)
                        helper.call(value ? 'SetClass' : 'ResetAutomatic', value ? '(ss)' : '(s)',
                            value ? [this._info.serial, value] : [this._info.serial]);
                });
                this._choices.add_child(button);
            }
            this.add_child(this._choices);
            this._enroll = new St.Button({label: 'Cadastrar voz desta faixa (10 s)',
                can_focus: true, style_class: 'button', style: 'padding: 3px 8px;'});
            this._enroll.connect('clicked', () => {
                if (this._info)
                    helper.call('EnrollTrack', '(ss)', [this._info.serial, '']);
            });
            this.add_child(this._enroll);
            this.updateInfo(null, false);
        }

        stream.connectObject(
            'notify::name', () => this._syncName(),
            'notify::description', () => this._syncName(),
            'notify::volume', () => this._syncVolume(),
            'notify::is-muted', () => this._syncMute(), this);
        const syncLimit = () => {
            const amplified = settings.get_boolean(AMPLIFIED_VOLUME);
            this.slider.block_signal_handler(this._sliderChangedId);
            this.slider.maximum_value = amplified
                ? control.get_vol_max_amplified() / this._normalVolume : 1;
            this.slider.clearMarks();
            if (amplified)
                this.slider.addMark(1);
            this.slider.unblock_signal_handler(this._sliderChangedId);
            this._syncVolume();
        };
        settings.connectObject(`changed::${AMPLIFIED_VOLUME}`, syncLimit, this);
        this._syncName();
        this._syncVolume();
        this._syncMute();
        syncLimit();
    }

    _syncName() {
        const {name, description} = this._stream;
        let title = name && description && name !== description
            ? `${name} — ${description}` : name || description || 'Fluxo de áudio';
        if (this._chromeTrack) {
            if (description?.toLowerCase() === 'playback')
                title = name || 'Chrome';
            title += ` · Faixa ${this._info?.track ?? this._chromeTrack}`;
            if (this._latest)
                title = `● ${title}`;
        }
        this._name.text = title;
        this.slider.accessible_name = `Volume de ${title}`;
        this._syncMute();
    }

    updateInfo(info, latest, state = null) {
        this._info = info;
        this._latest = latest;
        if (!this._classification)
            return;
        this._choices.reactive = Boolean(info);
        this._choices.opacity = info ? 255 : 100;
        this._enroll.reactive = Boolean(info && !state?.enrolling);
        this._enroll.opacity = this._enroll.reactive ? 255 : 100;
        this._enroll.label = state && info && state.enrollment_track === info.serial
            ? `Capturando voz… ${state.enrollment_seconds}/10 s` : 'Cadastrar voz desta faixa (10 s)';
        let text = info ? `${info.class} · ${Math.round(info.confidence * 100)}%` : 'Análise indisponível';
        if (info?.override)
            text += ' · manual';
        if (info?.manual_volume)
            text += ' · volume protegido';
        if (info?.narrator === 'COMPATÍVEL')
            text += ` · ${info.narrator_name?.slice(0, 24) ?? 'narrador compatível'}`;
        else if (info?.narrator === 'OUTRA_VOZ')
            text += ' · outra voz';
        if (info?.analysis_error || (info && info.capture !== 'OK'))
            text += ' · análise com falha';
        this._classification.text = text;
        this._name.set_style(latest ? 'font-weight: bold; color: #99c1f1;' : '');
        this._syncName();
        this._syncVolume();
        this._syncMute();
    }

    _syncVolume() {
        const level = this._info?.buffered ? this._info.volume / 100 : this._stream.volume / this._normalVolume;
        this.slider.block_signal_handler(this._sliderChangedId);
        this.slider.value = level;
        this.slider.unblock_signal_handler(this._sliderChangedId);
        this._percent.text = `${Math.round(level * 100)}%`;
    }

    _syncMute() {
        const muted = this._info?.buffered ? this._info.mute : this._stream.isMuted;
        this._muteIcon.icon_name = muted
            ? 'audio-volume-muted-symbolic' : 'audio-volume-high-symbolic';
        this._muteButton.accessible_name = `${muted ? 'Reativar' : 'Silenciar'} ${this._name.text}`;
    }
});

const MixerButton = GObject.registerClass(
class MixerButton extends PanelMenu.Button {
    _init(preferences, openPreferences) {
        super._init(0.5, TITLE);
        this.add_child(new St.Icon({
            gicon: Gio.ThemedIcon.new_from_names([
                'multimedia-equalizer-symbolic', 'audio-x-generic-symbolic',
            ]),
            style_class: 'system-status-icon',
        }));

        const header = new PopupMenu.PopupMenuItem(TITLE,
            {reactive: false, can_focus: false});
        header.label.set_style('font-weight: bold;');
        this.menu.addMenuItem(header);
        this._empty = new PopupMenu.PopupMenuItem('Nenhum aplicativo reproduzindo áudio',
            {reactive: false, can_focus: false});
        this.menu.addMenuItem(this._empty);

        this._listItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const scroll = new St.ScrollView({
            x_expand: true,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
            style: 'width: 19em; max-height: 19em;',
        });
        this._list = new St.BoxLayout({vertical: true});
        scroll.set_child(this._list);
        this._listItem.add_child(scroll);
        this.menu.addMenuItem(this._listItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addChromeVolumeSetting(preferences, 'voice-start-volume', 'Novas faixas de voz');
        this._addChromeVolumeSetting(preferences, 'sounds-start-volume', 'Novas faixas de sons');
        this._status = new PopupMenu.PopupMenuItem('Análise iniciando…',
            {reactive: false, can_focus: false});
        this._status.label.set_style('font-size: 0.85em;');
        this._status.label.clutter_text.line_wrap = true;
        this.menu.addMenuItem(this._status);
        const prefsItem = new PopupMenu.PopupMenuItem('Narrador e identificação…');
        prefsItem.connect('activate', openPreferences);
        this.menu.addMenuItem(prefsItem);

        this._rows = new Map();
        this._nextChromeTrack = 1;
        this._preferences = preferences;
        this._helper = new Helper(() => this._syncAnalysis());
        this.connect('destroy', () => this._helper.destroy());
        this._settings = new Gio.Settings({schema_id: 'org.gnome.desktop.sound'});
        this._control = getMixerControl();
        this._control.connectObject(
            'stream-added', (control, id) => this._addStream(control.lookup_stream_id(id)),
            'stream-removed', (_control, id) => this._removeStream(id), this);
        for (const stream of this._control.get_sink_inputs())
            this._addStream(stream);
        this._syncVisibility();
    }

    _addChromeVolumeSetting(preferences, key, title) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({vertical: true, x_expand: true, style: 'spacing: 4px;'});
        const label = new St.Label();
        const slider = new Slider(preferences.get_uint(key) / 100);
        slider.accessible_name = title;
        const changedId = slider.connect('notify::value', () => {
            preferences.set_uint(key, Math.round(slider.value * 100));
        });
        const sync = () => {
            const percent = preferences.get_uint(key);
            label.text = `${title} · ${percent}%`;
            slider.block_signal_handler(changedId);
            slider.value = percent / 100;
            slider.unblock_signal_handler(changedId);
        };
        preferences.connectObject(`changed::${key}`, sync, item);
        sync();
        box.add_child(label);
        box.add_child(slider);
        item.add_child(box);
        this.menu.addMenuItem(item);
    }

    _addStream(stream) {
        if (!(stream instanceof Gvc.MixerSinkInput) || stream.isEventStream || stream.isVirtual)
            return;

        const id = stream.get_id();
        if (this._rows.has(id))
            return;

        const chromeTrack = isChrome(stream) ? this._nextChromeTrack++ : 0;
        const row = new StreamRow(stream, this._control, this._settings, chromeTrack, this._helper);
        this._rows.set(id, row);
        this._list.add_child(row);
        this._syncVisibility();
        this._syncAnalysis();
    }

    _syncAnalysis() {
        const state = this._helper.state;
        this._status.label.text = !state ? 'Análise indisponível · controles ativos'
            : state.error ? 'Servidor de áudio indisponível'
                : state.enrollment || (state.buffer.enabled
                    ? `Retenção por faixa · ${state.buffer.deadline_ms} ms`
                    : 'Análise ativa · fluxos anteriores preservados');
        for (const row of this._rows.values()) {
            const info = state?.tracks.find(t => t.pulse_index === String(row._stream.get_index()));
            row.updateInfo(info ?? null, Boolean(info && state.latest === info.serial), state);
        }
    }

    _removeStream(id) {
        this._rows.get(id)?.destroy();
        if (this._rows.delete(id))
            this._syncVisibility();
    }

    _syncVisibility() {
        this._empty.visible = this._rows.size === 0;
        this._listItem.visible = this._rows.size > 0;
    }
});

export default class MixerRapidoExtension extends Extension {
    enable() {
        this._button = new MixerButton(this.getSettings(), () => this.openPreferences());
        const quickSettings = Main.panel.statusArea.quickSettings.container;
        const position = quickSettings.get_parent().get_children().indexOf(quickSettings);
        Main.panel.addToStatusArea(this.uuid, this._button, position, 'right');
    }

    disable() {
        this._button?.destroy();
        this._button = null;
        Gio.Subprocess.new(['systemctl', '--user', 'stop', 'mixer-rapido.service'],
            Gio.SubprocessFlags.NONE);
    }
}
