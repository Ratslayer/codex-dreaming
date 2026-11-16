import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime

from kb import KB, EMPTY_KB, digest, mask, ZONE


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "notes"
        self.source.mkdir()
        self.kb = KB(self.base / "kb")
        self.kb.init(self.source)
        self.inventory = {"projects": [{"projectKind": "local", "projectId": "p1", "path": str(self.base / "project"), "label": "Project"},
                                       {"projectKind": "local", "projectId": "p2", "path": str(self.base / "other"), "label": "Other"}],
                          "threads": [{"id": "s1", "projectId": "p1", "cwd": str(self.base / "project")},
                                      {"id": "s2", "projectId": "p2", "cwd": str(self.base / "other")}]}
        self.note("s1", "Do not ask permission again for work already authorized.")

    def state(self):
        return json.loads(self.kb.state_path.read_text())

    def note(self, sid, body, date="2099-01-01"):
        p = self.source / (sid + ".md")
        p.write_text(f"---\ntitle: Example\nsession_id: {sid}\ndate: {date}\n---\n# Example\n\n{body}\n")
        os.utime(p, (time.time() - 600, time.time() - 600))

    def begin(self):
        return self.kb.begin(self.inventory)["run_id"]

    def prepare(self, rid, stage):
        prepared = self.kb.prepare(rid, stage)
        return [json.loads(Path(j["path"]).read_text()) for j in prepared["jobs"]]

    def analyze(self, rid, sid="s1", statement="Respect previously granted authorization."):
        jobs = self.prepare(rid, "session")
        for job in jobs:
            source_sid = job["session_id"]
            state = self.state()
            evidence = [{"session_id": source_sid, "source_hash": state["sessions"][source_sid]["source_hash"],
                         "quote": "Do not ask permission again" if source_sid == "s1" else "Independent project behavior"}]
            self.kb.record(rid, job["id"], {"lessons": [{"statement": statement if source_sid == sid else "Keep this other project behavior scoped.",
                "condition": "The user already authorized the action.", "evidence": evidence}], "metrics": {}})
        return jobs

    def propose(self, rid, scope="p1", text="Respect previously granted authorization."):
        jobs = self.prepare(rid, "project")
        for job in jobs:
            if job["scope"] == scope:
                sid = next(s["id"] for s in self.state()["sessions"].values() if s["project_id"] == scope)
                lid = self.state()["sessions"][sid]["lessons"][0]["id"]
                self.kb.record(rid, job["id"], {"entries": [{"text": text, "lesson_ids": [lid]}], "reason": "Explicit user correction."})
            else:
                self.kb.record(rid, job["id"], {"no_change": True, "reason": "No relevant changes."})
        return list(self.state()["proposals"].values())[-1]

    def approve(self, proposal):
        return self.kb.decision(proposal["id"], "approve", proposal["proposal_hash"], "chat:user-turn-1", "Accept this exact proposal.")

    def test_full_review_gate_and_project_loading(self):
        rid = self.begin()
        self.analyze(rid)
        p = self.propose(rid)
        self.assertEqual(self.kb.read("projects/project/knowledge-base.md"), EMPTY_KB)
        self.assertNotIn("Respect previously", self.kb.context(self.base / "project"))
        self.kb.finish(rid)
        self.approve(p)
        self.assertIn("Respect previously", self.kb.context(self.base / "project" / "nested"))
        self.assertNotIn("Respect previously", self.kb.context(self.base / "other"))
        self.assertNotIn("Respect previously", self.kb.context(self.base / "project-sibling"))
        self.assertIn('"status": "approved"', self.kb.read(self.state()["sessions"]["s1"]["folder"] + "/assessments.md"))

    def test_rejection_persists_and_unchanged_input_skips(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid)
        self.kb.decision(p["id"], "reject", p["proposal_hash"], "chat:user-turn-2", "Too broad.")
        rid = self.begin()
        self.assertEqual(self.prepare(rid, "session"), [])
        jobs = self.prepare(rid, "project")
        lid = self.state()["sessions"]["s1"]["lessons"][0]["id"]
        self.kb.record(rid, jobs[0]["id"], {"entries": [{"text": "Respect previously granted authorization.", "lesson_ids": [lid]}], "reason": "Same proposal."})
        self.assertEqual(len(self.state()["proposals"]), 1)
        self.assertEqual(self.prepare(rid, "project"), [])

    def test_wrong_hash_and_missing_user_reference_are_rejected(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid)
        with self.assertRaises(ValueError):
            self.kb.decision(p["id"], "approve", "wrong", "chat:1", "yes")
        with self.assertRaises(ValueError):
            self.kb.decision(p["id"], "approve", p["proposal_hash"], "", "yes")
        self.assertEqual(self.kb.read("projects/project/knowledge-base.md"), EMPTY_KB)

    def test_source_change_mid_analysis_requires_retry(self):
        rid = self.begin(); jobs = self.prepare(rid, "session")
        self.note("s1", "The source changed while the job was running.")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.kb.record(rid, jobs[0]["id"], {"lessons": []})
        self.assertNotIn("processed_hash", self.state()["sessions"]["s1"])

    def test_lesson_update_preserves_id_and_makes_approval_stale(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid)
        old = self.state()["sessions"]["s1"]["lessons"][0]
        self.note("s1", "Do not ask permission again for work already authorized. Exception: shared branches.")
        rid = self.begin(); jobs = self.prepare(rid, "session")
        j = jobs[0]
        self.kb.record(rid, j["id"], {"lessons": [{"id": old["id"], "statement": "Respect authorization, subject to explicit shared-branch requirements.",
            "condition": "Prior authorization exists.", "evidence": [{"session_id": "s1", "source_hash": j["input_hash"], "quote": "Exception: shared branches."}]}]})
        self.assertEqual(self.state()["sessions"]["s1"]["lessons"][0]["id"], old["id"])
        self.assertTrue(self.kb.path(f".aggregator/lesson-revisions/{old['version']}.json").exists())
        with self.assertRaisesRegex(ValueError, "supporting lesson changed"):
            self.approve(p)

    def test_scope_hash_prevents_stale_second_approval(self):
        rid = self.begin(); self.analyze(rid); p1 = self.propose(rid); self.kb.finish(rid)
        self.note("s1", "Do not ask permission again. Additional evidence for this correction.")
        rid = self.begin(); self.analyze(rid); p2 = self.propose(rid, text="Use existing authorization before requesting another confirmation."); self.kb.finish(rid)
        # Both proposed changes share the same untouched active KB revision.
        self.approve(p2)
        with self.assertRaisesRegex(ValueError, "Stale approval|already decided"):
            self.approve(p1)

    def test_unassigned_old_and_aggregator_sessions_are_excluded(self):
        self.note("orphan", "Unknown project session.")
        self.note("old", "Too old.", "2000-01-01")
        self.note("self", "Do not learn from the aggregator.")
        self.inventory["threads"].append({"id": "self", "cwd": str(self.kb.root), "projectId": "self-project"})
        rid = self.begin(); jobs = self.prepare(rid, "session")
        self.assertEqual([j["session_id"] for j in jobs], ["s1"])
        self.assertIn("orphan", self.kb.read("review/unassigned.md"))
        self.assertNotIn("self", self.state()["sessions"])

    def test_cross_project_lessons_cannot_be_promoted(self):
        self.note("s2", "Independent project behavior must stay scoped.")
        rid = self.begin(); self.analyze(rid)
        jobs = self.prepare(rid, "project")
        lid = self.state()["sessions"]["s2"]["lessons"][0]["id"]
        job = next(j for j in jobs if j["scope"] == "p1")
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            self.kb.record(rid, job["id"], {"entries": [{"text": "Wrong project guidance.", "lesson_ids": [lid]}], "reason": "Should fail."})

    def test_fabricated_quote_or_misattributed_source_is_rejected(self):
        self.note("s2", "Independent project behavior must stay scoped.")
        rid = self.begin(); jobs = self.prepare(rid, "session")
        job = next(j for j in jobs if j["session_id"] == "s1")
        for sha, quote in [(job["input_hash"], "This quote was never in the source."),
                           (self.state()["sessions"]["s2"]["source_hash"], "Independent project behavior")]:
            with self.assertRaises(ValueError):
                self.kb.record(rid, job["id"], {"lessons": [{"statement": "Unsupported inference.", "condition": "Always", "evidence": [{"session_id": "s1", "source_hash": sha, "quote": quote}]}]})

    def test_original_excerpts_exclude_reasoning_and_keep_truncation(self):
        rid = self.begin(); job = self.prepare(rid, "session")[0]
        record = self.kb.excerpt("s1", {"thread_id": "s1", "items": [
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "userMessage", "id": "m1", "turn_id": "t1", "text": "Explicit correction from the user.", "truncated": False},
            {"type": "commandExecution", "id": "m2", "turn_id": "t1", "text": "Partial output only.", "truncated": True}]})
        text = Path(record["path"]).read_text()
        self.assertNotIn("private reasoning", text)
        self.assertIn("truncated=True", text)
        self.kb.record(rid, job["id"], {"lessons": [], "metrics": {"user_corrections": {"kind": "observed", "value": 1, "evidence": [{"session_id": "s1", "source_hash": record["source_hash"], "quote": "Explicit correction from the user."}]}}})

    def test_busy_lease_and_abandon_preserve_pending_inputs(self):
        rid = self.begin()
        self.assertEqual(self.kb.begin(self.inventory)["status"], "busy")
        self.prepare(rid, "session")
        with self.assertRaisesRegex(ValueError, "Pending jobs"):
            self.kb.finish(rid)
        self.kb.finish(rid, abandon=True)
        rid = self.begin()
        self.assertEqual(len(self.prepare(rid, "session")), 1)

    def test_transaction_recovers_and_readers_refuse_partial_state(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid)
        original = self.kb.recover
        with patch.object(self.kb, "recover", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.approve(p)
        with self.assertRaisesRegex(ValueError, "recovery pending"):
            self.kb.context(self.base / "project")
        self.kb.status(recover=True)
        self.assertEqual(self.state()["proposals"][p["id"]]["status"], "approved")
        self.assertIn("Respect previously", self.kb.context(self.base / "project"))
        self.assertFalse(self.kb.path(".aggregator/transaction.json").exists())

    def test_pending_invalidation_keeps_active_guidance(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid); self.approve(p)
        lid = self.state()["sessions"]["s1"]["lessons"][0]["id"]
        self.note("s1", "Do not ask permission again. Later evidence disproves this lesson.")
        rid = self.begin(); j = self.prepare(rid, "session")[0]
        self.kb.record(rid, j["id"], {"lessons": [], "assessments": [{"lesson_id": lid, "decision": "invalidated", "reason": "Later contradictory evidence.",
            "evidence": [{"session_id": "s1", "source_hash": j["input_hash"], "quote": "Later evidence disproves this lesson."}]}]})
        self.assertIn("Respect previously", self.kb.context(self.base / "project"))
        jobs = self.prepare(rid, "project")
        self.kb.record(rid, jobs[0]["id"], {"entries": [], "reason": "Withdraw the disproved lesson; requires user approval."})
        removal = list(self.state()["proposals"].values())[-1]
        self.assertIn("Respect previously", self.kb.context(self.base / "project"))
        self.kb.finish(rid); self.approve(removal)
        self.assertNotIn("Respect previously", self.kb.context(self.base / "project"))

    def test_rollback_requires_explicit_review_reference(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid); self.approve(p)
        self.kb.rollback("p1", digest(EMPTY_KB), "chat:user-rollback", "Restore empty KB.")
        self.assertEqual(self.kb.read("projects/project/knowledge-base.md"), EMPTY_KB)

    def test_external_edit_and_path_escape_are_detected(self):
        rid = self.begin(); self.analyze(rid); self.kb.finish(rid)
        self.kb.path("projects/project/knowledge-base.md").write_text("Unreviewed external edit")
        with self.assertRaisesRegex(ValueError, "outside publication"):
            self.kb.context(self.base / "project")
        with self.assertRaises(ValueError): self.kb.path("../escape")
        (self.kb.root / "escape-link").symlink_to(self.base / "notes", target_is_directory=True)
        with self.assertRaises(ValueError): self.kb.path("escape-link/out.md")

    def test_masking_is_idempotent(self):
        raw = "https://example.test/a?token=secret Bearer abcdefghijklmno"
        self.assertEqual(mask(mask(raw)), mask(raw))
        self.assertNotIn("secret", mask(raw))

    def test_global_requires_approved_project_evidence_and_has_daily_gate(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid)
        with patch("kb.datetime") as clock:
            clock.now.return_value = datetime(2030, 1, 1, 10, tzinfo=ZONE)
            rid = self.begin()
            self.assertEqual(self.prepare(rid, "global"), [])
            self.kb.finish(rid)
        self.approve(p)
        with patch("kb.datetime") as clock:
            clock.now.return_value = datetime(2030, 1, 2, 10, tzinfo=ZONE)
            rid = self.begin(); job = self.prepare(rid, "global")[0]
            lid = self.state()["sessions"]["s1"]["lessons"][0]["id"]
            with self.assertRaisesRegex(ValueError, "generalization"):
                self.kb.record(rid, job["id"], {"entries": [{"text": "Use existing authorization across projects.", "lesson_ids": [lid]}], "reason": "User preference."})
            self.kb.record(rid, job["id"], {"entries": [{"text": "Use existing authorization across projects.", "lesson_ids": [lid]}], "reason": "Explicit user preference.", "generalization": "The preference concerns all authorized agent work."})
            self.assertEqual(self.prepare(rid, "global"), [])
            self.kb.finish(rid)
        gp = list(self.state()["proposals"].values())[-1]
        self.approve(gp)
        self.assertIn("across projects", self.kb.context(self.base / "unknown"))

    def test_bounded_batch_and_quiet_period(self):
        for n in range(12):
            sid = "batch" + str(n)
            self.note(sid, "Independent material for this session.")
            self.inventory["threads"].append({"id": sid, "projectId": "p1", "cwd": str(self.base / "project")})
        hot = self.source / "hot.md"
        self.note("hot", "Still being updated.")
        os.utime(hot, None)
        self.inventory["threads"].append({"id": "hot", "projectId": "p1", "cwd": str(self.base / "project")})
        rid = self.begin(); jobs = self.prepare(rid, "session")
        self.assertEqual(len(jobs), 10)
        self.assertNotIn("hot", [j["session_id"] for j in jobs])
        self.assertEqual(self.state()["backlog"], 3)

    def test_reader_install_preserves_configuration_and_is_idempotent(self):
        from install_reader import install, START
        agents = self.base/'global-AGENTS.md'
        config = self.base/'config.toml'
        agents.write_text('Existing global instructions.\n')
        config.write_text('model = "example"\n[projects.example]\ntrust_level = "trusted"\n')
        install(agents, config, self.kb.root)
        first = agents.read_text(), config.read_text()
        install(agents, config, self.kb.root)
        self.assertEqual(first, (agents.read_text(), config.read_text()))
        self.assertEqual(agents.read_text().count(START), 1)
        self.assertIn('Existing global instructions.', agents.read_text())
        self.assertIn('[projects.example]', config.read_text())
        self.assertIn('project_doc_max_bytes = 65536', config.read_text())

    def test_new_assessment_invalidates_an_unreviewed_proposal(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid)
        lid = self.state()["sessions"]["s1"]["lessons"][0]["id"]
        self.note("s1", "Do not ask permission again. New contradictory evidence arrived.")
        rid = self.begin(); j = self.prepare(rid, "session")[0]
        self.kb.record(rid, j["id"], {"lessons": [], "assessments": [{"lesson_id": lid, "decision": "disputed", "reason": "New contradictory evidence.",
            "evidence": [{"session_id": "s1", "source_hash": j["input_hash"], "quote": "New contradictory evidence arrived."}]}]})
        with self.assertRaisesRegex(ValueError, "assessments changed"):
            self.approve(p)

    def test_tampered_proposal_or_review_cannot_be_approved(self):
        rid = self.begin(); self.analyze(rid); p = self.propose(rid); self.kb.finish(rid)
        md = self.kb.path(f"review/proposals/{p['id']}.md")
        original = md.read_text(); md.write_text(original + "Different review text")
        with self.assertRaisesRegex(ValueError, "presentation was modified"):
            self.approve(p)
        md.write_text(original)
        file = self.kb.path(f"review/proposals/{p['id']}.json")
        record = json.loads(file.read_text()); record['reason'] = 'Tampered rationale'; file.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "record was modified"):
            self.approve(p)

    def test_legacy_chat_without_project_id_maps_by_registered_cwd(self):
        self.inventory['threads'][0]['projectId'] = None
        self.inventory['threads'][0]['cwd'] = str(self.base/'project'/'nested')
        self.note('sibling', 'A similarly named directory is not the same project.')
        self.inventory['threads'].append({'id':'sibling','projectId':None,'cwd':str(self.base/'project-sibling')})
        rid = self.begin(); jobs = self.prepare(rid, 'session')
        self.assertEqual([j['session_id'] for j in jobs], ['s1'])
        self.assertEqual(jobs[0]['project_id'], 'p1')
        self.assertIn('sibling', self.kb.read('review/unassigned.md'))

    def test_revision_creates_a_new_draft_without_publication(self):
        rid = self.begin(); self.analyze(rid); parent = self.propose(rid); self.kb.finish(rid)
        eid = parent['entries'][0]['id']
        selection = {'selected_entry_ids':[eid], 'edits':{eid:'Respect authorization within the explicitly approved scope.'}}
        result = self.kb.revise(parent['id'], parent['proposal_hash'], selection, 'fixture:selection', 'Clarify the selected rule.')
        revised = self.state()['proposals'][result['proposal_id']]
        self.assertEqual(revised['status'], 'pending')
        self.assertEqual(revised['entries'][0]['id'], eid)
        self.assertIn('explicitly approved scope', revised['entries'][0]['text'])
        self.assertEqual(self.state()['proposals'][parent['id']]['status'], 'superseded')
        self.assertEqual(self.kb.read('projects/project/knowledge-base.md'), EMPTY_KB)
        repeated = self.kb.revise(parent['id'], parent['proposal_hash'], selection, 'fixture:selection', 'Clarify the selected rule.')
        self.assertEqual(repeated['proposal_id'], result['proposal_id'])
        self.approve(revised)
        self.assertIn('explicitly approved scope', self.kb.context(self.base/'project'))

    def test_subset_revision_keeps_other_entries_pending(self):
        rid = self.begin(); self.analyze(rid)
        job = self.prepare(rid, 'project')[0]
        lid = self.state()['sessions']['s1']['lessons'][0]['id']
        self.kb.record(rid, job['id'], {'entries':[{'text':'First supported candidate rule.', 'lesson_ids':[lid]}, {'text':'Second supported candidate rule.', 'lesson_ids':[lid]}], 'reason':'Two candidates.'})
        self.kb.finish(rid)
        parent = list(self.state()['proposals'].values())[-1]
        keep, defer = [e['id'] for e in parent['entries']]
        result = self.kb.revise(parent['id'], parent['proposal_hash'], {'selected_entry_ids':[keep]}, 'fixture:subset', 'Keep the selected candidate for review.')
        state = self.state()
        self.assertEqual(state['proposals'][parent['id']]['status'], 'pending')
        self.assertEqual(result['deferred_entry_ids'], [defer])
        self.assertEqual([e['id'] for e in state['proposals'][result['proposal_id']]['entries']], [keep])
        self.assertFalse(state['active']['p1'])

    def test_revision_rejects_stale_hash_or_unknown_selections(self):
        rid = self.begin(); self.analyze(rid); parent = self.propose(rid); self.kb.finish(rid)
        eid = parent['entries'][0]['id']
        before = self.kb.state_path.read_bytes()
        cases = [('wrong', {'selected_entry_ids':[eid]}), (parent['proposal_hash'], {'selected_entry_ids':['unknown']}),
                 (parent['proposal_hash'], {'selected_entry_ids':[eid], 'edits':{'unknown':'Wrong entry edit.'}})]
        for sha, selection in cases:
            with self.assertRaises(ValueError): self.kb.revise(parent['id'], sha, selection, 'fixture:bad', 'Bad selection.')
            self.assertEqual(before, self.kb.state_path.read_bytes())

    def test_scheduled_consolidation_preserves_manual_review_draft(self):
        from kb import digest
        rid = self.begin(); self.analyze(rid); parent = self.propose(rid); self.kb.finish(rid)
        eid = parent['entries'][0]['id']
        revision = self.kb.revise(parent['id'], parent['proposal_hash'], {'selected_entry_ids':[eid], 'edits':{eid:'Manual clarification of the supported behavior.'}}, 'fixture:manual', 'Requested clarification.')
        with self.kb.locked() as state:
            job = {'scope':'p1', 'input_hash':digest(self.kb.scope_input(state, 'p1')), 'base_hash':state['published']['p1']}
            writes = {}
            before = len(state['proposals'])
            entries = [dict(parent['entries'][0], text='Newly consolidated evidence-specific behavior.')]
            self.kb.record_scope(state, job, {'entries':entries, 'reason':'Scheduled consolidation.'}, writes)
            self.assertEqual(len(state['proposals']), before + 1)
            self.kb.commit(state, writes)
        self.assertEqual(self.state()['proposals'][revision['proposal_id']]['status'], 'pending')


if __name__ == "__main__":
    unittest.main()
