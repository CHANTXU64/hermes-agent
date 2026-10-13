"""Official partial edits retain Fork transaction/audit and complete old entry semantics."""
import json

import pytest
from tools import memory_tool as memory_module
from tools.memory_tool import MemoryStore, memory_tool, apply_memory_pending
from tools.memory_tool_store import MemoryStoreTransaction


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memory_module, 'get_memory_dir', lambda: tmp_path)
    result = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
    result.load_from_disk()
    assert result.add('memory', 'Project Alpha uses Python; reports stay Chinese.')['success']
    return result


def records(tmp_path):
    return [r for line in (tmp_path/'MEMORY_CHANGELOG.jsonl').read_text().splitlines()
            if (r := json.loads(line))['event_type'] == 'change']


def operation(**kw):
    return dict(action='patch', old_text='Python', content='Rust', reason='Update project language', evidence='Explicit fixture correction', **kw)


@pytest.mark.parametrize('batch',[False,True])
def test_partial_edit_journals_full_before_and_after_without_losing_other_facts(store,tmp_path,batch):
    result = json.loads(memory_tool(store=store, **({'operations':[operation()]} if batch else operation())))
    assert result['success'], result
    before='Project Alpha uses Python; reports stay Chinese.'
    after='Project Alpha uses Rust; reports stay Chinese.'
    assert store.read_target_entries_checked('memory') == ([after],True)
    assert [(r['action'],r['before'],r['after']) for r in records(tmp_path)] == [('patch',before,after)]
    assert json.loads(memory_tool(action='history',old_text='Alpha',store=store))['success']


@pytest.mark.parametrize('batch',[False,True])
def test_corrupt_patch_receipt_rolls_back_data_and_does_not_journal(store,tmp_path,monkeypatch,batch):
    apply=MemoryStoreTransaction.apply
    def corrupt(self,ops,**kw):
        result=apply(self,ops,**kw)
        result.pop('patched_entries_after' if batch else 'patched_entry_after',None)
        return result
    monkeypatch.setattr(MemoryStoreTransaction,'apply',corrupt)
    result=json.loads(memory_tool(store=store,**({'operations':[operation()]} if batch else operation())))
    assert result['success'] is False and 'rolled back' in result['error']
    assert store.read_target_entries_checked('memory') == (['Project Alpha uses Python; reports stay Chinese.'],True)
    assert records(tmp_path)==[]


def test_approved_partial_edit_preserves_pinned_entry_and_audit(store,tmp_path):
    result=apply_memory_pending(dict(target='memory',matched_entry='Project Alpha uses Python; reports stay Chinese.',**operation()),store)
    assert result['success'],result
    assert records(tmp_path)[0]['after']=='Project Alpha uses Rust; reports stay Chinese.'


def test_partial_edit_requires_same_evidence_as_legacy_replace(store):
    result=json.loads(memory_tool(action='patch',old_text='Python',content='Rust',store=store))
    assert result['success'] is False and 'reason' in result['error']


def test_schema_keeps_legacy_actions_and_adds_partial_edits():
    props=memory_module.MEMORY_SCHEMA['parameters']['properties']
    assert {'add','replace','patch','remove','history'} <= set(props['action']['enum'])
    assert {'add','replace','patch','remove'} <= set(props['operations']['items']['properties']['action']['enum'])
