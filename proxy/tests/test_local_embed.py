"""Built-in embedding model (smarter_compact.local_embed). The forward pass was checked against the model's official
ONNX export (cosine 1.000000 on five texts); these tests pin the parts that can break without downloading it."""

import importlib.util
import json
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smarter_compact import embed as embed_mod  # noqa: E402
from smarter_compact import local_embed  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_TOKENIZERS = importlib.util.find_spec("tokenizers") is not None


class DispatchTest(unittest.TestCase):
    def test_builtin_model_runs_in_process(self):
        calls = []
        orig = local_embed.embed
        local_embed.embed = lambda texts: calls.append(texts) or [[1.0]] * len(texts)
        try:
            self.assertEqual(embed_mod.default_embed("http://unused", local_embed.LOCAL_MODEL, "", ["a", "b"]), [[1.0], [1.0]])
        finally:
            local_embed.embed = orig
        self.assertEqual(calls, [["a", "b"]])

    def test_other_models_use_the_endpoint(self):
        orig = embed_mod.urllib_embed
        embed_mod.urllib_embed = lambda url, model, key, texts, timeout=0: [[url, model]]
        try:
            self.assertEqual(embed_mod.default_embed("http://x/v1", "nomic-embed-text", "", ["a"]), [["http://x/v1", "nomic-embed-text"]])
        finally:
            embed_mod.urllib_embed = orig


@unittest.skipUnless(HAVE_NUMPY, "numpy is not installed")
class NumpyTest(unittest.TestCase):
    def test_erf_matches_math_erf(self):
        import numpy as np

        xs = np.linspace(-5, 5, 2001)
        self.assertLess(max(abs(a - math.erf(x)) for a, x in zip(local_embed._erf(xs), xs)), 2e-7)

    def test_safetensors_loader_reads_f32_and_skips_buffers(self):
        import numpy as np

        w = np.arange(6, dtype="<f4").reshape(2, 3)
        ids = np.arange(4, dtype="<i8")
        header = {"w": {"dtype": "F32", "shape": [2, 3], "data_offsets": [0, 24]},
                  "ids": {"dtype": "I64", "shape": [4], "data_offsets": [24, 56]}, "__metadata__": {"format": "pt"}}
        raw = json.dumps(header).encode()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.safetensors"
            path.write_bytes(struct.pack("<Q", len(raw)) + raw + w.tobytes() + ids.tobytes())
            out = local_embed._load_safetensors(path)
        self.assertEqual(list(out), ["w"])
        self.assertTrue((out["w"] == w).all())


@unittest.skipUnless(HAVE_NUMPY and HAVE_TOKENIZERS, "numpy or tokenizers is not installed")
class TinyBertTest(unittest.TestCase):
    """A 2-layer random BERT: unit vectors, and a text's vector does not depend on what it is batched with."""

    def setUp(self):
        import numpy as np
        from tokenizers import Tokenizer, models, pre_tokenizers, processors

        vocab = {w: i for i, w in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "alpha", "beta", "gamma", "delta"])}
        tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
        tok.pre_tokenizer = pre_tokenizers.Whitespace()
        tok.post_processor = processors.TemplateProcessing(single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)])
        self.dir = tempfile.TemporaryDirectory()
        folder = Path(self.dir.name)
        tok.save(str(folder / "tokenizer.json"))
        d, inter, layers = 8, 16, 2
        (folder / "config.json").write_text(json.dumps({"num_attention_heads": 2, "num_hidden_layers": layers, "layer_norm_eps": 1e-12}))
        rng = np.random.default_rng(0)
        shapes = {"embeddings.word_embeddings.weight": (len(vocab), d), "embeddings.position_embeddings.weight": (16, d),
                  "embeddings.token_type_embeddings.weight": (2, d)}
        for ln in ["embeddings.LayerNorm"] + [f"encoder.layer.{i}.{n}" for i in range(layers) for n in ("attention.output.LayerNorm", "output.LayerNorm")]:
            shapes[f"{ln}.weight"] = (d,)
            shapes[f"{ln}.bias"] = (d,)
        for i in range(layers):
            p = f"encoder.layer.{i}"
            for name, (o, n) in {"attention.self.query": (d, d), "attention.self.key": (d, d), "attention.self.value": (d, d),
                                 "attention.output.dense": (d, d), "intermediate.dense": (inter, d), "output.dense": (d, inter)}.items():
                shapes[f"{p}.{name}.weight"] = (o, n)
                shapes[f"{p}.{name}.bias"] = (o,)
        header, blobs, off = {}, [], 0
        for name, shape in shapes.items():
            arr = rng.normal(size=shape).astype("<f4")
            if name.endswith("LayerNorm.weight"):
                arr = np.ones(shape, dtype="<f4")
            header[name] = {"dtype": "F32", "shape": list(shape), "data_offsets": [off, off + arr.nbytes]}
            blobs.append(arr.tobytes())
            off += arr.nbytes
        raw = json.dumps(header).encode()
        (folder / "model.safetensors").write_bytes(struct.pack("<Q", len(raw)) + raw + b"".join(blobs))
        self.bert = local_embed._Bert(folder)

    def tearDown(self):
        self.dir.cleanup()

    def test_unit_vectors_independent_of_batch_padding(self):
        import numpy as np

        alone = self.bert.forward(["alpha beta"])[0]
        batched = self.bert.forward(["alpha beta", "gamma delta alpha beta gamma delta"])[0]
        self.assertAlmostEqual(float(np.linalg.norm(alone)), 1.0, places=5)
        self.assertLess(float(np.abs(alone - batched).max()), 1e-5)

    def test_embed_keeps_input_order(self):
        orig = local_embed.model
        local_embed.model = lambda: self.bert
        try:
            texts = ["gamma delta alpha beta gamma", "alpha", "beta gamma"]
            out = local_embed.embed(texts)
            for t, v in zip(texts, out):
                self.assertLess(max(abs(a - b) for a, b in zip(v, self.bert.forward([t])[0])), 1e-5)
        finally:
            local_embed.model = orig


if __name__ == "__main__":
    unittest.main()
