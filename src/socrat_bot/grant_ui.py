"""Small same-origin operator console. Tokens are never written to browser storage."""


def page(scopes):
    choices = "".join(
        f'<label><input type="checkbox" name="scope" value="{s}"> {s}</label>'
        for s in sorted(scopes)
    )
    return (
        """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Socrat / API</title><style>:root{font:16px/1.5 monospace;color:#151515;background:white}body{max-width:55rem;margin:3rem auto;padding:0 1rem}fieldset,article{margin:1rem 0;padding:1rem;border:1px solid #aaa}label{display:block}button,input{font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>Socrat / API</h1><button type="button" onclick="fetch('/admin/tokens/logout',{method:'POST',headers:{'X-Bot-Admin':'1'}}).then(r=>{if(r.ok)document.body.textContent='Сеанс консоли завершён.'})">Выйти из консоли</button><p>Адрес: <code>/api/v1</code> · Authorization: Bearer TOKEN</p>
<form id="issue"><label>Название <input name="name" required maxlength="80"></label><label>Срок, дней <input name="days" type="number" min="1" max="365" value="30" required></label>
<fieldset><legend>Права</legend>"""
        + choices
        + """</fieldset><label>ID чатов через запятую (для обычной отправки) <input name="chats" title="Целые ID через запятую, например 12,-34" pattern=" *-?[0-9]+ *(, *-?[0-9]+ *)*"></label>
<p>Для внешнего клиента выберите только messages:send и его чаты. Остальные права действуют глобально; messages:advanced разрешает карточки и действия.</p><button>Выдать токен</button></form>
<pre id="result" role="status"></pre><h2>Выданные токены</h2><section id="list"></section>
<script>
const result=document.querySelector('#result'),list=document.querySelector('#list');
async function req(url,options={}){const r=await fetch(url,{...options,headers:{'Content-Type':'application/json','X-Bot-Admin':'1'}});if(!r.ok)throw Error('HTTP '+r.status+' '+await r.text());return r.status===204?null:r.json()}
async function load(){const rows=await req('/admin/tokens');list.replaceChildren();for(const r of rows){const a=document.createElement('article'),b=document.createElement('button');a.textContent=r.name+' · '+r.scopes.join(', ')+' · '+(r.chats.join(', ')||'все чаты')+' · до '+new Date(r.expires*1000).toLocaleDateString()+' ';b.textContent='Отозвать';b.onclick=async()=>{if(!confirm('Отозвать '+r.name+'?'))return;try{await req('/admin/tokens/'+r.id,{method:'DELETE'});result.textContent='Отозван';await load()}catch(e){result.textContent=e.message}};a.append(b);list.append(a)}}
document.querySelector('form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{const r=await req('/admin/tokens',{method:'POST',body:JSON.stringify({name:f.get('name'),days:Number(f.get('days')),scopes:f.getAll('scope'),chats:f.get('chats').split(',').map(x=>x.trim()).filter(Boolean)})});result.textContent='Скопируйте сейчас — повторно токен не показывается:\\n'+r.token;await load().catch(()=>{result.textContent+='\\nНе удалось обновить список.'})}catch(err){result.textContent=err.message}};load().catch(e=>result.textContent=e.message);
</script></html>"""
    )
