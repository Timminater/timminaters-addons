"""Allowlisted official CPU models; immutable revisions and file hashes."""
MODELS = {
    "5M": {"revision": "bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f", "bytes": 20843960,
           "sha256": "7ace108c4987e7f2a99a75a445240019d1d31c2117139d1bd05ffdfd25aca738",
           "config_sha256": "b5f27f61513f8fd1a3aa996f3720d3679cb40bce5bcb08eb3dc320ce726e7c2f"},
    "28M": {"revision": "49b26409ac87b5935539a5d8ffe7f397b720db47", "bytes": 111296464,
            "sha256": "dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d",
            "config_sha256": "0cdf44b8ea981191de53078e0a5ecbbd437046bd939a546d5dbe17868f71281f"},
    "123M": {"revision": "5a147287addda85f6acd8da3fb8858fe9af7e8f7", "bytes": 490445904,
             "sha256": "599fafa73cadbbc800b5a346ca900fa741ee3e5fd22e3b4a4430e151e74f4bc2",
             "config_sha256": "7bb4c042fb2d2ed6469b6758ba380dfeda3ab32df47690a87aec1527bdae79bf"},
    "209M": {"revision": "9874d252ab0aae16f7f7c999f98f98d6ad7d8365", "bytes": 837271648,
             "sha256": "1456effb9927e3daa8c7234f10497e3424233869245a9c72b8ecebaf7ad2b7fa",
             "config_sha256": "1764139bb6d37ee5ff543f470495c086cfa6c373f7134fc61c7e3d844538bfd8"},
}
for size, model in MODELS.items():
    model.update(repo="mistralai/Timestral-" + size, folder="timestral-" + size.lower())


def model_spec(size="209M"):
    if not isinstance(size, str) or size not in MODELS:
        raise ValueError("Kies Timestral 5M, 28M, 123M of 209M")
    return MODELS[size]
