import csv

import pytest


def make_task(tmp_path):
    from my_coding_agent.research_task import ResearchTask
    data = tmp_path / 'data.csv'
    data.write_text('x,y\n' + ''.join(f'{i},{2*i}\n' for i in range(20)), encoding='utf-8')
    return ResearchTask.prepare(data, tmp_path / 'task', target='y', seed=7)


def test_prepare_keeps_labels_out_of_source_and_features_and_pins_split(tmp_path):
    from my_coding_agent.research_task import ResearchTask
    task = make_task(tmp_path)
    assert {p.name for p in task.source_dir.iterdir()} == {'train.py'}
    assert list(csv.DictReader(task.dev_features.open()))[0].keys() == {'id', 'x'}
    assert task.manifest['counts'] == {'train': 12, 'dev': 4, 'final': 4}
    assert ResearchTask.load(task.root).manifest == task.manifest
    assert '/data/train.csv' in task.goal() and '/output/predictions.csv' in task.goal()
    task.dev_features.write_text('id,x\nwrong,3\n')
    with pytest.raises(ValueError, match='changed'):
        ResearchTask.load(task.root)


def test_prepare_rejects_duplicate_ids_and_nonfinite_targets(tmp_path):
    from my_coding_agent.research_task import ResearchTask
    data = tmp_path / 'bad.csv'
    data.write_text('id,x,y\na,1,2\na,2,3\n')
    with pytest.raises(ValueError):
        ResearchTask.prepare(data, tmp_path / 'bad', target='y', id_column='id')
