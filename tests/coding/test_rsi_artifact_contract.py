"""Artifact delivery uses real agents with scripted responses, not model-quality evidence."""
import asyncio
import importlib.util
import json

import pytest

from my_coding_agent.rsi_runtime import FrozenPolicy, ParentBudget, RsiRuntime, TrustedEvaluator
from my_coding_agent.rsi_branch import LocalBranchFactory, file_sha
from my_agent_llm import Config, Response, ToolCall



@pytest.mark.parametrize('changed', ['eligibility','terminal','cost'])
def test_v2_semantic_identity_retains_failure_and_cost(changed):
    from copy import deepcopy
    from my_coding_agent.dream_rsi_contract import semantic_identity
    trace = failure_trace()
    other = deepcopy(trace)
    node = other['nodes'][0]
    if changed == 'eligibility':
        node.update(eligible=True,status='done',raw={'evaluated':True,'combined_score':0.})
    elif changed == 'terminal':
        node['terminal']['iterations'] = 2
    else:
        node['cost']['tokens'] += 1
    assert semantic_identity(trace) != semantic_identity(other)


@pytest.mark.parametrize('key', ['requests','tokens','evaluations'])
def test_v2_cost_rejects_boolean(key):
    from my_coding_agent.dream_rsi_contract import validate_trace
    trace=failure_trace()
    trace['nodes'][0]['cost'][key]=True
    with pytest.raises(ValueError):
        validate_trace(trace)


@pytest.mark.parametrize('outcome', ['execution','invalid_predictions'])
def test_host_invalid_outcome_still_has_code_pin(tmp_path,outcome):
    from tests.coding.test_research_task import make_task
    from my_coding_agent.research_evaluator import NumericPredictionEvaluator
    task=make_task(tmp_path)
    evaluator=NumericPredictionEvaluator(task,image='sha256:'+'a'*64,output_dir=tmp_path/'eval')
    class Runner:
        async def run(self,code,train,features,output,budget):
            if outcome == 'invalid_predictions':
                (output/'predictions.csv').write_text('id,prediction\nwrong,nan\n')
            return {'image_id':'sha256:'+'a'*64,'exit_code':int(outcome=='execution')}
    evaluator.runner=Runner()
    budget=ParentBudget(max_requests=1,max_tokens=1000,max_seconds=10,max_evaluations=2)
    result=asyncio.run(evaluator.evaluate(task.source_dir,budget,None))
    assert not result['valid'] and result['score'] is None
    assert result['pins']['code_sha256'] == file_sha(task.source_dir/'train.py')

class ArtifactLLM:
    config = Config(provider='openai', model='scripted-artifact', max_tokens=64, max_retries=0)
    model = 'scripted-artifact'
    calls = 0

    async def achat(self, messages, **kwargs):
        self.calls += 1
        return Response(model=self.model, content='', tool_calls=[ToolCall(
            id=f'call-{self.calls}', name='write', args={'path': 'train.py', 'content': 'print(1)\n'})],
            usage={'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5})


class ArtifactEvaluator(TrustedEvaluator):
    feedback_type = 'host_rmse'
    count = 0

    async def evaluate(self, workspace, budget, llm):
        budget.begin_evaluation().update(status='completed')
        self.count += 1
        return {'valid': True, 'score': .2 if self.count == 1 else .6,
                'feedback_type': self.feedback_type, 'pins': {'code_sha256': file_sha(workspace/'train.py')}}


def run_artifact(tmp_path, contract, *, evaluator=None, llm=None, refine=0):
    source = tmp_path/'source'
    source.mkdir()
    (source/'train.py').write_text('pass')
    factory = LocalBranchFactory(source, tmp_path/'run', attempt_max_turns=1,
        allowed_files=('train.py',), allowed_tool_names=('read','write','edit','ls','find'))
    budget = ParentBudget(max_requests=3,max_tokens=100000,max_seconds=20,max_evaluations=3)
    runtime = RsiRuntime(factory=factory,evaluator=evaluator or ArtifactEvaluator(),budget=budget,
        policy=FrozenPolicy({'max_width':1,'max_refine':refine}),max_attempts=1+refine,
        task_id='artifact',split='train',delivery_contract=contract)
    return asyncio.run(runtime.run('edit train.py',llm=llm or ArtifactLLM()))


def test_max_turns_artifact_eligible_without_end_turn(tmp_path):
    result = run_artifact(tmp_path, 'artifact_budget_v1')
    attempt = result.report['attempts'][0]
    assert attempt['agent_terminal'] == {'stop_reason':'budget_exhausted','iterations':1,
        'limit_details':{'kind':'max_turns','limit':1,'observed':1}}
    assert attempt['valid'] and attempt['artifact']['closed_tools']
    assert attempt['artifact']['code_sha256'] == attempt['visible']['pins']['code_sha256']
    assert json.loads(result.trace_path.read_text())['version'] == 2
    assert result.selected_visible_score == .6


def test_default_contract_does_not_accept_max_turns(tmp_path):
    result = run_artifact(tmp_path, 'end_turn_v1')
    assert not result.report['attempts'][0]['valid']
    assert result.trace_path is None


def test_known_invalid_artifact_is_complete_but_never_selected(tmp_path):
    from my_coding_agent.dream_rsi_contract import selectable_ceiling, validate_trace

    class InvalidEvaluator(ArtifactEvaluator):
        async def evaluate(self, workspace, budget, llm):
            result = await super().evaluate(workspace, budget, llm)
            if self.count > 1:
                result.update(valid=False, score=None)
            return result

    result = run_artifact(tmp_path, 'artifact_budget_v1', evaluator=InvalidEvaluator(), refine=1)
    trace = json.loads(result.trace_path.read_text())
    validate_trace(trace)
    assert len(trace['nodes']) == 2 and all(not n['eligible'] for n in trace['nodes'])
    assert result.selected_path is None and result.report['selected_candidate'] is None
    assert selectable_ceiling(trace) == .2
    assert [n['cost']['requests'] for n in trace['nodes']] == [1, 1]
    assert [n['cost']['tokens'] for n in trace['nodes']] == [5, 5]
    for i, attempt in enumerate(result.report['attempts']):
        starts = [a['tool_call_id'] for a in attempt['actions'] if a['event'] == 'ToolExecutionStart']
        assert starts == [f'call-{i+1}']
        assert attempt['agent_terminal']['iterations'] == 1


def test_unknown_usage_blocks_artifact_trace(tmp_path):
    class UnknownLLM(ArtifactLLM):
        async def achat(self, messages, **kwargs):
            response = await super().achat(messages, **kwargs)
            response.usage = None
            return response

    result = run_artifact(tmp_path, 'artifact_budget_v1', llm=UnknownLLM())
    assert result.trace_path is None and result.selected_path is None
    assert result.report['budget']['blocked'] and result.report['budget']['held_tokens'] > 0


@pytest.mark.parametrize('terminal', [
    {'stop_reason':'budget_exhausted','iterations':4,'limit_details':{'kind':'max_seconds','limit':180,'observed':180}},
    {'stop_reason':'budget_exhausted','iterations':4},
    {'stop_reason':'cancelled','iterations':4},
    {'stop_reason':'budget_exhausted','iterations':4,'limit_details':{'kind':'max_turns','limit':8,'observed':4}},
])
def test_other_terminal_not_artifact_delivery(terminal):
    from my_coding_agent.rsi_runtime import _artifact_terminal_allowed
    assert not _artifact_terminal_allowed(terminal,4)


def failure_trace():
    artifact = {'closed_tools':True, 'code_sha256':'a'*64, 'files':{'train.py':'a'*64}}
    return {'version':2,'delivery_contract':'artifact_budget_v1','attempt_max_turns':4,'n_problems':1,
        'baseline':{'evaluated':True,'combined_score':.4},'nodes':[
            {'id':'b0_a0','branch':0,'attempt':0,'parent_id':None,'path':'unused','status':'failed',
             'eligible':False,'terminal':{'stop_reason':'end_turn','iterations':1},
              'raw':{'evaluated':False,'combined_score':None},'artifact':artifact,
              'diagnostic':{'score':.99, 'pins':{'code_sha256':'a'*64}},
             'cost':{'requests':1,'tokens':5,'evaluations':1,'usage_complete':True}}]}


def test_failed_diagnostic_cannot_raise_replay_ceiling():
    from my_coding_agent.dream_rsi_contract import validate_trace, selectable_ceiling, replay_score
    trace = failure_trace()
    validate_trace(trace)
    assert selectable_ceiling(trace) == .4
    assert replay_score(trace,trace['nodes'][0]) == 0


@pytest.mark.parametrize('mutation', [
    lambda t:t['nodes'][0]['cost'].update(usage_complete=False),
    lambda t:t['nodes'][0]['raw'].update(combined_score=.99),
    lambda t:t['nodes'][0].update(eligible=True),
    lambda t:t['baseline'].update(combined_score=float('nan')),
])
def test_corrupt_or_unknown_trace_rejected(mutation):
    from my_coding_agent.dream_rsi_contract import validate_trace
    trace=failure_trace()
    mutation(trace)
    with pytest.raises(ValueError):
        validate_trace(trace)

@pytest.mark.skipif(importlib.util.find_spec("dream_rsi") is None,
                    reason="optional pinned Dream SDK required")
def test_real_sdk_failed_artifact_keeps_baseline_and_known_cost(tmp_path):
    from my_coding_agent.dream_rsi_replay import replay_trace
    trace = failure_trace()
    source = tmp_path/'failed.json'
    source.write_text(json.dumps(trace))
    result = replay_trace(source,tmp_path/'replay',budget=1,use_active=False)
    assert result['result']['best'] == .4
    assert result['result']['budget'] == 1
    episode = json.loads((tmp_path/'replay/decisions.json').read_text())
    outcome = episode['events'][0]['outcomes'][0]
    assert outcome['eligible'] is False and outcome['observed_cost']['tokens'] == 5
    assert outcome['score'] == 0 and outcome['best_after'] == .4


def test_closed_tools_counts_only_current_attempt():
    from my_coding_agent.rsi_runtime import _closed_attempt_tools
    assert not _closed_attempt_tools([{'event':'ToolExecutionEnd','tool_call_id':'old'}])
    assert not _closed_attempt_tools([{'event':'ToolExecutionStart','tool_call_id':'new'}])
    assert _closed_attempt_tools([{'event':'ToolExecutionStart','tool_call_id':'new'},
        {'event':'ToolExecutionEnd','tool_call_id':'new'}])
    assert not _closed_attempt_tools([{'event':'ToolExecutionStart','tool_call_id':'same'},
        {'event':'ToolExecutionEnd','tool_call_id':'same'},
        {'event':'ToolExecutionStart','tool_call_id':'same'},
        {'event':'ToolExecutionEnd','tool_call_id':'same'}])


@pytest.mark.parametrize('mutation', [
    lambda t:t.update(attempt_max_turns=True),
    lambda t:t['nodes'][0]['terminal'].update(stop_reason='cancelled'),
    lambda t:t['nodes'][0]['terminal'].update(iterations=True),
    lambda t:t['nodes'][0]['terminal'].update(stop_reason='budget_exhausted',
        limit_details={'kind':'max_seconds','limit':4,'observed':4}),
    lambda t:t['nodes'][0]['artifact'].update(closed_tools=False),
    lambda t:t['nodes'][0]['artifact'].update(code_sha256='b'*64),
    lambda t:t['nodes'][0]['diagnostic']['pins'].update(code_sha256='b'*64),
])
def test_v2_requires_qualified_static_artifact_and_current_terminal(mutation):
    from my_coding_agent.dream_rsi_contract import validate_trace
    trace = failure_trace()
    mutation(trace)
    with pytest.raises(ValueError):
        validate_trace(trace)
