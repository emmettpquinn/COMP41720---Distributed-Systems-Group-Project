// DOM integration checks; these do not claim visual browser verification.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const html = await readFile(new URL('../static/index.html', import.meta.url), 'utf8');
const source = await readFile(new URL('../static/app.js', import.meta.url), 'utf8');
const event = (id, status = 'scheduled') => ({event_id:id, event_version:1, title:`Current ${id}`, status,
  starts_at:'2026-11-27T19:30:00Z', timezone:'Europe/Dublin', venue:{name:'Demo Venue',city:'Dublin'}});
const catalogue = [event('first'), event('second'), event('cancelled','cancelled')];
const searchResult = (overrides = {}) => ({mode:'semantic',label:'Showing semantic results',fallback_reason:null,
  embedding_provider:'simulated_http',filter:{from:'2026-11-17T12:00:00Z',until:'2026-11-28T00:00:00Z',
    after_local_minute:19*60,evening_from:null,timezone:'Europe/Dublin'},
  results:[{event_id:'second'},{event_id:'missing'},{event_id:'cancelled'},{event_id:'first'}], ...overrides});

async function until(predicate) {
  for (let i=0;i<300;i++) { if (predicate()) return; await delay(2); }
  assert.fail('DOM condition did not complete');
}

const bookingId = '11111111-1111-4111-8111-111111111111';
const bookingResult = (status='held', overrides={}) => ({booking_id:bookingId,booking_ref:bookingId,
  event_id:'first',seat_id:'A1',booking_status:status,price_minor:4500,currency:'EUR',held_until:'2026-11-19T16:10:00Z',payment_id:null,...overrides});

async function setup(t, {search=async()=>({status:200,body:searchResult()}), enabled=true,
  checkout=false, hold=async()=>({status:201,body:{status:'held',booking_ref:checkout ? bookingId : 'synthetic-reference',...(checkout ? {booking_id:bookingId} : {})}}),
  status=async()=>({status:200,body:bookingResult()}),
  pay=async()=>({status:202,body:{booking_id:bookingId,booking_status:'payment_pending'}}),
  ticket=async()=>({status:404,body:{detail:'No ticket'}}), savedBooking=null, pollDelay=2,
  detail=()=>({seats:[{seat_id:'A1',section:'A',label:'A1'}],availability:null})} = {}) {
  const dom = new JSDOM(html,{url:'http://localhost',runScripts:'outside-only'});
  t.after(()=>{dom.window.dispatchEvent(new dom.window.Event('pagehide'));dom.window.close();});
  const window=dom.window;
  const nativeTimeout=window.setTimeout.bind(window);
  window.setTimeout=(callback,ms,...args)=>nativeTimeout(callback,ms===2000 ? pollDelay : ms,...args);
  window.AbortSignal=globalThis.AbortSignal;
  if(savedBooking) window.sessionStorage.setItem('stagedoor-booking-v1',JSON.stringify(savedBooking));
  if (!window.crypto.randomUUID) Object.defineProperty(window.crypto,'randomUUID',{value:randomUUID});
  const requests=[];
  window.fetch=async(path,options={})=>{
    requests.push({path,options});
    let response;
    if(path==='/api/capabilities') response={status:200,body:{holds:true,search:enabled,checkout}};
    else if(path==='/api/catalogue/events') response={status:200,body:catalogue};
    else if(path.startsWith('/api/catalogue/events/')) {
      const id=decodeURIComponent(path.split('/').at(-1));
      response={status:200,body:{...catalogue.find(item=>item.event_id===id),description:'Fictional performance',
        ...detail(id)}};
    } else if(path.startsWith('/api/discovery/search?')) response=await search(new URL(path,'http://localhost').searchParams.get('q'));
    else if(path==='/api/booking/holds') response=await hold(options);
    else if(/^\/api\/booking\/bookings\/[^/]+\/checkout$/.test(path)) response=await pay(options);
    else if(/^\/api\/booking\/bookings\/[^/?]+\?/.test(path)) {
      assert.equal(new URL(path,'http://localhost').searchParams.get('customer_id'),window.sessionStorage.getItem('stagedoor-customer'));
      response=await status(options);
    }
    else if(path.startsWith('/api/ticketing/tickets/')) response=await ticket(options);
    else assert.fail(`Unexpected request ${path}`);
    return {ok:response.status<400,status:response.status,json:async()=>response.body};
  };
  await window.eval(source);
  const $=selector=>window.document.querySelector(selector);
  const submit=query=>{
    $('#search-query').value=query;
    $('#search-form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
  };
  return {$,submit,requests,window};
}

test('search preserves Discovery order and uses live Catalogue metadata',async t=>{
  const {$,submit,window}=await setup(t);
  assert.equal($('#search-form').hidden,false);
  submit('music Friday after 7pm');
  await until(()=>!$('#search-submit').disabled);
  assert.deepEqual([...window.document.querySelectorAll('.event-card')].map(node=>node.dataset.event),['second','first']);
  assert.match($('#event-list').textContent,/Current second/);
  assert.match($('#search-status').textContent,/Showing semantic results · Simulated model/);
  assert.match($('#search-filter').textContent,/after 19:00/);
  assert.match($('#search-filter').textContent,/Europe\/Dublin/);
});

test('keyword fallback is labelled and Browse all clears search state',async t=>{
  const {$,submit}=await setup(t,{search:async()=>({status:200,body:searchResult({mode:'keyword_fallback',label:'Showing keyword results',fallback_reason:'circuit_open'})})});
  submit('music');
  await until(()=>!$('#search-submit').disabled);
  assert.match($('#search-status').textContent,/Showing keyword results · Simulated model/);
  assert.equal($('#search-status').dataset.fallbackReason,'circuit_open');
  $('#browse-all').click();
  await until(()=>!$('#refresh').disabled);
  assert.equal($('#search-query').value,'');
  assert.equal($('#search-status').textContent,'');
  assert.match($('#list-status').textContent,/3 events/);
});

test('unsupported date errors are shown and controls recover',async t=>{
  const {$,submit}=await setup(t,{search:async()=>({status:422,body:{detail:'Supported dates: today, tonight, tomorrow, a weekday, this weekend.'}})});
  submit('music next weekend');
  await until(()=>!$('#search-submit').disabled);
  assert.match($('#list-status').textContent,/Supported dates/);
  assert.equal($('#event-list').children.length,0);
  $('#browse-all').click();
  await until(()=>!$('#refresh').disabled);
  assert.equal($('#event-list').children.length,3);
});

test('hold controls wait for search and real hold response remains visible',async t=>{
  let complete;
  const pending=new Promise(resolve=>{complete=resolve;});
  const {$,submit,requests}=await setup(t,{search:()=>pending});
  $('#event-list .event-card').click();
  await until(()=>Boolean($('.seat')));
  $('.seat').click();
  assert.equal($('#hold').disabled,false);
  submit('music');
  assert.equal($('#hold').disabled,true);
  $('#hold').click();
  assert.equal(requests.filter(request=>request.path==='/api/booking/holds').length,0);
  complete({status:200,body:searchResult()});
  await until(()=>!$('#search-submit').disabled);
  $('.seat').click();
  $('#hold').click();
  await until(()=>$('#reservation-status').textContent.includes('synthetic-reference'));
  assert.match($('#reservation-status').textContent,/Checkout is unavailable/);
  assert.match($('.seat').getAttribute('aria-label'),/reserved in this session/);
  const body=JSON.parse(requests.find(request=>request.path==='/api/booking/holds').options.body);
  assert.equal(body.event_id,'first');
  assert.match(body.customer_id,/^demo-/);
});

test('disabled search stays hidden',async t=>{
  const {$,requests}=await setup(t,{enabled:false});
  assert.equal($('#search-form').hidden,true);
  assert.equal($('#event-list').children.length,3);
  assert.equal(requests.some(request=>request.path.startsWith('/api/discovery/')),false);
  assert.equal($('#checkout-panel').hidden,true);
});

const theatreSeats = () => [...'ABCDE'].flatMap(row=>Array.from({length:10},(_,i)=>({seat_id:`${row}${i+1}`,section:'Stalls',label:`${row}${i+1}`})));

test('50 seats keep five labelled rows and natural column order even when the API sorts IDs lexically',async t=>{
  const seats=theatreSeats().sort((a,b)=>a.seat_id.localeCompare(b.seat_id));
  const page=await setup(t,{detail:()=>({seats,availability:null})});
  page.$('#event-list .event-card').click();
  await until(()=>Boolean(page.$('.seat')));
  const rows=[...page.window.document.querySelectorAll('.seat-row[data-row]')];
  assert.deepEqual(rows.map(row=>row.dataset.row),['A','B','C','D','E']);
  assert.deepEqual([...rows[0].querySelectorAll('.seat')].map(node=>node.textContent),Array.from({length:10},(_,i)=>`A${i+1}`));
  assert.equal(page.window.document.querySelectorAll('.seat').length,50);
  assert.equal(page.window.document.querySelectorAll('.seat-column-number').length,10);
  assert.equal(page.$('.seat-map').tabIndex,0);
  page.$('[data-seat="E10"]').click();page.$('#hold').click();
  await until(()=>page.$('#reservation-status').textContent.includes('synthetic-reference'));
  assert.equal(JSON.parse(page.requests.find(r=>r.path==='/api/booking/holds').options.body).seat_id,'E10');
});

test('missing numbered seats leave gaps and do not shift the remaining seats',async t=>{
  const seats=[{seat_id:'A3',section:'Stalls',label:'A3'},{seat_id:'A1',section:'Stalls',label:'A1'}];
  const page=await setup(t,{detail:()=>({seats,availability:null})});
  page.$('#event-list .event-card').click();
  await until(()=>Boolean(page.$('.seat')));
  const row=page.$('.seat-row[data-row="A"]');
  assert.deepEqual([...row.children].map(node=>node.textContent),['A','A1','','A3']);
  assert.equal(row.querySelectorAll('.seat-gap').length,1);
  assert.equal(page.$('[data-seat="A2"]'),null);
});

test('sections with identical seat labels retain independent seat IDs',async t=>{
  const seats=[{seat_id:'stalls-A1',section:'Stalls',label:'A1'},{seat_id:'balcony-A1',section:'Balcony',label:'A1'}];
  const page=await setup(t,{detail:()=>({seats,availability:null})});
  page.$('#event-list .event-card').click();
  await until(()=>Boolean(page.$('.seat')));
  assert.equal(page.window.document.querySelectorAll('.seat-section').length,2);
  page.$('[data-seat="balcony-A1"]').click();page.$('#hold').click();
  await until(()=>page.requests.some(r=>r.path==='/api/booking/holds'));
  assert.equal(JSON.parse(page.requests.find(r=>r.path==='/api/booking/holds').options.body).seat_id,'balcony-A1');
});

test('custom labels and duplicate positions preserve every seat without building a huge sparse map',async t=>{
  for(const labels of [['Accessible','A999999999999'],['A1','a1']]) {
    const seats=labels.map((label,i)=>({seat_id:`seat-${i}`,section:'Stalls',label}));
    const page=await setup(t,{detail:()=>({seats,availability:null})});
    page.$('#event-list .event-card').click();
    await until(()=>Boolean(page.$('.seat')));
    assert.equal(page.window.document.querySelectorAll('.seat').length,2);
    assert.equal(page.window.document.querySelectorAll('.seat-gap').length,0);
    assert.equal(page.window.document.querySelectorAll('.seat-row').length,0);
  }
});

test('projected unavailable seats stay disabled in the larger row layout',async t=>{
  const seats=theatreSeats();
  const page=await setup(t,{detail:()=>({seats,availability:{seats_remaining:49,available_seat_ids:seats.filter(s=>s.seat_id!=='A1').map(s=>s.seat_id),occurred_at:'2026-11-21T12:00:00Z'}})});
  page.$('#event-list .event-card').click();
  await until(()=>Boolean(page.$('.seat')));
  assert.equal(page.$('[data-seat="A1"]').disabled,true);
  assert.match(page.$('[data-seat="A1"]').getAttribute('aria-label'),/unavailable/);
  assert.equal(page.$('[data-seat="A10"]').disabled,false);
});

test('a Booking cancellation disables reservations even when Catalogue has not caught up',async t=>{
  const page=await setup(t,{checkout:true,hold:async()=>({status:409,body:{error:'event_cancelled',message:'This event has been cancelled.'}})});
  page.$('#event-list .event-card').click();
  await until(()=>Boolean(page.$('.seat')));
  page.$('.seat').click();page.$('#hold').click();
  await until(()=>page.$('#reservation-status').textContent.includes('Reservations are closed'));
  assert.equal(page.$('.seat').disabled,true);
  assert.equal(page.$('#hold').disabled,true);
  assert.equal(checkoutPosts(page).length,0);
});

async function holdForCheckout(page) {
  page.$('#event-list .event-card').click();
  await until(()=>Boolean(page.$('.seat')));
  page.$('.seat').click();
  page.$('#hold').click();
  await until(()=>Boolean(page.$('#checkout')) && !page.$('#checkout').disabled);
}

const checkoutPosts = page => page.requests.filter(r=>r.path.endsWith('/checkout'));

test('checkout reads the saved Booking price and sends no browser price to Payment',async t=>{
  const page=await setup(t,{checkout:true,status:async()=>({status:200,body:bookingResult('held',{price_minor:5200})})});
  await holdForCheckout(page);
  assert.match(page.$('.booking-total').textContent,/52\.00/);
  assert.equal(page.$('#hold').disabled,true);
  page.$('#checkout').click();
  await until(()=>checkoutPosts(page).length===1);
  assert.equal(checkoutPosts(page)[0].path,`/api/booking/bookings/${bookingId}/checkout`);
  assert.deepEqual(JSON.parse(checkoutPosts(page)[0].options.body),{customer_id:page.window.sessionStorage.getItem('stagedoor-customer')});
  assert.equal(page.requests.some(r=>r.path.startsWith('/api/payment/')),false);
});

test('202 never confirms a purchase; polling must return confirmed and double clicks send one POST',async t=>{
  let submitted=false, finish;
  const completion=new Promise(resolve=>{finish=resolve;});
  let pendingReads=0;
  const page=await setup(t,{checkout:true,
    pay:async()=>{submitted=true;return {status:202,body:bookingResult('confirmed')};},
    status:async()=>!submitted ? {status:200,body:bookingResult()} : ++pendingReads===1 ? {status:200,body:bookingResult('payment_pending')} : completion});
  await holdForCheckout(page);
  page.$('#checkout').click(); page.$('#checkout').click();
  await until(()=>pendingReads===2);
  assert.match(page.$('#checkout-status').textContent,/processing/);
  assert.doesNotMatch(page.$('#checkout-status').textContent,/Booking confirmed/);
  assert.equal(page.$('#checkout'),null);
  assert.equal(checkoutPosts(page).length,1);
  // Browsing does not discard the active checkout or start a second hold.
  page.$('#event-list .event-card[data-event="second"]').click();
  await until(()=>page.$('#detail h2').textContent==='Current second');
  page.$('.seat').click();
  assert.equal(page.$('#hold').disabled,true);
  finish({status:200,body:bookingResult('confirmed')});
  await until(()=>page.$('#checkout-status').textContent.includes('Booking confirmed'));
  await delay(15);
  assert.equal(pendingReads,2);
  assert.equal(checkoutPosts(page).length,1);
});

test('a lost checkout response recovers through GET without automatically posting again',async t=>{
  let submitted=false;
  const page=await setup(t,{checkout:true,pay:async()=>{submitted=true;throw new Error('Network timeout');},
    status:async()=>({status:200,body:bookingResult(submitted ? 'confirmed' : 'held')})});
  await holdForCheckout(page);
  page.$('#checkout').click();
  await until(()=>page.$('#checkout-status').textContent.includes('Booking confirmed'));
  assert.equal(checkoutPosts(page).length,1);
});

const staleFreeSeat = () => ({seats:[{seat_id:'A1',section:'A',label:'A1'}],
  availability:{seats_remaining:1,available_seat_ids:['A1'],occurred_at:'2026-11-22T12:00:00Z'}});

function assertBookedSeat(page) {
  const seat=page.$('[data-seat="A1"]');
  assert.equal(seat.disabled,true);
  assert.equal(seat.classList.contains('booked'),true);
  assert.equal(seat.classList.contains('reserved'),false);
  assert.equal(seat.getAttribute('aria-pressed'),'false');
  assert.match(seat.getAttribute('aria-label'),/booked/);
}

test('confirmation changes the held highlight to a taken seat and Refresh preserves it with a stale projection',async t=>{
  let submitted=false;
  const page=await setup(t,{checkout:true,detail:staleFreeSeat,
    pay:async()=>{submitted=true;return {status:202,body:{booking_id:bookingId,booking_status:'payment_pending'}};},
    status:async()=>({status:200,body:bookingResult(submitted ? 'confirmed' : 'held')})});
  await holdForCheckout(page);
  assert.equal(page.$('.seat').classList.contains('reserved'),true);
  page.$('#checkout').click();
  await until(()=>page.$('#checkout-status').textContent.includes('Booking confirmed'));
  assertBookedSeat(page);
  page.$('#refresh').click();
  await until(()=>!page.$('#refresh').disabled);
  assertBookedSeat(page);
  page.$('[data-event="second"]').click();
  await until(()=>page.$('#detail h2').textContent==='Current second');
  page.$('[data-event="first"]').click();
  await until(()=>page.$('#detail h2').textContent==='Current first');
  assertBookedSeat(page);
});

test('a reloaded confirmed booking is taken whether status arrives before or after the seat map',async t=>{
  for(const late of [false,true]) {
    let complete;
    const pending=new Promise(resolve=>{complete=resolve;});
    const page=await setup(t,{checkout:true,detail:staleFreeSeat,
      savedBooking:{id:bookingId,reference:bookingId,eventId:'first',seatId:'A1',title:'Saved event'},
      status:()=>late ? pending : Promise.resolve({status:200,body:bookingResult('confirmed')})});
    page.$('#event-list .event-card').click();
    await until(()=>Boolean(page.$('.seat')));
    if(late) complete({status:200,body:bookingResult('confirmed')});
    await until(()=>page.$('#checkout-status').textContent.includes('Booking confirmed'));
    assertBookedSeat(page);
    assert.equal(checkoutPosts(page).length,0);
  }
});

test('explicit payment-service unavailability retains the hold and permits only a manual same-ID retry',async t=>{
  let submitted=false;
  const page=await setup(t,{checkout:true,
    pay:async()=>checkoutPosts(page).length===1 ? {status:503,body:{detail:{error:'payment_service_unavailable'}}} : (submitted=true,{status:202,body:{}}),
    status:async()=>({status:200,body:bookingResult(submitted ? 'confirmed' : 'held')})});
  await holdForCheckout(page);
  page.$('#checkout').click();
  await until(()=>page.$('#checkout-status').textContent.includes('Payments are unavailable'));
  assert.match(page.$('#checkout-status').textContent,/seat is still held/);
  assert.equal(page.$('#checkout').disabled,false);
  await delay(15);
  assert.equal(checkoutPosts(page).length,1);
  page.$('#checkout').click();
  await until(()=>page.$('#checkout-status').textContent.includes('Booking confirmed'));
  assert.equal(checkoutPosts(page).length,2);
  assert.equal(new Set(checkoutPosts(page).map(r=>r.path)).size,1);
});

test('generic 503 is an unknown outcome and cancellation never becomes a confirmation',async t=>{
  let submitted=false;
  const page=await setup(t,{checkout:true,
    pay:async()=>{submitted=true;return {status:503,body:{detail:'Service unavailable'}};},
    status:async()=>({status:200,body:bookingResult(submitted ? 'released' : 'held')})});
  await holdForCheckout(page);
  page.$('#checkout').click();
  await until(()=>page.$('#checkout-status').textContent.includes('seat was released'));
  assert.match(page.$('#checkout-status').textContent,/If a late payment succeeds/);
  assert.equal(page.$('#check-ticket'),null);
  assert.equal(checkoutPosts(page).length,1);
  assert.equal(page.$('.seat').disabled,false);
});

test('missing price or unsupported currency prevents checkout',async t=>{
  for(const price of [{price_minor:undefined,currency:undefined},{price_minor:4500,currency:'JPY'}]) {
    const page=await setup(t,{checkout:true,status:async()=>({status:200,body:bookingResult('held',price)})});
    page.$('#event-list .event-card').click();
    await until(()=>Boolean(page.$('.seat')));
    page.$('.seat').click();page.$('#hold').click();
    await until(()=>Boolean(page.$('#checkout-status')) && page.$('#checkout-status').textContent.includes('total could not be read'));
    assert.equal(page.$('#checkout').disabled,true);
    page.$('#checkout').click();
    assert.equal(checkoutPosts(page).length,0);
  }
});

test('mismatched booking identity or unknown status is never treated as confirmation',async t=>{
  for(const bad of [{booking_id:'another-booking',booking_status:'confirmed'},{booking_status:'unrecognised'}]) {
    const page=await setup(t,{checkout:true,status:async()=>({status:200,body:bookingResult('held',bad)})});
    page.$('#event-list .event-card').click();
    await until(()=>Boolean(page.$('.seat')));
    page.$('.seat').click();page.$('#hold').click();
    await until(()=>page.$('#checkout-status')?.textContent.includes('could not be understood'));
    assert.equal(page.$('#check-ticket'),null);
    assert.equal(checkoutPosts(page).length,0);
    if(page.$('#checkout')) assert.equal(page.$('#checkout').disabled,true);
  }
});

test('reload restores identity and reads the outcome without trusting a saved price or posting checkout',async t=>{
  const page=await setup(t,{checkout:true,
    savedBooking:{id:bookingId,reference:bookingId,eventId:'first',seatId:'A1',title:'Saved event',amount:1,currency:'EUR',status:'confirmed'},
    status:async()=>({status:200,body:bookingResult('payment_pending')})});
  await until(()=>page.$('#checkout-status').textContent.includes('processing'));
  assert.match(page.$('.booking-total').textContent,/45\.00/);
  assert.equal(checkoutPosts(page).length,0);
  assert.equal(page.requests.filter(r=>r.path==='/api/booking/holds').length,0);
  assert.equal(page.$('#check-ticket'),null);
});

test('status outages stop after three failures and manual recovery reads status only',async t=>{
  let unavailable=true, reads=0;
  const page=await setup(t,{checkout:true,
    savedBooking:{id:bookingId,reference:bookingId,eventId:'first',seatId:'A1',title:'Saved event'},
    status:async()=>{reads++;return unavailable ? {status:503,body:{detail:'Unavailable'}} : {status:200,body:bookingResult('confirmed')};}});
  await until(()=>page.$('#checkout-status').textContent.includes('payment outcome may be unknown'));
  assert.equal(reads,3);
  unavailable=false;
  page.$('#check-booking').click();
  await until(()=>page.$('#checkout-status').textContent.includes('Booking confirmed'));
  assert.equal(checkoutPosts(page).length,0);
});

test('extended pending status has bounded polling and never cancels or retries payment',async t=>{
  let reads=0;
  const page=await setup(t,{checkout:true,
    savedBooking:{id:bookingId,reference:bookingId,eventId:'first',seatId:'A1',title:'Saved event'},
    status:async()=>{reads++;return {status:200,body:bookingResult('payment_pending')};}});
  await until(()=>page.$('#checkout-status').textContent.includes('Stopping these checks does not cancel'));
  assert.equal(reads,45);
  await delay(15);
  assert.equal(reads,45);
  assert.equal(checkoutPosts(page).length,0);
});

test('released bookings can progress through late-success refunds without a ticket or another checkout',async t=>{
  let state='held';
  const page=await setup(t,{checkout:true,pollDelay:1000,
    pay:async()=>{state='payment_pending';return {status:202,body:{booking_id:bookingId,booking_status:state}};},
    status:async()=>({status:200,body:bookingResult(state)})});
  await holdForCheckout(page);
  page.$('#checkout').click();
  await until(()=>page.$('#checkout-status').textContent.includes('processing'));
  assert.doesNotMatch(page.$('#checkout-panel').textContent,/countdown|expired|seconds remaining/i);
  for(const [next,text] of [['released','seat was released'],['refund_required','refund has been requested'],['refund_pending','refund has not completed'],['refunded','payment has been refunded']]) {
    state=next;
    page.$('#check-booking').click();
    await until(()=>page.$('#checkout-status').textContent.includes(text));
    assert.equal(page.$('#check-ticket'),null);
    assert.equal(page.$('#checkout'),null);
  }
  assert.equal(checkoutPosts(page).length,1);
});

test('ticket is shown only after Ticketing reports a valid ticket for this booking',async t=>{
  let calls=0;
  const page=await setup(t,{checkout:true,
    savedBooking:{id:bookingId,reference:bookingId,eventId:'first',seatId:'A1',title:'Saved event'},
    status:async()=>({status:200,body:bookingResult('confirmed')}),
    ticket:async()=>++calls===1 ? {status:404,body:{}} : {status:200,body:{booking_id:bookingId,status:'valid',valid:true,ticket_code:'TKT-DEMO'}}});
  await until(()=>Boolean(page.$('#check-ticket')));
  assert.equal(page.$('#ticket-status'),null);
  page.$('#check-ticket').click();
  await until(()=>page.$('#ticket-status')?.textContent.includes('not ready'));
  page.$('#check-ticket').click();
  await until(()=>page.$('#ticket-status')?.textContent.includes('TKT-DEMO'));
});

test('a delayed ticket response cannot display a valid ticket after Booking reports cancellation',async t=>{
  let status='confirmed', finish;
  const pendingTicket=new Promise(resolve=>{finish=resolve;});
  const page=await setup(t,{checkout:true,
    savedBooking:{id:bookingId,reference:bookingId,eventId:'first',seatId:'A1',title:'Saved event'},
    status:async()=>({status:200,body:bookingResult(status)}),ticket:()=>pendingTicket});
  await until(()=>Boolean(page.$('#check-ticket')));
  page.$('#check-ticket').click();
  status='released';
  page.$('#check-booking').click();
  await until(()=>page.$('#checkout-status').textContent.includes('seat was released'));
  finish({status:200,body:{booking_id:bookingId,status:'valid',valid:true,ticket_code:'TKT-STALE'}});
  await delay(10);
  assert.equal(page.$('#ticket-status'),null);
  assert.equal(page.$('#check-ticket'),null);
});

test('unavailable search never fabricates a keyword-fallback result',async t=>{
  const {$,submit}=await setup(t,{search:async()=>({status:503,body:{detail:'Unavailable'}})});
  submit('music');
  await until(()=>!$('#search-submit').disabled);
  assert.match($('#list-status').textContent,/Search could not be loaded/);
  assert.equal($('#search-status').textContent,'');
  assert.equal($('#event-list').children.length,0);
});
