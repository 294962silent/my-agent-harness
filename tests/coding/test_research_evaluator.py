import asyncio
import csv
import math

import pytest

from tests.coding.test_research_task import make_task


def test_host_rmse_ignores_order_and_rejects_invalid_predictions(tmp_path):
    from my_coding_agent.research_evaluator import score_predictions
    labels = [('a', 2.), ('b', 4.)]
    path = tmp_path / 'predictions.csv'
    path.write_text('id,prediction\nb,6\na,2\n')
    score = score_predictions(path, labels)
    assert score['rmse'] == pytest.approx(math.sqrt(2))
    assert score['score'] == pytest.approx(1 / (1 + math.sqrt(2)))
    for body in ('a,nan\nb,4', 'a,2\na,4', 'a,2', 'a,2\nc,4', 'a,2\nb,inf'):
        path.write_text('id,prediction\n' + body + '\n')
        with pytest.raises(ValueError):
            score_predictions(path, labels)


def test_numeric_evaluator_binds_predictions_and_blocks_extra_source(tmp_path):
    from my_coding_agent.research_evaluator import NumericPredictionEvaluator
    from my_coding_agent.rsi_runtime import ParentBudget
    task = make_task(tmp_path)
    evaluator = NumericPredictionEvaluator(task, image='sha256:' + 'a'*64, output_dir=tmp_path / 'eval')
    class Runner:
        async def run(self, code, train, features, output, budget):
            rows = list(csv.DictReader(features.open()))
            (output / 'predictions.csv').write_text('id,prediction\n' + ''.join(f"{r['id']},{2*float(r['x'])}\n" for r in rows))
            return {'image_id': 'sha256:' + 'a'*64, 'exit_code': 0}
    evaluator.runner = Runner()
    def budget():
        return ParentBudget(max_requests=1, max_tokens=1000, max_seconds=10, max_evaluations=2)
    result = asyncio.run(evaluator.evaluate(task.source_dir, budget(), None))
    assert result['valid'] and result['score'] == 1.0 and result['rmse'] == 0.0
    assert result['feedback_type'] == 'host_rmse' and result['pins']['task_manifest_sha256']
    assert result['artifacts']['predictions_sha256']
    assert 'labels' not in result['feedback']
    (task.source_dir / 'extra.py').write_text('bad')
    with pytest.raises(ValueError, match='train.py'):
        asyncio.run(evaluator.evaluate(task.source_dir, budget(), None))


def test_docker_policy_has_no_network_or_host_directory_mount_and_cleanup_on_cancel(tmp_path):
    from my_coding_agent.research_evaluator import DockerRunner
    runner = DockerRunner(image='sha256:'+'a'*64, docker_command=('docker',))
    calls = []
    async def command(*args, timeout):
        calls.append(args)
        if args[:2] == ('image', 'inspect'):
            return 0, ('sha256:'+'a'*64).encode(), b''
        if args[0] == 'wait':
            raise asyncio.CancelledError
        return 0, b'b'*64, b''
    runner._command = command
    for name in ('train.py','train.csv','features.csv'):
        (tmp_path/name).write_text('')
    out = tmp_path/'out'
    out.mkdir()
    from my_coding_agent.rsi_runtime import ParentBudget
    budget = ParentBudget(max_requests=1,max_tokens=1000,max_seconds=10,max_evaluations=1)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run(tmp_path/'train.py',tmp_path/'train.csv',tmp_path/'features.csv',out,budget))
    create = next(c for c in calls if c[0] == 'create')
    assert '--read-only' in create and '--cap-drop' in create and '--network' in create
    assert create[create.index('--network')+1] == 'none'
    assert '--memory' in create and '--cpus' in create and '--pids-limit' in create
    assert '--ulimit' in create
    assert '--cidfile' in create
    assert sum('type=bind' in x for x in create) == 4
    assert any('target=/output/predictions.csv' in x for x in create)
    assert calls[-1] == ('rm', '-f', 'b'*64)


def test_mutable_image_tag_and_final_online_mode_rejected(tmp_path):
    from my_coding_agent.research_evaluator import NumericPredictionEvaluator
    task = make_task(tmp_path)
    with pytest.raises(ValueError):
        NumericPredictionEvaluator(task,image='python:latest',output_dir=tmp_path/'eval')
    with pytest.raises(ValueError):
        NumericPredictionEvaluator(task,image='sha256:'+'a'*64,output_dir=tmp_path/'eval',split='final')


def test_invalid_candidate_is_unscored_and_final_is_one_shot(tmp_path):
    from my_coding_agent.research_evaluator import NumericPredictionEvaluator
    from my_coding_agent.rsi_runtime import ParentBudget
    evaluator = NumericPredictionEvaluator(make_task(tmp_path), image='sha256:'+'a'*64, output_dir=tmp_path/'eval')
    class Runner:
        async def run(self, *args):
            return {'image_id': 'sha256:'+'a'*64, 'exit_code': 1}
    evaluator.runner = Runner()
    budget = ParentBudget(max_requests=1,max_tokens=1000,max_seconds=10,max_evaluations=3)
    result = asyncio.run(evaluator.evaluate(evaluator.task.source_dir,budget,None))
    assert not result['valid'] and result['score'] is None and result['rmse'] is None
    asyncio.run(evaluator.evaluate_final(evaluator.task.source_dir,budget))
    with pytest.raises(ValueError, match='already attempted'):
        asyncio.run(evaluator.evaluate_final(evaluator.task.source_dir,budget))


def test_wsl_maps_local_paths_and_rejects_mount_separators(tmp_path):
    import os
    from my_coding_agent.research_evaluator import DockerRunner
    runner = DockerRunner(image='sha256:'+'a'*64, docker_command=('wsl.exe','-d','Ubuntu-20.04','--','docker'))
    if os.name == 'nt':
        assert runner._mount_path(tmp_path).startswith('/mnt/')
    with pytest.raises(ValueError, match='mount path'):
        runner._mount_path(tmp_path/'bad,name')


@pytest.mark.parametrize('failure', ['timeout', 'cleanup'])
def test_docker_timeout_removes_owned_container_and_cleanup_failure_blocks(tmp_path, failure):
    from my_coding_agent.research_evaluator import DockerRunner, ResearchBlocked
    from my_coding_agent.rsi_runtime import ParentBudget
    runner = DockerRunner(image='sha256:'+'a'*64,docker_command=('docker',))
    calls = []
    async def command(*args, timeout):
        calls.append(args)
        if args[:2] == ('image','inspect'):
            return 0, ('sha256:'+'a'*64).encode(), b''
        if args[0] == 'wait':
            raise asyncio.TimeoutError
        if args[0] == 'rm' and failure == 'cleanup':
            return 1,b'',b''
        return 0,b'b'*64,b''
    runner._command = command
    for name in ('train.py','train.csv','features.csv'):
        (tmp_path/name).write_text('')
    output = tmp_path/'out'
    output.mkdir()
    budget = ParentBudget(max_requests=1,max_tokens=1000,max_seconds=10,max_evaluations=1)
    with pytest.raises(ResearchBlocked if failure == 'cleanup' else asyncio.TimeoutError):
        asyncio.run(runner.run(tmp_path/'train.py',tmp_path/'train.csv',tmp_path/'features.csv',output,budget))
    assert calls[-1] == ('rm','-f','b'*64)


def test_missing_docker_image_is_blocked_not_a_zero_score(tmp_path):
    from my_coding_agent.research_evaluator import DockerRunner, ResearchBlocked
    runner = DockerRunner(image='sha256:'+'a'*64,docker_command=('docker',))
    async def missing(*args, timeout):
        return 1,b'',b'image missing'
    runner._command = missing
    with pytest.raises(ResearchBlocked):
        asyncio.run(runner.verify_image())


def test_cleanup_never_deletes_a_container_without_matching_ownership(tmp_path):
    from my_coding_agent.research_evaluator import DockerRunner, ResearchBlocked
    runner = DockerRunner(image='sha256:'+'a'*64,docker_command=('docker',))
    calls = []
    async def command(*args, timeout):
        calls.append(args)
        return 0, ('b'*64 + ' another-owner').encode(), b''
    runner._command = command
    with pytest.raises(ResearchBlocked):
        asyncio.run(runner._remove_owned('expected-owner',None,tmp_path/'missing.cid'))
    assert not any(c[0] == 'rm' for c in calls)
