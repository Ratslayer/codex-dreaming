"""Merge proposals and publication on synthetic KBs; no live knowledge is modified."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_kb
from kb import digest, entry_snapshot


class MergeWorkflow(unittest.TestCase):
    def setUp(self):
        self.case=test_kb.Workflow();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.kb=self.case.kb
        quotes=['Recover the existing operation ID before retrying a write with an unknown outcome.',
                'A lost write response is not proof of failure: check operation history before retrying.',
                'Keep unresolved code-review discussions open until acceptance evidence exists.']
        self.case.note('s1','\n'.join(quotes))
        rid=self.case.begin();job=self.case.prepare(rid,'session')[0]
        self.kb.record(rid,job['id'],{'lessons':[{'statement':q,'condition':'A write response is lost.' if i<2 else 'Resolving review threads.',
            'evidence':[{'session_id':'s1','source_hash':job['input_hash'],'quote':q}]} for i,q in enumerate(quotes)]})
        job=self.case.prepare(rid,'project')[0];lessons=self.case.state()['sessions']['s1']['lessons']
        self.kb.record(rid,job['id'],{'entries':[{'text':l['statement'],'lesson_ids':[l['id']]} for l in lessons],'reason':'Synthetic scoped candidates.'})
        self.kb.finish(rid)
        self.parent=list(self.case.state()['proposals'].values())[-1]

    def activate(self):
        self.case.approve(self.parent)

    def spec(self, pending=False):
        state=self.case.state()
        spec={'base_hash':state['published']['p1'],'replace_entries':[entry_snapshot(e) for e in self.parent['entries'][:2]],
              'text':'When a write response is lost, recover the existing operation ID and outcome before retrying; do not assume the first write failed.',
              'conditions':'Both originals concern writes with an unknown outcome. Keep operation identity and outcome recovery before any retry.'}
        if pending:spec.update(proposal_id=self.parent['id'],proposal_hash=self.parent['proposal_hash'])
        return spec

    def propose(self, pending=False):
        result=self.kb.merge('p1',self.spec(pending),'fixture:merge-request','Equivalent guidance under the same condition.')
        return self.case.state()['proposals'][result['proposal_id']]

    def test_active_merge_preserves_provenance_and_unrelated_rule(self):
        self.activate();before=self.case.state();session=before['sessions']['s1'];lesson_bytes=self.kb.read(session['folder']+'/lessons.md')
        proposal=self.propose();self.assertEqual(self.case.state()['active'],before['active'])
        merged=next(e for e in proposal['entries'] if e.get('replaces'))
        self.assertEqual(merged['replaces'],self.spec()['replace_entries'])
        expected={lid for e in self.parent['entries'][:2] for lid in e['lesson_ids']}
        self.assertEqual(set(merged['lesson_ids']),expected)
        self.assertEqual(len(merged['evidence']),2)
        self.case.approve(proposal)
        state=self.case.state();active=state['active']['p1']
        self.assertEqual(len(active),2)
        self.assertEqual({e['id'] for e in active},{merged['id'],self.parent['entries'][2]['id']})
        self.assertEqual(state['sessions']['s1']['lessons'],session['lessons'])
        self.assertEqual(self.kb.read(session['folder']+'/lessons.md'),lesson_bytes)
        self.kb.rollback('p1',before['published']['p1'],'fixture:rollback','Restore original entries.')
        self.assertEqual(self.case.state()['active']['p1'],before['active']['p1'])

    def test_pending_originals_remain_pending_until_selected_merge(self):
        proposal=self.propose(pending=True)
        merged=proposal['entries'][0]
        self.assertEqual(self.case.state()['active']['p1'],[])
        revised=self.kb.revise(proposal['id'],proposal['proposal_hash'],{'selected_entry_ids':[merged['id']]},'fixture:publish-selection','Publish the checked merge.')
        self.case.approve(self.case.state()['proposals'][revised['proposal_id']])
        self.assertEqual([e['id'] for e in self.case.state()['active']['p1']],[merged['id']])
        self.assertEqual(self.case.state()['proposals'][self.parent['id']]['status'],'pending')
        self.assertEqual(len(self.case.state()['sessions']['s1']['lessons']),3)

    def test_subset_revision_removes_only_explicit_merge_originals(self):
        self.activate();proposal=self.propose();merged=next(e for e in proposal['entries'] if e.get('replaces'))
        result=self.kb.revise(proposal['id'],proposal['proposal_hash'],{'selected_entry_ids':[merged['id']]},'fixture:subset','Publish this merge only.')
        revised=self.case.state()['proposals'][result['proposal_id']]
        self.case.approve(revised)
        self.assertEqual({e['id'] for e in self.case.state()['active']['p1']},{merged['id'],self.parent['entries'][2]['id']})

    def test_stale_unknown_cross_scope_and_duplicate_originals_rejected(self):
        self.activate();before=self.kb.state_path.read_bytes()
        cases=[]
        spec=self.spec();spec['base_hash']='0'*64;cases.append(('p1',spec))
        spec=self.spec();spec['replace_entries'][0]['text']='Different original wording.';cases.append(('p1',spec))
        spec=self.spec();spec['replace_entries']*=2;cases.append(('p1',spec))
        spec=self.spec();spec['base_hash']=self.case.state()['published']['p2'];cases.append(('p2',spec))
        for scope,spec in cases:
            with self.subTest(scope=scope),self.assertRaises(ValueError):self.kb.merge(scope,spec,'fixture:bad','Invalid merge.')
            self.assertEqual(self.kb.state_path.read_bytes(),before)

    def test_merge_cannot_drop_evidence_keep_originals_or_overlap(self):
        self.activate();proposal=self.propose();merged=next(e for e in proposal['entries'] if e.get('replaces'))
        with self.kb.locked(readonly=True) as state:
            bad=dict(merged,lesson_ids=merged['lesson_ids'][:1],lesson_versions={merged['lesson_ids'][0]:merged['lesson_versions'][merged['lesson_ids'][0]]})
            with self.assertRaisesRegex(ValueError,'union'):self.kb.validate_entries(state,'p1',[bad])
            with self.assertRaisesRegex(ValueError,'coexist'):self.kb.validate_entries(state,'p1',[merged,self.parent['entries'][0]])
            second=dict(merged,id='entry-another-merge')
            with self.assertRaisesRegex(ValueError,'overlap'):self.kb.validate_entries(state,'p1',[merged,second])

    def test_changed_assessment_blocks_publication(self):
        self.activate();proposal=self.propose();active=self.case.state()['active']['p1']
        self.case.note('s1','New feedback withdraws the previous retry rule.')
        rid=self.case.begin();job=self.case.prepare(rid,'session')[0]
        self.kb.record(rid,job['id'],{'lessons':[],'assessments':[{'lesson_id':self.parent['entries'][0]['lesson_ids'][0],'decision':'disputed','reason':'New evidence changes applicability.',
            'evidence':[{'session_id':'s1','source_hash':job['input_hash'],'quote':'New feedback withdraws the previous retry rule.'}]}]})
        self.kb.finish(rid)
        with self.assertRaisesRegex(ValueError,'assessments changed'):self.case.approve(proposal)
        self.assertEqual(self.case.state()['active']['p1'],active)

    def test_repeated_request_and_existing_merge_lineage(self):
        self.activate();spec=self.spec()
        first=self.kb.merge('p1',spec,'fixture:repeat','Same merge.')
        second=self.kb.merge('p1',spec,'fixture:repeat','Same merge.')
        self.assertEqual(first['proposal_id'],second['proposal_id'])
        proposal=self.case.state()['proposals'][first['proposal_id']];self.case.approve(proposal)
        with self.kb.locked(readonly=True) as state:
            minimal=[{k:e[k] for k in ['id','text','lesson_ids','lesson_versions']} for e in state['active']['p1']]
            validated=self.kb.validate_entries(state,'p1',minimal)
            self.assertTrue(next(e for e in validated if e.get('replaces'))['replaces'])

    def test_interrupted_merge_recovers_without_partial_reader_state(self):
        self.activate();proposal=self.propose()
        with patch.object(self.kb,'recover',side_effect=OSError('interruption')):
            with self.assertRaises(OSError):self.case.approve(proposal)
        with self.assertRaisesRegex(ValueError,'recovery pending'):self.kb.context(self.case.base/'project')
        self.kb.status(recover=True)
        self.assertEqual(len(self.case.state()['active']['p1']),2)
        self.assertTrue(any(e.get('replaces') for e in self.case.state()['active']['p1']))

    def test_global_merge_uses_approved_project_evidence(self):
        self.activate()
        with self.kb.locked() as state:
            job={'scope':'global','input_hash':digest(self.kb.scope_input(state,'global')),'base_hash':state['published']['global']}
            writes={}
            self.kb.record_scope(state,job,{'entries':self.parent['entries'][:2],'reason':'Global form of the two approved project rules.',
                'generalization':'The same unknown-write-outcome condition applies across projects.'},writes)
            self.kb.commit(state,writes)
        global_proposal=next(p for p in self.case.state()['proposals'].values() if p['scope']=='global')
        self.case.approve(global_proposal)
        state=self.case.state()
        spec=self.spec();spec['base_hash']=state['published']['global'];spec['replace_entries']=[entry_snapshot(e) for e in state['active']['global']]
        merged=self.kb.merge('global',spec,'fixture:global-merge','Equivalent already-global guidance.')
        self.case.approve(self.case.state()['proposals'][merged['proposal_id']])
        self.assertEqual(len(self.case.state()['active']['global']),1)
        self.assertEqual(len(self.case.state()['active']['p1']),3)


if __name__=='__main__':unittest.main()
