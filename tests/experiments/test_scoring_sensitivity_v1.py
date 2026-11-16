from __future__ import annotations

from experiments.memory_validation.followup_common import exact_mcnemar, seal, verify_seal, write_json


def test_exact_mcnemar_matches_frozen_five_discordant_result():
    assert exact_mcnemar(1, 4) == 0.375
    assert exact_mcnemar(0, 0) == 1.0
    assert exact_mcnemar(0, 5) == 0.0625


def test_frozen_inventory_binds_named_files_while_mutable_root_files_are_separate(tmp_path):
    root = tmp_path / "study"
    root.mkdir()
    assert seal(root, "plan.json", {"protocol": "offline"})
    assert verify_seal(root, "plan.json") == {"protocol": "offline"}
    write_json(root / "states" / "later.json", {"status": "started"})
    assert verify_seal(root, "plan.json") == {"protocol": "offline"}
    (root / "plan.json").write_text("{}\n", encoding="utf-8")
    try:
        verify_seal(root, "plan.json")
    except ValueError:
        pass
    else:
        raise AssertionError("changed frozen file must fail")
