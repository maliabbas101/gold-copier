const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../goldcopier/web/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../docs/frontend-preview.html'),'utf8');
const sample=JSON.parse(html.match(/if\(path==="\/demo"\) return ([\s\S]*?); if\(path==="\/dashboard"\)/)[1]);
const tick=()=>new Promise(r=>setTimeout(r,25));
async function setup(authenticated=false){
 const calls=[];
 const dom=new JSDOM('<div id="app"></div><div id="toast"></div><dialog id="modal"></dialog>',{url:authenticated?'http://localhost/#overview':'http://localhost/#demo',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
 w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.HTMLElement.prototype.scrollIntoView=function(){};
 w.fetch=async(url,opt={})=>{calls.push({url,opt});let body;if(url==='/api/config')body={demo:true,live_enabled:false,billing_enabled:false,billing_ready:false};else if(url==='/api/demo')body=sample;else if(url==='/api/dashboard')body=sample.dashboard;else if(url==='/api/auth/me'&&authenticated)body={user:sample.user,csrf:'csrf-test'};else if(authenticated&&opt.method&&opt.method!=='GET')body={ok:true};else return {ok:false,status:401,json:async()=>({detail:'Sign in'})};return {ok:true,status:200,json:async()=>structuredClone(body)}};
 w.eval(source);await tick();return {dom,w,calls};
}

test('real frontend renders overview, charts, positions and demo disclosure',async()=>{const{dom,w}=await setup();try{assert.match(w.document.querySelector('h1').textContent,/Alex/);assert.match(w.document.body.textContent,/Demo workspace/);assert.ok(w.document.querySelector('svg[aria-label="Equity snapshots for selected account"]'));assert.match(w.document.body.textContent,/XAUUSDm/);assert.equal(w.document.querySelectorAll('tbody tr').length,2)}finally{dom.window.close()}});

test('navigation exposes actual account, risk, trade and billing screens',async()=>{const{dom,w}=await setup();try{for(const[page,selector]of [['accounts','.account-card'],['risk','#risk-form'],['trades','#history-table'],['billing','.plan'],['analysts','.analyst-card'],['activity','.notification']]){w.location.hash=page;await tick();assert.ok(w.document.querySelector(selector),page)}}finally{dom.window.close()}});

test('account wizard opens and public demo cannot mutate an account',async()=>{const{dom,w,calls}=await setup();try{w.location.hash='accounts';await tick();w.document.querySelector('[data-action="connect"]').click();await tick();assert.ok(w.document.querySelector('#modal').open);const f=w.document.querySelector('#connect-form');assert.ok(f);f.querySelector('[name="name"]').value='Demo account';f.querySelector('[name="login"]').value='987654';f.querySelector('[name="server"]').value='Demo';f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.match(w.document.querySelector('#toast').textContent,/read-only/);assert.ok(!calls.some(c=>c.url==='/api/accounts'))}finally{dom.window.close()}});

test('trade command dialog requires explicit review and displays the selected ticket',async()=>{const{dom,w}=await setup();try{w.location.hash='trades';await tick();w.document.querySelector('[data-action="manage"]').click();await tick();assert.ok(w.document.querySelector('#confirm-command').required);assert.match(w.document.querySelector('#modal').textContent,/#7100231/);assert.equal(w.document.querySelector('#command-form [name="position"]').value,'7100231')}finally{dom.window.close()}});

test('user-supplied labels render as text instead of executable markup',async()=>{const orig=sample.user.name;sample.user.name='<img src=x onerror=alert(1)>';const{dom,w}=await setup();try{assert.ok(!w.document.querySelector('img[onerror]'));assert.match(w.document.querySelector('h1').textContent,/<img/)}finally{sample.user.name=orig;dom.window.close()}});


test('authenticated risk form sends validated numeric settings with CSRF token',async()=>{const{dom,w,calls}=await setup(true);try{w.location.hash='risk';await tick();const f=w.document.querySelector('#risk-form');f.querySelector('[name="risk_pct"]').value='0.7';f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();const call=calls.find(c=>c.opt.method==='PUT');assert.ok(call);const body=JSON.parse(call.opt.body);assert.equal(body.risk_pct,.7);assert.equal(body.be_enabled,true);assert.equal(body.respect_manual_stops,true);assert.equal(call.opt.headers['X-CSRF-Token'],'csrf-test');assert.match(w.document.querySelector('#toast').textContent,/saved/)}finally{dom.window.close()}});
