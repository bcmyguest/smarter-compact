"""Built-in embedding model: BAAI/bge-small-en-v1.5 (MIT, 384 dims) run with numpy, so semantic search needs no
embedding server (docs/context-store.md, "Vectors").

The weights (safetensors), tokenizer and config come from the Hugging Face Hub at a pinned revision on first use and
are cached under the state dir. Inference is a plain BERT forward pass: numpy, `tokenizers` and `huggingface_hub`
are already in LiteLLM's environment, so nothing is installed. CLS pooling, L2-normalized, as the model card says.
The proxy's background embedder calls embed() in process; the MCP server reaches the same model through the proxy's
OpenAI-compatible /v1/embeddings (`smarter_compact_hook.embed_handler`).
"""

import json
import math
import os
import struct
import threading
from pathlib import Path

LOCAL_MODEL = "smarter-compact-embed"
# bge v1.5's instruction for short queries against passages; the MCP server adds it to search queries only.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
REPO = "BAAI/bge-small-en-v1.5"
REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
FILES = ("config.json", "tokenizer.json", "model.safetensors")
MAX_TOKENS = 512
# Rows per forward pass; long rows are padded to the longest in the pass, so keep it small.
CHUNK = 8

_lock = threading.Lock()
_model = None


def cache_dir():
    root = os.environ.get("SMARTER_COMPACT_STATE_DIR") or str(Path.home() / ".local" / "share" / "smarter-compact")
    return Path(root) / "models"


def _load_safetensors(path):
    """{name: float32 array} from a safetensors file (8-byte header length, JSON header, raw little-endian data)."""
    import numpy as np

    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(n))
        base = 8 + n
    data = np.memmap(path, dtype=np.uint8, mode="r", offset=base)
    out = {}
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        if meta["dtype"] != "F32":  # buffers such as embeddings.position_ids are not weights
            continue
        start, end = meta["data_offsets"]
        out[name] = np.frombuffer(data[start:end], dtype="<f4").reshape(meta["shape"]).copy()
    return out


class _Bert:
    def __init__(self, folder):
        from tokenizers import Tokenizer

        cfg = json.loads((folder / "config.json").read_text())
        self.heads = cfg["num_attention_heads"]
        self.layers = cfg["num_hidden_layers"]
        self.eps = cfg["layer_norm_eps"]
        self.w = _load_safetensors(folder / "model.safetensors")
        self.prefix = "bert." if "bert.embeddings.word_embeddings.weight" in self.w else ""
        tok = Tokenizer.from_file(str(folder / "tokenizer.json"))
        tok.enable_truncation(MAX_TOKENS)
        tok.no_padding()
        self.tok = tok

    def _p(self, name):
        return self.w[self.prefix + name]

    def _ln(self, x, name):
        mu = x.mean(-1, keepdims=True)
        var = ((x - mu) ** 2).mean(-1, keepdims=True)
        return (x - mu) / (var + self.eps) ** 0.5 * self._p(f"{name}.weight") + self._p(f"{name}.bias")

    def _dense(self, x, name):
        return x @ self._p(f"{name}.weight").T + self._p(f"{name}.bias")

    def forward(self, texts):
        import numpy as np

        enc = self.tok.encode_batch(list(texts))
        n = max(len(e.ids) for e in enc)
        ids = np.zeros((len(enc), n), dtype=np.int64)
        types = np.zeros_like(ids)
        mask = np.zeros((len(enc), n), dtype=np.float32)
        for i, e in enumerate(enc):
            ids[i, : len(e.ids)] = e.ids
            types[i, : len(e.ids)] = e.type_ids
            mask[i, : len(e.ids)] = 1
        x = (self._p("embeddings.word_embeddings.weight")[ids]
             + self._p("embeddings.position_embeddings.weight")[np.arange(n)][None]
             + self._p("embeddings.token_type_embeddings.weight")[types])
        x = self._ln(x, "embeddings.LayerNorm")
        b, _, d = x.shape
        hd = d // self.heads
        bias = ((1.0 - mask) * -1e9)[:, None, None, :]
        for i in range(self.layers):
            p = f"encoder.layer.{i}"

            def split(t):
                return t.reshape(b, n, self.heads, hd).transpose(0, 2, 1, 3)

            q = split(self._dense(x, f"{p}.attention.self.query"))
            k = split(self._dense(x, f"{p}.attention.self.key"))
            v = split(self._dense(x, f"{p}.attention.self.value"))
            s = q @ k.transpose(0, 1, 3, 2) / math.sqrt(hd) + bias
            s = np.exp(s - s.max(-1, keepdims=True))
            s /= s.sum(-1, keepdims=True)
            a = (s @ v).transpose(0, 2, 1, 3).reshape(b, n, d)
            x = self._ln(self._dense(a, f"{p}.attention.output.dense") + x, f"{p}.attention.output.LayerNorm")
            h = _gelu(self._dense(x, f"{p}.intermediate.dense"))
            x = self._ln(self._dense(h, f"{p}.output.dense") + x, f"{p}.output.LayerNorm")
        cls = x[:, 0]
        return cls / np.linalg.norm(cls, axis=-1, keepdims=True)


def _erf(x):
    """Abramowitz & Stegun 7.1.26, |error| < 1.5e-7: numpy has no erf and scipy is not in the image."""
    import numpy as np

    sign = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return sign * y


def _gelu(x):
    return 0.5 * x * (1.0 + _erf(x / math.sqrt(2.0)))


def _folder():
    from huggingface_hub import hf_hub_download

    try:  # no network once cached (also offline)
        paths = [hf_hub_download(REPO, f, revision=REVISION, cache_dir=str(cache_dir()), local_files_only=True) for f in FILES]
    except Exception:
        paths = [hf_hub_download(REPO, f, revision=REVISION, cache_dir=str(cache_dir())) for f in FILES]
    return Path(paths[0]).parent


def model():
    """The loaded model, downloading it the first time (about 135 MB)."""
    global _model
    with _lock:
        if _model is None:
            _model = _Bert(_folder())
        return _model


def embed(texts):
    """Unit vectors for texts, in input order. Passes group texts of similar length, so short rows are not padded
    to a long one's 512 tokens."""
    m = model()
    texts = list(texts)
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    out = [None] * len(texts)
    with _lock:  # one forward pass at a time: numpy's BLAS already uses every core
        for i in range(0, len(order), CHUNK):
            idx = order[i : i + CHUNK]
            for j, row in zip(idx, m.forward([texts[j] for j in idx])):
                out[j] = row.astype(float).tolist()
    return out
