// static/js/place_confirm.js: the Confirm places page (templates/place_confirm.html,
// birddog/place_confirm.py). Cards are built with DOM calls, never innerHTML, since
// names and quotes come from OCR.
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const cardsEl = $('cards');
  const isAdmin = cardsEl.dataset.role === 'admin';
  const state = {page: 1, total: 0, pageSize: Number(cardsEl.dataset.pageSize) || 20};
  const GROUP_LABEL = {unmatched: 'Not matched', matched: 'Matched automatically', entry: 'Register entry'};

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c);
    return node;
  }

  async function call(method, url, body) {
    const response = await fetch(url, {
      method, headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
      body: body ? JSON.stringify(body) : undefined,
    }).catch(() => null);
    const data = response ? await response.json().catch(() => ({})) : {};
    if (!response || !response.ok || !data.success) {
      throw new Error(data.message || data.error || 'Something went wrong. Try again.');
    }
    return data;
  }

  function filters() {
    const f = new URLSearchParams();
    for (const id of ['group', 'source', 'book', 'state']) {
      const v = $(`f-${id}`).value;
      if (v) f.set(id, v);
    }
    f.set('page', state.page);
    return f;
  }

  function cleanTitle(t) {
    return (t || '').replace(/^File:/, '').replace(/\.pdf$/i, '').replace(/_/g, ' ');
  }

  function statusText(p) {
    if (p.confirmation_status === 'confirmed') {
      const what = p.confirmed_answer === 'location'
        ? `${p.confirmed_location_name || p.confirmed_location_id}` : 'not a place';
      return `Confirmed: ${what} (${p.confirmed_by === 'admin' ? 'by an admin' : 'two people agree'}).`;
    }
    if (p.confirmation_status === 'pending') {
      return `Waiting for a second answer (${p.decisions} so far).`;
    }
    return 'No answers yet.';
  }

  function choiceRadio(name, value, label, locationId, checked) {
    const id = `${name}-${value}-${locationId || ''}`.replace(/[^\w-]/g, '_');
    return el('div', {class: 'form-check'},
      el('input', {class: 'form-check-input', type: 'radio', name, id, value,
                   'data-location-id': locationId || null, checked: !!checked}),
      el('label', {class: 'form-check-label', for: id, text: label}));
  }

  function card(p, index) {
    const name = `pc-${state.page}-${index}`;
    const mine = p.mine || {};
    const isMine = (choice, loc) => mine.choice === choice && (choice !== 'location' || mine.location_id === loc);
    const headingId = `${name}-h`;

    const head = el('div', {class: 'd-flex flex-wrap gap-2 align-items-baseline'},
      el('h2', {id: headingId}, el('span', {text: p.normalized || p.as_written})),
      el('span', {class: `badge pc-badge-${p.place_group}`, text: GROUP_LABEL[p.place_group] || p.place_group}),
      el('span', {class: 'text-muted small', text: `${p.mentions} mention${p.mentions === 1 ? '' : 's'} in this book`}));
    const bookTitle = el('p', {class: 'pc-book small mb-0'}, 'Book: ', el('cite', {text: cleanTitle(p.title)}));

    const facts = el('dl', {class: 'row small mb-0 mt-2'},
      el('dt', {class: 'col-sm-3', text: 'As written'}), el('dd', {class: 'col-sm-9 pc-written', text: p.as_written || '—'}),
      el('dt', {class: 'col-sm-3', text: 'Administrative context'}), el('dd', {class: 'col-sm-9', text: p.admin_context || '—'}));

    let current;
    if (p.auto_location_id) {
      current = el('p', {class: 'pc-current'}, 'Current match: ',
        el('strong', {text: p.auto_location_name || p.auto_location_id}),
        p.match_score ? ` (score ${p.match_score.toFixed(2)})` : '');
    } else if (p.guess_latitude !== null && p.guess_latitude !== undefined) {
      current = el('p', {class: 'pc-current'}, 'Rough position: ',
        el('a', {href: `https://www.openstreetmap.org/?mlat=${p.guess_latitude}&mlon=${p.guess_longitude}#map=11/${p.guess_latitude}/${p.guess_longitude}`,
                 target: '_blank', rel: 'noopener', text: `${p.guess_latitude.toFixed(3)}, ${p.guess_longitude.toFixed(3)}`}),
        ` (${p.guess_confidence} confidence${p.guess_basis ? ': ' + p.guess_basis : ''})`);
    } else {
      current = el('p', {class: 'pc-current text-muted', text: p.guess_confidence === 'none'
        ? `No position found${p.guess_basis ? ' (' + p.guess_basis + ')' : ''}.` : 'No match and no rough position yet.'});
    }

    const examples = el('ul', {class: 'pc-examples small', 'aria-label': 'Where it was read'});
    for (const x of p.examples || []) {
      examples.append(el('li', {},
        el('a', {href: x.page_link, target: '_blank', rel: 'noopener', text: `Page ${x.page_no}`}),
        x.quote ? el('span', {}, ': «', el('span', {class: 'pc-quote', text: x.quote}), '»') : ''));
    }

    // the same name in other books: may be the same place, or another village
    let others = null;
    if ((p.other_books || []).length) {
      others = el('fieldset', {class: 'pc-choices pc-others'},
        el('legend', {text: 'Same answer for other books that name this place (each still needs its own confirmation)'}));
      for (const o of p.other_books) {
        const id = `${name}-also-${o.document_id}`;
        const where = o.confirmation_status === 'confirmed' ? 'confirmed'
          : o.confirmation_status === 'pending' ? 'waiting for a 2nd answer' : 'no answers yet';
        others.append(el('div', {class: 'form-check'},
          el('input', {class: 'form-check-input', type: 'checkbox', id, value: o.document_id, 'data-also': '1'}),
          el('label', {class: 'form-check-label small', for: id,
                       text: `${cleanTitle(o.title)} (${o.mentions} mention${o.mentions === 1 ? '' : 's'}; ${where})`})));
      }
    }

    const choices = el('fieldset', {class: 'pc-choices'}, el('legend', {text: 'Where is it?'}));
    if (p.auto_location_id) {
      choices.append(choiceRadio(name, 'location', `Confirm: ${p.auto_location_name || p.auto_location_id}`,
                                 p.auto_location_id, isMine('location', p.auto_location_id)));
    }
    for (const n of p.nearest || []) {
      choices.append(choiceRadio(name, 'location',
        `${n.name}${n.province ? ', ' + n.province : ''} (${n.km} km ${p.auto_location_id ? 'from the match' : 'from the rough position'})`,
        n.location_id, isMine('location', n.location_id)));
    }
    const otherId = `${name}-other`;
    const otherRadio = el('input', {class: 'form-check-input', type: 'radio', name, id: otherId, value: 'search'});
    const searchInput = el('input', {type: 'search', class: 'form-control form-control-sm mt-1', id: `${name}-q`,
                                     placeholder: 'Town name, e.g. Kaniv or Канів', autocomplete: 'off'});
    const searchLabel = el('label', {class: 'visually-hidden', for: `${name}-q`, text: 'Search the location table'});
    const results = el('ul', {class: 'pc-search-results', 'aria-live': 'polite'});
    let timer;
    searchInput.addEventListener('input', () => {
      otherRadio.checked = true;
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = searchInput.value.trim();
        results.replaceChildren();
        if (q.length < 2) return;
        try {
          const data = await call('GET', `/api/places/locations?q=${encodeURIComponent(q)}`);
          if (!data.locations.length) results.append(el('li', {class: 'text-muted small', text: 'No match in the location table.'}));
          for (const loc of data.locations) {
            const where = [loc.district, loc.province].filter(Boolean).join(', ');
            const item = choiceRadio(`${name}-pick`, 'location', `${loc.name}${where ? ' (' + where + ')' : ''}`, loc.location_id, false);
            const input = item.querySelector('input');   // its position, for the card map
            if (loc.latitude !== null && loc.latitude !== undefined) {
              input.dataset.lat = loc.latitude;
              input.dataset.lon = loc.longitude;
            }
            results.append(el('li', {}, item));
          }
        } catch (err) {
          results.append(el('li', {class: 'text-danger small', text: err.message}));
        }
      }, 250);
    });
    choices.append(el('div', {class: 'form-check'}, otherRadio,
      el('label', {class: 'form-check-label', for: otherId, text: 'Another place:'}), searchLabel, searchInput, results));
    choices.append(choiceRadio(name, 'not_place', 'Not a place', null, isMine('not_place')));
    choices.append(choiceRadio(name, 'unsure', "Can't tell", null, isMine('unsure')));

    const noteId = `${name}-note`;
    const note = el('textarea', {class: 'form-control form-control-sm', id: noteId, rows: 2, maxlength: 1000});
    note.value = mine.note || '';
    const status = el('p', {class: 'pc-status small', role: 'status', text: statusText(p)});
    const save = el('button', {type: 'submit', class: 'btn btn-primary btn-sm mt-2', text: isAdmin ? 'Save (admin decision)' : 'Save my answer'});

    const form = el('form', {class: 'card-body', 'aria-labelledby': headingId},
      head, bookTitle, facts, current, examples, choices, others,
      el('label', {for: noteId, class: 'form-label small mt-2 mb-1', text: 'Note (optional)'}), note,
      save, status);
    if (window.BirddogMap) {
      const mapEl = window.BirddogMap.cardMap(p, form, name);
      if (mapEl) current.after(mapEl);
    }
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      let picked = form.querySelector(`input[name="${name}-pick"]:checked`);
      const main = form.querySelector(`input[name="${name}"]:checked`);
      if (main && main.value !== 'search') picked = main;
      if (!picked) {
        status.textContent = main && main.value === 'search' ? 'Pick one of the search results.' : 'Choose an answer first.';
        status.classList.add('is-error');
        (main || form.querySelector(`input[name="${name}"]`)).focus();
        return;
      }
      save.disabled = true;
      status.classList.remove('is-error');
      status.textContent = 'Saving…';
      try {
        const also = [...form.querySelectorAll('input[data-also]:checked')].map((c) => Number(c.value));
        const data = await call('POST', '/api/places/decision', {
          place_key: p.place_key, document_id: p.document_id, also_document_ids: also,
          choice: picked.value, location_id: picked.dataset.locationId || null, note: note.value,
        });
        const r = data.result;
        const merged = {...p, confirmation_status: r.status, confirmed_answer: r.answer,
          confirmed_location_id: r.location_id, confirmed_by: r.confirmed_by,
          decisions: (p.decisions || 0) + 1,
          confirmed_location_name: r.location_id === p.auto_location_id ? p.auto_location_name
            : (picked.dataset.locationId === r.location_id ? picked.nextSibling.textContent : r.location_id)};
        let text = 'Saved. ' + statusText(merged);
        if (r.linked) text += ' Linked to this book.';
        if (r.unlinked) text += ' Removed the earlier link.';
        const done = (data.others || []).filter((o) => o.status !== 'error').length;
        const failed = (data.others || []).filter((o) => o.status === 'error');
        if (done) text += ` Also recorded for ${done} other book${done === 1 ? '' : 's'}.`;
        if (failed.length) text += ` Not recorded for ${failed.length}: ${failed[0].message}.`;
        status.textContent = text;
        form.closest('.pc-card').classList.toggle('is-confirmed', r.status === 'confirmed');
      } catch (err) {
        status.textContent = err.message;
        status.classList.add('is-error');
      } finally {
        save.disabled = false;
      }
    });
    return el('section', {class: `card pc-card${p.confirmation_status === 'confirmed' ? ' is-confirmed' : ''}`}, form);
  }

  async function load(focusList) {
    $('list-heading').textContent = 'Loading…';
    try {
      const data = await call('GET', `/api/places?${filters()}`);
      state.total = data.total;
      cardsEl.replaceChildren(...data.places.map(card));
      const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
      $('list-heading').textContent = data.total
        ? `${data.total} place${data.total === 1 ? '' : 's'}` : 'Nothing to confirm with these filters.';
      $('page-info').textContent = `Page ${state.page} of ${pages}`;
      $('prev').disabled = state.page <= 1;
      $('next').disabled = state.page >= pages;
      if (focusList) $('list-heading').focus();
    } catch (err) {
      $('list-heading').textContent = err.message;
    }
  }

  // filters from the address (links from the Map page: ?book=…&group=…)
  const wanted = new URLSearchParams(window.location.search);
  for (const id of ['group', 'source', 'book', 'state']) {
    const select = $(`f-${id}`);
    if (wanted.has(id) && [...select.options].some((o) => o.value === wanted.get(id))) select.value = wanted.get(id);
  }
  $('filters').addEventListener('change', () => { state.page = 1; load(false); });
  $('filters').addEventListener('submit', (e) => e.preventDefault());
  $('prev').addEventListener('click', () => { state.page -= 1; load(true); });
  $('next').addEventListener('click', () => { state.page += 1; load(true); });
  load(false);
})();
