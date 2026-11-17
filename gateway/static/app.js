const $ = (selector) => document.querySelector(selector);
const list = $('#event-list');
const panel = $('#detail');
const status = $('#list-status');
let capabilities = {holds: false, search: false};
let listing = false;
let listGeneration = 0;
let activeQuery = "";
let current = null;
let selectedSeat = null;
let busy = false;
let generation = 0;
let rejected = new Set();
let reserved = new Set();
let customerId;
try {
  customerId = sessionStorage.getItem('stagedoor-customer') || `demo-${crypto.randomUUID()}`;
  sessionStorage.setItem('stagedoor-customer', customerId);
} catch { customerId = `demo-${crypto.randomUUID()}`; }

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(6000)});
  const data = await response.json();
  if (!response.ok) throw {status: response.status, data};
  return data;
}

function dateParts(event) {
  const date = new Date(event.starts_at);
  return {
    day: date.toLocaleString('en-IE', {day: '2-digit', timeZone: event.timezone}),
    month: date.toLocaleString('en-IE', {month: 'short', timeZone: event.timezone}),
    full: date.toLocaleString('en-IE', {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: event.timezone})
  };
}

function setBusy(value) {
  busy = value;
  updateControls();
}

function updateControls() {
  document.querySelectorAll('.event-card, #refresh, #search-submit, #browse-all, #search-query').forEach(node => {
    node.disabled = busy || listing;
  });
  document.querySelectorAll('.seat').forEach(node => {
    node.disabled = busy || listing || node.dataset.unavailable === 'true';
  });
  const hold = $('#hold');
  if (hold) hold.disabled = busy || listing || !selectedSeat || !capabilities.holds || current.status === 'cancelled';
}

function renderCards(events) {
  list.replaceChildren();
  for (const event of events) {
    const date = dateParts(event);
    const card = element('button', undefined, 'event-card');
    card.type = 'button'; card.dataset.event = event.event_id;
    card.setAttribute('aria-pressed', String(current?.event_id === event.event_id));
    const calendar = element('span', undefined, 'event-date');
    calendar.append(element('strong', date.day), element('span', date.month.toUpperCase()));
    const text = element('span');
    text.append(element('h3', event.title), element('p', `${event.venue.name} · ${event.venue.city}`), element('p', date.full), element('span', event.status === 'cancelled' ? 'Cancelled' : 'Live performance', 'category'));
    card.append(calendar, text, element('span', '↗', 'arrow'));
    card.onclick = () => selectEvent(event.event_id);
    list.append(card);
  }
}

function showSearchMetadata(result) {
  const note = $('#search-status');
  const simulated = result.embedding_provider !== 'ollama';
  note.textContent = `${result.label}${simulated ? ' · Simulated model' : ''}`;
  note.dataset.mode = result.mode;
  note.dataset.provider = result.embedding_provider;
  note.dataset.fallbackReason = result.fallback_reason || '';
  const filter = result.filter;
  const options = {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: filter.timezone};
  const from = new Date(filter.from).toLocaleString('en-IE', options);
  let text = `From ${from}`;
  if (filter.until) text += ` until ${new Date(filter.until).toLocaleString('en-IE', options)}`;
  if (filter.after_local_minute !== null) {
    const hour = Math.floor(filter.after_local_minute / 60).toString().padStart(2, '0');
    const minute = (filter.after_local_minute % 60).toString().padStart(2, '0');
    text += ` · after ${hour}:${minute}`;
  }
  if (filter.evening_from) text += ` · evenings from ${filter.evening_from}`;
  $('#search-filter').textContent = `${text} (${filter.timezone}). Search results can lag event changes.`;
}

async function loadEvents(query = '') {
  if (busy) return;
  const request = ++listGeneration;
  ++generation;
  panel.removeAttribute('aria-busy');
  listing = true;
  updateControls();
  status.textContent = query ? 'Searching events…' : 'Loading events…';
  $('#search-status').textContent = '';
  $('#search-filter').textContent = '';
  try {
    const tasks = [api('/api/catalogue/events')];
    if (query) tasks.push(api(`/api/discovery/search?q=${encodeURIComponent(query)}&limit=50`));
    const [catalogue, result] = await Promise.all(tasks);
    if (request !== listGeneration) return;
    let events = catalogue;
    if (query) {
      if (!Array.isArray(result.results) || !['semantic', 'keyword_fallback'].includes(result.mode) || !['ollama', 'simulated', 'simulated_http'].includes(result.embedding_provider)) throw {status: 502};
      const byId = new Map(catalogue.map(event => [event.event_id, event]));
      // Discovery provides ordering; current Catalogue metadata provides venue/status.
      events = result.results.map(hit => byId.get(hit.event_id)).filter(event => event && event.status !== 'cancelled');
      showSearchMetadata(result);
    }
    renderCards(events);
    status.textContent = query ? `${events.length} matching events currently available` : events.length ? `${events.length} events · Times shown in each venue's timezone` : 'No events are listed yet.';
    if (current) await selectEvent(current.event_id);
  } catch (error) {
    if (request !== listGeneration) return;
    list.replaceChildren();
    const detail = error.data?.detail;
    status.textContent = query && error.status === 422 && typeof detail === 'string' ? detail : query ? 'Search could not be loaded. Try again or browse all events.' : 'Events could not be loaded. Please try Refresh.';
  } finally {
    if (request === listGeneration) { listing = false; updateControls(); }
  }
}

async function selectEvent(id) {
  if (busy) return;
  const request = ++generation;
  panel.setAttribute('aria-busy', 'true');
  try {
    const event = await api(`/api/catalogue/events/${encodeURIComponent(id)}`);
    if (request !== generation) return;
    if (current?.event_id !== id) { rejected = new Set(); reserved = new Set(); }
    current = event; selectedSeat = null;
    renderDetail();
    document.querySelectorAll('.event-card').forEach(card => card.setAttribute('aria-pressed', String(card.dataset.event === id)));
  } catch {
    if (request === generation) {
      panel.replaceChildren(element('p', 'Event details could not be loaded. Select the event to retry.', 'detail-body message error'));
    }
  } finally { if (request === generation) panel.removeAttribute('aria-busy'); }
}

function renderDetail(message = '', messageClass = '') {
  panel.removeAttribute('aria-busy');
  const event = current;
  const heading = element('div', undefined, 'detail-heading');
  heading.append(element('p', 'TAKE YOUR PLACE', 'eyebrow'), element('h2', event.title), element('p', `${event.venue.name} · ${dateParts(event).full}`));
  const body = element('div', undefined, 'detail-body');
  body.append(element('p', event.description, 'description'), element('div', 'STAGE', 'stage'));
  const grid = element('div', undefined, 'seat-grid');
  grid.setAttribute('aria-label', 'Choose a seat');
  const available = event.availability && new Set(event.availability.available_seat_ids);
  for (const seat of event.seats) {
    const refused = rejected.has(seat.seat_id);
    const heldHere = reserved.has(seat.seat_id);
    const unavailable = event.status === 'cancelled' || refused || heldHere || (available && !available.has(seat.seat_id));
    const button = element('button', seat.label, `seat${refused ? ' refused' : heldHere ? ' reserved' : ''}`);
    button.type = 'button'; button.dataset.seat = seat.seat_id;
    button.dataset.unavailable = String(Boolean(unavailable)); button.disabled = Boolean(unavailable);
    button.setAttribute('aria-label', `Seat ${seat.label}, section ${seat.section}${heldHere ? ', reserved in this session' : refused ? ', last reservation refused' : unavailable ? ', unavailable' : available ? ', projected available' : ', availability unknown'}`);
    button.setAttribute('aria-pressed', String(selectedSeat === seat.seat_id));
    button.onclick = () => {
      selectedSeat = seat.seat_id;
      document.querySelectorAll('.seat').forEach(node => node.setAttribute('aria-pressed', String(node === button)));
      setBusy(false);
    };
    grid.append(button);
  }
  body.append(grid);
  let freshness = 'Availability has not been received. Booking will check your choice.';
  if (event.availability) {
    const time = new Date(event.availability.occurred_at).toLocaleTimeString('en-IE', {timeZone: event.timezone});
    freshness = `${event.availability.seats_remaining} projected available · Last inventory change ${time}. Availability can change before reservation.`;
  }
  if (event.status === 'cancelled') freshness = 'This event has been cancelled. Reservations are closed.';
  body.append(element('p', freshness, 'freshness'));
  const hold = element('button', 'Reserve selected seat →', 'primary');
  hold.id = 'hold'; hold.type = 'button'; hold.onclick = reserve;
  body.append(hold);
  const result = element('p', message || (!capabilities.holds ? 'Seat reservations are unavailable in this stack.' : 'Choose a seat to request a reservation.'), `message ${messageClass}`);
  result.id = 'reservation-status'; result.setAttribute('role', 'status'); result.setAttribute('aria-live', 'polite');
  body.append(result);
  body.append(element('p', 'Checkout is not available yet. A reservation does not issue a ticket.', 'muted'));
  panel.replaceChildren(heading, body);
  setBusy(busy);
}

async function reserve() {
  if (busy || !selectedSeat || !capabilities.holds) return;
  const seat = selectedSeat;
  const eventId = current.event_id;
  // Ignore any older detail fetch that was in flight when the request started.
  ++generation;
  setBusy(true);
  $('#reservation-status').textContent = 'Checking your seat with Booking…';
  try {
    const result = await api('/api/booking/holds', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({event_id: eventId, seat_id: seat, customer_id: customerId})});
    if (result.status !== 'held' || !result.booking_ref) throw {status: 502};
    reserved.add(seat); selectedSeat = null;
    renderDetail(`Seat ${seat} is reserved. Reference: ${result.booking_ref}. Checkout is unavailable.`, 'success');
  } catch (error) {
    selectedSeat = null;
    if (error.status === 409) {
      rejected.add(seat);
      let refreshed = false;
      try { current = await api(`/api/catalogue/events/${encodeURIComponent(eventId)}`); refreshed = true; } catch {}
      renderDetail(refreshed ? 'That seat was taken. We requested the latest projection; your refused choice is marked. Choose another seat.' : 'That seat was taken. The map could not be refreshed. Please try Refresh.', 'error');
    } else if (error.status === 404) {
      renderDetail('Booking has not loaded this event or seat yet. Try another event or check again later.', 'error');
    } else {
      renderDetail('Booking could not confirm the result. The reservation outcome may be unknown. Choose the same seat to retry with this session, or check again later.', 'error');
    }
  } finally { setBusy(false); }
}

$('#refresh').onclick = () => loadEvents(activeQuery);
$('#search-form').onsubmit = (event) => {
  event.preventDefault();
  activeQuery = $('#search-query').value.trim();
  loadEvents(activeQuery);
};
$('#browse-all').onclick = () => {
  activeQuery = '';
  $('#search-query').value = '';
  loadEvents();
};
(async () => {
  try { capabilities = await api('/api/capabilities'); } catch {}
  $('#search-form').hidden = !capabilities.search;
  await loadEvents();
})();
