"""Fond satellite : choix de la source et relais des tuiles, sans réseau (transport simulé)."""

import base64

import httpx
import pytest

from geosphere_mcp import fond


def test_en_france():
    assert fond.en_france(12, 2102, 1462)  # Lyon
    assert not fond.en_france(12, 2048, 1362)  # Londres
    assert fond.en_france(10, 338, 470)  # Martinique


@pytest.fixture
def faux(monkeypatch):
    vus = []

    def rep(req: httpx.Request):
        vus.append(str(req.url))
        if "geopf" in req.url.host and "TILEMATRIX=19" in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    c = httpx.AsyncClient(transport=httpx.MockTransport(rep))
    monkeypatch.setattr(fond, "client", lambda: c)
    fond._cache.clear()
    return vus


@pytest.mark.asyncio
async def test_auto_ign_puis_eox(faux):
    r = await fond.fond(["12/2102/1462", "12/2048/1362"])
    t = r["tuiles"]
    assert [x["source"] for x in t] == ["ign", "eox"]
    assert base64.b64decode(t[0]["jpeg_base64"]) == b"\xff\xd8jpeg"
    assert {s["licence"] for s in r["source"]} == {"Licence Ouverte 2.0 (Etalab)", "CC BY 4.0"}
    assert "geopf" in faux[0] and "TILEROW=1462" in faux[0] and "eox" in faux[1] and faux[1].endswith("/12/1362/2048.jpg")


@pytest.mark.asyncio
async def test_cache_et_limites(faux):
    await fond.fond(["12/2102/1462"])
    await fond.fond(["12/2102/1462"])
    assert len(faux) == 1
    r = await fond.fond([f"5/{i}/1" for i in range(30)], "eox")
    assert len(r["tuiles"]) == fond.MAX_TUILES and r["ignorees"] == 6
    r = await fond.fond(["17/1/1"], "eox")
    assert "erreur" in r["tuiles"][0]
    with pytest.raises(ValueError):
        await fond.fond(["3/9/1"])


@pytest.mark.asyncio
async def test_trou_ign(faux):
    r = await fond.fond(["19/1/1"], "ign")
    assert "erreur" in r["tuiles"][0]
