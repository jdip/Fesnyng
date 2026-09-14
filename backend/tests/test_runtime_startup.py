import asyncio
import json

from fesnyng_backend.host_runtime import DockerRuntime
from test_peer_discovery import discovery_system


def test_start_recovers_when_first_native_health_connection_stalls(tmp_path, monkeypatch):
    host, _config, org, roster = discovery_system(tmp_path)
    agent = roster[0]["agent_id"]
    runtime = DockerRuntime(host, "http://host.invalid")

    async def exercise():
        attempts = 0
        handlers = set()

        async def respond(reader, writer):
            nonlocal attempts
            task = asyncio.current_task()
            handlers.add(task)
            try:
                await reader.readuntil(b"\r\n\r\n")
                attempts += 1
                if attempts == 1:
                    await reader.read()
                else:
                    body = b'{"healthy":true}'
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\n" + body
                    )
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def docker(*args, **kwargs):
            if args[0] == "container":
                return runtime.name(agent).encode()
            assert args[0] == "inspect"
            return json.dumps(
                {
                    "labels": {
                        "fesnyng.host": str(host.instance_id),
                        "fesnyng.organization": org,
                        "fesnyng.agent": agent,
                    },
                    "state": {"Running": True},
                    "ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]},
                }
            ).encode()

        monkeypatch.setattr(runtime, "docker", docker)
        try:
            async with server, asyncio.timeout(5):
                await runtime.start(org, agent)
            assert attempts == 2
        finally:
            server.close()
            await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    asyncio.run(exercise())
