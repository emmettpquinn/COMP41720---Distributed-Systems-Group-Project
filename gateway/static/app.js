const $ = (selector) => document.querySelector(selector);
const list = $('#event-list');
const panel = $('#detail');
const status = $('#list-status');
let capabilities = {holds: false, search: false, checkout: false};
let listing = false;
let listGeneration = 0;
let activeQuery = "";
let current = null;
let selectedSeat = null;
let busy = false;
let generation = 0;
let rejected = new Set();
let reserved = new Set();
const reservations = new Map();
const BOOKING_SESSION_KEY = 'stagedoor-booking-v1';
const POLL_INTERVAL_MS = 2000;
const MAX_STATUS_READS = 45;
const BOOKING_STATUSES = new Set(['held', 'payment_pending', 'confirmed', 'released', 'refund_required', 'refund_pending', 'refunded']);
const TERMINAL_STATUSES = new Set(['confirmed', 'released', 'refund_required', 'refund_pending', 'refunded']);
let booking = null;
let pollTimer;
let pollGeneration = 0;
let postingCheckout = false;
let readingStatus = false;
let readingTicket = false;
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
  const unfinished = capabilities.checkout && booking && !TERMINAL_STATUSES.has(booking.status);
  if (hold) hold.disabled = busy || listing || !selectedSeat || !capabilities.holds || current.status === 'cancelled' || Boolean(unfinished);
  const checkout = $('#checkout');
  if (checkout) checkout.disabled = busy || postingCheckout || readingStatus || booking.phase !== 'ready' || booking.status !== 'held' || !hasPrice(booking);
  const check = $('#check-booking');
  if (check) check.disabled = busy || postingCheckout || readingStatus;
  const ticket = $('#check-ticket');
  if (ticket) ticket.disabled = readingTicket;
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
    if (current?.event_id !== id) {
      rejected = new Set();
      reserved = reservations.get(id) || new Set();
      reservations.set(id, reserved);
    }
    current = event; selectedSeat = null;
    renderDetail();
    document.querySelectorAll('.event-card').forEach(card => card.setAttribute('aria-pressed', String(card.dataset.event === id)));
  } catch {
    if (request === generation) {
      panel.replaceChildren(element('p', 'Event details could not be loaded. Select the event to retry.', 'detail-body message error'));
    }
  } finally { if (request === generation) panel.removeAttribute('aria-busy'); }
}

function seatButton(seat, event, available) {
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
  return button;
}

function seatPosition(seat) {
  const match = /^([a-z]{1,3})([1-9]\d{0,2})$/i.exec(seat.label);
  if (!match || Number(match[2]) > 100) return null;
  return {row: match[1].toUpperCase(), column: Number(match[2])};
}

function renderSeatMap(event) {
  const map = element('div', undefined, 'seat-map');
  map.tabIndex = 0;
  map.setAttribute('aria-label', 'Choose a seat. Scroll horizontally for wider rows.');
  const grid = element('div', undefined, 'seat-grid seat-layout');
  const available = event.availability && new Set(event.availability.available_seat_ids);
  const sections = new Map();
  for (const seat of event.seats) {
    if (!sections.has(seat.section)) sections.set(seat.section, []);
    sections.get(seat.section).push(seat);
  }
  const natural = new Intl.Collator('en', {numeric: true});
  for (const [section, seats] of sections) {
    const group = element('section', undefined, 'seat-section');
    group.setAttribute('aria-label', `Section ${section} seating`);
    group.append(element('h3', `Section ${section}`));
    const rows = new Map();
    let columns = 0;
    let positioned = true;
    for (const seat of seats) {
      const position = seatPosition(seat);
      if (!position) { positioned = false; break; }
      if (!rows.has(position.row)) rows.set(position.row, new Map());
      const row = rows.get(position.row);
      if (row.has(position.column)) { positioned = false; break; }
      row.set(position.column, seat);
      columns = Math.max(columns, position.column);
    }
    // Legacy/custom labels remain selectable. Bound sparse layouts instead of
    // allocating enormous grids from a label supplied through the event API.
    if (!positioned || rows.size * columns > 2000) {
      const fallback = element('div', undefined, 'seat-grid');
      for (const seat of [...seats].sort((a, b) => natural.compare(a.label, b.label))) fallback.append(seatButton(seat, event, available));
      group.append(fallback);
    } else {
      const header = element('div', undefined, 'seat-row seat-column-headings');
      header.setAttribute('aria-hidden', 'true');
      header.append(element('span', '', 'seat-row-label'));
      for (let column = 1; column <= columns; column++) header.append(element('span', String(column), 'seat-column-number'));
      group.append(header);
      const rowNames = [...rows.keys()].sort((a, b) => a.length - b.length || natural.compare(a, b));
      for (const name of rowNames) {
        const row = element('div', undefined, 'seat-row');
        row.dataset.row = name; row.setAttribute('role', 'group'); row.setAttribute('aria-label', `Row ${name}`);
        row.append(element('span', name, 'seat-row-label'));
        for (let column = 1; column <= columns; column++) {
          const seat = rows.get(name).get(column);
          if (seat) {
            const button = seatButton(seat, event, available);
            button.dataset.row = name; button.dataset.column = String(column);
            row.append(button);
          } else {
            const gap = element('span', undefined, 'seat-gap');
            gap.setAttribute('aria-hidden', 'true'); row.append(gap);
          }
        }
        group.append(row);
      }
    }
    grid.append(group);
  }
  map.append(grid);
  return map;
}

function renderDetail(message = '', messageClass = '') {
  panel.removeAttribute('aria-busy');
  const event = current;
  const heading = element('div', undefined, 'detail-heading');
  heading.append(element('p', 'TAKE YOUR PLACE', 'eyebrow'), element('h2', event.title), element('p', `${event.venue.name} · ${dateParts(event).full}`));
  const body = element('div', undefined, 'detail-body');
  body.append(element('p', event.description, 'description'), element('div', 'STAGE', 'stage'), renderSeatMap(event));
  body.append(element('p', 'Scroll sideways for wider rows.', 'seat-scroll-hint'));
  const legend = element('div', undefined, 'seat-legend');
  legend.setAttribute('aria-label', 'Seat colours');
  legend.append(element('span', event.availability ? 'Available' : 'Availability unknown', 'legend-available'), element('span', 'Selected', 'legend-selected'), element('span', 'Unavailable', 'legend-unavailable'));
  body.append(legend);
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
  body.append(element('p', capabilities.checkout ? 'A seat hold is not a purchase. Continue in Your booking to check out.' : 'Checkout is not available yet. A reservation does not issue a ticket.', 'muted'));
  panel.replaceChildren(heading, body);
  setBusy(busy);
}

async function reserve() {
  if (busy || listing || !selectedSeat || !capabilities.holds || (capabilities.checkout && booking && !TERMINAL_STATUSES.has(booking.status))) return;
  const seat = selectedSeat;
  const eventId = current.event_id;
  // Ignore any older detail fetch that was in flight when the request started.
  ++generation;
  setBusy(true);
  $('#reservation-status').textContent = 'Checking your seat with Booking…';
  try {
    const result = await api('/api/booking/holds', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({event_id: eventId, seat_id: seat, customer_id: customerId})});
    if (result.status !== 'held' || !validId(result.booking_ref)) throw {status: 502};
    if (capabilities.checkout && (!validId(result.booking_id || result.booking_ref) || (result.booking_id && result.booking_id !== result.booking_ref))) throw {status: 502};
    reserved.add(seat); selectedSeat = null;
    renderDetail(`Seat ${seat} is reserved. Reference: ${result.booking_ref}.${capabilities.checkout ? ' Continue below to check out.' : ' Checkout is unavailable.'}`, 'success');
    if (capabilities.checkout) {
      booking = {id: result.booking_id || result.booking_ref, reference: result.booking_ref, eventId, seatId: seat, title: current.title, status: 'held', phase: 'checking'};
      rememberBooking();
      beginStatusCheck();
    }
  } catch (error) {
    selectedSeat = null;
    if (error.status === 409) {
      rejected.add(seat);
      let refreshed = false;
      try { current = await api(`/api/catalogue/events/${encodeURIComponent(eventId)}`); refreshed = true; } catch {}
      if (error.data?.error === 'event_cancelled') current = {...current, status: 'cancelled'};
      renderDetail(error.data?.error === 'event_cancelled' ? 'This event has been cancelled. Reservations are closed.' : refreshed ? 'That seat was taken. We requested the latest projection; your refused choice is marked. Choose another seat.' : 'That seat was taken. The map could not be refreshed. Please try Refresh.', 'error');
    } else if (error.status === 404) {
      renderDetail('Booking has not loaded this event or seat yet. Try another event or check again later.', 'error');
    } else {
      renderDetail('Booking could not confirm the result. The reservation outcome may be unknown. Choose the same seat to retry with this session, or check again later.', 'error');
    }
  } finally { setBusy(false); }
}

function validId(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 64 && value.trim() === value;
}

function hasPrice(value) {
  return Number.isSafeInteger(value.amount) && value.amount > 0 && value.currency === 'EUR';
}

function bookingPath(suffix = '') {
  const path = `/api/booking/bookings/${encodeURIComponent(booking.id)}${suffix}`;
  return suffix ? path : `${path}?customer_id=${encodeURIComponent(customerId)}`;
}

function rememberBooking() {
  // Restore only identity. Price and outcome must be read from Booking again.
  try {
    sessionStorage.setItem(BOOKING_SESSION_KEY, JSON.stringify({id: booking.id, reference: booking.reference, eventId: booking.eventId, seatId: booking.seatId, title: booking.title}));
  } catch {}
}

function restoreBooking() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(BOOKING_SESSION_KEY));
    if (!saved || !['id', 'reference', 'eventId', 'seatId'].every(key => validId(saved[key]))) return;
    booking = {id: saved.id, reference: saved.reference, eventId: saved.eventId, seatId: saved.seatId, title: typeof saved.title === 'string' ? saved.title.slice(0, 500) : 'Your reservation', status: 'unknown', phase: 'checking'};
    reservations.set(saved.eventId, new Set([saved.seatId]));
    beginStatusCheck();
  } catch {}
}

function renderCheckout() {
  const section = $('#checkout-panel');
  section.hidden = !capabilities.checkout || !booking;
  if (section.hidden) return;
  section.replaceChildren(element('p', 'YOUR BOOKING', 'eyebrow'), element('h2', booking.title));
  section.append(element('p', `Seat ${booking.seatId} · Reference: ${booking.reference}`, 'booking-reference'));
  const price = hasPrice(booking) ? new Intl.NumberFormat('en-IE', {style: 'currency', currency: booking.currency}).format(booking.amount / 100) : 'Total unavailable until the reservation is checked';
  section.append(element('p', price, 'booking-total'));
  const message = element('p', booking.message || 'Checking your reservation…', `message${booking.status === 'confirmed' ? ' success' : ''}`);
  message.id = 'checkout-status'; message.setAttribute('role', 'status'); message.setAttribute('aria-live', 'polite');
  section.append(message);
  if (booking.status === 'held') {
    const checkout = element('button', 'Pay and complete booking →', 'primary');
    checkout.id = 'checkout'; checkout.type = 'button'; checkout.onclick = checkoutBooking;
    section.append(checkout);
  }
  const check = element('button', 'Check booking status', 'text-button');
  check.id = 'check-booking'; check.type = 'button'; check.onclick = () => beginStatusCheck();
  section.append(check);
  if (booking.status === 'confirmed') {
    const ticket = element('button', 'Check ticket', 'text-button');
    ticket.id = 'check-ticket'; ticket.type = 'button'; ticket.onclick = checkTicket;
    section.append(ticket);
  }
  if (booking.ticketMessage) {
    const ticketStatus = element('p', booking.ticketMessage, 'message');
    ticketStatus.id = 'ticket-status'; ticketStatus.setAttribute('role', 'status');
    section.append(ticketStatus);
  }
  updateControls();
}

function beginStatusCheck(message = 'Checking your reservation…') {
  if (!booking || postingCheckout) return;
  clearTimeout(pollTimer);
  const token = ++pollGeneration;
  booking.phase = 'checking'; booking.message = message;
  readingStatus = true;
  renderCheckout();
  pollBooking(token, 0, 0);
}

function stopPolling(message) {
  clearTimeout(pollTimer);
  booking.phase = 'stopped'; booking.message = message;
  readingStatus = false;
  renderCheckout();
}

async function pollBooking(token, reads, errors) {
  const id = booking.id;
  let keepPolling = false;
  let nextErrors = 0;
  try {
    const result = await api(bookingPath());
    if (token !== pollGeneration || booking.id !== id) return;
    if (result.booking_id !== id || result.event_id !== booking.eventId || result.seat_id !== booking.seatId || !BOOKING_STATUSES.has(result.booking_status)) {
      stopPolling('The booking response could not be understood. Check booking status again before taking further action.');
      return;
    }
    booking.status = result.booking_status;
    if (booking.status !== 'confirmed') delete booking.ticketMessage;
    booking.amount = result.price_minor; booking.currency = result.currency;
    if (booking.status === 'payment_pending') {
      booking.phase = 'waiting'; booking.message = 'Payment is processing. Please wait while we check your booking.';
      keepPolling = true;
    } else if (booking.status === 'held') {
      booking.phase = 'ready'; booking.message = hasPrice(booking) ? 'Your seat is held. You can now complete your booking.' : 'Your seat is held, but its total could not be read. Check booking status before paying.';
    } else if (booking.status === 'confirmed') {
      booking.phase = 'complete'; booking.message = 'Booking confirmed. You can check below whether your ticket is ready.';
    } else {
      booking.phase = 'complete';
      const messages = {
        released: 'Your seat was released and this booking was not confirmed. If a late payment succeeds, it will be refunded. Check booking status for updates.',
        refund_required: 'Your booking was not confirmed. A refund has been requested. You can check its progress here.',
        refund_pending: 'Your refund has not completed yet. Please check again later.',
        refunded: 'Your payment has been refunded.',
      };
      booking.message = messages[booking.status];
      // A released booking can still receive a late charge and move to refund.
      keepPolling = booking.status !== 'refunded';
      reservations.get(booking.eventId)?.delete(booking.seatId);
      if (current?.event_id === booking.eventId) renderDetail();
    }
  } catch (error) {
    if (token !== pollGeneration || booking.id !== id) return;
    nextErrors = errors + 1;
    if (error.status === 404 || nextErrors >= 3) {
      stopPolling(error.status === 404 ? 'Booking could not find this reservation. Check again before starting another checkout.' : 'Booking status is temporarily unavailable. The payment outcome may be unknown. Use Check booking status to continue.');
      return;
    }
    booking.phase = 'checking'; booking.message = 'Booking status is temporarily unavailable. Checking again; no new payment request has been sent.';
    keepPolling = true;
  }
  if (token !== pollGeneration) return;
  readingStatus = false;
  renderCheckout();
  if (keepPolling) {
    if (reads + 1 >= MAX_STATUS_READS) {
      stopPolling('We are still waiting for a final booking result. Use Check booking status to continue. Stopping these checks does not cancel your payment.');
    } else {
      pollTimer = setTimeout(() => {
        readingStatus = true;
        updateControls();
        pollBooking(token, reads + 1, nextErrors);
      }, POLL_INTERVAL_MS);
    }
  }
}

function paymentUnavailable(error) {
  const detail = error.data?.detail;
  return error.status === 503 && [error.data?.error, error.data?.code, detail, detail?.error, detail?.code].includes('payment_service_unavailable');
}

async function checkoutBooking() {
  if (!capabilities.checkout || !booking || busy || postingCheckout || readingStatus || booking.phase !== 'ready' || booking.status !== 'held' || !hasPrice(booking)) return;
  clearTimeout(pollTimer);
  ++pollGeneration;
  postingCheckout = true;
  booking.phase = 'submitting'; booking.message = 'Starting checkout…';
  renderCheckout();
  let statusMessage = 'Checkout accepted. Checking your booking…';
  try {
    // Booking owns the saved price and Payment call. Never charge from the browser.
    await api(bookingPath('/checkout'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({customer_id: customerId})});
  } catch (error) {
    if (paymentUnavailable(error)) {
      booking.phase = 'ready'; booking.message = 'Payments are unavailable, try again. Your seat is still held.';
      postingCheckout = false;
      renderCheckout();
      return;
    }
    statusMessage = 'The checkout result was not received. Checking booking status before another payment attempt.';
  }
  postingCheckout = false;
  // A 202, timeout or generic 503 is not proof of payment or a failed charge.
  beginStatusCheck(statusMessage);
}

async function checkTicket() {
  if (!booking || booking.status !== 'confirmed' || readingTicket) return;
  const id = booking.id;
  readingTicket = true;
  updateControls();
  try {
    const ticket = await api(`/api/ticketing/tickets/${encodeURIComponent(id)}`);
    if (booking.id !== id || booking.status !== 'confirmed') return;
    if (ticket.booking_id !== id || ticket.valid !== true || ticket.status !== 'valid' || typeof ticket.ticket_code !== 'string' || !ticket.ticket_code.trim()) throw {status: 502};
    booking.ticketMessage = `Your ticket code: ${ticket.ticket_code}`;
  } catch (error) {
    if (booking.id !== id || booking.status !== 'confirmed') return;
    booking.ticketMessage = error.status === 404 ? 'Your ticket is not ready yet. Check again shortly.' : 'A valid ticket could not be confirmed. Check again shortly.';
  } finally {
    readingTicket = false;
    renderCheckout();
  }
}

window.addEventListener('pagehide', () => { clearTimeout(pollTimer); ++pollGeneration; });

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
  if (capabilities.checkout) restoreBooking();
  await loadEvents();
})();
