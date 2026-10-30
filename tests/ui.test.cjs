const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../goldcopier/web/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../docs/frontend-preview.html'),'utf8');
const sample=JSON.parse(html.match(/if\(path==="\/demo"\) return ([\s\S]*?); if\(path==="\/dashboard"\)/)[1]);
const tick=()=>new Promise(r=>setTimeout(r,25));
async function setup(authenticated=false,configOverrides={},dashboardOverride=null){
 const calls=[];
 const dom=new JSDOM('<div id="app"></div><div id="toast"></div><dialog id="modal"></dialog>',{url:authenticated?'http://localhost/#overview':'http://localhost/#demo',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
 w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.HTMLElement.prototype.scrollIntoView=function(){};
 w.fetch=async(url,opt={})=>{calls.push({url,opt});let body;if(url==='/api/config')body={demo:true,live_enabled:false,billing_enabled:false,billing_ready:false,...configOverrides};else if(url==='/api/demo')body=sample;else if(url==='/api/dashboard')body=dashboardOverride||sample.dashboard;else if(url==='/api/auth/me'&&authenticated)body={user:sample.user,csrf:'csrf-test'};else if(authenticated&&opt.method&&opt.method!=='GET')body={ok:true};else return {ok:false,status:401,json:async()=>({detail:'Sign in'})};return {ok:true,status:200,json:async()=>structuredClone(body)}};
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
