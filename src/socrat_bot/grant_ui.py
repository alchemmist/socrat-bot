"""Small same-origin operator console. Tokens are never written to browser storage."""


from base64 import b64encode
from pathlib import Path


_FAVICON = b64encode(Path(__file__).with_name("favicon.png").read_bytes()).decode("ascii")

SCOPE_HELP = {
    "messages:send": "Send plain text to any chat the bot can message.",
    "messages:advanced": "With messages:send, send cards, buttons and formatted messages.",
    "messages:edit": "Edit existing messages sent by the bot.",
    "cards:read": "List cards and read their details.",
    "cards:link": "Attach links to cards.",
    "cards:note": "Add notes to cards.",
    "audience:read": "View the bot’s audience.",
    "links:issue": "Create link codes.",
    "broadcasts:send": "Send a broadcast to multiple recipients.",
    "actions:read": "Look up an action by its ID.",
}


def page(scopes):
    choices = "".join(
        f'<div class="permission"><label><input type="checkbox" name="scope" value="{s}"> {s}</label>'
        f'<button type="button" class="help" aria-label="About {s}" aria-describedby="tip-{s}">?</button>'
        f'<span role="tooltip" id="tip-{s}">{SCOPE_HELP[s]}</span></div>'
        for s in sorted(scopes)
    )
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="data:image/png;base64,""" + _FAVICON + """">
<title>Socrat Bot / API</title><style>html::-webkit-scrollbar{display:none}:root{scrollbar-width:none;font:16px/1.5 monospace;color:#151515;background:white}body{max-width:47rem;margin:3rem auto;padding:0 1rem 3rem}fieldset,article{margin:1rem 0;padding:1rem;border:1px solid #aaa}label{display:block}button,input{font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere}footer{position:fixed;right:max(1.5rem,env(safe-area-inset-right));bottom:max(1.5rem,env(safe-area-inset-bottom));background:white}footer button{background:none;border:0;padding:0;color:#707070;text-decoration:underline;cursor:pointer}footer button:hover,footer button:focus-visible{color:#222}
.permission{display:flex;align-items:center;gap:.45rem;position:relative}
.help{border:0;border-radius:50%;background:#eee;color:#646464;width:1rem;height:1rem;padding:0;font:11px/1 monospace;cursor:help}
[role=tooltip]{display:none;position:absolute;z-index:1;left:0;bottom:100%;max-width:min(30ch,100%);padding:.35rem .55rem;background:#fff;border:1px solid #aaa;font-size:.85rem}
.help:hover+[role=tooltip],.help:focus+[role=tooltip]{display:block}
.columns{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem;align-items:start;margin-bottom:1rem}.columns fieldset{min-width:0;margin:0}.settings{padding-top:.65rem}.settings label+label{margin-top:.5rem}.settings input{max-width:100%;box-sizing:border-box}.settings select{font:inherit}[hidden]{display:none}article{padding:.55rem .75rem;margin:.5rem 0;overflow-wrap:anywhere}.badge{position:relative}.badge button{font:inherit;background:#eee;border:0;border-radius:3px;padding:0 .3em;cursor:help}.badge [role=tooltip]{width:max-content;max-width:20ch;white-space:pre-line}.badge:hover [role=tooltip],.badge:focus-within [role=tooltip]{display:block}@media(max-width:38rem){.columns{grid-template-columns:1fr}.settings{padding:0}}
#custom[hidden]{display:block;visibility:hidden}.settings>button{margin-top:.65rem}
</style>
<h1>Socrat Bot / API</h1><p>API endpoint: <code>/api/v1</code> · Authorization: Bearer TOKEN</p>
<form id="issue"><div class="columns"><fieldset><legend>Permissions</legend>"""
        + choices
        + """</fieldset><div class="settings"><label>Token name <input name="name" required maxlength="80"></label>
<label>Expires in <select name="expiry"><option value="1">1 day</option><option value="30" selected>30 days</option><option value="90">90 days</option><option value="custom">Custom</option><option value="never">Infinity</option></select></label>
<label id="custom" hidden>Days <input name="days" type="number" min="1" max="365" required disabled></label>
<button>Create token</button></div></div></form>
<pre id="result" role="status"></pre><h2>Active tokens</h2><section id="list"></section>
<footer><button type="button" onclick="fetch('/admin/tokens/logout',{method:'POST',headers:{'X-Bot-Admin':'1'}}).then(r=>{if(r.ok)document.body.textContent='You have signed out of the console.'})">Sign out</button></footer>
<script>
const result=document.querySelector('#result'),list=document.querySelector('#list');
async function req(url,options={}){const r=await fetch(url,{...options,headers:{'Content-Type':'application/json','X-Bot-Admin':'1'}});if(!r.ok)throw Error('HTTP '+r.status+' '+await r.text());return r.status===204?null:r.json()}
async function load(){const rows=await req('/admin/tokens');list.replaceChildren();for(const r of rows){const a=document.createElement('article'),b=document.createElement('button');if(r.expires && r.expires*1000<=Date.now())continue;
const badge=document.createElement('span'),trigger=document.createElement('button'),tip=document.createElement('span');
badge.className='badge';trigger.type='button';trigger.textContent='permissions';tip.id='permissions-'+r.id;trigger.setAttribute('aria-describedby',tip.id);tip.setAttribute('role','tooltip');tip.textContent=r.scopes.join('\\n');badge.append(trigger,tip);
const expiry=r.expires?'expires '+new Date(r.expires*1000).toLocaleDateString('en',{year:'numeric',month:'short',day:'numeric'}):'never expires';a.append(r.name+' · ',badge,' · '+expiry+' ');b.textContent='Revoke';b.onclick=async()=>{if(!confirm('Revoke '+r.name+'?'))return;try{await req('/admin/tokens/'+r.id,{method:'DELETE'});result.textContent='Token revoked.';await load()}catch(e){result.textContent=e.message}};a.append(b);list.append(a)}}
const expirySelect=document.querySelector('[name=expiry]'),custom=document.querySelector('#custom');
expirySelect.onchange=()=>{custom.hidden=expirySelect.value!=='custom';custom.querySelector('input').disabled=custom.hidden};
document.querySelector('form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{const r=await req('/admin/tokens',{method:'POST',body:JSON.stringify({name:f.get('name'),days:f.get('expiry')==='never'?null:Number(f.get('expiry')==='custom'?f.get('days'):f.get('expiry')),scopes:f.getAll('scope')})});result.textContent='Copy this token now. It will not be shown again:\\n'+r.token;await load().catch(()=>{result.textContent+='\\nCould not refresh the token list.'})}catch(err){result.textContent=err.message}};load().catch(e=>result.textContent=e.message);
</script></html>"""
    )
