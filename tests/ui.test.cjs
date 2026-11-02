const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../goldcopier/web/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../docs/frontend-preview.html'),'utf8');
const sample=JSON.parse(html.match(/if\(path==="\/demo"\) return ([\s\S]*?); if\(path==="\/dashboard"\)/)[1]);
const tick=()=>new Promise(r=>setTimeout(r,25));
async function setup(authenticated=false,configOverrides={},dashboardOverride=null,streaming=false){
 const calls=[];
 const dom=new JSDOM('<div id="app"></div><div id="toast"></div><dialog id="modal"></dialog>',{url:authenticated?'http://localhost/#overview':'http://localhost/#demo',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window;
 if(streaming){w.portfolioSources=[];w.EventSource=class{constructor(url){this.url=url;this.handlers={};w.portfolioSources.push(this);}addEventListener(name,fn){this.handlers[name]=fn;}close(){this.closed=true;}emit(name,data){this.handlers[name]?.({data:JSON.stringify(data)});}};}
 // Keep seeded chart fixtures within their intended time window on any test date.
 w.Date.now=()=>Math.max(...sample.dashboard.equity.map(p=>p.created))*1000+60000;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
 w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.HTMLElement.prototype.scrollIntoView=function(){};
 w.fetch=async(url,opt={})=>{calls.push({url,opt});let body;if(url==='/api/config')body={demo:true,live_enabled:false,billing_enabled:false,billing_ready:false,...configOverrides};else if(url==='/api/demo')body=sample;else if(url==='/api/dashboard')body=dashboardOverride||sample.dashboard;else if(url.startsWith('/api/portfolio/equity'))body={points:(dashboardOverride||sample.dashboard).equity||[]};else if(url==='/api/auth/me'&&authenticated)body={user:sample.user,csrf:'csrf-test'};else if(authenticated&&opt.method&&opt.method!=='GET')body={ok:true};else return {ok:false,status:401,json:async()=>({detail:'Sign in'})};return {ok:true,status:200,json:async()=>structuredClone(body)}};
 w.eval(source);await tick();return {dom,w,calls};
}

test('real frontend renders overview, charts, positions and demo disclosure',async()=>{const{dom,w}=await setup();try{assert.match(w.document.querySelector('h1').textContent,/Alex/);assert.match(w.document.body.textContent,/Demo workspace/);assert.ok(w.document.querySelector('svg[aria-label="Equity snapshots for selected account"]'));assert.match(w.document.body.textContent,/XAUUSDm/);assert.equal(w.document.querySelectorAll('tbody tr').length,2)}finally{dom.window.close()}});

test('navigation exposes actual account, risk, trade and billing screens',async()=>{const{dom,w}=await setup();try{for(const[page,selector]of [['accounts','.account-card'],['risk','#risk-form'],['trades','#history-table'],['billing','.plan'],['analysts','.analyst-card'],['activity','.notification']]){w.location.hash=page;await tick();assert.ok(w.document.querySelector(selector),page)}}finally{dom.window.close()}});

test('account wizard opens and public demo cannot mutate an account',async()=>{const{dom,w,calls}=await setup();try{w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect"]').click();await tick();assert.ok(w.document.querySelector('#modal').open);const f=w.document.querySelector('#connect-form');assert.ok(f);f.querySelector('[name="name"]').value='Demo account';f.querySelector('[name="login"]').value='987654';f.querySelector('[name="server"]').value='Demo';f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.match(w.document.querySelector('#toast').textContent,/read-only/);assert.ok(!calls.some(c=>c.url==='/api/accounts'))}finally{dom.window.close()}});

test('trade command dialog requires explicit review and displays the selected ticket',async()=>{const{dom,w}=await setup();try{w.location.hash='trades';await tick();w.document.querySelector('[data-action="manage"]').click();await tick();assert.ok(w.document.querySelector('#confirm-command').required);assert.match(w.document.querySelector('#modal').textContent,/#7100231/);assert.equal(w.document.querySelector('#command-form [name="position"]').value,'7100231')}finally{dom.window.close()}});

test('user-supplied labels render as text instead of executable markup',async()=>{const orig=sample.user.name;sample.user.name='<img src=x onerror=alert(1)>';const{dom,w}=await setup();try{assert.ok(!w.document.querySelector('img[onerror]'));assert.match(w.document.querySelector('h1').textContent,/<img/)}finally{sample.user.name=orig;dom.window.close()}});


test('authenticated risk form sends validated numeric settings with CSRF token',async()=>{const{dom,w,calls}=await setup(true);try{w.location.hash='risk';await tick();const f=w.document.querySelector('#risk-form');f.querySelector('[name="risk_pct"]').value='0.7';f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();const call=calls.find(c=>c.opt.method==='PUT');assert.ok(call);const body=JSON.parse(call.opt.body);assert.equal(body.risk_pct,.7);assert.equal(body.be_enabled,true);assert.equal(body.respect_manual_stops,true);assert.equal(call.opt.headers['X-CSRF-Token'],'csrf-test');assert.match(w.document.querySelector('#toast').textContent,/saved/)}finally{dom.window.close()}});

test('authenticated home and sign-in routes preserve the workspace session',async()=>{const{dom,w}=await setup(true);try{for(const page of ['home','login','signup','demo']){w.location.hash=page;await tick();await tick();assert.equal(w.location.hash,'#overview');assert.ok(w.document.querySelector('.shell'));assert.ok(!w.document.querySelector('#login-form'))}assert.equal(w.document.querySelector('.brand').getAttribute('href'),'#overview');assert.ok(w.document.querySelector('.top-right [data-action="logout"]'));assert.ok(w.document.querySelector('.top-right [aria-label="Open profile"]'))}finally{dom.window.close()}});

test('password visibility toggles without submitting or losing the password',async()=>{const{dom,w,calls}=await setup();try{w.location.hash='login';await tick();const input=w.document.querySelector('#password'),toggle=w.document.querySelector('[data-action="toggle-password"]');input.value='secret-password';toggle.click();await tick();assert.equal(input.type,'text');assert.equal(toggle.getAttribute('aria-label'),'Hide password');assert.equal(toggle.getAttribute('aria-pressed'),'true');toggle.click();await tick();assert.equal(input.type,'password');assert.equal(input.value,'secret-password');assert.ok(!calls.some(c=>c.url==='/api/auth/login'))}finally{dom.window.close()}});

test('modal errors are dismissible within the active dialog',async()=>{const{dom,w}=await setup();try{w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect"]').click();await tick();const f=w.document.querySelector('#connect-form');f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();const dialog=w.document.querySelector('#modal'),toast=w.document.querySelector('#toast');assert.ok(dialog.contains(toast));assert.equal(toast.className,'visible error');toast.querySelector('button').click();await tick();assert.equal(toast.className,'');assert.ok(dialog.open)}finally{dom.window.close()}});

test('logout clears session data and protected routes return to sign in',async()=>{const{dom,w,calls}=await setup(true);try{w.document.querySelector('.top-right [data-action="logout"]').click();await tick();assert.ok(w.document.querySelector('#login-form'));assert.equal(w.localStorage.getItem('gold-account'),null);assert.equal(calls.find(c=>c.url==='/api/auth/logout').opt.headers['X-CSRF-Token'],'csrf-test');w.location.hash='accounts';await tick();await tick();assert.equal(w.location.hash,'#login');assert.ok(!w.document.querySelector('.shell'))}finally{dom.window.close()}});

test('expired sessions close dialogs and remove protected workspace content',async()=>{const{dom,w}=await setup(true);try{w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect"]').click();await tick();w.fetch=async()=>({ok:false,status:401,json:async()=>({detail:'Sign in to continue'})});w.document.querySelector('#connect-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();assert.equal(w.location.hash,'#login');assert.equal(w.document.querySelector('#modal').open,false);assert.equal(w.document.querySelector('#modal').textContent,'');assert.ok(w.document.querySelector('#login-form'));assert.ok(!w.document.querySelector('.shell'));assert.match(w.document.querySelector('#toast').textContent,/Sign in/)}finally{dom.window.close()}});


test('local account page exposes copying instead of worker commands',async()=>{const{dom,w}=await setup(true,{demo:false,local_worker_available:true});try{w.location.hash='accounts';await tick();const card=w.document.querySelector('.account-card');assert.ok(card.querySelector('[data-action="toggle-copy"]'));assert.ok(!card.querySelector('[data-action="worker-setup"]'));w.document.querySelector('[data-action="connect"]').click();await tick();assert.ok(w.document.querySelector('#connect-analyst'));assert.match(w.document.querySelector('#hosting').textContent,/This computer/)}finally{dom.window.close()}});

test('connect MT5, wait for readiness and start copying from the same account page',async()=>{
 const dashboard=structuredClone(sample.dashboard);dashboard.accounts=[];
 const{dom,w,calls}=await setup(true,{demo:false,local_worker_available:true},dashboard);
 try{
  const originalFetch=w.fetch;
  w.fetch=async(url,opt={})=>{
   if(url==='/api/accounts'&&opt.method==='POST'){
    calls.push({url,opt});const body=JSON.parse(opt.body);
    dashboard.accounts.push({...structuredClone(sample.dashboard.accounts[0]),id:'new-local',name:body.name,login:body.login,server:body.server,hosting:'self',enabled:false,online:false,heartbeat:null,snapshot:{},follows:[{analyst_id:body.analyst_id}]});
    return {ok:true,status:200,json:async()=>({id:'new-local'})};
   }
   if(url==='/api/accounts/new-local/enabled'&&opt.method==='POST')dashboard.accounts[0].enabled=JSON.parse(opt.body).enabled;
   return originalFetch(url,opt);
  };
  w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect"]').click();await tick();
  const form=w.document.querySelector('#connect-form');form.elements.namedItem('name').value='My MT5';form.elements.namedItem('login').value='123456';form.elements.namedItem('server').value='Demo';form.elements.namedItem('consent').checked=true;
  const analyst=form.elements.namedItem('analyst_id').value;
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert.equal(w.document.querySelector('#modal').open,false);
  assert.equal(w.location.hash,'#accounts');
  assert.match(w.document.querySelector('.account-card').textContent,/Connecting to MT5/);
  assert.equal(w.document.querySelector('[data-action="toggle-copy"]').disabled,true);
  assert.equal(JSON.parse(calls.find(c=>c.url==='/api/accounts').opt.body).analyst_id,analyst);
  assert.ok(!calls.some(c=>c.url.endsWith('/enabled')));
  dashboard.accounts[0].online=true;dashboard.accounts[0].heartbeat=Date.now()/1000;
  await w.eval('refresh()');
  assert.match(w.document.querySelector('.account-card').textContent,/Ready/);
  const copy=w.document.querySelector('[data-action="toggle-copy"]');assert.equal(copy.disabled,false);copy.click();await tick();
  assert.equal(JSON.parse(calls.find(c=>c.url==='/api/accounts/new-local/enabled').opt.body).enabled,true);
  assert.match(w.document.querySelector('.account-card').textContent,/Copying is on. Waiting for the next valid signal/);
 }finally{dom.window.close()}
});

test('copy button targets its own card rather than another selected account',async()=>{
 const dashboard=structuredClone(sample.dashboard);const first=dashboard.accounts[0];first.enabled=false;first.online=true;first.snapshot.error=null;
 dashboard.accounts=[first,{...structuredClone(first),id:'second-account'}];
 const{dom,w,calls}=await setup(true,{demo:false,local_worker_available:true},dashboard);
 try{w.location.hash='accounts';await tick();w.document.querySelector('[data-action="toggle-copy"][data-account="second-account"]').click();await tick();assert.ok(calls.some(c=>c.url==='/api/accounts/second-account/enabled'));assert.ok(!calls.some(c=>c.url===`/api/accounts/${first.id}/enabled`))}finally{dom.window.close()}
});


test('pair mappings screen supports precise forex limits and broker catalogue suggestions',async()=>{
 const {dom,w,calls}=await setup(true);
 try{
  w.location.hash='pairs';await tick();
  assert.match(w.document.querySelector('h1').textContent,/Trading pairs/);
  w.document.querySelector('[data-action="map-pair"]').click();await tick();
  const f=w.document.querySelector('#mapping-form');assert.ok(f);
  f.elements.namedItem('pair').value='EURUSD';f.elements.namedItem('broker_symbol').value='EURUSDm';
  f.elements.namedItem('max_spread').value='0.00025';f.elements.namedItem('max_entry_distance').value='0.0008';
  f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
  const call=calls.find(c=>c.url.endsWith('/pairs')&&c.opt.method==='POST');assert.ok(call);
  const body=JSON.parse(call.opt.body);assert.equal(body.pair,'EURUSD');assert.equal(body.max_spread,.00025);assert.equal(body.max_entry_distance,.0008);
 }finally{dom.window.close()}
});

test('Exness connection form sends credentials only to authenticated API and never displays them in account cards',async()=>{
 const {dom,w,calls}=await setup(true,{demo:false});
 try{
  w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect-exness"]').click();await tick();
  const f=w.document.querySelector('#exness-form');assert.ok(f);assert.ok(f.elements.namedItem('consent').required);
  f.elements.namedItem('name').value='Direct account';f.elements.namedItem('account_id').value='12345';f.elements.namedItem('api_key').value='example-public-key';f.elements.namedItem('private_key').value='example-private-key';f.elements.namedItem('gateway').value='https://gateway.exness.com';
  f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
  const call=calls.find(c=>c.url==='/api/accounts/exness');assert.ok(call);assert.equal(call.opt.headers['X-CSRF-Token'],'csrf-test');
  assert.equal(JSON.parse(call.opt.body).private_key,'example-private-key');
 }finally{dom.window.close()}
});


test('disconnect and delete target their account card even if selection changes in the dialog',async()=>{
 for(const operation of ['disconnect','delete']){
  const dashboard=structuredClone(sample.dashboard);const first=dashboard.accounts[0];
  dashboard.accounts=[first,{...structuredClone(first),id:'second-account',name:'Other MT5',online:false,heartbeat:null,snapshot:{error:'Connection problem'},disconnected:false}];
  const {dom,w,calls}=await setup(true,{demo:false,local_worker_available:true},dashboard);
  try{
   w.location.hash='accounts';await tick();
   w.document.querySelector(`[data-action="${operation==='delete'?'delete-account':'disconnect'}"][data-account="second-account"]`).click();await tick();
   const f=w.document.querySelector('#account-removal-form');assert.ok(f);assert.equal(f.elements.namedItem('account_id').value,'second-account');
   assert.ok(f.querySelector('#removal-confirm').required);assert.match(f.textContent,/Existing positions and pending orders remain/);
   // Selection changes cannot redirect a pending confirmation to a different account.
   w.eval(`selected=${JSON.stringify(first.id)}`);
   f.querySelector('#removal-confirm').checked=true;f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
   const call=calls.find(c=>c.url==='/api/accounts/second-account'+(operation==='disconnect'?'/disconnect':''));assert.ok(call);
   assert.equal(call.opt.method,operation==='disconnect'?'POST':'DELETE');assert.equal(call.opt.headers['X-CSRF-Token'],'csrf-test');
   assert.equal(JSON.parse(call.opt.body).acknowledge_unmanaged_trades,true);
   assert.ok(!calls.some(c=>c.url==='/api/accounts'&&c.opt.method==='POST'));
  }finally{dom.window.close()}
 }
});

test('disconnected account stays visibly disconnected and cannot start copying',async()=>{
 const dashboard=structuredClone(sample.dashboard);dashboard.accounts[0].disconnected=true;
 const {dom,w}=await setup(true,{demo:false},dashboard);
 try{w.location.hash='accounts';await tick();const card=w.document.querySelector('.account-card');assert.match(card.textContent,/Disconnected/);assert.equal(card.querySelector('[data-action="toggle-copy"]').disabled,true);assert.equal(card.querySelector('[data-action="disconnect"]').disabled,true);assert.ok(card.querySelector('[data-action="delete-account"]'))}finally{dom.window.close()}
});


test('signal intake offers an explicit CMP template without silently queueing it',async()=>{
 const oldRole=sample.user.role;sample.user.role='admin';
 const{dom,w,calls}=await setup(true,{demo:false});
 try{
  const originalFetch=w.fetch;w.fetch=async(url,opt={})=>url==='/api/operator/status'?{ok:true,status:200,json:async()=>({accounts:[],unresolved:[],analysts:[{id:'analyst',name:'Trader'}]})}:originalFetch(url,opt);
  w.location.hash='operator';await tick();
  w.document.querySelector('[data-action="signal-tool"]').click();await tick();
  const select=w.document.querySelector('#signal-template');assert.ok(select);
  const confirm=w.document.querySelector('#signal-confirm');confirm.checked=true;
  select.value='cmp';select.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.match(w.document.querySelector('#signal-text').value,/BUY XAUUSD CMP/);
  assert.equal(confirm.checked,false);
  assert.ok(!calls.some(c=>c.url==='/api/operator/signals'));
 }finally{sample.user.role=oldRole;dom.window.close()}
});


test('portfolio stream updates prices, P&L, orders and equity without replacing an open dialog',async()=>{
 const dashboard=structuredClone(sample.dashboard),a=dashboard.accounts[0],now=Date.now()/1000;
 a.heartbeat=now;a.snapshot.error=null;
 dashboard.equity=[{account_id:a.id,created:now-20,equity:10000},{account_id:a.id,created:now-10,equity:10010}];
 const{dom,w}=await setup(true,{demo:false},dashboard,true);
 try{
  const stream=w.portfolioSources[0];assert.ok(stream);stream.onopen();
  w.location.hash='trades';await tick();
  w.document.querySelector('[data-action="manage"]').click();await tick();
  const dialog=w.document.querySelector('#modal'),percent=w.document.querySelector('#percent');percent.value='37';percent.focus();
  const next=structuredClone(dashboard);next.accounts[0].heartbeat=now+1;next.accounts[0].snapshot.equity=11234.56;
  next.accounts[0].snapshot.positions[0].price_current=3987.65;next.accounts[0].snapshot.positions[0].profit=123.45;
  next.accounts[0].snapshot.orders=[{ticket:99,symbol:'EURUSD',type:'BUY LIMIT',volume:.01,price_open:1.0805,copier:true}];
  delete next.equity;
  stream.emit('portfolio',{dashboard:next,server_time:now+1});await tick();
  assert.match(w.document.querySelector('.portfolio-summary').textContent,/11,234.56/);
  assert.match(w.document.querySelector('.content').textContent,/3,987.65/);
  assert.match(w.document.querySelector('.content').textContent,/123.45/);
  assert.match(w.document.querySelector('.content').textContent,/EURUSD/);
  assert.equal(w.document.querySelector('#modal'),dialog);assert.equal(dialog.open,true);assert.equal(percent.value,'37');
  assert.equal(w.portfolioSources.length,1);
 }finally{dom.window.close()}
});


test('stream reconnects, labels stale broker data, and ends when the session expires',async()=>{
 const dashboard=structuredClone(sample.dashboard),now=Date.now()/1000;
 dashboard.accounts[0].heartbeat=now-45;dashboard.accounts[0].snapshot.error=null;
 const{dom,w}=await setup(true,{demo:false},dashboard,true);
 try{
  const stream=w.portfolioSources[0];stream.onopen();
  stream.emit('portfolio',{dashboard:structuredClone(dashboard),server_time:now});await tick();
  assert.match(w.document.querySelector('[data-live-status]').textContent,/Stale/);
  stream.onerror();assert.match(w.document.querySelector('[data-live-status]').textContent,/Stale/);
  const recovered=structuredClone(dashboard);recovered.accounts[0].heartbeat=now;
  stream.onopen();stream.emit('portfolio',{dashboard:recovered,server_time:now});await tick();
  assert.match(w.document.querySelector('[data-live-status]').textContent,/Live/);
  stream.emit('expired',{});await tick();await tick();
  assert.equal(stream.closed,true);assert.equal(w.location.hash,'#login');assert.ok(!w.document.querySelector('.shell'));
 }finally{dom.window.close()}
});


test('live chart grows as snapshots arrive and history uses the chosen account and period',async()=>{
 const dashboard=structuredClone(sample.dashboard),a=dashboard.accounts[0],now=Date.now()/1000;
 a.snapshot.error=null;a.heartbeat=now;
 dashboard.equity=[{account_id:a.id,created:now-10,equity:10000},{account_id:a.id,created:now-5,equity:10010}];
 const{dom,w,calls}=await setup(true,{demo:false},dashboard,true);
 try{
  const stream=w.portfolioSources[0];stream.onopen();const before=w.document.querySelector('svg[aria-label="Equity snapshots for selected account"] path[stroke="#58866e"]').getAttribute('d');
  const next=structuredClone(dashboard);delete next.equity;next.accounts[0].snapshot.equity=10100;next.accounts[0].heartbeat=now+1;
  stream.emit('portfolio',{dashboard:next,server_time:now+1});await tick();
  assert.notEqual(w.document.querySelector('svg[aria-label="Equity snapshots for selected account"] path[stroke="#58866e"]').getAttribute('d'),before);
  w.document.querySelector('[data-period="30D"]').click();await tick();
  assert.ok(calls.some(c=>c.url.includes(`/portfolio/equity?account_id=${a.id}&period=30D`)));
  assert.equal(w.document.querySelector('[data-period="30D"]').className,'active');
 }finally{dom.window.close()}
});


test('trade search keeps its text, focus and filter while a portfolio frame arrives',async()=>{
 const{dom,w}=await setup(true,{demo:false},null,true);
 try{
  w.location.hash='trades';await tick();const search=w.document.querySelector('#trade-search');search.value='executed';search.focus();search.dispatchEvent(new w.Event('input',{bubbles:true}));
  const stream=w.portfolioSources[0];stream.onopen();stream.emit('portfolio',{dashboard:structuredClone(sample.dashboard)});await tick();
  assert.equal(w.document.querySelector('#trade-search').value,'executed');assert.equal(w.document.activeElement.id,'trade-search');
  assert.ok([...w.document.querySelectorAll('#history-table tbody tr')].every(row=>row.hidden===!row.textContent.toLowerCase().includes('executed')));
 }finally{dom.window.close()}
});


test('hidden tabs close the stream and foreground tabs reconnect without accepting old frames',async()=>{
 const{dom,w}=await setup(true,{demo:false},null,true);
 try{
  const old=w.portfolioSources[0];
  Object.defineProperty(w.document,'hidden',{configurable:true,value:true});w.document.dispatchEvent(new w.Event('visibilitychange'));
  assert.equal(old.closed,true);
  Object.defineProperty(w.document,'hidden',{configurable:true,value:false});w.document.dispatchEvent(new w.Event('visibilitychange'));await tick();
  assert.equal(w.portfolioSources.length,2);
  const before=w.document.querySelector('.chart-total').textContent;
  const stale=structuredClone(sample.dashboard);stale.accounts[0].snapshot.equity=99999999;
  old.emit('portfolio',{dashboard:stale});await tick();
  assert.equal(w.document.querySelector('.chart-total').textContent,before);
 }finally{dom.window.close()}
});


test('polling fallback updates the portfolio when streaming is unavailable',async()=>{
 const dashboard=structuredClone(sample.dashboard);
 const{dom,w}=await setup(true,{demo:false},dashboard);
 try{
  dashboard.accounts[0].snapshot.equity=12345.67;dashboard.accounts[0].heartbeat=Date.now()/1000;
  await new Promise(r=>setTimeout(r,1100));
  assert.match(w.document.querySelector('.chart-total').textContent,/12,345.67/);
 }finally{dom.window.close()}
});


test('focusing the account selector never freezes live portfolio values',async()=>{
 const{dom,w}=await setup(true,{demo:false},null,true);
 try{
  const select=w.document.querySelector('#account-select');select.focus();
  const next=structuredClone(sample.dashboard);next.accounts[0].snapshot.equity=15432.10;
  const stream=w.portfolioSources[0];stream.onopen();stream.emit('portfolio',{dashboard:next});await tick();
  assert.equal(w.document.querySelector('#account-select'),select);
  assert.equal(w.document.activeElement,select);
  assert.match(w.document.querySelector('.chart-total').textContent,/15,432.10/);
 }finally{dom.window.close()}
});


test('an open trade dialog stays bound to its original account when live account data changes',async()=>{
 const dashboard=structuredClone(sample.dashboard),originalId=dashboard.accounts[0].id;
 const{dom,w,calls}=await setup(true,{demo:false},dashboard,true);
 try{
  w.location.hash='trades';await tick();w.document.querySelector('[data-action="manage"]').click();await tick();
  const form=w.document.querySelector('#command-form');assert.equal(form.elements.namedItem('account_id').value,originalId);
  const next=structuredClone(dashboard);next.accounts[0].id='replacement-account';
  const stream=w.portfolioSources[0];stream.onopen();stream.emit('portfolio',{dashboard:next});await tick();
  form.elements.namedItem('action').value='close';form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
  assert.ok(calls.some(c=>c.url===`/api/accounts/${originalId}/commands`));
  assert.ok(!calls.some(c=>c.url==='/api/accounts/replacement-account/commands'));
 }finally{dom.window.close()}
});


test('operator configures one read-only trader source instead of retyping trades',async()=>{
 const oldRole=sample.user.role;sample.user.role='admin';
 const dashboard=structuredClone(sample.dashboard);dashboard.accounts[0].purpose='master';
 const{dom,w,calls}=await setup(true,{demo:false},dashboard);
 try{
  const original=w.fetch;const a=dashboard.accounts[0];
  w.fetch=async(url,opt={})=>url==='/api/operator/status'?{ok:true,status:200,json:async()=>({accounts:[{...a,owned:true}],unresolved:[],sources:[],discord_logs:[],analysts:[{id:'direct-feed',name:'Trader source',source_mode:'broker',active:true,channel_id:'123456'}]})}:original(url,opt);
  w.location.hash='operator';await tick();
  assert.match(w.document.querySelector('.content').textContent,/No second signal entry is required/);
  w.document.querySelector('[data-action="source-setup"]').click();await tick();
  const form=w.document.querySelector('#source-form');assert.ok(form);
  assert.equal(form.elements.namedItem('account_id').value,a.id);
  assert.equal(form.elements.namedItem('log_channel_id').value,'123456');
  form.querySelector('#source-consent').checked=true;form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
  const call=calls.find(c=>c.url==='/api/operator/master-sources');assert.ok(call);
  assert.equal(JSON.parse(call.opt.body).confirm_read_only,true);
  assert.equal(JSON.parse(call.opt.body).account_id,a.id);
  assert.ok(!calls.some(c=>c.url==='/api/operator/signals'));
 }finally{sample.user.role=oldRole;dom.window.close()}
});


test('manual tests are clearly optional and CMP template needs no current price',async()=>{
 const oldRole=sample.user.role;sample.user.role='admin';
 const{dom,w}=await setup(true,{demo:false});
 try{
  const original=w.fetch;w.fetch=async(url,opt={})=>url==='/api/operator/status'?{ok:true,status:200,json:async()=>({accounts:[],unresolved:[],analysts:[{id:'discord',name:'Discord feed',source_mode:'discord'}]})}:original(url,opt);
  w.location.hash='operator';await tick();w.document.querySelector('[data-action="signal-tool"]').click();await tick();
  assert.match(w.document.querySelector('#modal').textContent,/Do not enter the same Discord message here/);
  assert.match(w.document.querySelector('#source_id').value,/^manual-/);
  const template=w.document.querySelector('#signal-template');template.value='cmp';template.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(w.document.querySelector('#signal-text').value.split('\n')[0],'BUY XAUUSD CMP');
 }finally{sample.user.role=oldRole;dom.window.close()}
});


test('trader account setup stops requiring a followed analyst',async()=>{
 const oldRole=sample.user.role;sample.user.role='admin';
 const{dom,w}=await setup(true,{demo:false,local_worker_available:true});
 try{
  w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect"]').click();await tick();
  const purpose=w.document.querySelector('#purpose');purpose.value='master';purpose.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(w.document.querySelector('#connect-analyst').required,false);
  assert.equal(w.document.querySelector('#connect-analyst').disabled,true);
 }finally{sample.user.role=oldRole;dom.window.close()}
});
