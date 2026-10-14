import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from agent_delivery_loop.work_order import parse_work_order


class WorkOrderSchemaTests(unittest.TestCase):
    def test_existing_work_orders_match_schema_and_parser(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / ".agents/protocol/work-order.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        paths = sorted((root / ".agents/work-orders").glob("*.json")) + sorted(
            (root / "examples/work-orders").glob("*.json")
        )
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                validator.validate(json.loads(raw))
                parse_work_order(raw)
