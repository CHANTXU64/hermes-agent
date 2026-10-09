"""Quick-command session identity must use the real multiplex routing store."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionStore, SessionSource
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture
def routing(tmp_path, monkeypatch):
    import hermes_state
    root=tmp_path/'hermes'
    root.mkdir()
    (root/'config.yaml').write_text('{}\n', encoding='utf-8')
    homes={'default':root}
    for name in ['work','review']:
        homes[name]=root/'profiles'/name
        homes[name].mkdir(parents=True)
        (homes[name]/'config.yaml').write_text('{}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME',str(root))
    monkeypatch.setattr(hermes_state,'DEFAULT_DB_PATH',hermes_state._IMPORT_DEFAULT_DB_PATH)
    token=set_hermes_home_override(root)
    config=GatewayConfig(multiplex_profiles=True)
    store=SessionStore(root/'sessions',config)
    runner=GatewayRunner.__new__(GatewayRunner)
    runner.config=config
    runner.session_store=store
    runner._running_agents={}
    runner._pending_messages={}
    runner._check_slash_access=lambda source,canonical_cmd:None
    command='printf \'{"session_id":"%s","home":"%s"}\' "$HERMES_SESSION_ID" "$HERMES_HOME"'
    qcmd={'type':'exec','session_env':True,'command':command}
    runner._hm_quick_commands=lambda:{'save-probe':qcmd}
    try:
        yield runner,store,homes
    finally:
        reset_hermes_home_override(token)
        for db in store._db_handles.values():
            if db is not None: db.close()


def source(profile, **kwargs):
    fields=dict(platform=Platform.TELEGRAM,chat_id='same-human',
                user_id='same-human',chat_type='dm',profile=profile)
    fields.update(kwargs)
    return SessionSource(**fields)


@pytest.mark.asyncio
async def test_shared_chat_id_profiles_use_their_own_durable_session(routing):
    runner,store,homes=routing
    sources=[source(name) for name in homes]
    expected={}
    for src in sources:
        with _profile_runtime_scope(homes[src.profile],{}):
            expected[src.profile]=store.get_or_create_session(src).session_id
    assert len(set(expected.values()))==len(sources)

    async def invoke(src):
        with _profile_runtime_scope(homes[src.profile],{}):
            event=MessageEvent(text='/save-probe',message_type=MessageType.TEXT,source=src)
            handled,result,command=await runner._hm_dispatch_quick_and_plugin_commands(event,src,'save-probe')
            assert handled and command=='save-probe'
            return json.loads(result)

    results=await asyncio.gather(*(invoke(src) for src in sources))
    for src,result in zip(sources,results):
        assert result=={'session_id':expected[src.profile],'home':str(homes[src.profile])}


@pytest.mark.asyncio
async def test_missing_secondary_mapping_never_falls_back_to_default(routing, monkeypatch):
    runner,store,homes=routing
    with _profile_runtime_scope(homes['default'],{}):
        store.get_or_create_session(source('default'))
    before=set(store._entries)
    async def forbidden(*args,**kwargs):
        pytest.fail('No command may be spawned for an unmapped profile')
    monkeypatch.setattr(asyncio,'create_subprocess_shell',forbidden)
    src=source('work')
    with _profile_runtime_scope(homes['work'],{}):
        event=MessageEvent(text='/save-probe',message_type=MessageType.TEXT,source=src)
        handled,result,_=await runner._hm_dispatch_quick_and_plugin_commands(event,src,'save-probe')
    assert handled and 'requires an active Hermes session' in result
    assert set(store._entries)==before


@pytest.mark.asyncio
@pytest.mark.parametrize('group_per_user,thread_per_user,thread_id',[(False,False,None),(True,True,'topic')])
async def test_quick_command_respects_store_group_and_thread_policy(routing,group_per_user,thread_per_user,thread_id):
    runner,store,homes=routing
    store.config.group_sessions_per_user=group_per_user
    store.config.thread_sessions_per_user=thread_per_user
    src=source('work',chat_type='group',chat_id='shared-group',thread_id=thread_id)
    with _profile_runtime_scope(homes['work'],{}):
        expected=store.get_or_create_session(src).session_id
        qcmd=runner._hm_quick_commands()['save-probe']
        result=await runner._hm_run_exec_quick_command('save-probe',qcmd['command'],qcmd,src)
    assert json.loads(result)['session_id']==expected


@pytest.mark.asyncio
async def test_async_peek_fallback_keeps_profile_namespace(routing, monkeypatch):
    from gateway.session import AsyncSessionStore
    runner,store,homes=routing
    src=source('work')
    with _profile_runtime_scope(homes['default'],{}):
        store.get_or_create_session(source('default'))
    with _profile_runtime_scope(homes['work'],{}):
        expected=store.get_or_create_session(src).session_id
        monkeypatch.setattr(GatewayRunner, "async_session_store", property(lambda _: AsyncSessionStore(store)))
        runner.session_store=SimpleNamespace(_generate_session_key=store._generate_session_key)
        qcmd=runner._hm_quick_commands()['save-probe']
        result=await runner._hm_run_exec_quick_command('save-probe',qcmd['command'],qcmd,src)
    assert json.loads(result)['session_id']==expected
