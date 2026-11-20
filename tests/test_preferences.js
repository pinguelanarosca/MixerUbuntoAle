import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

Gio.resources_register(Gio.Resource.load('/usr/share/gnome-shell/org.gnome.Shell.Extensions.src.gresource'));
const root = `${GLib.get_home_dir()}/.local/share/gnome-shell/extensions/mixer-rapido@alee.local`;
const output = GLib.getenv('MIXER_TEST_OUTPUT');
if (!output)
    throw new Error('Defina MIXER_TEST_OUTPUT para o diretório de evidências.');
const {default: Prefs} = await import(`file://${root}/prefs.js`);
const metadata = JSON.parse(new TextDecoder().decode(GLib.file_get_contents(`${root}/metadata.json`)[1]));
const app = new Adw.Application({application_id: 'org.gnome.Shell.Extensions.MixerRapido.TestPreferences',
    flags: Gio.ApplicationFlags.NON_UNIQUE});
app.connect('activate', () => {
    const window = new Adw.PreferencesWindow({application: app, title: 'Mixer Rápido — Preferências',
        default_width: 720, default_height: 720});
    const prefs = new Prefs({...metadata, path: root, dir: Gio.File.new_for_path(root)});
    prefs.fillPreferencesWindow(window);
    window.present();
    GLib.timeout_add_once(GLib.PRIORITY_DEFAULT, 1800, () => {
        const rows = [];
        function walk(widget) {
            if (widget instanceof Adw.EntryRow && widget.text === GLib.getenv('MIXER_TEST_VOICE_NAME')) {
                widget.text = GLib.getenv('MIXER_TEST_VOICE_RENAME');
                widget.emit('apply');
            }
            if (widget instanceof Adw.PreferencesRow)
                rows.push({type: widget.constructor.name, title: widget.title, value: widget.value, text: widget.text});
            for (let child = widget.get_first_child(); child; child = child.get_next_sibling())
                walk(child);
        }
        walk(window);
        GLib.file_set_contents(`${output}/preferences-widgets.json`, JSON.stringify(rows, null, 2));
    });
    GLib.timeout_add_once(GLib.PRIORITY_DEFAULT, 10000, () => {
        window.close();
        app.quit();
    });
});
app.run([]);
