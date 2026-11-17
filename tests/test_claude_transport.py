import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from mcp.types import CallToolResult,TextContent
from pydantic import BaseModel
from talkturn import claude_transport


class LegacyCallToolResult(BaseModel):
    """MCP v1's public camelCase fields, using the same Pydantic alias export API."""
    content:list[TextContent]
    isError:bool=False
    structuredContent:dict|None=None


@asynccontextmanager
async def fake_stdio(_):
    yield ('read-stream','write-stream')


class ClaudeTransportTest(unittest.TestCase):
    def test_sdk_error_aliases_timeout_and_acceptance_without_live_sends(self):
        listing={'sections':[{'kind':'sessions','rows':[{'name':'original','ref':'fresh'}]}]}
        for sdk in ('1.26.0','2.3.0'):
            for result_type in (LegacyCallToolResult,CallToolResult):
                for failure in (None,'list_flag','list_payload','send_flag','send_payload'):
                    with self.subTest(sdk=sdk,result_type=result_type.__name__,failure=failure):
                        list_payload={'success':False,'error':'discovery rejected'} if failure=='list_payload' else listing
                        list_result=result_type(content=[TextContent(type='text',text=json.dumps(list_payload))],
                                                isError=failure=='list_flag')
                        details={'success':failure!='send_payload','held':True}
                        send_result=result_type(content=[],structuredContent=details,isError=failure=='send_flag')
                        session=SimpleNamespace(initialize=AsyncMock(),call_tool=AsyncMock(side_effect=[list_result,send_result]))
                        context=MagicMock()
                        context.__aenter__=AsyncMock(return_value=session)
                        context.__aexit__=AsyncMock(return_value=False)
                        with patch.object(claude_transport,'executable',return_value='synthetic-claude.exe'), \
                             patch.object(claude_transport,'stdio_client',side_effect=fake_stdio), \
                             patch.object(claude_transport,'ClientSession',return_value=context) as client, \
                             patch.object(claude_transport,'version',return_value=sdk):
                            if failure:
                                with self.assertRaises(RuntimeError):
                                    asyncio.run(claude_transport.transfer('original','test message','fresh'))
                            else:
                                result=asyncio.run(claude_transport.transfer('original','test message','fresh'))
                                self.assertEqual(result,{'delivered':True,'result':{'success':True,'held':True}})
                        expected_timeout=timedelta(seconds=20) if sdk.startswith('1.') else 20
                        self.assertEqual(client.call_args.kwargs['read_timeout_seconds'],expected_timeout)
                        calls=session.call_tool.call_args_list
                        self.assertEqual(calls[0].args,('ListAgents',{}))
                        if failure and failure.startswith('list_'):
                            self.assertEqual(len(calls),1)
                        else:
                            self.assertEqual(len(calls),2)
                            self.assertEqual(calls[1].args,('SendMessage',{
                                'to':'original [fresh]','message':'test message','summary':'카카오톡에서 원래 작업에 답장'}))


if __name__=='__main__':
    unittest.main()
