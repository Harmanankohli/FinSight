import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import SendMessageRequest, Role
from google.protobuf.json_format import MessageToDict

async def t():
    h = httpx.AsyncClient(timeout=httpx.Timeout(60))
    async with h:
        card = await A2ACardResolver(h, "http://localhost:8003").get_agent_card()
        client = await create_client(agent=card, client_config=ClientConfig(streaming=True, httpx_client=h))
        req = SendMessageRequest(message=new_text_message("Analyze NVDA", role=Role.ROLE_USER))
        async for event in client.send_message(req):
            if event.HasField("artifact_update"):
                for p in event.artifact_update.artifact.parts:
                    if p.data:
                        d = MessageToDict(p.data)
                        print("Recommendation:", d.get("recommendation"))
                        print("Sharpe:", d.get("metrics", {}).get("sharpe_ratio"))
                        print("Vol:", d.get("metrics", {}).get("annual_volatility"))
                        print("Reasoning:", d.get("reasoning", "")[:200])
                    elif p.text:
                        print("Text:", p.text[:200])
                break
            if event.HasField("task") and event.task.status.state == 3:
                for art in event.task.artifacts:
                    for p in art.parts:
                        if p.data:
                            d = MessageToDict(p.data)
                            print("Recommendation:", d.get("recommendation"))
                break
        await client.close()

asyncio.run(t())
