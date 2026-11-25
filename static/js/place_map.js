// static/js/place_map.js: the Map page (templates/place_map.html, birddog/place_map.py).
// Three layers from /api/map; popups are built with DOM calls, never HTML strings,
// since names and quotes come from OCR.
(() => {
  'use strict';
  const BM = window.BirddogMap;
  const status = document.getElementById('status');

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined && c !== '') node.append(c);
    return node;
  }
  const link = (href, text, external) => el('a', {href, text, target: external ? '_blank' : null,
                                                  rel: external ? 'noopener' : null});
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  function communityPopup(l) {
    const where = [l.district, l.province].filter(Boolean).join(', ');
    return el('div', {class: 'pm-popup'},
      el('h3', {text: l.name}),
      l.alternate_names ? el('p', {class: 'small text-muted', text: `Also: ${l.alternate_names}`}) : null,
      where ? el('p', {text: `1900: ${where}`}) : null,
      l.population ? el('p', {text: `Jewish population: ${l.population}`}) : null,
      el('p', {class: 'small', text: `${plural(l.documents || 0, 'document')} linked; named in ${plural(l.books || 0, 'book')}`}));
  }

  function bookPopup(p) {
    const list = el('ul', {class: 'small'});
    for (const b of p.books) {
      list.append(el('li', {}, link(b.url, b.title, true), ` (${b.mentions}) · `,
        link(`/places/confirm?book=${b.document_id}&group=matched&state=all`, 'confirm')));
    }
    return el('div', {class: 'pm-popup'},
      el('h3', {text: p.name}),
      el('p', {text: `${plural(p.mentions, 'mention')} in ${plural(p.books.length, 'book')}`}), list);
  }

  function roughPopup(r) {
    return el('div', {class: 'pm-popup'},
      el('h3', {text: r.name}),
      r.as_written && r.as_written !== r.name ? el('p', {class: 'small', text: `As written: ${r.as_written}`}) : null,
      r.admin_context ? el('p', {class: 'small', text: r.admin_context}) : null,
      el('p', {text: `Rough position, ${r.confidence || 'unknown'} confidence`}),
      r.basis ? el('p', {class: 'small text-muted', text: r.basis}) : null,
      r.nearest ? el('p', {class: 'small', text: `Nearest known town: ${r.nearest} (${r.nearest_km} km)`}) : null,
      el('p', {class: 'small'}, 'Book: ', r.page_link ? link(r.page_link, r.title, true) : r.title),
      el('p', {}, link(`/places/confirm?book=${r.document_id}&group=unmatched`, 'Confirm where it is')));
  }

  async function load() {
    const response = await fetch('/api/map', {credentials: 'same-origin'}).catch(() => null);
    const data = response && response.ok ? await response.json().catch(() => null) : null;
    if (!data || !data.success) {
      status.textContent = 'The map data could not be loaded. Reload the page to try again.';
      status.classList.add('is-error');
      return;
    }
    const map = BM.create(document.getElementById('map'), {preferCanvas: true, keyboard: true});
    const communities = L.layerGroup(), books = L.layerGroup(), rough = L.layerGroup();
    const byName = new Map();

    for (const l of data.locations) {
      if (!BM.has(l.latitude, l.longitude)) continue;
      const m = BM.marker(l.latitude, l.longitude, 'match', l.name, {radius: 4, weight: 1, fillOpacity: 0.45});
      m.bindPopup(() => communityPopup(l));
      m.addTo(communities);
      byName.set(l.name, m);
    }
    for (const p of data.book_places) {
      if (!BM.has(p.latitude, p.longitude)) continue;
      const radius = Math.min(18, 5 + 2 * Math.sqrt(p.mentions));
      BM.marker(p.latitude, p.longitude, 'guess', `${p.name}: ${plural(p.mentions, 'mention')}`, {radius, fillOpacity: 0.45})
        .bindPopup(() => bookPopup(p)).addTo(books);
    }
    for (const r of data.rough) {
      if (!BM.has(r.latitude, r.longitude)) continue;
      BM.marker(r.latitude, r.longitude, 'nearest', `${r.name} (rough)`, {radius: 7, dashArray: '3 3', fillOpacity: 0})
        .bindPopup(() => roughPopup(r)).addTo(rough);
    }
    communities.addTo(map); books.addTo(map); rough.addTo(map);
    L.control.layers(null, {
      [`Communities (${communities.getLayers().length})`]: communities,
      [`Named in books (${books.getLayers().length})`]: books,
      [`Rough positions (${rough.getLayers().length})`]: rough,
    }, {collapsed: false}).addTo(map);

    // frame the middle 96% of points, so a few far-off places don't zoom out to all of Europe
    const all = [...communities.getLayers(), ...books.getLayers(), ...rough.getLayers()].map((m) => m.getLatLng());
    if (all.length) {
      const q = (values, f) => values.sort((a, b) => a - b)[Math.min(values.length - 1, Math.floor(f * values.length))];
      const lats = all.map((p) => p.lat), lons = all.map((p) => p.lng);
      map.fitBounds([[q(lats, 0.02), q(lons, 0.02)], [q(lats, 0.98), q(lons, 0.98)]], {padding: [12, 12]});
    } else {
      map.setView([49.0, 31.0], 6);
    }

    const list = document.getElementById('find-list');
    for (const name of [...byName.keys()].sort()) list.append(el('option', {value: name}));
    document.getElementById('find').addEventListener('change', (e) => {
      const m = byName.get(e.target.value.trim());
      if (!m) return;
      if (!map.hasLayer(communities)) communities.addTo(map);
      map.setView(m.getLatLng(), 11);
      m.openPopup();
    });
    status.textContent = `${communities.getLayers().length} communities, ${books.getLayers().length} named in books, ${rough.getLayers().length} rough positions.`;
  }

  load();
})();
