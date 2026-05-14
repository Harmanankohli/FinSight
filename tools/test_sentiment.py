import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import SendMessageRequest, Role
from google.protobuf.json_format import MessageToDict

async def t():
    h = httpx.AsyncClient(timeout=httpx.Timeout(120))
    async with h:
        card = await A2ACardResolver(h, "http://localhost:8004").get_agent_card()
        client = await create_client(agent=card, client_config=ClientConfig(streaming=True, httpx_client=h))
        req = SendMessageRequest(message=new_text_message("Sentiment for NVDA", role=Role.ROLE_USER))
        async for event in client.send_message(req):
            if event.HasField("artifact_update"):
                for p in event.artifact_update.artifact.parts:
                    if p.data:
                        d = MessageToDict(p.data)
                        print("Signal:", d.get("overall_signal"))
                        print("Confidence:", d.get("confidence_score"))
                        n = d.get("narrative", "")
                        print("Narrative:", n[:300])
                    elif p.text:
                        print("Text:", p.text[:200])
                break
            if event.HasField("task") and event.task.status.state == 3:
                for art in event.task.artifacts:
                    for p in art.parts:
                        if p.data:
                            d = MessageToDict(p.data)
                            print("Signal:", d.get("overall_signal"))
                break
        await client.close()

asyncio.run(t())
