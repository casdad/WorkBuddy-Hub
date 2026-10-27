"""Strict, stateless compatibility boundaries around the attributed adapters."""
import json
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from .vendor.responses_adapter import responses_request_to_chat, ResponsesStreamConverter
from .vendor.anthropic_adapter import anthropic_request_to_chat, AnthropicStreamConverter
from .streaming import OwnedStreamingResponse


def convert(body,kind):
    if not isinstance(body,dict):
        raise HTTPException(400,'请求必须是 JSON 对象')
    if not isinstance(body.get('tools',[]),list) or any(not isinstance(t,dict) for t in body.get('tools',[])):
        raise HTTPException(400,'tools 必须是对象数组')
    if kind=='responses':
        allowed={'model','input','instructions','tools','tool_choice','stream','max_output_tokens','temperature','top_p','reasoning','store'}
        if set(body)-allowed or body.get('store') is True:
            raise HTTPException(400,'Responses 支持无状态文本和函数工具；不支持 store、previous_response_id、内置工具或其他扩展字段')
        for tool in body.get('tools',[]):
            if tool.get('type')!='function':
                raise HTTPException(400,'仅支持 function 工具')
        if isinstance(body.get('input'),list):
            for item in body['input']:
                if not isinstance(item,dict) or item.get('type','message') not in ('message','function_call','function_call_output'):
                    raise HTTPException(400,'不支持此 Responses 输入类型')
                if item.get('type') in ('function_call','function_call_output'):
                    if not isinstance(item.get('call_id'),str) or not item['call_id']:
                        raise HTTPException(400,'函数调用和结果需要非空 call_id')
                    if item['type']=='function_call' and (not isinstance(item.get('name'),str) or not item['name'] or not isinstance(item.get('arguments'),str)):
                        raise HTTPException(400,'函数调用需要名称与 JSON 字符串参数')
                    if item['type']=='function_call_output' and not isinstance(item.get('output'),str):
                        raise HTTPException(400,'函数结果仅支持文本')
                content=item.get('content')
                if isinstance(content,list) and any(not isinstance(p,dict) or p.get('type') not in ('text','input_text','output_text') for p in content):
                    raise HTTPException(400,'Responses 当前仅支持文本内容；图片请使用 Chat 接口')
        reasoning=body.get('reasoning')
        if reasoning is not None and (not isinstance(reasoning,dict) or set(reasoning)-{'effort'}):
            raise HTTPException(400,'reasoning 仅支持 effort')
        chat=responses_request_to_chat(body)
        if isinstance(body.get('tool_choice'),dict):
            choice=body['tool_choice']
            if choice.get('type')!='function' or not choice.get('name'):
                raise HTTPException(400,'不支持此 tool_choice')
            chat['tool_choice']={'type':'function','function':{'name':choice['name']}}
        return chat,ResponsesStreamConverter
    allowed={'model','messages','system','tools','tool_choice','stream','max_tokens','temperature','top_p','stop_sequences'}
    if set(body)-allowed:
        raise HTTPException(400,'Messages 存在不支持的字段；支持文本、图片、函数工具和流式输出')
    if not isinstance(body.get('messages'),list):
        raise HTTPException(400,'messages 必须是对象数组')
    system=body.get('system')
    if system is not None and not isinstance(system,str):
        if not isinstance(system,list) or any(not isinstance(p,dict) or p.get('type')!='text' or not isinstance(p.get('text'),str) for p in system):
            raise HTTPException(400,'system 仅支持文本')
    for message in body['messages']:
        if not isinstance(message,dict):
            raise HTTPException(400,'messages 必须是对象数组')
        content=message.get('content')
        if isinstance(content,list):
            for block in content:
                if not isinstance(block,dict) or block.get('type') not in ('text','image','tool_use','tool_result'):
                    raise HTTPException(400,'不支持此 Messages 内容块')
                if block['type']=='tool_use' and (not isinstance(block.get('id'),str) or not block['id'] or not isinstance(block.get('name'),str) or not block['name'] or not isinstance(block.get('input'),dict)):
                    raise HTTPException(400,'tool_use 需要 id、name 与对象类型的 input')
                if block['type']=='tool_result' and (not isinstance(block.get('tool_use_id'),str) or not block['tool_use_id']):
                    raise HTTPException(400,'tool_result 需要非空 tool_use_id')
                if block.get('type')=='tool_result' and isinstance(block.get('content'),list):
                    if any(not isinstance(p,dict) or p.get('type')!='text' or not isinstance(p.get('text'),str) for p in block['content']):
                        raise HTTPException(400,'工具结果仅支持文本')
                elif block.get('type')=='tool_result' and not isinstance(block.get('content'),str):
                    raise HTTPException(400,'工具结果仅支持文本')
    return anthropic_request_to_chat(body),AnthropicStreamConverter


async def protocol_response(hub,key,body,kind,validate,session):
    chat,converter_class=convert(body,kind)
    wants_stream=body.get('stream',False)
    if not isinstance(wants_stream,bool):
        raise HTTPException(400,'stream 必须是布尔值')
    chat['stream']=wants_stream
    validate(chat)
    response=await hub.gateway(key,chat,session,kind)
    converter=converter_class(model=body['model'])
    if not wants_stream:
        result=json.loads(response.body)
        chunks=[]
        for choice in result['choices']:
            delta=choice['message']
            if delta.get('tool_calls'):
                delta['tool_calls']=[{**call,'index':i} for i,call in enumerate(delta['tool_calls'])]
            chunks.append({'index':choice['index'],'delta':delta,'finish_reason':choice['finish_reason']})
        converter.feed_line('data: '+json.dumps({'choices':chunks,'usage':result.get('usage',{})}))
        converter.finish()
        return JSONResponse(converter.get_nonstream_response(),headers={'X-Request-Id':response.headers['x-request-id']})
    async def translated():
        completed=False
        size=0
        iterator=response.body_iterator
        try:
            async for part in iterator:
                part=part.decode() if isinstance(part,bytes) else part
                size+=len(part.encode())
                if size>8*1024*1024:
                    yield 'event: error\ndata: '+json.dumps({'error':{'message':'转换结果超过大小限制'}})+'\n\n'
                    return
                for line in part.splitlines():
                    if line.startswith('data:') and line[5:].strip()!='[DONE]':
                        chunk=json.loads(line[5:])
                        if chunk.get('error'):
                            yield 'event: error\ndata: '+json.dumps(chunk)+'\n\n'
                            return
                        output=converter.feed_line(line)
                        if output:
                            yield output
                    elif line=='data: [DONE]':
                        completed=True
            if completed:
                yield converter.finish()
        finally:
            await iterator.aclose()
    return OwnedStreamingResponse(translated(),finalize=response.finalize,media_type='text/event-stream',headers={'X-Request-Id':response.headers['x-request-id'],'Cache-Control':'no-cache'})
