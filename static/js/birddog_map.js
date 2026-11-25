// static/js/birddog_map.js: small Leaflet maps inside Birddog pages (the Confirm
// places cards, the Map page). Needs Leaflet (templates/_leaflet.html) loaded first.
// Same grey Esri basemap as the Grafana dashboards, so places look alike in both.
// Names come from OCR: tooltips are set as text nodes, never as HTML.
(() => {
  'use strict';
  const ESRI = 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/{name}/MapServer/tile/{z}/{y}/{x}';
  const CREDIT = 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors';
  const COLORS = {
    guess: '#8a5a0b',      // --bd-ochre: Claude's rough position
    match: '#1f5f57',      // --bd-primary: the current (automatic) match
    confirmed: '#2c6e45',  // --bd-green
    nearest: '#56625f',    // --bd-ink-muted: candidates
    picked: '#a3312a',     // --bd-danger: what the person has picked
  };

  function text(value) {
    const span = document.createElement('span');
    span.textContent = value;
    return span;
  }

  function has(lat, lon) {
    return typeof lat === 'number' && typeof lon === 'number' && !Number.isNaN(lat) && !Number.isNaN(lon);
  }

  // A map in `el`, basemap and labels added; options go to L.map.
  function create(el, options) {
    const map = L.map(el, {zoomSnap: 0.5, scrollWheelZoom: false, ...(options || {})});
    L.tileLayer(ESRI.replace('{name}', 'World_Light_Gray_Base'), {attribution: CREDIT, maxZoom: 16}).addTo(map);
    L.tileLayer(ESRI.replace('{name}', 'World_Light_Gray_Reference'), {maxZoom: 16, pane: 'shadowPane'}).addTo(map);
    // wheel zoom only after a click, so the page still scrolls past the map
    map.on('click focus', () => map.scrollWheelZoom.enable());
    map.on('mouseout blur', () => map.scrollWheelZoom.disable());
    return map;
  }

  // circle marker; kind is a COLORS key, label is plain text
  function marker(lat, lon, kind, label, extra) {
    const filled = kind !== 'nearest';
    const m = L.circleMarker([lat, lon], {
      radius: kind === 'nearest' ? 7 : 8, weight: kind === 'picked' ? 3 : 2,
      color: COLORS[kind], fillColor: COLORS[kind], fillOpacity: filled ? 0.75 : 0.15,
      ...Object.fromEntries(Object.entries(extra || {}).filter(([k]) => k !== 'permanent')),
    });
    if (label) {
      m.bindTooltip(text(label), (extra || {}).permanent
        ? {permanent: true, direction: 'right', offset: [8, 0], className: 'bd-map-label'}
        : {direction: 'top', offset: [0, -6]});
    }
    return m;
  }

  // Shows the map only when it scrolls into view: a page of 20 cards would
  // otherwise load 20 maps' tiles at once.
  function whenVisible(el, build) {
    if (!('IntersectionObserver' in window)) { build(); return; }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { io.disconnect(); build(); }
    }, {rootMargin: '200px'});
    io.observe(el);
  }

  // The map on one Confirm places card. `p` is the card (place_confirm.list_places),
  // `form` the card's form, `name` its radio group. Clicking a nearest town picks
  // it; picking any answer marks it on the map. Returns null when nothing has a position.
  function cardMap(p, form, name) {
    const points = [];
    if (has(p.guess_latitude, p.guess_longitude)) {
      points.push({lat: p.guess_latitude, lon: p.guess_longitude, kind: 'guess',
                   label: `Rough position (${p.guess_confidence || 'unknown'} confidence)`});
    }
    if (has(p.auto_latitude, p.auto_longitude)) {
      points.push({lat: p.auto_latitude, lon: p.auto_longitude, kind: 'match',
                   label: `Current match: ${p.auto_location_name || p.auto_location_id}`, id: p.auto_location_id});
    }
    if (has(p.confirmed_latitude, p.confirmed_longitude) && p.confirmed_location_id !== p.auto_location_id) {
      points.push({lat: p.confirmed_latitude, lon: p.confirmed_longitude, kind: 'confirmed',
                   label: `Confirmed: ${p.confirmed_location_name || p.confirmed_location_id}`});
    }
    (p.nearest || []).forEach((n, i) => {
      if (has(n.latitude, n.longitude)) {
        points.push({lat: n.latitude, lon: n.longitude, kind: 'nearest', id: n.location_id,
                     label: n.name, permanent: true});
      }
    });
    if (!points.length) return null;

    const wrap = document.createElement('div');
    wrap.className = 'pc-map-wrap';
    const el = document.createElement('div');
    el.className = 'pc-map';
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'Map of the rough position, the current match and the nearest towns. The same choices are listed below.');
    const legend = document.createElement('p');
    legend.className = 'pc-map-legend small';
    const kinds = [...new Set(points.map((x) => x.kind))];
    const words = {guess: 'rough position', match: 'current match', confirmed: 'confirmed',
                   nearest: 'nearest towns (click to pick)'};
    for (const k of kinds) {
      const dot = document.createElement('span');
      dot.className = `pc-dot pc-dot-${k}`;
      dot.setAttribute('aria-hidden', 'true');
      legend.append(dot, ` ${words[k]} `);
    }
    wrap.append(el, legend);

    whenVisible(el, () => {
      const map = create(el, {keyboard: true});
      let picked = null;
      for (const x of points) {
        const m = marker(x.lat, x.lon, x.kind, x.label, x.permanent ? {permanent: true} : null).addTo(map);
        if (x.kind === 'nearest' || x.kind === 'match') {
          m.on('click', () => {
            const radio = form.querySelector(`input[name="${name}"][data-location-id="${CSS.escape(x.id)}"]`);
            if (radio) { radio.checked = true; radio.dispatchEvent(new Event('change', {bubbles: true})); radio.focus(); }
          });
        }
      }
      const bounds = L.latLngBounds(points.map((x) => [x.lat, x.lon]));
      map.fitBounds(bounds.pad(0.3), {maxZoom: 11});
      if (points.length === 1) map.setZoom(10);

      // mark whatever answer is picked (a listed town or a search result)
      form.addEventListener('change', () => {
        // same rule as the card's submit: its own answer, or the search pick
        // when that answer is "Another place"
        const main = form.querySelector(`input[name="${name}"]:checked`);
        const input = main && main.value === 'search'
          ? form.querySelector(`input[name="${name}-pick"]:checked`) : main;
        if (picked) { picked.remove(); picked = null; }
        if (!input || !input.dataset.locationId) return;
        let lat = Number(input.dataset.lat), lon = Number(input.dataset.lon);
        if (!input.dataset.lat) {
          const x = points.find((pt) => pt.id === input.dataset.locationId);
          if (!x) return;
          lat = x.lat; lon = x.lon;
        }
        if (!has(lat, lon)) return;
        picked = marker(lat, lon, 'picked', 'Your answer', {radius: 11, fillOpacity: 0}).addTo(map);
        if (!map.getBounds().contains([lat, lon])) map.fitBounds(bounds.extend([lat, lon]).pad(0.3), {maxZoom: 11});
      });
    });
    return wrap;
  }

  window.BirddogMap = {create, marker, whenVisible, cardMap, COLORS, has};
})();
