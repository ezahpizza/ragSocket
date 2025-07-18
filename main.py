import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import aiohttp

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

app = FastAPI(
    title="Deepgram STT & LLM Stream Proxy",
    description="Proxies real-time audio to Deepgram and LLM stream from Next.js API back to client.",
    version="1.0.0"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
NEXT_JS_CHAT_API_URL = os.getenv("NEXT_JS_CHAT_API_URL", "http://localhost:3000/api/chat")
DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"

async def deepgram_to_client_receiver(client_ws: WebSocket, dg_ws: aiohttp.ClientWebSocketResponse):
    """Listens for messages from Deepgram and forwards them to the client."""
    try:
        async for msg in dg_ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                response = json.loads(msg.data)
                transcript = response.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                is_final = response.get("is_final", False)

                if transcript.strip():
                    await client_ws.send_json({
                        "type": "stt_transcript",
                        "text": transcript,
                        "isFinal": is_final
                    })
            elif msg.type in [aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR]:
                print("Deepgram connection closed or errored.")
                break
    except Exception as e:
        print(f"Error in receiver task: {e}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"{websocket.client.host}:{websocket.client.port}"
    print(f"Client connected: {client_id}")

    try:
        # Outer loop to keep client connection alive and wait for start signals.
        while True:
            # 1. Wait for a "start_transcription" signal from the client.
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("action") != "start_transcription":
                continue

            print("Start signal received. Establishing Deepgram connection.")
            
            # 2. Establish a new Deepgram connection for a single utterance.
            dg_ws = None
            aiohttp_session = aiohttp.ClientSession()
            try:
                deepgram_params = {
                    "model": "nova-2", "language": "en-US", "smart_format": "true",
                    "interim_results": "true", "endpointing": "true", "utterance_end_ms": "2500",
                    "punctuate": "true",
                }
                query_string = "&".join([f"{k}={v}" for k, v in deepgram_params.items()])
                full_url = f"{DEEPGRAM_WS_URL}?{query_string}"
                headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
                
                dg_ws = await aiohttp_session.ws_connect(full_url, headers=headers)
                
                # 3. Start a background task to receive messages from Deepgram.
                receiver_task = asyncio.create_task(deepgram_to_client_receiver(websocket, dg_ws))
                
                # 4. Enter a forwarding loop.
                while not receiver_task.done():
                    client_data_task = asyncio.create_task(websocket.receive_bytes())
                    
                    done, pending = await asyncio.wait(
                        {client_data_task, receiver_task},
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    if client_data_task in done:
                        await dg_ws.send_bytes(client_data_task.result())
                    
                    if client_data_task in pending:
                        client_data_task.cancel()
                        
            finally:
                # 5. Clean up resources for this utterance session.
                if 'receiver_task' in locals() and not receiver_task.done():
                    receiver_task.cancel()
                if dg_ws and not dg_ws.closed:
                    await dg_ws.close()
                if aiohttp_session and not aiohttp_session.closed:
                    await aiohttp_session.close()
                print("Deepgram utterance session cleaned up. Awaiting next start signal.")

    except (WebSocketDisconnect, ConnectionResetError):
        print(f"Client {client_id} disconnected.")
    except Exception as e:
        print(f"An error occurred for client {client_id}: {e}")
    finally:
        print(f"Closing connection and all resources for client {client_id}.")