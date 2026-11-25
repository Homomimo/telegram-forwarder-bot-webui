import json
import tempfile
import unittest
from pathlib import Path

from clone_receipts import read_receipts
from clone_receipts import Uncertain


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "receipts.jsonl"
        self.row = dict(version=1, token="token", actor=777, **{"from": 11, "to": 22}, source=42, target=71, operation="send")

    def read(self, rows, **kw):
        self.file.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return read_receipts(self.file, "token", "11", "22", (42,), **kw)

    def test_valid_and_original_actor_edit_receipts(self):
        result = self.read([self.row])
        self.assertEqual(result, {42: 71})
        self.assertEqual((result.engine, result.actor), ("tdl", "777"))
        result = self.read([dict(self.row, operation="edit")], target=71, actor="777")
        self.assertEqual(result, {42: 71})

    def test_corrupt_missing_duplicate_or_cross_task_receipts_never_make_maps(self):
        for changes in ({"token": "wrong"}, {"from": 99}, {"to": 99}, {"source": 43},
                        {"source": True}, {"target": 0}, {"target": True}, {"actor": 0},
                        {"operation": "edit"}, {"version": True}):
            with self.subTest(changes=changes), self.assertRaises(Uncertain):
                self.read([dict(self.row, **changes)])
        for rows in ([], [self.row, self.row]):
            with self.assertRaises(Uncertain):
                self.read(rows)
        for changes in ({"target": 72}, {"actor": 888}):
            with self.assertRaises(Uncertain):
                self.read([dict(self.row, operation="edit", **changes)], target=71, actor="777")
        self.file.write_text('{"version":', encoding="utf-8")
        with self.assertRaises(Uncertain):
            read_receipts(self.file, "token", "11", "22", (42,))
