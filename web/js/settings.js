// Seite „Einstellungen“: Sprache (gilt für das ganze Dashboard) und Darstellung.
import { h, icon, api, toast } from './ui.js';
import { LANG, LANGS, T, setLang } from './i18n.js';
import { load, save, S, refreshState } from './core.js';
import { setSdr } from './standards.js';

/** Sendegerät wählen (POST /api/sdr, nur ohne laufende Sendung) und HackRF-Hinweise */
function sdrCard() {
  const box = h('div', { tid: 'settings-sdr' });
  const draw = () => {
    const sdr = S.state.sdr || { kind: 'pluto' }, pl = S.state.pluto || {};
    const pick = async kind => {
      try {
        const r = await api('POST', '/api/sdr', { kind });
        S.state.sdr = r.sdr;
        setSdr(r.sdr?.kind);
        toast(T('Sendegerät: {name}', { name: kind === 'hackrf' ? 'HackRF One' : 'ADALM-Pluto' }), 'ok');
        if (r.errors?.length) toast(r.errors.join(' · '), 'error', 9000);
        await refreshState();
      } catch (e) { toast(e.message, 'error', 7000); }
      draw();
    };
    const opt = (kind, name, desc) => h('label.onb-item', null,
      h('input', { type: 'radio', name: 'sdr', tid: `settings-sdr-${kind}`, checked: sdr.kind === kind || null, onchange: () => pick(kind) }),
      h('span', null, h('b', null, name), h('br'), h('small.muted', null, desc)));
    const hack = sdr.kind === 'hackrf';
    box.replaceChildren(...[
      h('p.muted', null, T('Welches Gerät sendet. Wechseln geht nur, wenn keine Sendung läuft.')),
      opt('pluto', 'ADALM-Pluto', T('Alle Standards; DVB-S/S2, DVB-T2, PAL u. a. mit dem Modulator der Muxwright-Firmware auf dem Pluto.')),
      opt('hackrf', 'HackRF One', T('Offizielle Firmware und libhackrf von Great Scott Gadgets. DAB+, UKW, DVB-T, DVB-C (Annex A) und DATV, Modulator am PC, bis 20 MS/s.')),
      hack ? h('div', { tid: 'settings-sdr-hackrf-info' },
        h('p.small', null, icon(pl.connected ? 'check' : 'x'), ' ', pl.connected ? `${pl.model || 'HackRF'} · FW ${pl.fw || '?'} · libhackrf ${pl.lib || '?'}` : (pl.error || sdr.lib_error || T('HackRF nicht verbunden'))),
        pl.fw_outdated ? h('p.small', null, icon('alert'), ' ', T('Firmware älter als die offizielle {rel} – Seite „Firmware“, Gerät „HackRF One“.', { rel: pl.fw_release })) : null,
        h('ul.small.muted', null,
          h('li', null, T('Halbduplex: kein Kontrollempfang und keine RX-Vorschau während des Sendens.')),
          h('li', null, T('Dämpfung 0 … 47 dB (ZF-Verstärkung); der HF-Verstärker bleibt immer aus.')),
          h('li', null, T('8-Bit-DAC: für DVB-C lieber 64-QAM als 256-QAM.')),
          h('li', null, T('Windows: Der HackRF braucht den WinUSB-Treiber (z. B. mit Zadig einrichten).')))) : null].filter(Boolean));
  };
  draw();
  return h('section.card', null, h('h3', null, icon('chip'), ' ', T('Sendegerät')), box);
}

export function pageSettings({ applyTheme } = {}) {
  const sel = h('select', { tid: 'settings-lang', 'aria-label': T('Sprache') }, Object.entries(LANGS).map(([k, n]) => h('option', { value: k }, n)));
  sel.value = LANG;
  sel.addEventListener('change', () => setLang(sel.value));
  const themes = [['system', T('wie System'), 'auto'], ['light', T('hell'), 'sun'], ['dark', T('dunkel'), 'moon']];
  const seg = h('div.seg', { role: 'group', 'aria-label': T('Darstellung') });
  const drawSeg = () => seg.replaceChildren(...themes.map(([k, label, ic]) => h('button', {
    type: 'button', tid: `settings-theme-${k}`, class: load('theme', 'system') === k ? 'on' : null,
    onclick: () => { save('theme', k); applyTheme?.(k); drawSeg(); } }, icon(ic), ' ', label)));
  drawSeg();
  return h('div.page-stack', { tid: 'page-einstellungen' },
    h('section.card', null, h('h3', null, icon('flag'), ' ', T('Sprache')),
      h('p.muted', null, T('Gilt für das ganze Dashboard, die Einrichtung und die Logdatei. Die Seite lädt dabei neu.')),
      h('label.inrow', null, sel)),
    sdrCard(),
    h('section.card', null, h('h3', null, icon('sun'), ' ', T('Darstellung')), seg));
}
