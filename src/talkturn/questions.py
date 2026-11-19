"""Forward supported native question hooks; only explicit user answers resolve them."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from .runtime import ROOT, read_json, write_json, locked


class AnswerRejected(ValueError):
    """A question-response validation message safe to show to the person answering."""


def pending():
    return [item for item in read_json('questions.json',[]) if item.get('status')=='waiting' and item['expires_at']>time.time()]


def actionable(record):
    return record.get('kind')=='permission' or record.get('kind')=='question' and record.get('provider')=='claude'


def quoted_request_id(text):
    """Only a complete ID on the question card's first line identifies a request."""
    first=text.strip().splitlines()[0] if isinstance(text,str) and text.strip() else ''
    match=re.fullmatch(r'질문\s*(Q[0-9A-Fa-f]{8})',first.strip())
    return match[1].upper() if match else None


def describe(record):
    from .result_format import session_heading
    payload=record['payload']
    tool=payload.get('tool_input')
    tool=tool if isinstance(tool,dict) else {}
    heading=session_heading({'source':record['provider'],'session_id':payload.get('session_id')})
    lines=[f"질문 {record['id']}" if actionable(record) else '알림 전용 · 원래 Codex 창에서 응답']
    if heading:lines.append(heading)
    settings=read_json('delivery.json',{})
    chat_replies=settings.get('reply_mode')!='manual' and settings.get('reply_routing_enabled') is not False
    if record['kind']=='question':
        questions=tool.get('questions',[])
        for number,question in enumerate(questions,1):
            lines.append(f"{number}. {question.get('question') or question.get('title') or ''}")
            for index,option in enumerate(question.get('options',[]),1):
                label=option.get('label','') if isinstance(option,dict) else str(option)
                description=option.get('description','') if isinstance(option,dict) else ''
                lines.append(f'   {index}) {label}' + (f' — {description}' if description else ''))
        if record['provider']!='claude':
            lines.append('이 Codex 질문은 알림 전용. 선택·응답은 원래 창에서 진행.')
        elif not chat_replies:
            lines.append('카톡 답장이 꺼져 있어요. PC의 원래 작업 창에서 답하세요.')
        else:
            lines.append('이 말풍선에 선택지 번호나 답을 적어 답장하세요.' if any(q.get('options') for q in questions)
                         else '이 말풍선에 답을 적어 답장하세요.')
            if len(questions)>1:
                lines.append('질문 순서대로 답을 쓰고 답 사이에 세미콜론(;)을 넣으세요.')
    elif record['kind']=='permission':
        lines.append('작업 승인 요청: '+payload.get('tool_name',''))
        # Show the concrete action, without sending unrelated transcript or suggested permanent rules.
        preview={key:value for key,value in tool.items() if key in ('command','description','file_path','path','url','host','reason','justification')}
        if preview:
            lines.append(json.dumps(preview,ensure_ascii=False,indent=2))
        if not chat_replies:
            lines.append('카톡 답장이 꺼져 있어요. PC의 원래 작업 창에서 요청을 확인하세요.')
        elif record.get('remote_approval'):
            lines.append('이 말풍선에 ‘승인’ 또는 ‘거절’로 답장하세요. 이번 요청에만 적용됩니다.')
        else:
            lines.append('승인하려면 PC의 원래 작업 창에서 전체 내용을 확인하세요. 거절하려면 이 말풍선에 ‘거절’로 답장하세요.')
    else:
        lines.append(str(payload.get('message') or payload.get('title') or '원래 작업 창에서 사용자 입력을 기다립니다.'))
        lines.append('원래 창에서 확인 필요.')
    return '\n'.join(lines)


def prepare(provider,payload):
    event=payload.get('hook_event_name')
    if event=='PreToolUse' and isinstance(payload.get('tool_input'),dict) and isinstance(payload['tool_input'].get('questions'),list):
        if provider=='claude' and payload.get('tool_name')!='AskUserQuestion':
            return None
        kind='question'
    elif event=='PermissionRequest':
        kind='permission'
    else:
        # Notifications (including idle_prompt) have no answer channel, so never pose them as questions.
        return None
    if payload.get('agent_id'):
        return None
    record={'id':'Q'+uuid.uuid4().hex[:8].upper(),'provider':provider,'kind':kind,
            'payload':payload,'status':'waiting','expires_at':time.time()+600}
    # An incomplete action preview is never enough for remote approval.
    if kind=='permission':
        tool=payload.get('tool_input',{})
        allowed={'command','description','file_path','path','url','host','reason','justification'}
        record['remote_approval']=isinstance(tool,dict) and bool(set(tool)&{'command','file_path','path','url','host'}) and set(tool)<=allowed
    return record


def answer(request_id,text):
    if not re.fullmatch(r'Q[0-9A-Fa-f]{8}',request_id):
        raise AnswerRejected('질문 번호 형식은 Q1234ABCD입니다.')
    with locked('questions.lock'):
        records=read_json('questions.json',[])
        matches=[record for record in records if record['id'].upper()==request_id.upper()]
        if len(matches)!=1 or matches[0]['status']!='waiting' or matches[0]['expires_at']<=time.time():
            raise AnswerRejected('만료됐거나 이미 답한 질문이에요. PC의 원래 작업 창에서 확인하세요.')
        record=matches[0]
        if record['kind']=='permission':
            choices={'승인':'allow','허용':'allow','거절':'deny','거부':'deny'}
            if not isinstance(text,str) or text not in choices:
                raise AnswerRejected('승인 또는 거절을 정확히 입력해 주세요.' if record.get('remote_approval')
                                     else '이 요청은 카톡에서 거절만 할 수 있어요. 승인은 PC의 원래 작업 창에서 확인하세요.')
            if choices[text]=='allow' and not record.get('remote_approval'):
                raise AnswerRejected('승인하려면 PC의 원래 작업 창에서 전체 내용을 확인하세요.')
            output={'hookSpecificOutput':{'hookEventName':'PermissionRequest','decision':{'behavior':choices[text]}}}
        elif record['kind']=='question' and record['provider']=='claude':
            tool=copy.deepcopy(record['payload']['tool_input'])
            questions=tool.get('questions',[])
            if not isinstance(text,(str,list)) or isinstance(text,list) and any(not isinstance(value,str) for value in text):
                raise AnswerRejected('질문별 답변이 필요합니다.')
            values=[value.strip() for value in (text if isinstance(text,list) else text.split(';'))]
            if len(values)!=len(questions) or any(not value for value in values):
                raise AnswerRejected('한 가지 답을 적어 주세요.' if len(questions)==1 else
                                     f'질문 순서대로 답 {len(questions)}개를 쓰고 답 사이에 세미콜론(;)을 넣으세요.')
            answers={}
            for question,value in zip(questions,values):
                options=question.get('options',[])
                if re.fullmatch(r'\d+(?:,\d+)*',value):
                    indexes=[int(x)-1 for x in value.split(',')]
                    if any(index<0 or index>=len(options) for index in indexes) or len(indexes)>1 and not question.get('multiSelect'):
                        raise AnswerRejected('질문에 표시된 선택지 번호를 입력하세요. 여러 번호는 복수 선택 질문에서만 쓸 수 있어요.')
                    value=', '.join(options[index]['label'] if isinstance(options[index],dict) else str(options[index]) for index in indexes)
                answers[question['question']]=value
            tool['answers']=answers
            output={'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'allow','updatedInput':tool}}
        else:
            raise AnswerRejected('이 질문은 알림 전용이에요. PC의 원래 작업 창에서 답하세요.')
        record['response']=output
        record['status']='answered'
        write_json('questions.json',records)
    return f'{request_id.upper()} 답변 접수. 대기 중인 원래 훅으로 전달됨.'


def handle(provider,payload, *, wait=True):
    from .plain_delivery import deliver
    from .sessions import receives_notifications
    if not receives_notifications(provider,payload):
        return {}
    record=prepare(provider,payload)
    if not record or not (ROOT/'enabled').exists():
        return {}
    from .tasks import record as record_task
    record_task(provider,payload,'승인 대기' if record['kind']=='permission' else '질문 대기')
    with locked('questions.lock'):
        records=read_json('questions.json',[])
        records.append(record)
        write_json('questions.json',records[-100:])
    try:
        deliver(describe(record),'question-'+record['id'],origin={
            'source':provider,'session_id':payload.get('session_id',''),'model':payload.get('model',''),
            'cwd':payload.get('cwd',''),'request_id':record['id'],'kind':'question_card'},render_long=False)
    except Exception:
        with locked('questions.lock'):
            records=read_json('questions.json',[])
            for item in records:
                if item['id']==record['id']:
                    item['status']='delivery_failed'
            write_json('questions.json',records)
        raise
    if not wait or not actionable(record):
        return {}
    while (ROOT/'reply-enabled').exists():
        with locked('questions.lock'):
            records=read_json('questions.json',[])
            current=next((item for item in records if item['id']==record['id']),None)
            if current and current['status']=='answered':
                # answer() accepted this response under the same lock before expiry.
                # A later polling tick must not discard an already accepted answer.
                current['status']='returned_to_hook'
                write_json('questions.json',records)
                record_task(provider,payload,'답변 전달')
                return current['response']
            if time.time()>=record['expires_at']:
                break
        time.sleep(1)
    with locked('questions.lock'):
        records=read_json('questions.json',[])
        for item in records:
            if item['id']==record['id']:
                item['status']='expired'
        write_json('questions.json',records)
    # No response means normal local prompt. Never approve on timeout.
    return {}


def main():
    global ROOT
    sys.stdin.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True,choices=['codex','claude'])
    parser.add_argument('--home')
    args=parser.parse_args()
    if args.home:
        from . import runtime
        runtime.ROOT=ROOT=Path(args.home).resolve()
        os.environ['TALKTURN_HOME']=str(ROOT)
    try:
        result=handle(args.source,json.load(sys.stdin))
    except Exception as error:
        write_json('question-status.json',{'error':type(error).__name__,'time':time.time()})
        result={}
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':
    main()
