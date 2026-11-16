"""Deterministic renderer checks; fixtures are synthetic and never publish knowledge."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT/'skills/agent-kb-review/scripts/render_checklist.py'
spec = importlib.util.spec_from_file_location('render_checklist', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sample():
    return {'schema_version':2, 'kb_root':'/tmp/synthetic-kb', 'groups':[
        {'project':'Example', 'proposal_id':'proposal-sample', 'proposal_hash':'a'*64,
         'entries':[{'id':'entry-one','title':'Respect the selected scope','text':'Original evidence-supported rule.',
                     'lesson_ids':['lesson-one'], 'lesson_versions':{'lesson-one':'b'*64},
                     'rationale':'Explicit recorded correction.', 'evidence':[{'quote':'Keep the operation within the selected scope.', 'session_id':'session-one','source_hash':'c'*64}]}]}]}


class ChecklistRendering(unittest.TestCase):
    def test_safe_literal_fragment_and_source_paths(self):
        original=sample(); before=copy.deepcopy(original)
        fragment=module.render(original)
        self.assertEqual(original,before)
        self.assertNotIn('__AGENT_KB_REVIEW_DATA__',fragment)
        self.assertNotRegex(fragment,r'<!doctype|<html|<body')
        raw=re.search(r'id="agent-kb-review-data">(.*?)</script>',fragment,re.S)[1]
        data=json.loads(raw)
        self.assertEqual(data['groups'][0]['entries'][0]['evidence'][0]['source_path'],'/tmp/synthetic-kb/.aggregator/sources/'+'c'*64+'.md')
        self.assertEqual(data['review_id'],json.loads(re.search(r'id="agent-kb-review-data">(.*?)</script>',module.render(original),re.S)[1])['review_id'])

    def test_script_injection_is_inert_data(self):
        data=sample();text='</script><script>window.bad=true</script>'
        data['groups'][0]['entries'][0]['text']=text
        fragment=module.render(data)
        self.assertNotIn('<script>window.bad',fragment)
        encoded=re.search(r'id="agent-kb-review-data">(.*?)</script>',fragment,re.S)[1]
        self.assertEqual(json.loads(encoded)['groups'][0]['entries'][0]['text'],text)

    def test_invalid_or_ambiguous_input_rejected(self):
        cases=[]
        d=sample();d['kb_root']='relative';cases.append(d)
        d=sample();d['groups'][0]['proposal_hash']='missing';cases.append(d)
        d=sample();d['groups'][0]['entries']*=2;cases.append(d)
        d=sample();d['groups'][0]['entries'][0]['lesson_versions']={};cases.append(d)
        d=sample();d['groups'][0]['entries'][0]['requested_text']='';cases.append(d)
        d=sample();d['groups'][0]['entries'][0]['evidence'][0]['source_path']='/tmp/outside/source.md';cases.append(d)
        for data in cases:
            with self.subTest(data=data),self.assertRaises(ValueError):module.render(data)

    def test_preview_and_displayed_edits_survive_rendering(self):
        data=sample();data['preview']=True;data['groups'][0]['entries'][0]['requested_text']='Narrower requested behavior.'
        rendered=module.render(data)
        encoded=json.loads(re.search(r'id="agent-kb-review-data">(.*?)</script>',rendered,re.S)[1])
        self.assertTrue(encoded['preview'])
        self.assertEqual(encoded['groups'][0]['entries'][0]['requested_text'],'Narrower requested behavior.')

    def test_merge_metadata_preserves_originals_and_rejects_evidence_loss(self):
        data=sample();entry=data['groups'][0]['entries'][0]
        originals=[{'id':'original-a','text':'First equivalent rule.','lesson_ids':['lesson-one'],'lesson_versions':{'lesson-one':'b'*64}},
                   {'id':'original-b','text':'Second equivalent rule.','lesson_ids':['lesson-two'],'lesson_versions':{'lesson-two':'d'*64}}]
        entry.update(replaces=originals,merge_conditions='Same condition; retain the exceptions.',lesson_ids=['lesson-one','lesson-two'],lesson_versions={'lesson-one':'b'*64,'lesson-two':'d'*64})
        fragment=module.render(data)
        rendered=json.loads(re.search(r'id="agent-kb-review-data">(.*?)</script>',fragment,re.S)[1])
        self.assertEqual(rendered['groups'][0]['entries'][0]['replaces'],originals)
        entry['lesson_ids']=['lesson-one'];entry['lesson_versions']={'lesson-one':'b'*64}
        with self.assertRaisesRegex(ValueError,'retain every original'):module.render(data)


class PublicationContinuity(unittest.TestCase):
    def test_later_project_failure_preserves_earlier_publication(self):
        import test_kb
        case=test_kb.Workflow();case.setUp();self.addCleanup(case.doCleanups)
        case.note('s2','Independent project behavior must stay scoped.')
        rid=case.begin();case.analyze(rid)
        jobs=case.prepare(rid,'project')
        for job in jobs:
            session=next(s for s in case.state()['sessions'].values() if s['project_id']==job['scope'])
            lesson=session['lessons'][0]
            case.kb.record(rid,job['id'],{'entries':[{'text':lesson['statement'],'lesson_ids':[lesson['id']]}],'reason':'Synthetic scoped candidates.'})
        case.kb.finish(rid)
        proposals={p['scope']:p for p in case.state()['proposals'].values()}
        drafts={}
        for scope,parent in proposals.items():
            eid=parent['entries'][0]['id']
            result=case.kb.revise(parent['id'],parent['proposal_hash'],{'selected_entry_ids':[eid]},'submission:two-projects','Publish selected rules.')
            drafts[scope]=case.state()['proposals'][result['proposal_id']]
        case.note('s2','Independent project behavior must stay scoped. New feedback disputes the earlier rule.')
        rid=case.begin();job=case.prepare(rid,'session')[0]
        session=case.state()['sessions']['s2']
        case.kb.record(rid,job['id'],{'lessons':[],'assessments':[{'lesson_id':session['lessons'][0]['id'],'decision':'disputed','reason':'New feedback changed the evidence.',
            'evidence':[{'session_id':'s2','source_hash':job['input_hash'],'quote':'New feedback disputes the earlier rule.'}]}]})
        case.kb.finish(rid)
        case.approve(drafts['p1'])
        with self.assertRaisesRegex(ValueError,'assessments changed'):case.approve(drafts['p2'])
        state=case.state()
        self.assertEqual(state['active']['p1'],drafts['p1']['entries'])
        self.assertEqual(state['active']['p2'],[])
        self.assertEqual(len(state['decisions']),1)
        self.assertEqual(state['proposals'][drafts['p2']['id']]['status'],'pending')


if __name__=='__main__': unittest.main()
