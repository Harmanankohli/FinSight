import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import SendMessageRequest
from google.protobuf.json_format import MessageToDict

async def test_agent(url, name, query, timeout=90):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as h:
            card = await A2ACardResolver(h, url).get_agent_card()
            client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=h))
            msg = new_text_message(query, role=1)
            async for event in client.send_message(SendMessageRequest(message=msg)):
                if hasattr(event, 'task') and event.task:
                    t = event.task
                    print(f'{name}: State={t.status.state}')
                    for art in t.artifacts:
                        for p in art.parts:
                            if p.text:
                                print(f'  Text: {p.text[:300]}')
                            elif p.data:
                                d = MessageToDict(p.data)
                                print(f'  Keys: {list(d.keys())[:6]}')
                                if 'recommendation' in d:
                                    print(f'  Rec: {d["recommendation"]}')
                                if 'overall_signal' in d:
                                    print(f'  Signal: {d["overall_signal"]}')
                    break
            await client.close()
    except Exception as e:
        print(f'{name}: ERROR — {type(e).__name__}: {str(e)[:150]}')

async def main():
    await test_agent('http://localhost:8002', 'RAG', 'Analyze NVDA risks')
    await test_agent('http://localhost:8003', 'QUANT', 'Analyze NVDA')
    await test_agent('http://localhost:8004', 'SENTIMENT', 'Sentiment for NVDA')

asyncio.run(main())
