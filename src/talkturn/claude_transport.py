"""Use Claude's documented cross-session MCP tools; never start a replacement chat."""
import asyncio
from datetime import timedelta
from importlib.metadata import version
import json
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .runtime import executable


def decoded(result):
    payload=result.model_dump(by_alias=True)
    if payload.get('isError',False):
        raise RuntimeError('Claude MCP tool request failed')
    if isinstance(payload.get('structuredContent'),dict):
        return payload['structuredContent']
    for content in result.content:
        if getattr(content, 'type', None) == 'text':
            try:
                return json.loads(content.text)
            except ValueError:
                pass
    return {'text':'\n'.join(content.text for content in result.content if getattr(content,'type',None)=='text')}


attempted=[]  # Non-empty once the message may have left; before that a failure is safe to retry.


async def transfer(name=None, message=None,expected_ref=None):
    parameters = StdioServerParameters(command=executable('claude'), args=['mcp', 'serve'])
    timeout=timedelta(seconds=20) if version('mcp').split('.')[0]=='1' else 20
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timeout) as session:
            await session.initialize()
            listing = decoded(await session.call_tool('ListAgents', {}))
            if listing.get('error') or listing.get('success') is False:
                raise RuntimeError('Claude session discovery failed')
            rows = [row for section in listing.get('sections', [])
                    if section.get('kind') == 'sessions' for row in section.get('rows', [])]
            if name is None:
                return {'targets':rows}
            matches = [row for row in rows if row.get('name') == name]
            if expected_ref:
                matches=[row for row in matches if row.get('ref')==expected_ref]
            if len(matches) != 1:
                raise RuntimeError('Original Claude session is missing or its name is ambiguous')
            row = matches[0]
            if expected_ref and row.get('ref')!=expected_ref:
                raise RuntimeError('Claude receiving session changed; message was not sent')
            address = f"{row['name']} [{row['ref']}]"
            attempted.append(True)
            result = await session.call_tool('SendMessage', {
                'to':address,'message':message,'summary':'카카오톡에서 원래 작업에 답장'})
            details=decoded(result)
            if details.get('error') or details.get('success') is False:
                raise RuntimeError('Claude did not accept the reply')
            return {'delivered':True,'result':details}


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 1:
        result = asyncio.run(transfer())
    else:
        sys.stdin.reconfigure(encoding='utf-8')
        request = json.load(sys.stdin)
        try:
            result = asyncio.run(transfer(request['name'], request['message'],request.get('expected_ref')))
        except Exception as error:
            # The reason goes to the caller's log; exit 3 tells it nothing was sent yet.
            print(type(error).__name__+': '+str(error)[:300],file=sys.stderr)
            sys.exit(1 if attempted else 3)
    print(json.dumps(result, ensure_ascii=True))
