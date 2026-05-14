import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import SendMessageRequest, Role
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct

async def t():
    h = httpx.AsyncClient(timeout=httpx.Timeout(60))
    async with h:
        card = await A2ACardResolver(h, "http://localhost:8002").get_agent_card()
        client = await create_client(agent=card, client_config=ClientConfig(streaming=True, httpx_client=h))
        meta = Struct()
        meta.update({"ticker": "NVDA", "period": "5y"})
        req = SendMessageRequest(
            message=new_text_message("Analyze NVDA risks", role=Role.ROLE_USER),
            metadata=meta,
        )
        count = 0
        async for event in client.send_message(req):
            count += 1
            fields = []
            if event.HasField("task"):
                fields.append(f"task(state={event.task.status.state})")
            if event.HasField("status_update"):
                fields.append(f"status(state={event.status_update.status.state})")
            if event.HasField("artifact_update"):
                parts_info = []
                for p in event.artifact_update.artifact.parts:
                    if p.data: parts_info.append("data")
                    elif p.text: parts_info.append("text")
                fields.append(f"artifact(parts={parts_info})")
            print(f"Event #{count}: {', '.join(fields)}")
            if event.HasField("artifact_update"):
                for p in event.artifact_update.artifact.parts:
                    if p.data:
                        d = MessageToDict(p.data)
                        print(f"  -> Keys: {list(d.keys())[:6]}")
                        print(f"  -> Summary: {d.get('summary','')[:200]}")
        print(f"Total events: {count}")
        await client.close()

asyncio.run(t())
