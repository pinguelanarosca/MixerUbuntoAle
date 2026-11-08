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
const CHROME_START_VOLUME = 'chrome-start-volume';

function isChrome(stream) {
    return [stream.name, stream.applicationId, stream.iconName].some(value =>
        ['google chrome', 'google-chrome', 'com.google.chrome', 'chromium',
            'chromium-browser', 'org.chromium.chromium'].includes(value?.toLowerCase()));
}

const StreamRow = GObject.registerClass(
class StreamRow extends St.BoxLayout {
    _init(stream, control, settings, chromeTrack, initialVolume) {
        super._init({vertical: true, style: 'spacing: 4px; padding: 5px 0;'});
        this._stream = stream;
        this._chromeTrack = chromeTrack;
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
        this._muteButton.connect('clicked', () => stream.change_is_muted(!stream.isMuted));
        controls.add_child(this._muteButton);
        this.slider = new Slider(0);
        controls.add_child(this.slider);
        this.add_child(controls);
        const setVolume = volume => {
            if (stream.set_volume(volume))
                stream.push_volume();
        };
        let initialVolumeId = 0;
        const cancelInitialVolume = () => {
            if (initialVolumeId) {
                GLib.Source.remove(initialVolumeId);
                initialVolumeId = 0;
            }
        };
        this._sliderChangedId = this.slider.connect('notify::value', () => {
            cancelInitialVolume();
            setVolume(Math.round(this.slider.value * this._normalVolume));
        });
        if (initialVolume !== null) {
            // The audio server may still restore the application's volume after stream-added.
            initialVolumeId = GLib.timeout_add_once(GLib.PRIORITY_DEFAULT, 150, () => {
                initialVolumeId = 0;
                setVolume(initialVolume);
            });
            this.connect('destroy', cancelInitialVolume);
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
        syncLimit();
    }

    _syncName() {
        const {name, description} = this._stream;
        let title = name && description && name !== description
            ? `${name} — ${description}` : name || description || 'Fluxo de áudio';
        if (this._chromeTrack) {
            if (description?.toLowerCase() === 'playback')
                title = name || 'Chrome';
            title += ` · Faixa ${this._chromeTrack}`;
        }
        this._name.text = title;
        this.slider.accessible_name = `Volume de ${title}`;
        this._syncMute();
    }

    _syncVolume() {
        const level = this._stream.volume / this._normalVolume;
        this.slider.block_signal_handler(this._sliderChangedId);
        this.slider.value = level;
        this.slider.unblock_signal_handler(this._sliderChangedId);
        this._percent.text = `${Math.round(level * 100)}%`;
    }

    _syncMute() {
        const muted = this._stream.isMuted;
        this._muteIcon.icon_name = muted
            ? 'audio-volume-muted-symbolic' : 'audio-volume-high-symbolic';
        this._muteButton.accessible_name = `${muted ? 'Reativar' : 'Silenciar'} ${this._name.text}`;
    }
});

const MixerButton = GObject.registerClass(
class MixerButton extends PanelMenu.Button {
    _init(preferences) {
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
        this._addChromeVolumeSetting(preferences);

        this._rows = new Map();
        this._nextChromeTrack = 1;
        this._preferences = preferences;
        this._settings = new Gio.Settings({schema_id: 'org.gnome.desktop.sound'});
        this._control = getMixerControl();
        this._control.connectObject(
            'stream-added', (control, id) => this._addStream(control.lookup_stream_id(id), true),
            'stream-removed', (_control, id) => this._removeStream(id), this);
        for (const stream of this._control.get_sink_inputs())
            this._addStream(stream);
        this._syncVisibility();
    }

    _addChromeVolumeSetting(preferences) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({vertical: true, x_expand: true, style: 'spacing: 4px;'});
        const label = new St.Label();
        const slider = new Slider(preferences.get_uint(CHROME_START_VOLUME) / 100);
        slider.accessible_name = 'Volume inicial das novas faixas do Chrome';
        const changedId = slider.connect('notify::value', () => {
            preferences.set_uint(CHROME_START_VOLUME, Math.round(slider.value * 100));
        });
        const sync = () => {
            const percent = preferences.get_uint(CHROME_START_VOLUME);
            label.text = `Novas faixas do Chrome · ${percent}%`;
            slider.block_signal_handler(changedId);
            slider.value = percent / 100;
            slider.unblock_signal_handler(changedId);
        };
        preferences.connectObject(`changed::${CHROME_START_VOLUME}`, sync, item);
        sync();
        box.add_child(label);
        box.add_child(slider);
        item.add_child(box);
        this.menu.addMenuItem(item);
    }

    _addStream(stream, isNew = false) {
        if (!(stream instanceof Gvc.MixerSinkInput) || stream.isEventStream || stream.isVirtual)
            return;

        const id = stream.get_id();
        if (this._rows.has(id))
            return;

        const chromeTrack = isChrome(stream) ? this._nextChromeTrack++ : 0;
        const initialVolume = chromeTrack && isNew
            ? Math.round(this._control.get_vol_max_norm() *
                this._preferences.get_uint(CHROME_START_VOLUME) / 100)
            : null;
        const row = new StreamRow(stream, this._control, this._settings, chromeTrack, initialVolume);
        this._rows.set(id, row);
        this._list.add_child(row);
        this._syncVisibility();
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
        this._button = new MixerButton(this.getSettings());
        const quickSettings = Main.panel.statusArea.quickSettings.container;
        const position = quickSettings.get_parent().get_children().indexOf(quickSettings);
        Main.panel.addToStatusArea(this.uuid, this._button, position, 'right');
    }

    disable() {
        this._button?.destroy();
        this._button = null;
    }
}
