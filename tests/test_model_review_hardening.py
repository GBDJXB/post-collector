"""独立审查发现：新建议不得伪装历史建议，损坏证据不能绕过确认。"""
import json

import pytest
from test_model_tagging import bound_proposal, proposal_row
from test_model_tagging import model_data as model_data

from pc.domain import ClassificationResult, TagCandidate
from pc.services import model_tagging as mt
from pc.services import tagging


@pytest.mark.parametrize('model', [False, True])
def test_new_suggestions_cannot_mint_snapshotless_legacy_rows(model_data, model):
    conn, ids, _, _ = model_data
    model_id = conn.execute("INSERT INTO model_version(capability,model_name) VALUES ('llm','test') RETURNING model_version_id").fetchone()[0] if model else None
    proposal = ClassificationResult(tags=(TagCandidate('已有甲', .8),), model_version_id=model_id)
    with pytest.raises(ValueError, match='快照'):
        tagging.apply_classification(conn, proposal, content_ids=[ids[0]])
    assert conn.execute('SELECT count(*) FROM classification_run').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM tag_assignment').fetchone()[0] == 0


@pytest.mark.parametrize('evidence', [[], None, {'model_proposal': None}, {'model_proposal': {}}, {'model_proposal': {'run_id': True, 'version': 'bad'}}])
def test_malformed_proposal_proof_is_cleanly_rejected(model_data, evidence):
    conn, *_ = model_data
    access, proposal, args = bound_proposal(model_data)
    tagging.apply_classification(conn, proposal, **args)
    ident, _ = proposal_row(conn)
    conn.execute('UPDATE tag_assignment SET evidence=?', (json.dumps(evidence),))
    expected = evidence.get('model_proposal') if isinstance(evidence, dict) else None
    with pytest.raises(ValueError, match=r'证据|版本'):
        tagging.review_suggestions(conn, [ident], confirm=True, expected_runs={ident: expected}, model_access=access)
    assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 0


def test_sensitive_prepared_data_is_not_exposed_by_debug_repr(model_data):
    conn, ids, *_ = model_data
    prepared = mt.build_input(conn, content_id=ids[0])
    assert '正文一' not in repr(prepared) and 'data' not in repr(prepared)
    catalog = mt.load_catalog(conn)
    assert '已有甲' not in repr(catalog) and 'PRIVATE' not in repr(catalog)
