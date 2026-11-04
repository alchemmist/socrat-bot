import asyncio,os
from unittest.mock import patch
os.environ['DATABASE_URL']='sqlite+pysqlite:///./test.db'
from sqlalchemy import delete
from socrat_bot import app
from socrat_bot.db import init_schema,session
from socrat_bot.models import TelegramCard
from socrat_bot.cards import build_alert,render_card


def alert(episode):return {'status':'firing','spi_episode':episode,'labels':{'alertname':'Test','pod':'p','namespace':'n'}}

def test_episode_keys_are_immutable_and_fit_mute_suffix():
    a=alert('a'*64);f=build_alert(a);a['status']='resolved';assert build_alert(a).key==f.key
    assert build_alert(alert('b'*64)).key!=f.key
    assert len(f.key)<100
    assert '/spi/' not in build_alert({'labels':{'alertname':'Legacy'}}).key


def test_partial_failed_copy_is_retried_and_guard_rejects_replacement():
    init_schema();key=build_alert(alert('a'*64)).key
    with session()as db:
        db.execute(delete(TelegramCard))
        for chat in [1,2]:db.add(TelegramCard(key=key,chat_id=chat,message_id=chat*10,text=render_card(build_alert(alert('a'*64)))))
    calls=[];fail={2}
    class Response:
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,json):
            calls.append(json)
            return Response({'ok':False,'description':'temporary failure'}if json['chat_id']in fail else {'ok':True})
    payload=app.CardLinkIn(key=key,revision=1,url='https://github.com/alchemmist/homelab/issues/9#issuecomment-123',expected_copies=[{'chat_id':1,'message_id':10},{'chat_id':2,'message_id':20}])
    async def run():
        with patch.object(app.httpx,'AsyncClient',Client), patch.dict(os.environ,{'INTERNAL_TOKEN':'test-internal','TELEGRAM_BOT_TOKEN':'test-telegram'}):
            first=await app.add_card_link(payload,'test-internal');assert len(first['updated'])==1 and len(first['pending'])==1
            with session()as db:assert db.get(TelegramCard,(key,2)).spi_url is None
            fail.clear();second=await app.add_card_link(payload,'test-internal');assert len(second['updated'])==2 and not second['pending']
            assert len(calls)==4 # cached same-URL success still executes idempotent edit
            newer=payload.model_copy(update={'revision':2,'url':'https://github.com/alchemmist/homelab/issues/9#issuecomment-456'})
            await app.add_card_link(newer,'test-internal')
            try:await app.add_card_link(payload,'test-internal');assert False
            except app.HTTPException as e:assert e.status_code==409
            with session()as db:
                row=db.get(TelegramCard,(key,1));assert row.text.count('>SPI</a>')==1
                row.message_id=999
            try:await app.add_card_link(payload,'test-internal');assert False
            except app.HTTPException as e:assert e.status_code==409
    asyncio.run(run())


def test_latest_spi_target_replaces_old_anchor_keeps_footer():
    old='https://github.com/alchemmist/homelab/issues/9';new=old+'#issuecomment-12'
    text=app.add_link('base\n\n<i>Muted for 15m</i>',old)
    revised=app.replace_spi_link(text,old,'SPI',new,'SPI')
    assert revised.count('>SPI</a>')==1 and new in revised and revised.endswith('<i>Muted for 15m</i>')
