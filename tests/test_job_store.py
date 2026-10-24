"""Conversion job store across replicas. Needs the web dependencies main.py imports:

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import main
except Exception:  # pragma: no cover - local dev without OCCT / web deps
    main = None


class _Doc:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data)


class _FakeFirestore:
    """The shared store both replicas see."""

    def __init__(self):
        self.docs = {}
        self.fail_reads = False

    def collection(self, _name):
        store = self

        class _Collection:
            def document(self, job_id):
                class _Ref:
                    def set(self, data):
                        store.docs[job_id] = dict(data)

                    def update(self, data):
                        store.docs.setdefault(job_id, {}).update(data)

                    def get(self):
                        if store.fail_reads:
                            raise RuntimeError("Firestore unavailable")
                        return _Doc(store.docs.get(job_id))

                return _Ref()

        return _Collection()


def _replica(shared):
    store = main.ConversionJobStore()
    store._db = shared
    return store


@unittest.skipIf(main is None, "main.py dependencies not installed")
class JobStoreAcrossReplicasTest(unittest.TestCase):
    def test_a_poll_on_another_replica_sees_the_finished_job(self):
        shared = _FakeFirestore()
        a, b = _replica(shared), _replica(shared)

        a.create_job("job1", "part.step", 100, owner="user:uid_1")
        # A poll lands on replica B while the job is still queued…
        self.assertEqual(b.get_job("job1")["status"], "queued")
        # …replica A finishes it…
        a.update_job("job1", {"status": "done", "result": {"stl": "AAAA"}})
        # …and B reports the result instead of a stale "queued"
        self.assertEqual(b.get_job("job1")["status"], "done")
        self.assertEqual(b.get_job("job1")["result"], {"stl": "AAAA"})
        self.assertEqual(b.get_job("job1")["owner"], "user:uid_1")

    def test_falls_back_to_its_own_copy_when_firestore_is_unreachable(self):
        shared = _FakeFirestore()
        a = _replica(shared)
        a.create_job("job2", "part.step", 100, owner="user:uid_1")
        shared.fail_reads = True
        self.assertEqual(a.get_job("job2")["status"], "queued")
        self.assertIsNone(a.get_job("missing"))

    def test_reports_its_mode(self):
        self.assertEqual(_replica(_FakeFirestore()).mode, "firestore")
        memory = main.ConversionJobStore()
        memory._db = None
        self.assertEqual(memory.mode, "memory")
        memory.create_job("job3", "part.step", 1, owner="service")
        self.assertEqual(memory.get_job("job3")["status"], "queued")

    def test_health_says_which_store_is_in_use(self):
        body = main.health()
        self.assertEqual(body["status"], "ok")
        self.assertIn(body["jobStore"], ("firestore", "memory"))


if __name__ == "__main__":
    unittest.main()
