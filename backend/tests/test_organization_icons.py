import asyncio
import base64
import os
import struct
import zlib
from io import BytesIO

import httpx
from PIL import Image, PngImagePlugin

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://workspace.example"


def _png(width: int, height: int, pixels: bytes | None = None) -> str:
    pixels = pixels if pixels is not None else b"\x12\x34\x56\xff" * (width * height)
    rows = b"".join(
        b"\x00" + pixels[index : index + width * 4] for index in range(0, len(pixels), width * 4)
    )

    def chunk(kind: bytes, content: bytes) -> bytes:
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content))
        )

    encoded = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    )
    encoded += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(encoded).decode()


def _png_dimensions(data_url: str) -> tuple[int, int]:
    decoded = base64.b64decode(data_url.split(",", 1)[1])
    return struct.unpack(">II", decoded[16:24])


def _image_data_url(image: Image.Image, media_type: str, format: str, **save: object) -> str:
    output = BytesIO()
    image.save(output, format=format, **save)
    return f"data:{media_type};base64," + base64.b64encode(output.getvalue()).decode()


def _returned_image(data_url: str) -> Image.Image:
    return Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1])))


def test_organization_icons_are_validated_personalized_and_durable(organization):
    settings, control, owner, org, _, _ = organization
    member = control.add_member(
        org.id,
        "member",
        "Member",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    other_org = control.create_organization(owner.id, "Other organization")
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def login(client: httpx.AsyncClient, login_name: str) -> None:
        response = await client.post(
            "/auth/login",
            json={"login": login_name, "password": "correct horse battery staple"},
        )
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    async def exercise() -> None:
        path = f"/organizations/{org.id}"
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as owner_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as member_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as anonymous,
        ):
            assert (await anonymous.put(f"{path}/icon", json={"icon": None})).status_code == 401
            await login(owner_device, "owner")
            await login(member_device, "member")
            assert (await owner_device.get(path)).json()["icon"] is None
            assert (
                await owner_device.put(
                    f"{path}/icon", json={"icon": {"kind": "emoji", "value": "🌍"}}
                )
            ).json()["icon"] == {
                "kind": "emoji",
                "value": "🌍",
            }
            assert (await owner_device.get("/organizations")).json()[0]["icon"] == {
                "kind": "emoji",
                "value": "🌍",
            }
            assert (
                await owner_device.put(
                    f"{path}/icon", json={"icon": {"kind": "emoji", "value": "1️⃣"}}
                )
            ).json()["icon"] == {"kind": "emoji", "value": "1️⃣"}
            assert (
                await owner_device.put(
                    f"{path}/icon", headers={"X-CSRF-Token": "missing"}, json={"icon": None}
                )
            ).status_code == 403
            assert (await member_device.put(f"{path}/icon", json={"icon": None})).status_code == 403
            assert (
                await member_device.put(f"/organizations/{other_org.id}/icon", json={"icon": None})
            ).status_code == 404
            for icon in (
                {"kind": "emoji", "value": "not an icon"},
                {"kind": "emoji", "value": "\u202e🌍"},
                {"kind": "image", "value": "data:image/svg+xml;base64,PHN2Zy8+"},
                {"kind": "image", "value": "data:image/png;base64,not-base64"},
            ):
                assert (
                    await owner_device.put(f"{path}/icon", json={"icon": icon})
                ).status_code == 422
            normalized = await owner_device.put(
                f"{path}/icon", json={"icon": {"kind": "image", "value": _png(256, 64)}}
            )
            assert normalized.status_code == 200
            icon = normalized.json()["icon"]
            assert icon["kind"] == "image" and icon["value"].startswith("data:image/png;base64,")
            assert _png_dimensions(icon["value"]) == (128, 32)
            for media_type, format in (("image/jpeg", "JPEG"), ("image/webp", "WEBP")):
                accepted = await owner_device.put(
                    f"{path}/icon",
                    json={
                        "icon": {
                            "kind": "image",
                            "value": _image_data_url(
                                Image.new("RGB", (256, 64)), media_type, format
                            ),
                        }
                    },
                )
                assert accepted.status_code == 200
                with _returned_image(accepted.json()["icon"]["value"]) as returned:
                    assert returned.format == "PNG" and returned.size == (128, 32)
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("private", "must be removed")
            metadata_response = await owner_device.put(
                f"{path}/icon",
                json={
                    "icon": {
                        "kind": "image",
                        "value": _image_data_url(
                            Image.new("RGBA", (16, 16)), "image/png", "PNG", pnginfo=metadata
                        ),
                    }
                },
            )
            assert metadata_response.status_code == 200
            with _returned_image(metadata_response.json()["icon"]["value"]) as returned:
                assert "private" not in returned.info
            animation = BytesIO()
            first, second = Image.new("RGBA", (16, 16)), Image.new("RGBA", (16, 16), "red")
            first.save(animation, format="PNG", save_all=True, append_images=[second], duration=10)
            animated = "data:image/png;base64," + base64.b64encode(animation.getvalue()).decode()
            assert (
                await owner_device.put(
                    f"{path}/icon", json={"icon": {"kind": "image", "value": animated}}
                )
            ).status_code == 422
            encoded_png = base64.b64decode(
                _png(128, 128, os.urandom(128 * 128 * 4)).split(",", 1)[1]
            )
            truncated = (
                "data:image/png;base64,"
                + base64.b64encode(encoded_png[: len(encoded_png) // 2]).decode()
            )
            assert (
                await owner_device.put(
                    f"{path}/icon", json={"icon": {"kind": "image", "value": truncated}}
                )
            ).status_code == 422
            assert (
                create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
                .state.control_store.organization_for_member(owner.id, org.id)
                .icon_kind
            ) == "image"
            oversized = _png(750, 750, os.urandom(750 * 750 * 4))
            assert len(base64.b64decode(oversized.split(",", 1)[1])) > 2 * 1024 * 1024
            assert (
                await owner_device.put(
                    f"{path}/icon", json={"icon": {"kind": "image", "value": oversized}}
                )
            ).status_code == 422
            too_many_pixels = _png(2001, 2001)
            assert (
                await owner_device.put(
                    f"{path}/icon", json={"icon": {"kind": "image", "value": too_many_pixels}}
                )
            ).status_code == 422
            reset = await owner_device.put(f"{path}/icon", json={"icon": None})
            assert reset.status_code == 200 and reset.json()["icon"] is None

    asyncio.run(exercise())
    restarted = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    restored = restarted.state.control_store.organization_for_member(owner.id, org.id)
    assert restored.icon_kind is None and restored.icon_value is None
    assert member.user_id != owner.id
