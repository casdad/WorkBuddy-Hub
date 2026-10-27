"""Direct contract tests for the guarded MIT protocol adapters."""
import json
import pytest
from fastapi import HTTPException
from hub.api import validate_chat
from hub.protocols import convert
from hub.vendor.responses_adapter import ResponsesStreamConverter
from hub.vendor.anthropic_adapter import AnthropicStreamConverter


def test_responses_multiturn_tool_results_keep_ids_and_order():
    body={'model':'test-model','input':[
        {'role':'user','content':'天气'},
        {'type':'function_call','call_id':'call_a','name':'weather','arguments':'{"city":"上海"}'},
        {'type':'function_call','call_id':'call_b','name':'weather','arguments':'{"city":"北京"}'},
        {'type':'function_call_output','call_id':'call_a','output':'晴'},
        {'type':'function_call_output','call_id':'call_b','output':'雨'},
        {'role':'user','content':'比较两地'}]}
    chat,_=convert(body,'responses');validate_chat(chat)
    assert [m['role'] for m in chat['messages']]==['user','assistant','tool','tool','user']
    assert [c['id'] for c in chat['messages'][1]['tool_calls']]==['call_a','call_b']
    assert chat['messages'][2]=={'role':'tool','tool_call_id':'call_a','content':'晴'}
    assert chat['messages'][3]['tool_call_id']=='call_b'


def test_messages_tool_result_precedes_user_text_and_preserves_error():
    body={'model':'test-model','max_tokens':50,'messages':[
        {'role':'assistant','content':[{'type':'tool_use','id':'call_a','name':'weather','input':{'city':'上海'}}]},
        {'role':'user','content':[{'type':'text','text':'请解释'},
          {'type':'tool_result','tool_use_id':'call_a','content':[{'type':'text','text':'服务暂停'}],'is_error':True}]}]}
    chat,_=convert(body,'messages');validate_chat(chat)
    assert [m['role'] for m in chat['messages']]==['assistant','tool','user']
    assert chat['messages'][1]=={'role':'tool','tool_call_id':'call_a','content':'Error: 服务暂停'}
    assert json.loads(chat['messages'][0]['tool_calls'][0]['function']['arguments'])=={'city':'上海'}


@pytest.mark.parametrize('kind',['responses','messages'])
def test_invalid_tool_results_are_rejected_instead_of_silently_dropped(kind):
    body={'model':'test-model','input':[{'type':'function_call_output','output':'result'}]} if kind=='responses' else {
        'model':'test-model','messages':[{'role':'user','content':[{'type':'text','text':'hello'},{'type':'tool_result','content':'result'}]}]}
    with pytest.raises(HTTPException) as error:convert(body,kind)
    assert error.value.status_code==400


@pytest.mark.parametrize('converter_class',[ResponsesStreamConverter,AnthropicStreamConverter])
def test_fragmented_parallel_tool_calls_keep_separate_arguments(converter_class):
    converter=converter_class(model='test-model')
    frames=[
        {'choices':[{'index':0,'delta':{'tool_calls':[
            {'index':0,'id':'call_a','type':'function','function':{'name':'weather','arguments':'{"city":"'}},
            {'index':1,'id':'call_b','type':'function','function':{'name':'weather','arguments':'{"city":"'}}]},'finish_reason':None}]},
        {'choices':[{'index':0,'delta':{'tool_calls':[
            {'index':1,'function':{'arguments':'北京"}'}},{'index':0,'function':{'arguments':'上海"}'}}]},'finish_reason':'tool_calls'}],
         'usage':{'prompt_tokens':17,'completion_tokens':9}}]
    stream=''.join(converter.feed_line('data: '+json.dumps(f)) for f in frames)+converter.finish()
    result=converter.get_nonstream_response()
    if converter_class is ResponsesStreamConverter:
        calls=[c for c in result['output'] if c['type']=='function_call']
        assert [(c['call_id'],json.loads(c['arguments'])['city']) for c in calls]==[('call_a','上海'),('call_b','北京')]
        assert 'response.completed' in stream
    else:
        calls=[c for c in result['content'] if c['type']=='tool_use']
        assert [(c['id'],c['input']['city']) for c in calls]==[('call_a','上海'),('call_b','北京')]
        assert 'message_stop' in stream
    assert result['usage']['input_tokens']==17
    assert result['usage']['output_tokens']==9
