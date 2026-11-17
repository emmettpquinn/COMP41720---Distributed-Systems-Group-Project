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

async function setup(t, {search=async()=>({status:200,body:searchResult()}), enabled=true} = {}) {
  const dom = new JSDOM(html,{url:'http://localhost',runScripts:'outside-only'});
  t.after(()=>dom.window.close());
  const window=dom.window;
  window.AbortSignal=globalThis.AbortSignal;
  if (!window.crypto.randomUUID) Object.defineProperty(window.crypto,'randomUUID',{value:randomUUID});
  const requests=[];
  window.fetch=async(path,options={})=>{
    requests.push({path,options});
    let response;
    if(path==='/api/capabilities') response={status:200,body:{holds:true,search:enabled,checkout:false}};
    else if(path==='/api/catalogue/events') response={status:200,body:catalogue};
    else if(path.startsWith('/api/catalogue/events/')) {
      const id=decodeURIComponent(path.split('/').at(-1));
      response={status:200,body:{...catalogue.find(item=>item.event_id===id),description:'Fictional performance',
        seats:[{seat_id:'A1',section:'A',label:'A1'}],availability:null}};
    } else if(path.startsWith('/api/discovery/search?')) response=await search(new URL(path,'http://localhost').searchParams.get('q'));
    else if(path==='/api/booking/holds') response={status:201,body:{status:'held',booking_ref:'synthetic-reference'}};
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
});

test('unavailable search never fabricates a keyword-fallback result',async t=>{
  const {$,submit}=await setup(t,{search:async()=>({status:503,body:{detail:'Unavailable'}})});
  submit('music');
  await until(()=>!$('#search-submit').disabled);
  assert.match($('#list-status').textContent,/Search could not be loaded/);
  assert.equal($('#search-status').textContent,'');
  assert.equal($('#event-list').children.length,0);
});
