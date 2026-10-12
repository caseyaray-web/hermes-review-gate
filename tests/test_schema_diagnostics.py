"""Unsupported ledgers are preserved, never silently downgraded."""
import json

import pytest

from local_first_review import state


@pytest.mark.parametrize('version', [13, 99])
def test_unsupported_schema_is_actionable_and_read_only(tmp_path, monkeypatch, version):
    path = tmp_path / state.CONFIG_NAME
    document = state._empty_state()
    document.update(version=version, workflow_legacy_accounting='unknown',
                    workflow_verdict_intents={'pending': {'status': 'prepared',
                                                         'evidence': {'opaque': ['retain', 7]}}})
    original = json.dumps(document, indent=3).encode() + b'\n'
    path.write_bytes(original)
    monkeypatch.setattr(state, 'state_path', lambda: path)

    with pytest.raises(ValueError, match=rf'unsupported schema version {version}.*supports.*11.*compatible.*Do not downgrade'):
        state.load_state()
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('version', [13, 99])
def test_mutation_refusal_preserves_ledger_but_may_create_lock(tmp_path, monkeypatch, version):
    path = tmp_path / state.CONFIG_NAME
    document = state._empty_state()
    document.update(version=version, pending_external_receipts={'opaque': ['retain', 7]})
    original = json.dumps(document, indent=3).encode() + b'\n'
    path.write_bytes(original)
    monkeypatch.setattr(state, 'state_path', lambda: path)

    with pytest.raises(ValueError, match=rf'unsupported schema version {version}'):
        with state.locked_state(write=True):
            pytest.fail('unsupported ledger must never reach the mutation body')
    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == {path, path.with_suffix(path.suffix + '.lock')}


def test_v11_unknown_evidence_roundtrip_remains_intact(tmp_path, monkeypatch):
    path = tmp_path / state.CONFIG_NAME
    document = state._empty_state()
    document['retained_diagnostic'] = {'opaque_receipts': [{'result': [1, 'unchanged']}]}
    monkeypatch.setattr(state, 'state_path', lambda: path)
    state.save_state(document)
    assert state.load_state() == document
